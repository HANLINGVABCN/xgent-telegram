"""Telegram-only presentation; receiver selection is read at actual delivery."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_policy = ContextVar('telegram_presentation', default='auto')
_UNKNOWN = object()
_receiver = ContextVar('telegram_receiver', default=_UNKNOWN)


def presentation_mode():
    return _policy.get()


def label_enabled():
    return presentation_mode() != 'self'


def should_label(scope):
    if not label_enabled():
        return False
    return presentation_mode() in {'menu', 'footer'} or _receiver.get() is _UNKNOWN or _receiver.get() != scope.conversation_id


def presentation_snapshot():
    return {'mode': presentation_mode()}


@contextmanager
def telegram_label(enabled=True):
    mode = ('auto' if enabled else 'inline') if isinstance(enabled, bool) else str(enabled)
    if mode not in {'auto', 'inline', 'menu', 'footer', 'self'}:
        mode = 'auto'
    token = _policy.set(mode)
    try:
        yield
    finally:
        _policy.reset(token)


@contextmanager
def replay_presentation(snapshot):
    snapshot = snapshot or {}
    mode = snapshot.get('mode', 'auto' if snapshot.get('label', True) else 'inline')
    with telegram_label(mode):
        yield


async def render_telegram_text(text, parse_mode=None, *, limit=4096):
    from xgent_app import conversations
    scope = conversations.current_scope(required=False)
    selected = _UNKNOWN
    if scope is not None and conversations._manager is not None:
        db = await conversations.get_conversations().factory()
        state = await db.get_conversation_state()
        selected = state.get('telegram_conversation_id')
    token = _receiver.set(selected)
    try:
        return conversations.conversation_labelled_text(text, parse_mode, limit=limit)
    finally:
        _receiver.reset(token)


async def render_telegram_caption(text, parse_mode=None):
    """Keep a full caption as adjacent text if its mandatory source cannot fit."""
    full = await render_telegram_text(text, parse_mode, limit=2**31)
    bounded = await render_telegram_text(text, parse_mode, limit=1024)
    if full == bounded:
        return bounded, None
    # Never truncate the user's description, or send a foreign file without a source.
    return await render_telegram_text(None, parse_mode, limit=1024), text


def conversation_reply(function):
    """Model/compression output is a reply even when started from a menu button."""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        with telegram_label('auto'):
            return await function(*args, **kwargs)
    return wrapped


def without_conversation_label(function):
    """Lightweight status/usage: inline source only when receiver differs."""
    @wraps(function)
    async def wrapped(*args, **kwargs):
        with telegram_label('inline'):
            return await function(*args, **kwargs)
    return wrapped


def stream_part(function):
    # Every foreign fragment needs a source, not just the first fragment.
    return function
