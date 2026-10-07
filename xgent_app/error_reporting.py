"""Permanent, channel-independent error summaries. Never retain hidden full errors."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import re

MAX_ERROR_LINES = 50
_ERROR_CONTEXT = ContextVar('xgent_error_context', default=None)
_OMITTED = re.compile(r'^已折叠 (\d+) 行报错（永久省略）$')
_WRAPPER = '完整上下文请求失败，未获得有效回复，也没有自动减少附件：'


def error_text(value) -> str:
    """Keep 50 body lines + an explicit omission count, idempotently.

    Called after secret redaction. The omitted tail is not kept in metadata,
    HTML attributes or model-visible history, regardless of folding settings.
    """
    text = str(value or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    text = re.sub(r'^AttachmentContextError\s*:\s*', '', text)
    if text.startswith(_WRAPPER):
        text = text[len(_WRAPPER):].lstrip()
    lines = text.split('\n') if text else ['未知错误']
    omitted = 0
    match = _OMITTED.fullmatch(lines[-1])
    if match:
        omitted = int(match[1]); lines.pop()
    omitted += max(0, len(lines) - MAX_ERROR_LINES)
    result = '\n'.join(lines[:MAX_ERROR_LINES])
    if omitted:
        result += f'\n已折叠 {omitted} 行报错（永久省略）'
    return result


def error_context():
    return _ERROR_CONTEXT.get()


@contextmanager
def runtime_error_scope(chat_id, generation):
    token = _ERROR_CONTEXT.set((chat_id, generation))
    try:
        yield
    finally:
        _ERROR_CONTEXT.reset(token)
