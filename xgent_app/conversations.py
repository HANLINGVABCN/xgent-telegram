"""Logical conversation scopes and independent cross-process execution slots.

No Telegram imports at module load. Context is captured at admission and serialized before queues;
changing the selected conversation never rebinds an admitted operation.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import functools
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULT_CONVERSATION_ID = 'global_memory'
DEFAULT_CONVERSATION_NAME = '默认会话'
SCHEMA_VERSION = 7


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
    origin: str = 'legacy'
    purpose: str = 'chat'
    telegram_delivery: bool | None = None
    workspace_path: str | None = None


_scope = contextvars.ContextVar('xgent_conversation', default=None)
_manager = None


def current_scope(*, required=True) -> ConversationScope | None:
    scope = _scope.get()
    if scope is None and required:
        raise ConversationError('会话操作缺少作用域，已拒绝读写。')
    return scope


@contextlib.contextmanager
def bind_conversation(scope: ConversationScope | None):
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
    from xgent_app.interaction import interaction
    scope = ConversationScope(str(snapshot['conversation_id']), int(snapshot['generation']),
                              snapshot.get('run_id'), str(snapshot.get('name') or ''),
                              str(snapshot.get('origin') or 'legacy'), str(snapshot.get('purpose') or 'chat'),
                              snapshot.get('telegram_delivery'))
    with interaction(scope.origin, scope.purpose), bind_conversation(scope):
        yield scope


def restore_reply_route(snapshot):
    """Continue a form's original delivery contract, retaining the new run identity."""
    if not snapshot:
        return
    scope = current_scope()
    if snapshot.get('conversation_id') != scope.conversation_id or snapshot.get('generation') != scope.generation:
        raise ConversationChanged('表单已不属于当前会话或上下文。')
    _scope.set(dataclasses.replace(scope, origin=snapshot.get('origin', 'legacy'),
                                  purpose=snapshot.get('purpose', 'chat'),
                                  telegram_delivery=snapshot.get('telegram_delivery')))


_reply_generation = contextvars.ContextVar('reply_generation', default=None)


def set_reply_generation(provider, model):
    scope = current_scope(required=False)
    if scope is not None:
        _reply_generation.set({'conversation_id':scope.conversation_id,'run_id':scope.run_id,
            'provider':str(provider or ''),'model':str(model or ''),'generated_at':time.time()})


def stamp_frame(frame: dict) -> dict:
    scope = current_scope(required=False)
    if scope is None or frame.get('type') in {'conversation_state', 'settings_state', 'knowledge_state', 'queue_state'}:
        return dict(frame)
    result = {'conversation_id': scope.conversation_id, 'generation': scope.generation,
              'run_id': scope.run_id, 'conversation_name': scope.name, **frame}
    generated = _reply_generation.get()
    if (scope.run_id and generated and generated['run_id'] == scope.run_id
            and generated['conversation_id'] == scope.conversation_id
            and frame.get('type') in {'message','edit','document','photo'}):
        result.setdefault('ai_generation', {key:generated[key] for key in ('provider','model','generated_at')})
    return result


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


@dataclasses.dataclass
class ConversationRun:
    scope: ConversationScope
    lock: ExecutionFileLock
    stop: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    outcome: str = 'completed'
    delivery: Any = None
    completions: list = dataclasses.field(default_factory=list)


class ConversationProcessingLock:
    """Serialize one conversation while letting unrelated tasks progress."""
    def __init__(self):
        self._locks = {}

    def _current(self):
        scope = current_scope(required=False)
        key = scope.conversation_id if scope else None
        return self._locks.setdefault(key, asyncio.Lock())

    def locked(self):
        if current_scope(required=False) is None:
            return any(lock.locked() for lock in tuple(self._locks.values()))
        return self._current().locked()

    async def __aenter__(self):
        await self._current().acquire()
        return self

    async def __aexit__(self, *args):
        self._current().release()


