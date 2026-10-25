"""Transactional, model-invisible pending text. Shared by server and CLI processes."""
from __future__ import annotations

import json
import time
import uuid

from .conversations import ConversationChanged, ConversationError, current_scope

WAITING = ('pending', 'paused', 'invalid')
REASONS = {
    'waiting': '等待当前任务的安全接入点', 'stopped': '已停止，等待手动继续',
    'failed': '上一轮异常结束，待发消息已保留', 'restart': '来源进程已退出，等待手动继续',
    'ask': '等待表单提交', 'ask_cancelled': '表单已取消，等待手动继续',
    'generation': '上下文已改变，请取回后重新发送', 'archived': '对话已归档',
    'interrupted': '已接入，但执行中断；不会自动重发',
}


async def one(conn, sql, args=()):
    cursor = await conn.execute(sql, args)
    row = await cursor.fetchone()
    await cursor.close()
    return dict(row) if row is not None else None


async def rows(conn, sql, args=()):
    cursor = await conn.execute(sql, args)
    result = [dict(row) for row in await cursor.fetchall()]
    await cursor.close()
    return result


async def bump(conn):
    await conn.execute("INSERT INTO config(key,value) VALUES('message_queue_revision','1') "
                       "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")


async def migrate_queue(conn):
    await conn.execute('''CREATE TABLE IF NOT EXISTS message_queue (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
        request_key TEXT NOT NULL UNIQUE, conversation_id TEXT NOT NULL, generation INTEGER NOT NULL,
        text TEXT NOT NULL, origin TEXT NOT NULL, source_instance TEXT NOT NULL,
        dispatch_instance TEXT NOT NULL, chat_id INTEGER NOT NULL, status TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL, updated_at REAL NOT NULL, run_id TEXT, record_id INTEGER,
        edit_request_id TEXT, receipt_id INTEGER, receipt_revision INTEGER NOT NULL DEFAULT 0,
        receipt_requested INTEGER NOT NULL DEFAULT 0)''')
    await conn.execute('CREATE INDEX IF NOT EXISTS idx_queue_pending ON message_queue(status,seq)')
    await conn.execute('CREATE INDEX IF NOT EXISTS idx_queue_conversation ON message_queue(conversation_id,seq)')
    await conn.execute('''CREATE TABLE IF NOT EXISTS message_queue_controls (
        conversation_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
        state TEXT NOT NULL, reason TEXT NOT NULL, owner TEXT)''')
    await conn.execute('CREATE TABLE IF NOT EXISTS message_queue_instances (id TEXT PRIMARY KEY, created_at REAL NOT NULL)')
    # Dedupe configuration input without ever storing its value (not even a hash).
    await conn.execute('CREATE TABLE IF NOT EXISTS message_queue_requests ('
        'id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, generation INTEGER NOT NULL, kind TEXT NOT NULL)')


