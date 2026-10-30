"""SQLite conversation metadata and migrations; mixed into the legacy database.

Only explicit statements are scoped. Provider/configuration/audit storage stays
shared; there is no SQL rewriting or context-dependent database connection.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from xgent_app.conversations import (
    DEFAULT_CONVERSATION_ID, DEFAULT_CONVERSATION_NAME, SCHEMA_VERSION,
    ConversationError, ConversationBusy, ExecutionFileLock, current_scope, require_conversation_id,
)
from xgent_app.queue_store import QueueStore, migrate_queue


async def _config(conn, key, default=None):
    cursor = await conn.execute('SELECT value FROM config WHERE key=?', (key,))
    row = await cursor.fetchone()
    await cursor.close()
    return json.loads(row['value']) if row else default


async def _set_config(conn, key, value):
    await conn.execute('INSERT INTO config(key,value) VALUES(?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, json.dumps(value, ensure_ascii=False)))


class ConversationStore(QueueStore):
    async def backup_legacy_conversations(self):
        conn = await self._get_conn()
        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row['name'] for row in await cursor.fetchall()}
        version = await _config(conn, 'conversation_schema_version', 0) if 'config' in tables else 0
        imported = await _config(conn, 'message_policy_import_pending') if 'config' in tables else None
        if imported is not None and (not isinstance(imported, str) or len(imported) != 32
                                     or any(char not in '0123456789abcdef' for char in imported)):
            raise ConversationError('导入消息迁移标记无效，原数据已保留。')
        if version > SCHEMA_VERSION:
            raise ConversationError('数据库版本较新，请升级程序，不能降级混跑。')
        if 'global_messages' not in tables or self.db_path == ':memory:' or version >= SCHEMA_VERSION and not imported:
            return
        database = Path(self.db_path).resolve()
        suffix = f'.pre-message-policy-{imported}.sqlite3' if imported else f'.pre-conversations-v{1 if not version else SCHEMA_VERSION}.sqlite3'
        backup = database.with_name(database.name + suffix)
        guard = ExecutionFileLock(str(database) + '.migration')
        while not guard.acquire():
            await asyncio.sleep(0.05)
        try:
            if backup.exists():
                return
            temporary = backup.with_name(backup.name + '.' + uuid.uuid4().hex + '.tmp')
            # Both paths are fixed siblings of the explicitly configured database.
            if temporary.parent != database.parent or backup.parent != database.parent:
                raise ConversationError('数据库备份路径无效。')
            destination = sqlite3.connect(str(temporary))
            try:
                await conn.backup(destination)
            finally:
                destination.close()
            os.replace(temporary, backup)
        finally:
            guard.release()

    async def _migrate_conversations_v1(self):
        async with self._transaction() as conn:
            version = await _config(conn, 'conversation_schema_version', 0)
            if version > SCHEMA_VERSION:
                raise ConversationError('数据库版本较新，请升级程序，不能降级混跑。')
            if version >= 1:
                return
            cursor = await conn.execute('PRAGMA table_info(chat_sessions)')
            columns = {row['name'] for row in await cursor.fetchall()}
            for column, definition in (
                ('archived', 'INTEGER NOT NULL DEFAULT 0'),
                ('generation', 'INTEGER NOT NULL DEFAULT 0'),
                ('agent_iteration', 'INTEGER NOT NULL DEFAULT 0'),
            ):
                if column not in columns:
                    await conn.execute(f'ALTER TABLE chat_sessions ADD COLUMN {column} {definition}')
            generation = int(await _config(conn, 'attachment_generation', 0))
            cursor = await conn.execute('SELECT MAX(generation) AS n FROM ui_messages')
            highest = max(generation, int((await cursor.fetchone())['n'] or 0))
            cursor = await conn.execute('SELECT payload FROM context_compressions')
            rounds = [json.loads(row['payload']) for row in await cursor.fetchall()]
            cursor = await conn.execute('SELECT payload FROM context_compression_jobs')
            jobs = [json.loads(row['payload']) for row in await cursor.fetchall()]
            for entry in rounds + jobs:
                highest = max(highest, int(entry.get('generation') or 0))
            now = time.time()
            cid = DEFAULT_CONVERSATION_ID
            await conn.execute('UPDATE global_messages SET session_id=?', (cid,))
            await conn.execute('UPDATE chat_messages SET session_id=?', (cid,))
            await conn.execute('UPDATE trigger_tasks SET conversation_id=?', (cid,))
            # Old relay session_id identifies a CLI process, not a conversation.
            # Stamp legacy envelopes explicitly before any worker can replay them.
            for table, key in (('cli_relay_ops', 'conversation_context'),
                               ('channel_outbox', '_conversation_context'),
                               ('channel_deadletter', '_conversation_context')):
                cursor = await conn.execute(f'SELECT id,payload FROM {table}')
                for row in await cursor.fetchall():
                    try:
                        payload = json.loads(row['payload'])
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(payload, dict) or key in payload:
                        continue
                    payload[key] = {'conversation_id': cid,
                                    'generation': int((payload.get('ui_context') or {}).get('generation', generation)),
                                    'run_id': None, 'name': DEFAULT_CONVERSATION_NAME}
                    await conn.execute(f'UPDATE {table} SET payload=? WHERE id=?',
                                       (json.dumps(payload, ensure_ascii=False), row['id']))
            await conn.execute('DELETE FROM chat_sessions WHERE id<>?', (cid,))
            await conn.execute('INSERT OR IGNORE INTO chat_sessions '
                               '(id,name,created_at,last_active) VALUES(?,?,?,?)',
                               (cid, DEFAULT_CONVERSATION_NAME, now, now))
            await conn.execute('UPDATE chat_sessions SET generation=?,archived=0,model=NULL WHERE id=?',
                               (generation, cid))
            # A compression sequence is local to its conversation, not globally unique.
            await conn.execute('ALTER TABLE context_compressions RENAME TO context_compressions_legacy')
            await conn.execute('CREATE TABLE context_compressions '
                               '(session_id TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL, '
                               'PRIMARY KEY(session_id,sequence))')
            for entry in rounds:
                entry['conversation_id'] = cid
                await conn.execute('INSERT INTO context_compressions VALUES(?,?,?)',
                                   (cid, entry['sequence'], json.dumps(entry, ensure_ascii=False)))
            await conn.execute('DROP TABLE context_compressions_legacy')
            await conn.execute("ALTER TABLE context_compression_jobs ADD COLUMN session_id TEXT NOT NULL DEFAULT 'global_memory'")
            for entry in jobs:
                entry['conversation_id'] = cid
                await conn.execute('UPDATE context_compression_jobs SET payload=?,session_id=? WHERE job_id=?',
                                   (json.dumps(entry, ensure_ascii=False), cid, entry['job_id']))
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_conversation_history '
                               'ON global_messages(session_id,timestamp,id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_compression_session '
                               'ON context_compression_jobs(session_id)')
            await _set_config(conn, 'conversation_generation_counter', highest)
            await _set_config(conn, 'current_chat_id', cid)
            await _set_config(conn, 'conversation_revision', 1)
            await _set_config(conn, 'conversation_running', None)
            await _set_config(conn, 'conversation_schema_version', 1)
        self._config_cache.clear()

    async def _repair_turn_and_branch_tokens(self, conn):
        await conn.execute(
            "UPDATE global_messages SET turn_id=NULL WHERE turn_id IS NOT NULL AND ("
            "msg_type='command' OR (msg_type<>'token_usage' AND id > COALESCE((SELECT final_record_id FROM conversation_turns "
            "WHERE conversation_turns.id=global_messages.turn_id AND final_record_id IS NOT NULL), id)))"
        )
        await conn.execute(
            "UPDATE global_messages SET turn_id=("
            "SELECT m.turn_id FROM global_messages m "
            "WHERE m.session_id=global_messages.session_id AND m.id<global_messages.id "
            "ORDER BY m.id DESC LIMIT 1"
            ") WHERE msg_type='token_usage' AND turn_id IS NULL"
        )
        branch_cursor = await conn.execute(
            "SELECT b.id branch_id, b.parent_conversation_id parent_id, "
            "m.source_record_id, m.turn_id branch_turn_id "
            "FROM chat_sessions b JOIN global_messages m ON m.session_id=b.id "
            "WHERE b.parent_conversation_id IS NOT NULL AND b.deleting=0 "
            "AND m.id=(SELECT MAX(id) FROM global_messages WHERE session_id=b.id AND withdrawn=0) "
            "AND m.msg_type='ai_reply' AND m.source_record_id IS NOT NULL"
        )
        for br in await branch_cursor.fetchall():
            tok_cursor = await conn.execute(
                "SELECT * FROM global_messages WHERE session_id=? AND id>? AND withdrawn=0 ORDER BY id LIMIT 1",
                (br['parent_id'], br['source_record_id']),
            )
            tok = await tok_cursor.fetchone()
            await tok_cursor.close()
            if tok and tok['msg_type'] == 'token_usage':
                await conn.execute(
                    '''INSERT INTO global_messages
                    (chat_id,user_id,msg_type,role,content,timestamp,session_id,metadata,turn_id,withdrawn,source_record_id)
                    VALUES(?,?,?,?,?,?,?,?,?,0,?)''',
                    (tok['chat_id'], tok['user_id'], tok['msg_type'], tok['role'], tok['content'],
                     tok['timestamp'], br['branch_id'], tok['metadata'], br['branch_turn_id'], tok['id']),
                )
        await branch_cursor.close()

    async def migrate_conversations(self):
        await self._migrate_conversations_v1()
        async with self._transaction() as conn:
            version = int(await _config(conn, 'conversation_schema_version', 0))
            if version > SCHEMA_VERSION:
                raise ConversationError('数据库版本较新，请升级程序。')
            if version == SCHEMA_VERSION:
                await self._repair_turn_and_branch_tokens(conn)
                if not await _config(conn, 'message_policy_import_pending'):
                    return
            if version < 2:
                for column, definition in (
                    ('context_start_record_id', 'INTEGER NOT NULL DEFAULT 0'),
                    ('context_epoch', 'INTEGER NOT NULL DEFAULT 0'),
                    ('deleting', 'INTEGER NOT NULL DEFAULT 0'),
                    ('title_auto_pending', 'INTEGER NOT NULL DEFAULT 0'),
                ):
                    await conn.execute(f'ALTER TABLE chat_sessions ADD COLUMN {column} {definition}')
                await conn.execute("UPDATE chat_sessions SET title_auto_pending=1 WHERE name IS NULL OR name='新对话'")
                await conn.execute('ALTER TABLE ui_messages ADD COLUMN conversation_id TEXT')
                await conn.execute('UPDATE ui_messages SET conversation_id=(SELECT id FROM chat_sessions '
                                   'WHERE chat_sessions.generation=ui_messages.generation)')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_ui_conversation_history ON ui_messages(conversation_id,timestamp,ui_message_id)')
                for table in ('context_compressions', 'context_compression_jobs'):
                    await conn.execute(f'ALTER TABLE {table} ADD COLUMN context_epoch INTEGER NOT NULL DEFAULT 0')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_active_context ON global_messages(session_id,id)')
                await _set_config(conn, 'conversation_schema_version', 2)
            if version < 3:
                active = await _config(conn, 'current_chat_id')
                await _set_config(conn, 'telegram_conversation_id', active)
                await _set_config(conn, 'conversation_schema_version', 3)
            if version < 4:
                await migrate_queue(conn)
                await _set_config(conn, 'conversation_schema_version', 4)
            if version < 5:
                await conn.execute('CREATE TABLE IF NOT EXISTS conversation_groups ('
                    'id TEXT PRIMARY KEY,name TEXT NOT NULL,sort_order INTEGER NOT NULL DEFAULT 0,'
                    'created_at REAL NOT NULL,updated_at REAL NOT NULL)')
                now = time.time()
                await conn.execute('INSERT OR IGNORE INTO conversation_groups VALUES(?,?,?,?,?)',
                                   ('ungrouped', '未分组', 0, now, now))
                cursor = await conn.execute('PRAGMA table_info(chat_sessions)')
                columns = {row['name'] for row in await cursor.fetchall()}
                for column, definition in (('group_id', "TEXT NOT NULL DEFAULT 'ungrouped'"), ('pinned', 'INTEGER NOT NULL DEFAULT 0')):
                    if column not in columns:
                        await conn.execute(f'ALTER TABLE chat_sessions ADD COLUMN {column} {definition}')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_conversation_group ON chat_sessions(group_id,archived,pinned)')
                await _set_config(conn, 'conversation_schema_version', 5)
            if version < 6:
                cursor = await conn.execute('PRAGMA table_info(chat_sessions)')
                session_columns = {row['name'] for row in await cursor.fetchall()}
                for column, definition in (
                    ('parent_conversation_id', 'TEXT'),
                    ('parent_turn_id', 'TEXT'),
                    ('workspace_path', 'TEXT'),
                    ('branch_mode', 'TEXT'),
                    ('branch_base_snapshot_id', 'TEXT'),
                    ('branch_created_at', 'REAL'),
                ):
                    if column not in session_columns:
                        await conn.execute(f'ALTER TABLE chat_sessions ADD COLUMN {column} {definition}')
                cursor = await conn.execute('PRAGMA table_info(global_messages)')
                message_columns = {row['name'] for row in await cursor.fetchall()}
                for column, definition in (
                    ('turn_id', 'TEXT'),
                    ('withdrawn', 'INTEGER NOT NULL DEFAULT 0'),
                    ('source_record_id', 'INTEGER'),
                ):
                    if column not in message_columns:
                        await conn.execute(f'ALTER TABLE global_messages ADD COLUMN {column} {definition}')
                cursor = await conn.execute('PRAGMA table_info(ui_messages)')
                ui_columns = {row['name'] for row in await cursor.fetchall()}
                for column, definition in (
                    ('turn_id', 'TEXT'),
                    ('withdrawn', 'INTEGER NOT NULL DEFAULT 0'),
                ):
                    if column not in ui_columns:
                        await conn.execute(f'ALTER TABLE ui_messages ADD COLUMN {column} {definition}')
                await conn.execute('''CREATE TABLE IF NOT EXISTS artifact_objects (
                    sha256 TEXT PRIMARY KEY,size INTEGER NOT NULL,path TEXT NOT NULL,
                    kind TEXT NOT NULL,created_at REAL NOT NULL)''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS conversation_snapshots (
                    id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,turn_id TEXT,
                    phase TEXT NOT NULL,root_digest TEXT NOT NULL,file_count INTEGER NOT NULL,
                    total_size INTEGER NOT NULL,created_at REAL NOT NULL)''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS snapshot_entries (
                    snapshot_id TEXT NOT NULL,path TEXT NOT NULL,object_sha256 TEXT NOT NULL,
                    mode INTEGER NOT NULL,size INTEGER NOT NULL,
                    PRIMARY KEY(snapshot_id,path))''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS conversation_turns (
                    id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,run_id TEXT UNIQUE,
                    status TEXT NOT NULL,started_at REAL NOT NULL,finished_at REAL,
                    before_snapshot_id TEXT,after_snapshot_id TEXT,user_record_id INTEGER,
                    final_record_id INTEGER,snapshot_complete INTEGER NOT NULL DEFAULT 0,
                    error TEXT)''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS turn_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,turn_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,kind TEXT NOT NULL,effect TEXT NOT NULL,
                    details TEXT,created_at REAL NOT NULL,UNIQUE(turn_id,sequence))''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS conversation_branch_drafts (
                    conversation_id TEXT PRIMARY KEY,source_record_id INTEGER NOT NULL,
                    text TEXT NOT NULL,metadata TEXT,created_at REAL NOT NULL)''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS conversation_mutation_requests (
                    request_id TEXT PRIMARY KEY,operation TEXT NOT NULL,conversation_id TEXT NOT NULL,
                    response TEXT NOT NULL,created_at REAL NOT NULL)''')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_turns_conversation ON conversation_turns(conversation_id,started_at,id)')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_messages_turn ON global_messages(turn_id,id)')
                await conn.execute('CREATE INDEX IF NOT EXISTS idx_snapshots_conversation ON conversation_snapshots(conversation_id,created_at,id)')
                cursor = await conn.execute('SELECT id FROM chat_sessions ORDER BY created_at,id')
                for session in await cursor.fetchall():
                    rows_cursor = await conn.execute('SELECT id,role,msg_type,timestamp FROM global_messages '
                                                     'WHERE session_id=? AND turn_id IS NULL ORDER BY id',
                                                     (session['id'],))
                    pending = []
                    user_row = final_row = None
                    started_at = finished_at = None

                    async def flush_legacy_turn():
                        nonlocal pending, user_row, final_row, started_at, finished_at
                        if not pending or user_row is None:
                            pending, user_row, final_row = [], None, None
                            started_at = finished_at = None
                            return
                        turn_id = 'legacy_' + hashlib.sha256(
                            f"{session['id']}:{user_row}".encode()).hexdigest()[:32]
                        await conn.execute('''INSERT OR IGNORE INTO conversation_turns
                            (id,conversation_id,status,started_at,finished_at,user_record_id,
                             final_record_id,snapshot_complete,error)
                            VALUES(?,?,'legacy',?,?,?,?,0,'')''',
                            (turn_id, session['id'], started_at, finished_at, user_row, final_row))
                        await conn.executemany('UPDATE global_messages SET turn_id=? WHERE id=? AND turn_id IS NULL',
                                               [(turn_id, row_id) for row_id in pending])
                        pending, user_row, final_row = [], None, None
                        started_at = finished_at = None

                    for row in await rows_cursor.fetchall():
                        if row['msg_type'] == 'command':
                            await flush_legacy_turn()
                            continue
                        starts_turn = row['role'] == 'user' and row['msg_type'] in {
                            'user_text', 'user_file', 'user_photo', 'user_sticker',
                        }
                        if starts_turn:
                            await flush_legacy_turn()
                            user_row = row['id']
                            started_at = row['timestamp']
                        if user_row is None:
                            continue
                        pending.append(row['id'])
                        finished_at = row['timestamp']
                        if row['role'] == 'assistant' and row['msg_type'] == 'ai_reply':
                            final_row = row['id']
                    await flush_legacy_turn()
                await self._repair_turn_and_branch_tokens(conn)
                await _set_config(conn, 'conversation_schema_version', 6)
            if version < 7:
                legacy = await _config(conn, 'conversation_running')
                runs = ({legacy['conversation_id']: {**legacy, 'legacy_lock': True}}
                        if isinstance(legacy, dict) and legacy.get('conversation_id') else {})
                await _set_config(conn, 'conversation_runs', runs)
                await _set_config(conn, 'conversation_running', None)
                await _set_config(conn, 'conversation_schema_version', 7)
            if version < 8 or await _config(conn, 'message_policy_import_pending'):
                from .message_migrations import migrate_message_policies
                await migrate_message_policies(conn)
                await _set_config(conn, 'conversation_schema_version', 8)
                await conn.execute("DELETE FROM config WHERE key='message_policy_import_pending'")
            if version < 9:
                await conn.execute('''CREATE TABLE IF NOT EXISTS telegram_topics (
                    bot_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,conversation_id TEXT NOT NULL,
                    thread_id INTEGER,status TEXT NOT NULL,title TEXT NOT NULL,updated_at REAL NOT NULL,
                    PRIMARY KEY(bot_id,chat_id,conversation_id))''')
                await conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_topic_thread '
                    'ON telegram_topics(bot_id,chat_id,thread_id) WHERE thread_id IS NOT NULL')
                await _set_config(conn,'conversation_schema_version',9)
        self._config_cache.clear()

    async def get_conversation_groups(self):
        conn = await self._get_conn()
        cursor = await conn.execute('SELECT * FROM conversation_groups ORDER BY sort_order,created_at,id')
        rows = [dict(row) for row in await cursor.fetchall()]
        await cursor.close()
        return rows

    async def organize_conversations(self, action, data):
        """Group and pin changes are metadata only; never rebind a live run."""
        name = str(data.get('name') or '').strip()
        if action in {'create_group', 'rename_group'} and (not name or len(name) > 80):
            raise ConversationError('分组名称需要 1–80 个字符。')
        async with self._transaction() as conn:
            revision = int(await _config(conn, 'conversation_revision', 0))
            if type(data.get('revision')) is not int or data['revision'] != revision:
                raise ConversationError('对话列表已变化，请刷新后重试。')
            gid = str(data.get('group_id') or 'ungrouped')
            cursor = await conn.execute('SELECT id FROM conversation_groups WHERE id=?', (gid,))
            if not await cursor.fetchone():
                raise ConversationError('分组不存在，请刷新列表。')
            if action == 'create_group':
                gid = uuid.uuid4().hex
                now = time.time()
                await conn.execute('INSERT INTO conversation_groups VALUES(?,?,(SELECT COALESCE(MAX(sort_order),0)+1 FROM conversation_groups),?,?)',
                                   (gid, name, now, now))
            elif action in {'rename_group', 'delete_group'}:
                if gid == 'ungrouped':
                    raise ConversationError('“未分组”是保留分组，不能重命名或删除。')
                if action == 'rename_group':
                    await conn.execute('UPDATE conversation_groups SET name=?,updated_at=? WHERE id=?', (name, time.time(), gid))
                else:
                    await conn.execute("UPDATE chat_sessions SET group_id='ungrouped',pinned=0 WHERE group_id=?", (gid,))
                    await conn.execute('DELETE FROM conversation_groups WHERE id=?', (gid,))
            else:
                cid = str(data.get('id') or '')
                cursor = await conn.execute('SELECT * FROM chat_sessions WHERE id=? AND deleting=0', (cid,))
                item = await cursor.fetchone()
                if item is None:
                    raise ConversationError('对话不存在。')
                if action == 'move':
                    if item['group_id'] != gid:
                        await conn.execute('UPDATE chat_sessions SET group_id=?,pinned=0 WHERE id=?', (gid, cid))
                elif action in {'pin', 'unpin'}:
                    # A stale UI cannot pin a conversation after it moved groups.
                    if item['group_id'] != gid:
                        raise ConversationError('对话所属分组已变化，请刷新后重试。')
                    await conn.execute('UPDATE chat_sessions SET pinned=? WHERE id=?', (int(action == 'pin'), cid))
                else:
                    raise ConversationError('未知分组操作。')
            await self._bump_conversation_revision(conn)
        return gid

    async def _context_start(self, conn):
        cursor = await conn.execute('SELECT context_start_record_id FROM chat_sessions WHERE id=?', (self._conversation_id(),))
        row = await cursor.fetchone()
        if row is None:
            raise ConversationError('会话不存在。')
        return int(row['context_start_record_id'])

    async def _bump_conversation_revision(self, conn):
        await _set_config(conn, 'conversation_revision', int(await _config(conn, 'conversation_revision', 0)) + 1)

    async def _select_fallback(self, conn):
        cursor = await conn.execute('SELECT s.id FROM chat_sessions s WHERE s.archived=0 AND s.deleting=0 '
                                   'ORDER BY COALESCE((SELECT MAX(timestamp) FROM global_messages m '
                                   'WHERE m.session_id=s.id AND m.withdrawn=0 AND m.msg_type IN '
                                   "('user_text','user_file','user_photo','ai_reply','media_reply')),s.created_at) DESC,s.id LIMIT 1")
        row = await cursor.fetchone()
        active = row['id'] if row else await self._new_conversation(conn)
        await _set_config(conn, 'current_chat_id', active)
        self._config_cache.pop('current_chat_id', None)
        return active

    async def conversation_delete_info(self, cid):
        session = await self.get_session(cid)
        if session is None:
            raise ConversationError('该会话已不存在。')
        conn = await self._get_conn()
        cursor = await conn.execute("SELECT COUNT(*) n FROM trigger_tasks WHERE conversation_id=? "
                                    "AND status NOT IN ('completed','cancelled','failed')", (cid,))
        return {**session, 'active_tasks': int((await cursor.fetchone())['n'])}

    async def begin_conversation_delete(self, cid):
        async with self._transaction() as conn:
            cursor = await conn.execute('SELECT * FROM chat_sessions WHERE id=?', (cid,))
            row = await cursor.fetchone()
            if row is None:
                return False
            if not row['deleting']:
                generation = await self._allocate_conversation_generation(conn)
                await conn.execute('UPDATE chat_sessions SET deleting=1,generation=? WHERE id=?', (generation,cid))
            now = time.time()
            await conn.execute("UPDATE trigger_tasks SET status='cancelled',next_run_at=NULL,updated_at=? WHERE conversation_id=?", (now,cid))
            await conn.execute('UPDATE trigger_runs SET delivered_at=COALESCE(delivered_at,?) '
                               'WHERE task_id IN (SELECT id FROM trigger_tasks WHERE conversation_id=?)', (now,cid))
            runs = await _config(conn, 'conversation_runs', {})
            running = runs.get(cid)
            if running:
                running['stop_requested'] = True
                await _set_config(conn, 'conversation_runs', runs)
            if await _config(conn, 'current_chat_id') == cid:
                await self._select_fallback(conn)
            if await _config(conn, 'telegram_conversation_id') == cid:
                await _set_config(conn, 'telegram_conversation_id', None)
                self._config_cache.pop('telegram_conversation_id', None)
            await self._bump_conversation_revision(conn)
        return True

    async def pending_conversation_deletions(self):
        conn = await self._get_conn()
        cursor = await conn.execute('SELECT id FROM chat_sessions WHERE deleting=1')
        return [row['id'] for row in await cursor.fetchall()]

    async def finish_conversation_delete(self, cid):
        async with self._transaction() as conn:
            cursor = await conn.execute('SELECT deleting FROM chat_sessions WHERE id=?', (cid,))
            row = await cursor.fetchone()
            if row is None:
                return
            if not row['deleting']:
                raise ConversationError('删除操作尚未确认。')
            runs = await _config(conn, 'conversation_runs', {})
            if cid in runs:
                raise ConversationError('正在停止该会话的回合，请稍候。')
            await conn.execute('DELETE FROM trigger_runs WHERE task_id IN (SELECT id FROM trigger_tasks WHERE conversation_id=?)', (cid,))
            await conn.execute('DELETE FROM trigger_tasks WHERE conversation_id=?', (cid,))
            for table in ('global_messages','chat_messages','context_compressions','context_compression_jobs'):
                await conn.execute(f'DELETE FROM {table} WHERE session_id=?', (cid,))
            await conn.execute('DELETE FROM ui_messages WHERE conversation_id=?', (cid,))
            await conn.execute('DELETE FROM message_queue WHERE conversation_id=?', (cid,))
            await conn.execute('DELETE FROM message_queue_controls WHERE conversation_id=?', (cid,))
            await conn.execute('DELETE FROM message_queue_requests WHERE conversation_id=?', (cid,))
            await conn.execute('DELETE FROM turn_operations WHERE turn_id IN (SELECT id FROM conversation_turns WHERE conversation_id=?)', (cid,))
            await conn.execute('DELETE FROM conversation_turns WHERE conversation_id=?', (cid,))
            await conn.execute('DELETE FROM conversation_branch_drafts WHERE conversation_id=?', (cid,))
            await conn.execute('DELETE FROM conversation_mutation_requests WHERE conversation_id=?', (cid,))
            for table, key in (('cli_relay_ops','conversation_context'),('channel_outbox','_conversation_context'),('channel_deadletter','_conversation_context')):
                cursor = await conn.execute(f'SELECT id,payload FROM {table}')
                ids = []
                for record in await cursor.fetchall():
                    try:
                        context = json.loads(record['payload']).get(key) or {}
                        if context.get('conversation_id') == cid:
                            ids.append((record['id'],))
                    except (ValueError,TypeError,AttributeError):
                        pass
                if ids:
                    await conn.executemany(f'DELETE FROM {table} WHERE id=?', ids)
            await conn.execute('DELETE FROM chat_sessions WHERE id=?', (cid,))
            await self._bump_conversation_revision(conn)

    def _conversation_id(self, explicit=None):
        return require_conversation_id(explicit)

    async def _conversation_generation(self, conn, conversation_id=None):
        cid = conversation_id or self._conversation_id()
        cursor = await conn.execute('SELECT generation FROM chat_sessions WHERE id=? AND deleting=0', (cid,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise ConversationError('会话不存在，操作未执行。')
        return int(row['generation'])

    async def _validate_conversation_write(self, conn, explicit=None):
        cid = self._conversation_id(explicit)
        if await self._conversation_generation(conn, cid) != current_scope().generation:
            raise ConversationError('会话已清空或压缩，旧结果未写入新上下文。')
        return cid

    async def _allocate_conversation_generation(self, conn):
        value = int(await _config(conn, 'conversation_generation_counter', 0)) + 1
        await _set_config(conn, 'conversation_generation_counter', value)
        return value

    async def _new_conversation(self, conn, name=None):
        cid = uuid.uuid4().hex
        generation = await self._allocate_conversation_generation(conn)
        now = time.time()
        await conn.execute('INSERT INTO chat_sessions '
                           '(id,name,model,created_at,last_active,archived,generation,agent_iteration,title_auto_pending) '
                           'VALUES(?,?,NULL,?,?,0,?,0,?)',
                           (cid, name or '新对话', now, now, generation,int(name is None)))
        return cid

    async def get_conversation_state(self):
        # One statement gives an atomic selection/revision/run snapshot.
        conn = await self._get_conn()
        cursor = await conn.execute("SELECT key,value FROM config WHERE key IN "
                                    "('current_chat_id','telegram_conversation_id','conversation_revision','conversation_runs')")
        config = {row['key']: json.loads(row['value']) for row in await cursor.fetchall()}
        await cursor.close()
        return {'current_chat_id': config.get('current_chat_id', DEFAULT_CONVERSATION_ID),
                'telegram_conversation_id': config.get('telegram_conversation_id'),
                'revision': int(config.get('conversation_revision', 0)),
                'runs': config.get('conversation_runs', {}),
                'running': config.get('conversation_runs', {}).get(config.get('current_chat_id', DEFAULT_CONVERSATION_ID))}

    async def manage_conversation(self, action, conversation_id=None, name=None, *, selector='shared', select=True):
        from xgent_app.interaction import selection_key
        selected_key = selection_key(selector)
        if action not in {'create', 'switch', 'rename', 'archive', 'restore'}:
            raise ConversationError('未知会话操作。')
        if action == 'rename' or name is not None:
            name = str(name or '').strip()
            if not name or len(name) > 80:
                raise ConversationError('会话名称需要 1–80 个字符。')
        async with self._transaction() as conn:
            active = await _config(conn, selected_key)
            cid = str(conversation_id or active or '')
            if action == 'create':
                cid = await self._new_conversation(conn, name)
                active = cid
            else:
                cursor = await conn.execute('SELECT * FROM chat_sessions WHERE id=?', (cid,))
                session = await cursor.fetchone()
                if session is None or session['deleting']:
                    raise ConversationError('会话不存在，请刷新列表。')
                if action == 'switch':
                    if session['archived']:
                        raise ConversationError('请先恢复已归档会话。')
                    active = cid
                elif action == 'rename':
                    await conn.execute('UPDATE chat_sessions SET name=?,title_auto_pending=0 WHERE id=?', (name, cid))
                elif action == 'restore':
                    await conn.execute('UPDATE chat_sessions SET archived=0 WHERE id=?', (cid,))
                elif action == 'archive':
                    await conn.execute('UPDATE chat_sessions SET archived=1 WHERE id=?', (cid,))
                    if await _config(conn, 'telegram_conversation_id') == cid:
                        await _set_config(conn, 'telegram_conversation_id', None)
                    if await _config(conn, 'current_chat_id') == cid:
                        await self._select_fallback(conn)
                    active = await _config(conn, selected_key)
            if action in {'create', 'switch'}:
                await conn.execute('UPDATE chat_sessions SET last_active=? WHERE id=?', (time.time(), active))
            if select:
                await _set_config(conn, selected_key, active)
            await _set_config(conn, 'conversation_revision', int(await _config(conn, 'conversation_revision', 0)) + 1)
        self._config_cache.pop('current_chat_id', None)
        self._config_cache.pop('telegram_conversation_id', None)
        return {'ok': True, 'conversation_id': cid, **await self.get_conversation_state()}

    async def set_bot_conversation(self, cid, generation, expected_bot):
        """Explicit Web action: CAS only the Bot selector, never the Web/CLI view.

        Returns the target session name when the selector actually changed,
        or None for an idempotent repeat (so callers notify only once).
        """
        from .queue_store import one
        from .conversations import ConversationChanged
        changed = None
        async with self._transaction() as conn:
            session = await one(conn, 'SELECT generation,archived,deleting,name FROM chat_sessions WHERE id=?', (cid,))
            if not session or session['deleting'] or session['archived'] or session['generation'] != generation:
                raise ConversationChanged('目标会话已改变或已归档，请刷新后再设置 Bot。')
            selected = await _config(conn, 'telegram_conversation_id')
            if selected != expected_bot and selected != cid:
                raise ConversationChanged('Bot 当前对话已被其他端切换，请刷新后确认。')
            if selected != cid:
                await _set_config(conn, 'telegram_conversation_id', cid)
                await self._bump_conversation_revision(conn)
                changed = str(session['name'] or cid)
        self._config_cache.pop('telegram_conversation_id', None)
        return changed

    async def request_conversation_stop(self, run_id, conversation_id=None):
        async with self._transaction() as conn:
            runs = await _config(conn, 'conversation_runs', {})
            running = next((run for run in runs.values() if run.get('run_id') == run_id), None)
            if not running:
                return False
            if conversation_id and running.get('conversation_id') != conversation_id:
                return False
            if running.get('stop_requested'):
                return True
            running['stop_requested'] = True
            await self._queue_pause_conversation(conn, running['conversation_id'], 'stopped')
            await _set_config(conn, 'conversation_runs', runs)
            await _set_config(conn, 'conversation_revision', int(await _config(conn, 'conversation_revision', 0)) + 1)
            return True

    async def finish_conversation_run(self, run_id):
        async with self._transaction() as conn:
            runs = await _config(conn, 'conversation_runs', {})
            cid = next((cid for cid, run in runs.items() if run.get('run_id') == run_id), None)
            if cid is not None:
                del runs[cid]
                await _set_config(conn, 'conversation_runs', runs)
                await _set_config(conn, 'conversation_revision', int(await _config(conn, 'conversation_revision', 0)) + 1)

    async def reset_agent_iteration(self):
        async with self._transaction() as conn:
            cid = await self._validate_conversation_write(conn)
            await conn.execute('UPDATE chat_sessions SET agent_iteration=0 WHERE id=?', (cid,))
        return 0

    async def reserve_agent_iteration(self):
        async with self._transaction() as conn:
            cid = await self._validate_conversation_write(conn)
            await conn.execute('UPDATE chat_sessions SET agent_iteration=agent_iteration+1 WHERE id=?', (cid,))
            cursor = await conn.execute('SELECT agent_iteration FROM chat_sessions WHERE id=?', (cid,))
            return int((await cursor.fetchone())['agent_iteration'])


    async def begin_conversation_run(self, running):
        async with self._transaction() as conn:
            runs = await _config(conn, 'conversation_runs', {})
            cid = running['conversation_id']
            if cid in runs:
                raise ConversationBusy('当前对话正在执行，请等待完成或停止该回合。')
            runs[cid] = running
            await _set_config(conn, 'conversation_runs', runs)
            await _set_config(conn, 'conversation_revision', int(await _config(conn, 'conversation_revision', 0)) + 1)
