"""SQLite conversation metadata and migrations; mixed into the legacy database.

Only explicit statements are scoped. Provider/configuration/audit storage stays
shared; there is no SQL rewriting or context-dependent database connection.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from xgent_app.conversations import (
    DEFAULT_CONVERSATION_ID, DEFAULT_CONVERSATION_NAME, SCHEMA_VERSION,
    ConversationError, ExecutionFileLock, current_scope, require_conversation_id,
)


async def _config(conn, key, default=None):
    cursor = await conn.execute('SELECT value FROM config WHERE key=?', (key,))
    row = await cursor.fetchone()
    await cursor.close()
    return json.loads(row['value']) if row else default


async def _set_config(conn, key, value):
    await conn.execute('INSERT INTO config(key,value) VALUES(?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, json.dumps(value, ensure_ascii=False)))


class ConversationStore:
    async def backup_legacy_conversations(self):
        conn = await self._get_conn()
        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row['name'] for row in await cursor.fetchall()}
        version = await _config(conn, 'conversation_schema_version', 0) if 'config' in tables else 0
        if version > SCHEMA_VERSION:
            raise ConversationError('数据库版本较新，请升级程序，不能降级混跑。')
        if 'global_messages' not in tables or self.db_path == ':memory:' or version:
            return
        database = Path(self.db_path).resolve()
        backup = database.with_name(database.name + '.pre-conversations-v1.sqlite3')
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

    async def migrate_conversations(self):
        async with self._transaction() as conn:
            version = await _config(conn, 'conversation_schema_version', 0)
            if version > SCHEMA_VERSION:
                raise ConversationError('数据库版本较新，请升级程序，不能降级混跑。')
            if version == SCHEMA_VERSION:
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
            await _set_config(conn, 'conversation_schema_version', SCHEMA_VERSION)
        self._config_cache.clear()

    def _conversation_id(self, explicit=None):
        return require_conversation_id(explicit)

    async def _conversation_generation(self, conn, conversation_id=None):
        cid = conversation_id or self._conversation_id()
        cursor = await conn.execute('SELECT generation FROM chat_sessions WHERE id=?', (cid,))
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
                           '(id,name,model,created_at,last_active,archived,generation,agent_iteration) '
                           'VALUES(?,?,NULL,?,?,0,?,0)',
                           (cid, name or '新对话', now, now, generation))
        return cid

    async def get_conversation_state(self):
        # One statement gives an atomic selection/revision/run snapshot.
        conn = await self._get_conn()
        cursor = await conn.execute("SELECT key,value FROM config WHERE key IN "
                                    "('current_chat_id','conversation_revision','conversation_running')")
        config = {row['key']: json.loads(row['value']) for row in await cursor.fetchall()}
        await cursor.close()
        return {'current_chat_id': config.get('current_chat_id', DEFAULT_CONVERSATION_ID),
                'revision': int(config.get('conversation_revision', 0)),
                'running': config.get('conversation_running')}

    async def manage_conversation(self, action, conversation_id=None, name=None):
        if action not in {'create', 'switch', 'rename', 'archive', 'restore'}:
            raise ConversationError('未知会话操作。')
        if action == 'rename' or name is not None:
            name = str(name or '').strip()
            if not name or len(name) > 80:
                raise ConversationError('会话名称需要 1–80 个字符。')
        async with self._transaction() as conn:
            active = await _config(conn, 'current_chat_id', DEFAULT_CONVERSATION_ID)
            cid = str(conversation_id or active)
            if action == 'create':
                cid = await self._new_conversation(conn, name)
                active = cid
            else:
                cursor = await conn.execute('SELECT * FROM chat_sessions WHERE id=?', (cid,))
                session = await cursor.fetchone()
                if session is None:
                    raise ConversationError('会话不存在，请刷新列表。')
                if action == 'switch':
                    if session['archived']:
                        raise ConversationError('请先恢复已归档会话。')
                    active = cid
                elif action == 'rename':
                    await conn.execute('UPDATE chat_sessions SET name=? WHERE id=?', (name, cid))
                elif action == 'restore':
                    await conn.execute('UPDATE chat_sessions SET archived=0 WHERE id=?', (cid,))
                elif action == 'archive':
                    await conn.execute('UPDATE chat_sessions SET archived=1 WHERE id=?', (cid,))
                    if cid == active:
                        cursor = await conn.execute('SELECT id FROM chat_sessions WHERE archived=0 '
                                                    'ORDER BY last_active DESC,id LIMIT 1')
                        other = await cursor.fetchone()
                        active = other['id'] if other else await self._new_conversation(conn)
            if action in {'create', 'switch'}:
                await conn.execute('UPDATE chat_sessions SET last_active=? WHERE id=?', (time.time(), active))
            await _set_config(conn, 'current_chat_id', active)
            await _set_config(conn, 'conversation_revision', int(await _config(conn, 'conversation_revision', 0)) + 1)
        self._config_cache.pop('current_chat_id', None)
        return {'ok': True, 'conversation_id': cid, **await self.get_conversation_state()}

    async def request_conversation_stop(self, run_id, conversation_id=None):
        async with self._transaction() as conn:
            running = await _config(conn, 'conversation_running')
            if not running or running.get('run_id') != run_id:
                return False
            if conversation_id and running.get('conversation_id') != conversation_id:
                return False
            running['stop_requested'] = True
            await _set_config(conn, 'conversation_running', running)
            await _set_config(conn, 'conversation_revision', int(await _config(conn, 'conversation_revision', 0)) + 1)
            return True

    async def finish_conversation_run(self, run_id):
        async with self._transaction() as conn:
            running = await _config(conn, 'conversation_running')
            if running and running.get('run_id') == run_id:
                await _set_config(conn, 'conversation_running', None)
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
            await _set_config(conn, 'conversation_running', running)
            await _set_config(conn, 'conversation_revision', int(await _config(conn, 'conversation_revision', 0)) + 1)
