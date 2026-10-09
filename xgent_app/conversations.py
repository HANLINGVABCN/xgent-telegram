"""Logical conversation scopes and the single cross-process execution slot.

No Telegram imports at module load. Context is captured at admission and serialized before queues;
changing the selected conversation never rebinds an admitted operation.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import functools
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULT_CONVERSATION_ID = 'global_memory'
DEFAULT_CONVERSATION_NAME = '默认会话'
SCHEMA_VERSION = 2


class ConversationError(ValueError):
    status = 409


class ConversationBusy(ConversationError):
    pass


class ConversationChanged(ConversationError):
    pass


@dataclasses.dataclass(frozen=True)
class ConversationScope:
    conversation_id: str
    generation: int
    run_id: str | None = None
    name: str = ''


_scope = contextvars.ContextVar('xgent_conversation', default=None)
_manager = None


def current_scope(*, required=True) -> ConversationScope | None:
    scope = _scope.get()
    if scope is None and required:
        raise ConversationError('会话操作缺少作用域，已拒绝读写。')
    return scope


@contextlib.contextmanager
def bind_conversation(scope: ConversationScope):
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


def advance_generation(generation: int) -> None:
    # Replace, never mutate: already-created child tasks retain their old version.
    _scope.set(dataclasses.replace(current_scope(), generation=int(generation)))


def refresh_scope_name(name):
    scope = current_scope(required=False)
    if scope is not None:
        _scope.set(dataclasses.replace(scope, name=str(name)))


async def notify_conversation_metadata(database):
    if _manager is not None and str(Path(_manager.lock.path).resolve()) == str(Path(str(database.db_path) + '.turn.lock').resolve()):
        await _manager.poll_once()


def conversation_snapshot() -> dict | None:
    scope = current_scope(required=False)
    return dataclasses.asdict(scope) if scope is not None else None


@contextlib.contextmanager
def replay_conversation(snapshot):
    if not isinstance(snapshot, dict) or not snapshot.get('conversation_id'):
        token = _scope.set(None)
        try:
            yield
        finally:
            _scope.reset(token)
        return
    scope = ConversationScope(str(snapshot['conversation_id']), int(snapshot['generation']),
                              snapshot.get('run_id'), str(snapshot.get('name') or ''))
    with bind_conversation(scope):
        yield scope


def stamp_frame(frame: dict) -> dict:
    scope = current_scope(required=False)
    if scope is None or frame.get('type') in {'conversation_state', 'settings_state', 'knowledge_state'}:
        return dict(frame)
    return {'conversation_id': scope.conversation_id, 'generation': scope.generation,
            'run_id': scope.run_id, 'conversation_name': scope.name, **frame}


def require_conversation_id(explicit=None) -> str:
    scope = current_scope()
    if explicit is not None and str(explicit) != scope.conversation_id:
        raise ConversationError('会话归属不匹配，操作已拒绝。')
    return scope.conversation_id


class ExecutionFileLock:
    """Non-inherited OS lock; never delete the lock file (avoids inode races)."""
    def __init__(self, database_path):
        self.path = str(Path(database_path).resolve()) + '.turn.lock'
        self.fd = None

    def acquire(self) -> bool:
        if self.fd is not None:
            return False
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.set_inheritable(fd, False)
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b'0')
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self):
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def busy(self):
        if self.fd is not None:
            return True
        probe = ExecutionFileLock(self.path.removesuffix('.turn.lock'))
        if not probe.acquire():
            return True
        probe.release()
        return False


class ConversationManager:
    def __init__(self, factory, database_path, notify=None):
        self.factory = factory
        self.lock = ExecutionFileLock(database_path)
        self.notify = notify
        self._poll_task = None
        self._background = set()
        self._completions = []
        self._last_state = None
        self._stop_event = None
        self._run_id = None
        self._admission_lock = asyncio.Lock()
        self.maintenance = None

    async def state(self):
        db = await self.factory()
        state = await db.get_conversation_state()
        # A dead owner leaves metadata, not a live lock. Never resurrect that run.
        if state.get('running') and not self.lock.busy():
            await db.finish_conversation_run(state['running']['run_id'])
            state['running'] = None
        return state

    async def resolve(self, conversation_id=None, *, expected=False, allow_archived=False):
        db = await self.factory()
        state = await db.get_conversation_state()
        active = state['current_chat_id']
        cid = str(conversation_id or active)
        if expected and cid != active:
            raise ConversationChanged('当前会话已在另一端切换，请确认后重新发送；内容尚未提交。')
        session = await db.get_session(cid)
        if session is None or session.get('deleting'):
            raise ConversationChanged('该会话不存在，请刷新会话列表。')
        if session.get('archived') and not allow_archived:
            raise ConversationChanged('该会话已归档，请先恢复。')
        return ConversationScope(cid, int(session['generation']), name=session.get('name') or '新对话')

    @contextlib.asynccontextmanager
    async def operation(self, conversation_id=None, *, execution=False, wait=False,
                        expected=False, allow_archived=False, fresh=False):
        inherited = current_scope(required=False)
        if isinstance(conversation_id, ConversationScope):
            scope = conversation_id
        elif inherited is not None and not fresh:
            if conversation_id is not None:
                require_conversation_id(conversation_id)
            scope = inherited
        else:
            scope = await self.resolve(conversation_id, expected=expected, allow_archived=allow_archived)
        acquired = False
        if execution and not (scope.run_id and scope.run_id == self._run_id):
            while True:
                async with self._admission_lock:
                    if self.lock.acquire():
                        acquired = True
                        break
                if not wait:
                    raise ConversationBusy('另一个会话正在执行，请等待完成或停止该回合后重试。')
                await asyncio.sleep(0.1)
            try:
                # Check again after acquisition, before any message is persisted.
                fresh_scope = await self.resolve(scope.conversation_id, expected=expected,
                                                 allow_archived=allow_archived)
                if fresh_scope.generation != scope.generation:
                    raise ConversationChanged('对话已清空或压缩，旧操作未提交。')
                scope = dataclasses.replace(scope, run_id=uuid.uuid4().hex)
                self._run_id = scope.run_id
                self._completions = []
                self._stop_event = asyncio.Event()
                db = await self.factory()
                await db.begin_conversation_run({
                    'run_id': scope.run_id, 'conversation_id': scope.conversation_id,
                    'name': scope.name, 'pid': os.getpid(), 'started_at': time.time(),
                    'stop_requested': False,
                })
                await self.poll_once()
            except BaseException:
                self._run_id = None
                self._stop_event = None
                self.lock.release()
                raise
        final_scope = None
        try:
            with bind_conversation(scope):
                try:
                    yield scope
                except BaseException as exc:
                    if not hasattr(exc, 'conversation_context'):
                        exc.conversation_context = dataclasses.asdict(current_scope())
                    raise
                finally:
                    final_scope = current_scope(required=False)
        finally:
            if inherited and not inherited.run_id and final_scope and inherited.conversation_id == final_scope.conversation_id and inherited.generation != final_scope.generation:
                _scope.set(dataclasses.replace(inherited, generation=final_scope.generation))
            if acquired:
                try:
                    db = await self.factory()
                    await db.finish_conversation_run(scope.run_id)
                finally:
                    self._run_id = None
                    self._stop_event = None
                    self.lock.release()
                    completions, self._completions = self._completions, []
                    for callback in completions:
                        with contextlib.suppress(Exception):
                            callback()
                await self.poll_once()

    def stop_event(self):
        return self._stop_event

    def attach_stop_event(self, event):
        if self._stop_event is not None and self._stop_event.is_set():
            event.set()
        self._stop_event = event

    async def request_stop(self, run_id, conversation_id=None):
        if not run_id:
            raise ConversationChanged('停止按钮已失效，请使用当前回合的停止按钮。')
        db = await self.factory()
        accepted = await db.request_conversation_stop(str(run_id), conversation_id)
        if accepted and run_id == self._run_id and self._stop_event is not None:
            self._stop_event.set()
        return accepted

    async def manage(self, action, conversation_id=None, name=None):
        db = await self.factory()
        result = await db.manage_conversation(action, conversation_id, name)
        await self.poll_once()
        return result

    async def poll_once(self):
        if self.maintenance is not None:
            await self.maintenance()
        state = await self.state()
        running = state.get('running') or {}
        if running.get('run_id') == self._run_id and running.get('stop_requested') and self._stop_event:
            self._stop_event.set()
        encoded = json.dumps(state, sort_keys=True, ensure_ascii=False)
        if encoded != self._last_state:
            self._last_state = encoded
            if self.notify is not None:
                result = self.notify(state)
                if hasattr(result, '__await__'):
                    await result
        return state

    def start(self):
        if self._poll_task is None or self._poll_task.done():
            # Do not inherit the scope of the request that happened to start us.
            async def poll():
                token = _scope.set(None)
                try:
                    while True:
                        try:
                            await self.poll_once()
                        except Exception:
                            import logging
                            logging.getLogger(__name__).exception('会话状态同步失败')
                        await asyncio.sleep(0.3)
                finally:
                    _scope.reset(token)
            self._poll_task = asyncio.create_task(poll(), name='conversation-state')

    def track_background(self, task):
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def close(self):
        tasks = list(self._background)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None


def configure_conversations(factory, database_path, notify=None):
    global _manager
    _manager = ConversationManager(factory, database_path, notify)
    return _manager


def get_conversations() -> ConversationManager:
    if _manager is None:
        raise ConversationError('会话服务尚未初始化。')
    return _manager


@contextlib.asynccontextmanager
async def conversation_operation(conversation_id=None, **options):
    async with get_conversations().operation(conversation_id, **options) as scope:
        yield scope


def conversation_entry(*, execution=False, wait=False, resolve=None, complete=None, guard=None):
    """Wrap an entry, before its first side effect; nested entries keep identity."""
    def decorate(function):
        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            if guard is not None and not await guard(*args, **kwargs):
                return None
            execute = execution() if callable(execution) else execution
            cid = kwargs.pop('conversation_id', None)
            if resolve is not None:
                cid = resolve(*args, **kwargs) or cid
            async with conversation_operation(cid, execution=execute, wait=wait) as admitted:
                try:
                    result = await function(*args, **kwargs)
                    if complete is not None:
                        await complete(*args, **kwargs)
                    return result
                except ConversationError:
                    event = get_conversations().stop_event()
                    if admitted.run_id and event is not None and event.is_set():
                        return None  # A stopped/cleared turn must not revive its old data.
                    raise
        return wrapped
    return decorate


_callback_encoder = lambda value: value
_callback_resolver = lambda value: value


def configure_callback_encoder(encoder, resolver=None):
    global _callback_encoder, _callback_resolver
    _callback_encoder = encoder
    _callback_resolver = resolver or (lambda value: value)


def decode_conversation_callback(value):
    if not value.startswith('cv:'):
        return value
    _, generation, action = value.split(':', 2)
    scope = current_scope()
    if str(scope.generation) != generation:
        raise ConversationChanged('这个按钮属于另一会话或旧上下文，请切回原会话并重新打开菜单。')
    return action


def bind_callback_markup(markup):
    scope = current_scope(required=False)
    if scope is None or not getattr(markup, 'inline_keyboard', None):
        return markup
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = []
    for row in markup.inline_keyboard:
        bound = []
        for button in row:
            data = getattr(button, 'callback_data', None)
            if data and not str(data).startswith(('cv:', 'act_stop_generation')):
                fields = button.to_dict()
                action = _callback_resolver(str(data))
                fields['callback_data'] = _callback_encoder(f'cv:{scope.generation}:{action}')
                bound.append(InlineKeyboardButton.de_json(fields, None))
            else:
                bound.append(button)
        rows.append(bound)
    return InlineKeyboardMarkup(rows)



def conversation_secret_environment():
    scope = current_scope(required=False)
    if scope is None:
        return {}
    from xgent_app.agent_ask import SECRET_STORE
    return SECRET_STORE.environment(scope.conversation_id)


def conversation_labelled_text(text, parse_mode=None, *, limit=4096):
    """Label scoped Telegram replies, including menus without a model run.

    Callers retain unmodified history/Web text; unscoped system notices stay plain.
    """
    scope = current_scope(required=False)
    from xgent_app.telegram_presentation import label_enabled
    if scope is None or not text or not label_enabled():
        return text
    label = f'🗂 {scope.name or scope.conversation_id[:8]} · {scope.conversation_id[:6]}'
    mode = str(parse_mode or '').lower()
    if mode == 'html':
        import html
        label = html.escape(label)
    elif mode.startswith('markdown'):
        from telegram.helpers import escape_markdown
        label = escape_markdown(label, version=2 if 'v2' in mode else 1)
    prefix = label + '\n'
    if str(text).startswith(prefix) or len(prefix) + len(str(text)) > limit:
        return text
    return prefix + str(text)


def defer_completion(frame, callback):
    """Publish completion only after releasing the OS slot (including CLI relay)."""
    kind = frame.get('type')
    completion = kind in {'turn_end', 'turn_error', 'generation_end'} or (
        kind == 'compression_state' and frame.get('busy') is False)
    if completion and _manager is not None and frame.get('run_id') and frame['run_id'] == _manager._run_id:
        _manager._completions.append(callback)
        return True
    return False


async def conversation_still_exists(snapshot):
    if _manager is None or not snapshot or not snapshot.get('conversation_id'):
        return True
    db = await _manager.factory()
    session = await db.get_session(snapshot['conversation_id'])
    return session is not None and not session.get('deleting')