class ConversationManager:
    def __init__(self, factory, database_path, notify=None, code_root=None):
        self.factory = factory
        self.lock = ExecutionFileLock(database_path)
        self.notify = notify
        self._poll_task = None
        self._background = set()
        self._last_state = None
        self._runs = {}
        self._execution_locks = {}
        self._observed_runs = {}
        self.text_queue = None
        self._admission_lock = asyncio.Lock()
        self.maintenance = None
        self.terminal_enabled = False
        self._terminal_id = None
        self._terminal_revision = 0
        self.code_root = Path(code_root).resolve() if code_root else None
        data_root = Path(database_path).resolve().parent if database_path != ':memory:' else None
        self.object_root = data_root / 'xgent_storage' / 'conversation_objects' if data_root else None

    def execution_lock(self, cid):
        if cid not in self._execution_locks:
            key = hashlib.sha256(str(cid).encode('utf-8')).hexdigest()
            self._execution_locks[cid] = ExecutionFileLock(self.lock.path + '.conversation-' + key)
        return self._execution_locks[cid]

    def runtime(self, run_id=None):
        if run_id is None:
            scope = current_scope(required=False)
            run_id = scope.run_id if scope else None
        return self._runs.get(run_id)

    @property
    def _run_id(self):
        run = self.runtime()
        return run.scope.run_id if run else None

    @property
    def _reply_delivery(self):
        run = self.runtime()
        return run.delivery if run else None

    @property
    def _run_outcome(self):
        run = self.runtime()
        return run.outcome if run else 'completed'

    @_run_outcome.setter
    def _run_outcome(self, outcome):
        run = self.runtime()
        if run:
            run.outcome = outcome

    def busy(self, conversation_id=None):
        scope = current_scope(required=False)
        cid = conversation_id or (scope.conversation_id if scope else None)
        if self.lock.busy():
            return True
        if cid is not None:
            return self.execution_lock(cid).busy()
        return any(lock.busy() for lock in tuple(self._execution_locks.values())) or any(
            self.execution_lock(cid).busy() for cid in tuple(self._observed_runs))

    async def enable_terminal(self):
        """One selection per CLI process; never persisted as a shared global pointer."""
        if not self.terminal_enabled:
            db = await self.factory()
            state = await db.get_conversation_state()
            if not self.terminal_enabled:
                self._terminal_id = state['current_chat_id']
                self.terminal_enabled = True
        return await self.state()

    async def _terminal_state(self, state):
        if self.terminal_enabled:
            if self._terminal_id is not None:
                db = await self.factory()
                checked_id = self._terminal_id
                session = await db.get_session(checked_id)
                if checked_id == self._terminal_id and (session is None or session.get('archived') or session.get('deleting')):
                    self._terminal_id = None
                    self._terminal_revision += 1
            state = {**state, 'running': state.get('runs', {}).get(self._terminal_id), 'terminal_conversation_id': self._terminal_id,
                     'terminal_revision': self._terminal_revision}
        return state

    async def state(self):
        db = await self.factory()
        state = await db.get_conversation_state()
        recovered = False
        # Probe each owner separately; a live B never hides a crashed A.
        for cid, saved in list(state.get('runs', {}).items()):
            if saved['run_id'] in self._runs:
                continue
            guard = ExecutionFileLock(self.lock.path.removesuffix('.turn.lock'))
            if not guard.acquire():
                continue
            probe = ExecutionFileLock(self.execution_lock(cid).path.removesuffix('.turn.lock'))
            acquired = False
            try:
                acquired = bool(saved.get('legacy_lock') or probe.acquire())
                if acquired:
                    fresh = (await db.get_conversation_state()).get('runs', {}).get(cid)
                    if fresh and fresh['run_id'] == saved['run_id']:
                        await db.queue_finish_run(saved['run_id'], 'interrupted')
                        await db.finish_conversation_run(saved['run_id'])
                        recovered = True
            finally:
                if acquired and not saved.get('legacy_lock'):
                    probe.release()
                guard.release()
        if recovered:
            state = await db.get_conversation_state()
        self._observed_runs = state.get('runs', {})
        return await self._terminal_state(state)

    async def resolve(self, conversation_id=None, *, expected=False, allow_archived=False, selector=None):
        db = await self.factory()
        state = await db.get_conversation_state()
        from xgent_app.interaction import identity, selection_key
        key = selection_key(selector)
        if key == 'terminal_conversation_id':
            if not self.terminal_enabled:
                await self.enable_terminal()
            state = await self._terminal_state(state)
        active = state.get(key)
        if conversation_id is None and active is None:
            subject = '终端' if key == 'terminal_conversation_id' else 'Bot'
            raise ConversationChanged(f'请先选择或新建{subject}对话；本次内容尚未提交。')
        cid = str(conversation_id or active)
        if expected and cid != active:
            raise ConversationChanged('当前会话已在另一端切换，请确认后重新发送；内容尚未提交。')
        session = await db.get_session(cid)
        if session is None or session.get('deleting'):
            raise ConversationChanged('该会话不存在，请刷新会话列表。')
        if session.get('archived') and not allow_archived:
            raise ConversationChanged('该会话已归档，请先恢复。')
        origin, purpose = identity()
        deliver = (purpose == 'chat' and cid == state.get('telegram_conversation_id')) if origin in {'web','cli'} else True
        return ConversationScope(cid, int(session['generation']), name=session.get('name') or '新对话',
                                 origin=origin, purpose=purpose, telegram_delivery=deliver if origin != 'legacy' else None,
                                 workspace_path=session.get('workspace_path'))

    @contextlib.asynccontextmanager
    async def operation(self, conversation_id=None, *, execution=False, wait=False,
                        expected=False, allow_archived=False, fresh=False, selector=None):
        inherited = current_scope(required=False)
        if isinstance(conversation_id, ConversationScope):
            scope = conversation_id
        elif inherited is not None and not fresh:
            if conversation_id is not None:
                require_conversation_id(conversation_id)
            scope = inherited
        else:
            scope = await self.resolve(conversation_id, expected=expected, allow_archived=allow_archived, selector=selector)
        acquired = False
        run = None
        slot = None
        if execution and not (scope.run_id and self.runtime(scope.run_id)):
            await self.state()
            slot = self.execution_lock(scope.conversation_id)
            admission_deadline = time.monotonic() + 1.0
            while True:
                await self._admission_lock.acquire()
                guarded = False
                try:
                    guarded = self.lock.acquire()
                    if guarded:
                        if slot.acquire():
                            acquired = True
                            break
                finally:
                    if not acquired:
                        if guarded:
                            self.lock.release()
                        self._admission_lock.release()
                if guarded and not wait:
                    raise ConversationBusy('当前对话正在执行，请等待完成或停止该回合后重试。')
                if not guarded and not wait and time.monotonic() >= admission_deadline:
                    raise ConversationBusy('正在维护会话文件，请稍后重试。')
                await asyncio.sleep(0.1 if guarded else 0.01)
            try:
                # Check again after acquisition, before any message is persisted.
                fresh_scope = await self.resolve(scope.conversation_id, expected=expected,
                                                 allow_archived=allow_archived, selector=selector)
                if fresh_scope.generation != scope.generation:
                    raise ConversationChanged('对话已清空或压缩，旧操作未提交。')
                scope = dataclasses.replace(scope, run_id=uuid.uuid4().hex)
                run = ConversationRun(scope, slot)
                self._runs[scope.run_id] = run
                if scope.origin == 'web' and scope.purpose == 'chat':
                    from .reply_delivery import WebRunDelivery
                    run.delivery = WebRunDelivery(scope)
                db = await self.factory()
                await db.begin_conversation_run({
                    'run_id': scope.run_id, 'conversation_id': scope.conversation_id,
                    'name': scope.name, 'pid': os.getpid(), 'started_at': time.time(),
                    'stop_requested': False,
                    'queue_instance': self.text_queue.instance if self.text_queue else None,
                })
            except BaseException:
                if run and run.delivery:
                    run.delivery.close()
                self._runs.pop(scope.run_id, None)
                slot.release()
                raise
            finally:
                # Admission and file restoration share this brief global guard;
                # model and command execution only hold this conversation's lock.
                self.lock.release()
                self._admission_lock.release()
        final_scope = None
        turn_id = None
        turn_guard = contextlib.nullcontext(None)
        try:
            if acquired:
                await self.poll_once()
            if acquired and self.code_root is not None and self.object_root is not None:
                from .conversation_branching import begin_turn, bind_turn
                db = await self.factory()
                turn_id = await begin_turn(db, scope)
                turn_guard = bind_turn(turn_id)
            with bind_conversation(scope), turn_guard:
                try:
                    yield scope
                except BaseException as exc:
                    if acquired:
                        run.outcome = 'stopped' if isinstance(exc, asyncio.CancelledError) else 'failed'
                    if not hasattr(exc, 'conversation_context'):
                        exc.conversation_context = dataclasses.asdict(current_scope())
                    raise
                finally:
                    final_scope = current_scope(required=False)
        except BaseException as exc:
            # Failures before ``yield`` (for example admission/snapshot setup)
            # used to leave the durable message pending, so every poll retried
            # forever and the UI remained on “排队中”.  Give them the same
            # terminal queue semantics as failures during model execution.
            if acquired:
                run.outcome = 'stopped' if isinstance(exc, asyncio.CancelledError) else 'failed'
            if not hasattr(exc, 'conversation_context'):
                exc.conversation_context = dataclasses.asdict(scope)
            raise
        finally:
            if inherited and not inherited.run_id and final_scope and inherited.conversation_id == final_scope.conversation_id and inherited.generation != final_scope.generation:
                _scope.set(dataclasses.replace(inherited, generation=final_scope.generation))
            if acquired:
                if run.delivery is not None:
                    run.delivery.close()
                try:
                    db = await self.factory()
                    outcome = 'stopped' if run.stop.is_set() else run.outcome
                    if turn_id is not None:
                        from .conversation_branching import finish_turn
                        await finish_turn(db, scope, turn_id, status=outcome)
                    await db.queue_finish_run(scope.run_id, outcome)
                    await db.finish_conversation_run(scope.run_id)
                finally:
                    self._runs.pop(scope.run_id, None)
                    slot.release()
                    completions, run.completions = run.completions, []
                    for callback in completions:
                        with contextlib.suppress(Exception):
                            callback()
                await self.poll_once()

    def stop_event(self):
        run = self.runtime()
        scope = current_scope(required=False)
        if run is None and scope is not None and scope.run_id is None:
            run = next((active for active in self._runs.values()
                        if active.scope.conversation_id == scope.conversation_id
                        and active.scope.generation == scope.generation), None)
        return run.stop if run else None

    @contextlib.asynccontextmanager
    async def file_maintenance_slot(self, conversation_id=None):
        """Brief file restore exclusion, not a model run or a queue transition."""
        await self.state()
        slot = self.execution_lock(conversation_id) if conversation_id is not None else None
        async with self._admission_lock:
            if not self.lock.acquire():
                raise ConversationBusy('正在执行任务，请等待完成后再恢复附件；未停止任务或修改文件。')
            if slot is not None:
                try:
                    if not slot.acquire():
                        raise ConversationBusy('当前对话正在执行，请等待结束后再撤回。')
                finally:
                    self.lock.release()
        try:
            if slot is None and (await (await self.factory()).get_conversation_state()).get('runs'):
                raise ConversationBusy('正在执行任务，请等待完成后再恢复附件；未停止任务或修改文件。')
            yield
        finally:
            (slot or self.lock).release()

    def attach_stop_event(self, event):
        run = self.runtime()
        if run is not None:
            if run.stop.is_set():
                event.set()
            run.stop = event

    async def request_stop(self, run_id, conversation_id=None):
        if not run_id:
            raise ConversationChanged('停止按钮已失效，请使用当前回合的停止按钮。')
        db = await self.factory()
        accepted = await db.request_conversation_stop(str(run_id), conversation_id)
        run = self.runtime(run_id)
        if accepted and run is not None:
            run.stop.set()
        return accepted

    async def manage(self, action, conversation_id=None, name=None, *, selector=None):
        db = await self.factory()
        from xgent_app.interaction import selection_key
        key = selection_key(selector)
        if key == 'terminal_conversation_id':
            await self.enable_terminal()
            cid = conversation_id or self._terminal_id
            if action == 'switch':
                await self.resolve(cid, selector='terminal')
                self._terminal_id = cid
                self._terminal_revision += 1
                result = {'ok': True, 'conversation_id': cid}
            else:
                result = await db.manage_conversation(action, cid, name, select=False)
                if action == 'create':
                    self._terminal_id = result['conversation_id']
                    self._terminal_revision += 1
            result = {**result, **await self.state()}
        else:
            result = await db.manage_conversation(action, conversation_id, name,
                selector='telegram' if key == 'telegram_conversation_id' else 'shared')
        await self.poll_once()
        return result

    async def poll_once(self):
        if self.maintenance is not None:
            await self.maintenance()
        state = await self.state()
        for run in list(self._runs.values()):
            running = state.get('runs', {}).get(run.scope.conversation_id) or {}
            delivery = run.delivery
            if delivery is not None and delivery.active and not delivery.subscribed and (
                    running.get('run_id') == delivery.scope.run_id and
                    state.get('telegram_conversation_id') == delivery.scope.conversation_id):
                db = await self.factory()
                session = await db.get_session(delivery.scope.conversation_id)
                if session and not session.get('deleting') and session['generation'] == delivery.scope.generation:
                    delivery.subscribe()
                else:
                    delivery.close()
            if running.get('run_id') == run.scope.run_id and running.get('stop_requested'):
                run.stop.set()
        encoded = json.dumps(state, sort_keys=True, ensure_ascii=False)
        if encoded != self._last_state:
            self._last_state = encoded
            if self.notify is not None:
                result = self.notify(state)
                if hasattr(result, '__await__'):
                    await result
        if self.text_queue is not None:
            await self.text_queue.poll()
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
        if self.text_queue is not None:
            self.text_queue._closed = True
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
        if self.text_queue is not None:
            await self.text_queue.close()


