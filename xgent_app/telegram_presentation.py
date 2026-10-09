"""Telegram-only, serializable presentation policy; never alter model/Web text."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_policy = ContextVar('telegram_presentation', default=True)


def label_enabled():
    return _policy.get()


def presentation_snapshot():
    return {'label': label_enabled()}


@contextmanager
def telegram_label(enabled=True):
    token = _policy.set(bool(enabled))
    try:
        yield
    finally:
        _policy.reset(token)


@contextmanager
def replay_presentation(snapshot):
    with telegram_label((snapshot or {}).get('label', True)):
        yield


def without_conversation_label(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        with telegram_label(False):
            return await function(*args, **kwargs)
    return wrapped


def stream_part(function):
    @wraps(function)
    async def wrapped(self, *args, **kwargs):
        with telegram_label(label_enabled() and len(self.message_ids) <= 1):
            return await function(self, *args, **kwargs)
    return wrapped
