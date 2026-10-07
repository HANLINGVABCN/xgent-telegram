"""Track display fragments of one reply without changing Telegram delivery."""
from __future__ import annotations

import contextvars
import functools

_current = contextvars.ContextVar('reply_presentation', default=None)


def track_reply_presentation(function):
    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        state = {'active': True, 'ids': None}
        token = _current.set(state)
        try:
            return await function(*args, **kwargs)
        finally:
            state['active'] = False
            _current.reset(token)
    return wrapped


def bind_reply_message_ids(ids):
    state = _current.get()
    if state and state['active']:
        state['ids'] = ids


def remember_reply_messages(messages):
    state = _current.get()
    if not state or not state['active'] or state['ids'] is None:
        return
    for message in messages:
        mid = getattr(message, 'message_id', None)
        if type(mid) is int and mid not in state['ids']:
            state['ids'].append(mid)
