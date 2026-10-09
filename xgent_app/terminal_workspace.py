"""Per-terminal view, drafts' admission identity and persisted-history reconciliation.

No HTTP service, second agent or global selection. The existing manager owns runs.
"""
from __future__ import annotations

import asyncio
import contextlib
from contextvars import ContextVar
from dataclasses import dataclass
import json
import os
from typing import Callable

from .conversations import bind_conversation, ConversationChanged
from .interaction import interaction


@dataclass
class Submission:
    snapshot: dict
    on_admitted: Callable[[], None] = lambda: None
    accepted: bool = False
    error: str = ''

    def admit(self):
        if not self.accepted:
            self.accepted = True
            self.on_admitted()


_submission = ContextVar('terminal_submission', default=None)


@contextlib.contextmanager
def submitted(snapshot, on_admitted=lambda: None):
    item = Submission(dict(snapshot), on_admitted)
    token = _submission.set(item)
    try:
        yield item
    finally:
        _submission.reset(token)


def current_submission():
    return _submission.get()


class TerminalWorkspace:
    """One instance per running terminal, with a bounded history window per view."""
    def __init__(self, manager, screen, render_record, config=None):
        self.manager, self.screen, self.render_record = manager, screen, render_record
        self.config = config
        self.frame = {}
        self.items = []
        self.model_name = ''
        self.finished = {}
        self._history_stamp = {}
        self._limits = {}
        self._lock = asyncio.Lock()
        self._poll = None
        self._local_runs = {}
        self._closed = False
        self._finishing = set()

    @property
    def selected(self):
        return self.frame.get('terminal_conversation_id')

    def snapshot(self):
        item = next((c for c in self.items if c['id'] == self.selected), {})
        return {'conversation_id': self.selected, 'generation': item.get('generation'),
                'name': item.get('name') or '选择对话',
                'revision': self.frame.get('terminal_revision', 0)}

    def on_state(self, frame):
        old_run = self.frame.get('running') or {}
        self.frame = dict(frame)
        self.items = list(frame.get('items', self.items))
        snapshot = self.snapshot()
        existing = {item['id'] for item in self.items}
        from .cli_bridge import set_visible_conversation
        set_visible_conversation(snapshot['conversation_id'])
        self.screen.drop_conversations(existing)
        self.screen.select_conversation(snapshot['conversation_id'], snapshot['generation'])
        self.finished.pop(self.selected, None)
        new_run = frame.get('running') or {}
        if new_run.get('pid') == os.getpid():
            self._local_runs[new_run['conversation_id']] = new_run['run_id']
        if old_run and old_run.get('run_id') != new_run.get('run_id'):
            cid = old_run['conversation_id']
            if old_run.get('pid') == os.getpid() and not self._closed:
                from types import SimpleNamespace
                task = asyncio.create_task(self.end_run(SimpleNamespace(conversation_id=cid, run_id=old_run['run_id'])))
                self._finishing.add(task)
                task.add_done_callback(self._finishing.discard)
            if cid in existing and cid != self.selected:
                self.finished[cid] = '已结束'
                self.screen.flash((old_run.get('name') or cid[:6]) + ' · 后台回合已结束')
        self.screen.invalidate()

    async def start(self):
        self.screen.conversation_routing = True
        await self.manager.enable_terminal()
        await self.refresh_state()
        await self.refresh_history(force=True)
        self._poll = asyncio.create_task(self._watch(), name='terminal-history')

    async def close(self):
        self._closed = True
        if self._poll is not None:
            self._poll.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll
        if self._finishing:
            await asyncio.gather(*self._finishing, return_exceptions=True)

    async def refresh_state(self):
        db = await self.manager.factory()
        self.on_state({**await self.manager.state(), 'items': await db.get_all_sessions()})
        if self.config is not None:
            model = str(await db.get_config_fresh('default_model', '') or '')
            if model != self.model_name:
                self.model_name = model
                self.screen.invalidate()

    async def _watch(self):
        while True:
            try:
                await self.refresh_state()
                await self.refresh_history()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.screen.flash('历史同步暂不可用，保留当前内容；稍后自动重试。')
            await asyncio.sleep(.75)

    async def refresh_history(self, *, force=False, older=False, conversation_id=None):
        cid = conversation_id or self.selected
        if cid is None or self._closed or cid in self._local_runs:
            return
        async with self._lock:
            db = await self.manager.factory()
            session = await db.get_session(cid)
            if session is None or session.get('deleting'):
                return
            limit = min(5000, self._limits.get(cid, 100) + (100 if older else 0))
            self._limits[cid] = limit
            conn = await db._get_conn()
            cursor = await conn.execute("SELECT (SELECT MAX(id) FROM global_messages WHERE session_id=?), "
                "(SELECT MAX(timestamp) FROM global_messages WHERE session_id=?), "
                "(SELECT SUM(revision) FROM ui_messages WHERE conversation_id=?)", (cid, cid, cid))
            stamp = (tuple(await cursor.fetchone()), session['generation'], limit)
            await cursor.close()
            if not force and self._history_stamp.get(cid) == stamp:
                return
            with interaction('cli', 'history'):
                scope = await self.manager.resolve(cid, allow_archived=True, selector='terminal')
                with bind_conversation(scope):
                    rows = await db.get_display_history(limit)
                    rendered = []
                    for row in rows:
                        if row.get('msg_type') == 'ui_message':
                            continue  # Other clients' menu navigation is not a new chat message.
                        key = ('h:' + str(row['id']) if row.get('id') is not None
                               else 'u:' + str(row.get('ui_message_id')))
                        rendered.append((key, self.render_record(row),
                                         json.dumps(row, sort_keys=True, ensure_ascii=False)))
                    self.screen.replace_history(cid, rendered)
            self._history_stamp[cid] = stamp
            if older:
                self.screen.flash(f'已加载 {len(rows)} 条历史' + ('（已到最早记录）' if len(rows) < limit else ''))

    def begin_run(self, scope):
        if scope.run_id:
            self._local_runs[scope.conversation_id] = scope.run_id

    async def end_run(self, scope):
        if self._local_runs.get(scope.conversation_id) == scope.run_id:
            self._local_runs.pop(scope.conversation_id, None)
        self.screen.finish_run(scope.conversation_id, scope.run_id)
        await self.refresh_history(force=True, conversation_id=scope.conversation_id)

    async def manage(self, action, cid=None, name=None):
        with interaction('cli', 'management'):
            if action == 'delete_info':
                db = await self.manager.factory()
                return await db.conversation_delete_info(cid)
            if action == 'delete':
                if self.config is None:
                    raise ConversationChanged('删除服务尚未就绪。')
                result = await self.config['delete_conversation'](cid)
            elif action == 'reset_context':
                result = await self.config['reset_conversation_context'](cid)
            elif action == 'restore_open':
                await self.manager.manage('restore', cid, selector='terminal')
                result = await self.manager.manage('switch', cid, selector='terminal')
            else:
                result = await self.manager.manage(action, cid, name, selector='terminal')
            await self.refresh_state()
            await self.refresh_history(force=True)
            return result