class QueueStore:
    async def queue_request(self, request_key):
        conn = await self._get_conn()
        item = await one(conn, 'SELECT * FROM message_queue WHERE request_key=?', (request_key,))
        if item:
            return {'kind': 'chat', 'item': item, 'conversation_id': item['conversation_id'], 'generation': item['generation']}
        return await one(conn, 'SELECT * FROM message_queue_requests WHERE id=?', (request_key,))

    async def queue_accept_management(self, scope, request_key):
        async with self._transaction() as conn:
            cid = await self._validate_conversation_write(conn, scope.conversation_id)
            item = await one(conn, 'SELECT * FROM message_queue WHERE request_key=?', (request_key,))
            if item:
                return {'new': False, 'kind': 'chat', 'item': item}
            previous = await one(conn, 'SELECT * FROM message_queue_requests WHERE id=?', (request_key,))
            if previous:
                if previous['conversation_id'] != cid:
                    raise ConversationChanged('请求标识已属于另一个会话。')
                return {'new': False, **previous}
            await conn.execute('INSERT INTO message_queue_requests VALUES(?,?,?,?)', (request_key, cid, scope.generation, 'management'))
            return {'new': True, 'kind': 'management'}

    async def queue_register(self, instance):
        async with self._transaction() as conn:
            await conn.execute('INSERT OR IGNORE INTO message_queue_instances VALUES(?,?)', (instance, time.time()))

    async def queue_instances(self):
        return await rows(await self._get_conn(), 'SELECT id FROM message_queue_instances')

    async def _queue_sync(self, conn):
        """Never revive old generations, or auto-resume an unarchived conversation."""
        changed = await conn.execute("UPDATE message_queue SET status='invalid',reason='generation',"
            "revision=revision+1,updated_at=? WHERE status IN ('pending','paused') AND EXISTS "
            '(SELECT 1 FROM chat_sessions s WHERE s.id=conversation_id AND s.generation<>message_queue.generation)',
            (time.time(),))
        archived = await conn.execute("UPDATE message_queue SET status='paused',reason='archived',"
            "revision=revision+1,updated_at=? WHERE status='pending' AND conversation_id IN "
            '(SELECT id FROM chat_sessions WHERE archived=1 OR deleting=1)', (time.time(),))
        await conn.execute("INSERT INTO message_queue_controls(conversation_id,generation,state,reason) "
            "SELECT DISTINCT q.conversation_id,q.generation,'paused','archived' FROM message_queue q JOIN chat_sessions s ON s.id=q.conversation_id "
            "WHERE q.status='paused' AND q.reason='archived' AND s.archived=1 "
            "ON CONFLICT(conversation_id) DO UPDATE SET generation=excluded.generation,state='paused',reason='archived',owner=NULL")
        controlled = await conn.execute("UPDATE message_queue SET status='paused',revision=revision+1,reason="
            '(SELECT c.reason FROM message_queue_controls c WHERE c.conversation_id=message_queue.conversation_id),updated_at=? '
            "WHERE status='pending' AND EXISTS (SELECT 1 FROM message_queue_controls c WHERE c.conversation_id=message_queue.conversation_id "
            "AND c.generation=message_queue.generation AND c.state<>'ready')", (time.time(),))
        await conn.execute("UPDATE message_queue_controls SET state='paused',reason='archived' WHERE "
            "state<>'paused' AND conversation_id IN (SELECT id FROM chat_sessions WHERE archived=1 OR deleting=1)")
        if changed.rowcount or archived.rowcount or controlled.rowcount:
            await bump(conn)

    async def queue_enqueue(self, *, scope, instance, request_key, text, chat_id):
        if not isinstance(text, str) or not text.strip():
            raise ConversationError('消息不能为空。')
        async with self._transaction() as conn:
            previous = await one(conn, 'SELECT * FROM message_queue WHERE request_key=?', (request_key,))
            if previous:
                if previous['text'] != text or previous['conversation_id'] != scope.conversation_id or previous['generation'] != scope.generation:
                    raise ConversationChanged('请求标识已用于另一条消息，请重新提交。')
                return previous
            if await one(conn, 'SELECT id FROM message_queue_requests WHERE id=?', (request_key,)):
                raise ConversationChanged('此请求已作为管理输入接收，不会再次发给 AI。')
            session = await one(conn, 'SELECT * FROM chat_sessions WHERE id=?', (scope.conversation_id,))
            if not session or session['deleting'] or session['archived'] or session['generation'] != scope.generation:
                raise ConversationChanged('会话已变化，消息尚未入队。')
            await self._queue_sync(conn)
            control = await one(conn, 'SELECT * FROM message_queue_controls WHERE conversation_id=? AND generation=?',
                                (scope.conversation_id, scope.generation))
            paused = control and control['state'] != 'ready'
            run = await one(conn, "SELECT value FROM config WHERE key='conversation_running'")
            earlier = await one(conn, "SELECT id FROM message_queue WHERE status IN ('pending','paused') LIMIT 1")
            receipt = bool(scope.origin == 'telegram' and (paused or earlier or run and json.loads(run['value'])))
            now, identifier = time.time(), uuid.uuid4().hex
            await conn.execute('INSERT INTO message_queue(id,request_key,conversation_id,generation,text,origin,'
                'source_instance,dispatch_instance,chat_id,status,reason,created_at,updated_at,receipt_requested) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier, request_key, scope.conversation_id, scope.generation, text, scope.origin, instance, instance,
                 chat_id, 'paused' if paused else 'pending', control['reason'] if paused else 'waiting', now, now, bool(receipt)))
            await bump(conn)
            return await one(conn, 'SELECT * FROM message_queue WHERE id=?', (identifier,))

    async def queue_snapshot(self, cid):
        async with self._transaction() as conn:
            await self._queue_sync(conn)
            session = await one(conn, 'SELECT generation,archived,deleting FROM chat_sessions WHERE id=?', (cid,))
            if not session or session['deleting']:
                raise ConversationChanged('会话不存在。')
            items = await rows(conn, "SELECT * FROM message_queue WHERE conversation_id=? AND status IN ('pending','paused','invalid') ORDER BY seq", (cid,))
            revision = await one(conn, "SELECT value FROM config WHERE key='message_queue_revision'")
            control = await one(conn, 'SELECT * FROM message_queue_controls WHERE conversation_id=? AND generation=?', (cid, session['generation']))
            return {'conversation_id': cid, 'generation': session['generation'],
                    'revision': int(revision['value']) if revision else 0, 'items': items,
                    'paused': bool(control and control['state'] != 'ready'),
                    'reason': control['reason'] if control else ''}

    async def queue_head(self):
        async with self._transaction() as conn:
            await self._queue_sync(conn)
            return await one(conn, "SELECT q.* FROM message_queue q JOIN chat_sessions s ON s.id=q.conversation_id "
                "WHERE q.status='pending' AND q.generation=s.generation AND s.archived=0 AND s.deleting=0 ORDER BY q.seq LIMIT 1")

    async def queue_change(self, cid, generation, identifier, revision, action, request_id=None):
        if action not in {'cancel', 'edit'}:
            raise ConversationError('未知队列操作。')
        async with self._transaction() as conn:
            await self._queue_sync(conn)
            item = await one(conn, 'SELECT * FROM message_queue WHERE id=? AND conversation_id=?', (identifier, cid))
            if not item or item['generation'] != generation:
                raise ConversationChanged('排队消息已失效。')
            if item['status'] == 'edited' and action == 'edit' and request_id and item['edit_request_id'] == request_id:
                return item  # A lost HTTP response must not lose the recovered text.
            if item['status'] == 'cancelled' and action == 'cancel':
                return item
            if item['status'] not in WAITING:
                raise ConversationChanged('消息已接入本轮，无法撤回。')
            if item['revision'] != revision:
                raise ConversationChanged('队列已更新，请在当前消息上重试。')
            await conn.execute('UPDATE message_queue SET status=?,reason=?,revision=revision+1,updated_at=?,edit_request_id=? WHERE id=?',
                               ('edited' if action == 'edit' else 'cancelled', '', time.time(), request_id, identifier))
            await bump(conn)
            return await one(conn, 'SELECT * FROM message_queue WHERE id=?', (identifier,))

    async def _queue_pause_all(self, conn, reason):
        await conn.execute("INSERT INTO message_queue_controls(conversation_id,generation,state,reason) "
            "SELECT DISTINCT q.conversation_id,q.generation,'paused',? FROM message_queue q JOIN chat_sessions s "
            "ON s.id=q.conversation_id AND s.generation=q.generation WHERE q.status IN ('pending','paused') "
            "ON CONFLICT(conversation_id) DO UPDATE SET generation=excluded.generation,state='paused',reason=excluded.reason,owner=NULL", (reason,))
        changed = await conn.execute("UPDATE message_queue SET status='paused',reason=?,revision=revision+1,updated_at=? "
                                     "WHERE status IN ('pending','paused') AND (status<>'paused' OR reason<>?)", (reason, time.time(), reason))
        if changed.rowcount:
            await bump(conn)

    async def queue_pause_all(self, reason):
        async with self._transaction() as conn:
            await self._queue_pause_all(conn, reason)

    async def queue_wait_ask(self, scope, owner):
        async with self._transaction() as conn:
            await conn.execute("INSERT INTO message_queue_controls VALUES(?,?,'ask','ask',?) ON CONFLICT(conversation_id) "
                "DO UPDATE SET generation=excluded.generation,state='ask',reason='ask',owner=excluded.owner", (scope.conversation_id, scope.generation, owner))
            await conn.execute("UPDATE message_queue SET status='paused',reason='ask',revision=revision+1 WHERE "
                "conversation_id=? AND generation=? AND status='pending'", (scope.conversation_id, scope.generation))
            await bump(conn)

    async def queue_resume(self, scope, owner, *, ask=False, cancel_ask=False, revision=None, request_key=None):
        async with self._transaction() as conn:
            await self._queue_sync(conn)
            cid = await self._validate_conversation_write(conn, scope.conversation_id)
            if request_key:
                old = await one(conn, 'SELECT * FROM message_queue_requests WHERE id=?', (request_key,))
                if old:
                    if old['conversation_id'] != cid or old['generation'] != scope.generation or old['kind'] != 'resume':
                        raise ConversationChanged('请求标识已使用。')
                    return
            current_revision = await one(conn, "SELECT value FROM config WHERE key='message_queue_revision'")
            if revision is not None and revision != (int(current_revision['value']) if current_revision else 0):
                raise ConversationChanged('队列已更新，请查看当前状态后再继续。')
            session = await one(conn, 'SELECT archived FROM chat_sessions WHERE id=?', (cid,))
            if session['archived']:
                raise ConversationChanged('请先恢复已归档会话。')
            control = await one(conn, 'SELECT * FROM message_queue_controls WHERE conversation_id=? AND generation=?', (cid, scope.generation))
            if control and control['state'] == 'ask' and not ask:
                raise ConversationChanged('请先提交或取消当前表单。')
            # Submitting a form never resumes a queue explicitly paused by Stop.
            if ask and control and control['state'] != 'ask':
                return
            reason = 'ask_cancelled' if cancel_ask else ''
            await conn.execute('INSERT INTO message_queue_controls VALUES(?,?,?,?,NULL) ON CONFLICT(conversation_id) '
                'DO UPDATE SET generation=excluded.generation,state=excluded.state,reason=excluded.reason,owner=NULL',
                (cid, scope.generation, 'paused' if cancel_ask else 'ready', reason))
            await conn.execute('UPDATE message_queue SET status=?,reason=?,dispatch_instance=?,revision=revision+1,updated_at=? '
                "WHERE conversation_id=? AND generation=? AND status IN ('pending','paused')",
                ('paused' if cancel_ask else 'pending', reason or 'waiting', owner, time.time(), cid, scope.generation))
            if request_key:
                await conn.execute('INSERT INTO message_queue_requests VALUES(?,?,?,?)', (request_key, cid, scope.generation, 'resume'))
            await bump(conn)

    async def queue_claim(self, scope, *, first_id=None, owner=None):
        """Caller holds the OS execution slot; transaction binds records exactly once."""
        async with self._transaction() as conn:
            await self._queue_sync(conn)
            cid = await self._validate_conversation_write(conn, scope.conversation_id)
            running = await one(conn, "SELECT value FROM config WHERE key='conversation_running'")
            run = json.loads(running['value']) if running else None
            if not run or run['run_id'] != scope.run_id or run.get('stop_requested'):
                return []
            if first_id:
                head = await one(conn, "SELECT id,dispatch_instance FROM message_queue WHERE status='pending' ORDER BY seq LIMIT 1")
                if not head or head['id'] != first_id or head['dispatch_instance'] != owner:
                    return []
            batch = await rows(conn, "SELECT * FROM message_queue WHERE conversation_id=? AND generation=? AND status='pending' ORDER BY seq", (cid, scope.generation))
            now = time.time()
            for item in batch:
                metadata = {'queue_id': item['id'], 'queue_origin': item['origin'], 'queue_created_at': item['created_at'],
                            'queue_run_id': scope.run_id, 'src': 'message-queue'}
                record = await self._insert_global_record(conn, item['chat_id'], item['chat_id'], 'user_text', 'user', item['text'], now, cid, metadata)
                await conn.execute('INSERT INTO chat_messages(session_id,role,content,timestamp) VALUES(?,?,?,?)', (cid, 'user', item['text'], now))
                await conn.execute("UPDATE message_queue SET status='claimed',reason='',run_id=?,record_id=?,revision=revision+1,updated_at=? WHERE id=?", (scope.run_id, record, now, item['id']))
                item.update(record_id=record, run_id=scope.run_id, status='claimed', revision=item['revision'] + 1)
            if batch:
                title = ' '.join(batch[0]['text'].split())
                title = title[:32] + ('…' if len(title) > 32 else '')
                await conn.execute('UPDATE chat_sessions SET name=CASE WHEN title_auto_pending=1 THEN ? ELSE name END,title_auto_pending=0,last_active=? WHERE id=?', (title, now, cid))
                await self._bump_conversation_revision(conn)
                await bump(conn)
            return batch

    async def queue_finish_run(self, run_id, outcome):
        async with self._transaction() as conn:
            if outcome in {'failed', 'stopped', 'interrupted'}:
                await self._queue_pause_all(conn, 'stopped' if outcome == 'stopped' else 'failed')
            changed = await conn.execute("UPDATE message_queue SET status=?,reason=?,revision=revision+1,updated_at=? WHERE run_id=? AND status='claimed'",
                ('completed' if outcome in {'completed', 'ask'} else 'interrupted', '' if outcome in {'completed', 'ask'} else 'interrupted', time.time(), run_id))
            if changed.rowcount:
                await bump(conn)

    async def queue_recover_instance(self, instance):
        async with self._transaction() as conn:
            await self._queue_sync(conn)
            await conn.execute("INSERT INTO message_queue_controls(conversation_id,generation,state,reason) SELECT DISTINCT conversation_id,generation,'paused','restart' "
                "FROM message_queue WHERE dispatch_instance=? AND status IN ('pending','paused') "
                "ON CONFLICT(conversation_id) DO UPDATE SET generation=excluded.generation,state='paused',reason='restart',owner=NULL", (instance,))
            changed = await conn.execute("UPDATE message_queue SET status='paused',reason='restart',revision=revision+1,updated_at=? WHERE dispatch_instance=? AND status IN ('pending','paused') AND reason<>'restart'", (time.time(), instance))
            controls = await conn.execute("UPDATE message_queue_controls SET state='paused',reason='restart',owner=NULL WHERE owner=?", (instance,))
            await conn.execute('DELETE FROM message_queue_instances WHERE id=?', (instance,))
            if changed.rowcount or controls.rowcount:
                await bump(conn)

    async def queue_receipts(self):
        return await rows(await self._get_conn(), 'SELECT * FROM message_queue WHERE receipt_requested=1 AND receipt_revision<>revision')

    async def queue_request_receipt(self, identifier):
        async with self._write() as conn:
            await conn.execute('UPDATE message_queue SET receipt_requested=1 WHERE id=?', (identifier,))

    async def queue_item(self, identifier):
        return await one(await self._get_conn(), 'SELECT * FROM message_queue WHERE id=?', (identifier,))

    async def queue_bind_receipt(self, identifier, mid, revision):
        async with self._write() as conn:
            await conn.execute('UPDATE message_queue SET receipt_id=?,receipt_revision=? WHERE id=?', (mid, revision, identifier))
