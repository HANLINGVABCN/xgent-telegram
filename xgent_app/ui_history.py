"""Durable menu presentation, separate from model-visible conversation records."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import json
import secrets
import weakref
from dataclasses import dataclass
from typing import Any


PROCESS_ID = secrets.token_hex(16)
EXPIRED = '\u83dc\u5355\u5df2\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u6253\u5f00\u3002'
CHANGED = '\u83dc\u5355\u5df2\u66f4\u65b0\uff0c\u8bf7\u5728\u66f4\u65b0\u540e\u7684\u83dc\u5355\u91cd\u65b0\u70b9\u51fb\u3002'
SAVE_FAILED = '\u83dc\u5355\u72b6\u6001\u4fdd\u5b58\u5931\u8d25\uff0c\u5df2\u5b8c\u6210\u7684\u8bbe\u7f6e\u4e0d\u4f1a\u56de\u6eda\u3002'
_TRANSIENT_ACTIONS = {'act_stop_generation', 'act_finish_text_stitch', 'act_cancel_text_stitch'}
_WORKFLOW_PREFIXES = ('act_confirm', 'act_save', 'act_search_saved', 'act_search_fetched',
                      'act_saved_', 'pick_default_', 'pick_fetch_', 'fetch_market_',
                      'page_', 'back_saved_', 'back_fetched_')
_factory = None
_resolve = lambda value: value
_guard = lambda: ''
_scope = contextvars.ContextVar('ui_history_scope', default=None)
_locks = weakref.WeakValueDictionary()


class UiHistoryError(ValueError):
    """A menu must not be replayed, silently dropped, or revived after clearing."""


class UiHistorySnapshot(list):
    def __init__(self, rows=(), *, generation=None, tombstones=()):
        super().__init__(rows)
        self.generation = generation
        self.tombstones = list(tombstones)


@dataclass
class UiScope:
    generation: int
    capture_text: bool = False
    ui_message_id: str | None = None
    message_id: int | None = None
    revision: int | None = None
    guard: str | None = None


def configure_ui_history(factory, resolve, guard) -> None:
    global _factory, _resolve, _guard
    _factory, _resolve, _guard = factory, resolve, guard


def resolve_callback(value: str) -> str:
    return str(_resolve(value))


def callback_lock(ui_message_id: str) -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), ui_message_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


@contextlib.asynccontextmanager
async def ui_operation(*, capture_text=False, binding=None, message_id=None):
    current = _scope.get()
    if _factory is None or current is False or (current is not None and binding is None):
        yield current
        return
    db = await _factory()
    scope = UiScope(await db.get_attachment_generation(), capture_text)
    if binding is not None:
        scope.ui_message_id = binding['ui_message_id']
        scope.generation = binding['generation']
        scope.revision = binding['revision']
        scope.message_id = message_id
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


async def advance_ui_generation() -> None:
    scope = _scope.get()
    if isinstance(scope, UiScope) and _factory is not None:
        db = await _factory()
        # Child tasks keep the old scope and must not inherit this explicit clear.
        _scope.set(UiScope(await db.get_attachment_generation(), scope.capture_text))


def active_ui_generation() -> int | None:
    scope = _scope.get()
    return scope.generation if isinstance(scope, UiScope) else None


def without_ui_history(function):
    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        token = _scope.set(False)
        try:
            return await function(*args, **kwargs)
        finally:
            _scope.reset(token)
    return wrapped


def relay_ui_context() -> dict | None:
    scope = _scope.get()
    if scope is False:
        return {'disabled': True}
    if isinstance(scope, UiScope):
        return {'generation': scope.generation, 'capture_text': scope.capture_text,
                'guard': str(_guard() or '')}
    return None


@contextlib.contextmanager
def replay_ui_context(snapshot):
    if not isinstance(snapshot, dict):
        yield
        return
    scope = False if snapshot.get('disabled') else UiScope(
        int(snapshot['generation']), bool(snapshot.get('capture_text')), guard=snapshot.get('guard', ''),
    )
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


def normalize_markup(rows: Any) -> list:
    if not isinstance(rows, (list, tuple)):
        return []
    result = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue
        buttons = []
        for button in row:
            if not isinstance(button, dict):
                continue
            item = {'text': str(button.get('text') or '')}
            if button.get('url'):
                item['url'] = str(button['url'])
                if button.get('web_app'):
                    item['web_app'] = True
            elif button.get('callback_data'):
                action = str(button.get('callback_action') or resolve_callback(str(button['callback_data'])))
                if action.startswith('cb_'):
                    item['unavailable'] = True
                else:
                    item['callback_data'] = action
            else:
                continue
            item['button_id'] = f'{len(result)}:{len(buttons)}'
            buttons.append(item)
        if buttons:
            result.append(buttons)
    return result


def durable_markup(rows: list) -> bool:
    return any(button.get('url') or button.get('unavailable') or (
        button.get('callback_data') and button['callback_data'] not in _TRANSIENT_ACTIONS
        and not button['callback_data'].startswith('retry_compress:')
    ) for row in rows for button in row)


def ui_record(row: dict) -> dict:
    payload = json.loads(row['payload']) if isinstance(row.get('payload'), str) else row['payload']
    return {
        'id': None, 'ui_message_id': row['ui_message_id'], 'revision': row['revision'],
        'ui_generation': row['generation'], 'timestamp': row['timestamp'],
        'msg_type': 'ui_message', 'role': 'assistant', 'content': payload.get('content', ''),
        'parse_mode': payload.get('parse_mode'), 'reply_markup': payload.get('reply_markup', []),
        'deleted': bool(payload.get('deleted')), 'media': [],
    }


async def capture_ui_frame(frame: dict, chat_id: int, *, source=None) -> dict:
    scope = _scope.get()
    if _factory is None or scope is False or frame.get('type') not in {'message', 'edit', 'edit_markup', 'delete'}:
        return frame
    markup = normalize_markup(frame.get('reply_markup'))
    create = durable_markup(markup) or (isinstance(scope, UiScope) and scope.capture_text
                                      and frame.get('reply_markup') is None)
    if frame['type'] == 'message' and not create:
        return frame
    bind = scope if (isinstance(scope, UiScope) and scope.ui_message_id
                     and frame.get('message_id') == scope.message_id and frame['type'] != 'message') else None
    try:
        db = await _factory()
        generation = scope.generation if isinstance(scope, UiScope) else await db.get_attachment_generation()
        row = await db.apply_ui_frame(
            source or ('web:' + PROCESS_ID), int(chat_id), frame, markup,
            generation=generation, create=create,
            guard=scope.guard if isinstance(scope, UiScope) and scope.guard is not None else str(_guard() or ''),
            ui_message_id=bind.ui_message_id if bind else None,
            expected_revision=bind.revision if bind else None,
        )
    except UiHistoryError:
        raise
    except Exception as exc:
        raise UiHistoryError(SAVE_FAILED) from exc
    if row is None:
        return frame
    if bind:
        bind.revision = row['revision']
    display = ui_record(row)
    return {
        **frame, 'ui_message_id': display['ui_message_id'], 'revision': display['revision'],
        'ui_generation': display['ui_generation'], 'timestamp': display['timestamp'],
        'text': display['content'], 'parse_mode': display['parse_mode'],
        'reply_markup': display['reply_markup'], 'msg_type': 'ui_message',
    }


async def validated_button(db, ui_message_id: str, revision: int, button_id: str) -> tuple[dict, str]:
    row = await db.get_ui_message(ui_message_id)
    if row is None or row['generation'] != await db.get_attachment_generation():
        raise UiHistoryError(EXPIRED)
    payload = json.loads(row['payload'])
    if payload.get('deleted'):
        raise UiHistoryError(EXPIRED)
    if row['revision'] != revision:
        raise UiHistoryError(CHANGED)
    for buttons in payload.get('reply_markup', []):
        for button in buttons:
            if button.get('button_id') == button_id and button.get('callback_data'):
                action = button['callback_data']
                if (action.startswith(_WORKFLOW_PREFIXES)
                        and (not payload.get('guard') or payload['guard'] != str(_guard() or ''))):
                    raise UiHistoryError(EXPIRED)
                return row, action
    raise UiHistoryError(EXPIRED)


def hide_audit_record(record: dict) -> bool:
    if record.get('msg_type') == 'button_click':
        return True
    if record.get('msg_type') != 'system_op':
        return False
    metadata = record.get('metadata') or {}
    try:
        metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
    except (TypeError, ValueError):
        return False
    if not isinstance(metadata, dict):
        return False
    if metadata.get('display_media') or metadata.get('compression_task'):
        return False
    if metadata.get('ui_audit') is True:
        return True
    text = str(record.get('content') or '')
    if metadata.get('command') == '/start' and text == '\u542f\u52a8\u673a\u5668\u4eba':
        return True
    if metadata.get('key') and text == '[Web] \u4fee\u6539\u914d\u7f6e ' + str(metadata['key']):
        return True
    if text.startswith((
        '\u8bbe\u7f6e AI \u56de\u590d\u8d85\u65f6: ', '\u8bbe\u7f6e\u547d\u4ee4\u7b49\u5f85\u7a97\u53e3: ',
        '\u8bbe\u7f6e Agent \u6700\u5927\u8f6e\u6570: ', '\u8bbe\u7f6e\u7a7a\u95f2\u63d0\u9192\u95f4\u9694: ',
        '\u8bbe\u7f6e\u8bb0\u5fc6\u6df1\u5ea6: ', '\u8bbe\u7f6e\u667a\u80fd\u5339\u914d\u9608\u503c: ',
        '\u8bbe\u7f6e Web \u7aef\u53e3: ', '\u8bbe\u7f6e\u6a21\u578b\u4ef7\u683c: ',
    )):
        return True
    return (any(key in metadata for key in ('skill_state', 'disabled_skills', 'hidden_skills'))
            and text.startswith(('Skill \u8bbe\u7f6e\u5df2\u66f4\u65b0:', 'Skill \u72b6\u6001\u5df2\u66f4\u65b0:')))