def configure_conversations(factory, database_path, notify=None, code_root=None):
    global _manager
    _manager = ConversationManager(factory, database_path, notify, code_root=code_root)
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
            if data and not str(data).startswith(('cv:', 'act_stop_generation', 'q:')):
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
    from xgent_app.telegram_presentation import should_label, presentation_mode
    if scope is None or not should_label(scope):
        return text
    label = f'🗂 {scope.name or scope.conversation_id[:8]} · {scope.conversation_id[:6]}'
    mode = str(parse_mode or '').lower()
    if mode == 'html':
        import html
        label = html.escape(label)
    elif mode.startswith('markdown'):
        from telegram.helpers import escape_markdown
        label = escape_markdown(label, version=2 if 'v2' in mode else 1)
    body = str(text or '')
    if body == label or body.startswith((label + '\n', label + ' · ')) or body.endswith('\n' + label):
        return text
    prefix = label + (' · ' if presentation_mode() == 'inline' else '\n')
    result = (body.rstrip() + '\n' + label if presentation_mode() == 'footer'
              else prefix + body if body else label)
    measured = result
    if mode == 'html':
        from xgent_app.protocols import ProtocolParser
        import re
        measured = html.unescape(re.sub(r'<[^>]*>', '', ProtocolParser.to_telegram_html(measured)))
    if len(measured.encode('utf-16-le', errors='surrogatepass')) // 2 > limit:
        return text
    return result


def defer_completion(frame, callback):
    """Publish completion only after releasing the OS slot (including CLI relay)."""
    kind = frame.get('type')
    completion = kind in {'turn_end', 'turn_error', 'generation_end'} or (
        kind == 'compression_state' and frame.get('busy') is False)
    run = _manager.runtime(frame.get('run_id')) if _manager is not None and frame.get('run_id') else None
    if completion and run is not None:
        run.completions.append(callback)
        return True
    return False


async def conversation_still_exists(snapshot):
    if _manager is None or not snapshot or not snapshot.get('conversation_id'):
        return True
    db = await _manager.factory()
    session = await db.get_session(snapshot['conversation_id'])
    return session is not None and not session.get('deleting')
