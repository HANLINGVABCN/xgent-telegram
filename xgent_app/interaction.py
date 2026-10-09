"""Entry-owned interaction identity; model/tools inherit it without new arguments."""
from contextlib import contextmanager
from contextvars import ContextVar

_entry = ContextVar('xgent_interaction', default=('legacy', 'chat'))


@contextmanager
def interaction(origin, purpose='chat'):
    token = _entry.set((origin, purpose))
    try:
        yield
    finally:
        _entry.reset(token)


def identity():
    origin, purpose = _entry.get()
    if origin == 'legacy':
        from xgent_app.conversations import current_scope
        scope = current_scope(required=False)
        if scope is not None:
            return scope.origin, scope.purpose
    return origin, purpose


def selection_key(selector=None):
    if selector is None:
        selector = 'telegram' if identity()[0] == 'telegram' else 'shared'
    if selector not in {'telegram', 'shared'}:
        raise ValueError('未知会话选择端')
    return 'telegram_conversation_id' if selector == 'telegram' else 'current_chat_id'


def is_temporary_key(key):
    return (key == 'state' or key.startswith(('temp_', 'editing_')) or key.endswith('_buffer')
            or key in {'ask_input_target', 'fetched_cache', 'provider_import_mode',
                       '_pending_price_model', 'pending_update_zip_url'})


def telegram_delivery_allowed():
    from xgent_app.conversations import current_scope
    scope = current_scope(required=False)
    return scope is None or scope.telegram_delivery is not False
