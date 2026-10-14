"""Live ownership and scheduling around the durable text queue. No model engine."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import uuid

from .conversations import ConversationBusy, ConversationChanged, ConversationError, ExecutionFileLock, bind_conversation, current_scope
from .interaction import interaction
from .queue_store import REASONS


def public_item(item):
    return {key: item[key] for key in ('id', 'seq', 'conversation_id', 'generation', 'text', 'origin',
            'status', 'reason', 'revision', 'created_at', 'record_id') if key in item}


def public_snapshot(snapshot):
    return {**snapshot, 'items': [public_item(item) for item in snapshot['items']]}


class MessageQueue:
    def __init__(self, manager, dispatch, notify=None, receipts=None):
        self.manager, self.dispatch, self.notify, self.receipts = manager, dispatch, notify, receipts
        self.instance = uuid.uuid4().hex
        self._alive = self._instance_lock(self.instance)
        self._started = False
        self._start_lock = asyncio.Lock()
        self._poll_lock = asyncio.Lock()
        self._task = None
        self._closed = False
        self._revision = -1

    def _instance_lock(self, instance):
        # IDs come only from our database; do not accept arbitrary path components.
        if len(instance) != 32 or any(ch not in '0123456789abcdef' for ch in instance):
            raise ConversationChanged('无效的队列进程标识。')
        return ExecutionFileLock(str(Path(self.manager.lock.path).resolve()) + '.queue-' + instance)

    async def start(self):
        async with self._start_lock:
            if self._closed:
                raise ConversationChanged('消息队列已关闭。')
            if self._started:
                return
            if not self._alive.acquire():
                raise ConversationChanged('消息队列已关闭。')
            try:
                await (await self.manager.factory()).queue_register(self.instance)
                self._started = True
            except BaseException:
                self._alive.release()
                raise

    async def submit(self, text, *, chat_id, request_key=None):
        await self.start()
        scope = current_scope()
        if scope.purpose != 'chat':
            raise ConversationChanged('只有普通聊天文本可以排队。')
        key = request_key or f'{scope.origin}:{self.instance}:{uuid.uuid4().hex}'
        try:
            item = await (await self.manager.factory()).queue_enqueue(scope=scope, instance=self.instance,
                request_key=key, text=text, chat_id=chat_id)
        except ConversationError:
            raise
        except Exception as exc:
            raise ConversationError('待发消息保存失败，输入尚未提交，请重试。') from exc
        return item

    async def snapshot(self, cid):
        await self.start()
        return public_snapshot(await (await self.manager.factory()).queue_snapshot(cid))

    async def recover_sources(self, db):
        for row in await db.queue_instances():
            if row['id'] == self.instance:
                continue
            probe = self._instance_lock(row['id'])
            if probe.acquire():
                try:
                    await db.queue_recover_instance(row['id'])
                finally:
                    probe.release()

    async def poll(self):
        if not self._started or self._closed or self._poll_lock.locked():
            return
        async with self._poll_lock:
            db = await self.manager.factory()
            await self.recover_sources(db)
            # Sync invalidations before notifying. A single monotonic revision is
            # shared across processes; the UI fetches only its selected queue.
            head = await db.queue_head()
            revision = await db.get_config_fresh('message_queue_revision', 0)
            if revision != self._revision:
                if self.notify:
                    await self.notify(int(revision))
                self._revision = revision
            if self.receipts:
                await self.receipts(db)
            if (head and head['dispatch_instance'] == self.instance and not self.manager.lock.busy()
                    and (self._task is None or self._task.done())):
                self._task = asyncio.create_task(self._drain(head), name='queued-text-turn')
                self.manager.track_background(self._task)

    async def _drain(self, head):
        if self._closed:
            return
        try:
            origin = 'cli' if self.manager.terminal_enabled else ('web' if head['origin'] == 'cli' else head['origin'])
            with interaction(origin, 'chat'), bind_conversation(None):
                scope = await self.manager.resolve(head['conversation_id'])
                if scope.generation != head['generation']:
                    return
                async with self.manager.operation(scope, execution=True):
                    db = await self.manager.factory()
                    await self.recover_sources(db)
                    batch = await db.queue_claim(current_scope(), first_id=head['id'], owner=self.instance)
                    if batch:
                        await self.ensure_delivery(batch)
                        await self.dispatch(batch)
        except (ConversationBusy, ConversationChanged):
            pass  # Another owner won, or a reset invalidated the original input.
        except asyncio.CancelledError:
            raise
        except Exception:
            import logging
            logging.getLogger(__name__).exception('排队消息执行失败；未领取消息保留暂停')

    async def claim_continuation(self):
        if (self.manager.lock.fd is None or not current_scope().run_id
                or self.manager._run_outcome in {'failed', 'stopped', 'interrupted'}):
            return []
        db = await self.manager.factory()
        await self.recover_sources(db)
        batch = await db.queue_claim(current_scope())
        await self.ensure_delivery(batch)
        return batch

    async def ensure_delivery(self, batch):
        if any(item['origin'] == 'telegram' for item in batch):
            # A native question joins this run, never grants permission to old
            # muted operations. Web's current reply subscription stays sticky.
            delivery = self.manager._reply_delivery
            if delivery:
                delivery.subscribe()
            from . import conversations
            conversations._scope.set(replace(current_scope(), telegram_delivery=True))

    async def close(self):
        self._closed = True
        if self._started:
            try:
                await (await self.manager.factory()).queue_recover_instance(self.instance)
            finally:
                self._alive.release()
        self._started = False


def mark_outcome(outcome):
    from . import conversations
    manager = conversations._manager
    scope = current_scope(required=False)
    if manager and scope and scope.run_id == manager._run_id:
        if manager._run_outcome not in {'failed', 'stopped', 'interrupted'}:
            manager._run_outcome = outcome


def batch_messages(batch):
    return [{'role': 'user', 'content': item['text']} for item in batch]


def queue_card_text(item):
    status = {'pending': '⏳ 排队中', 'paused': '⏸ 已暂停', 'invalid': '⚠️ 已失效',
              'claimed': '✅ 已接入本轮', 'completed': '✅ 已处理', 'interrupted': '⚠️ 执行中断',
              'cancelled': '❌ 已取消', 'edited': '✏️ 已取回编辑'}[item['status']]
    reason = REASONS.get(item.get('reason'), '')
    preview = item['text'][:240] + ('…' if len(item['text']) > 240 else '')
    return f"{status} · {item['id'][:8]}\n{reason}\n\n{preview}"
