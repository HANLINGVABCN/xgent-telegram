"""Optional Web presentation, independent of Telegram's reduced HTML dialect.

Only display frames/JSON metadata carry this information. Model text, Telegram
payloads, CLI output and the protocol execution path are never rewritten here.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .protocols import ProtocolParser

_current = ContextVar('web_display', default=None)


def build_web_display(text: str, *, fold: bool = True, hide_unclosed: bool = False,
                      reply_message_id=None) -> dict:
    if fold:
        segments = [
            {'kind': 'markdown', 'text': part['text']} if part['kind'] == 'prose' else
            {'kind': 'html', 'text': ProtocolParser._render_block_blockquote(part, raw_copy=True)}
            for part in ProtocolParser.scan_folded_segments(text, hide_unclosed=hide_unclosed)
        ]
    else:
        segments = [{'kind': 'markdown', 'text': text}]
    result = {'version': 1, 'segments': segments}
    # Telegram may split a single reply into many transport messages. Web renders
    # the complete Markdown once, so a split cannot break a list/table/code fence.
    if type(reply_message_id) is int:
        result['reply_message_id'] = reply_message_id
    return result


def current_web_display():
    return _current.get()


@contextmanager
def web_display_scope(display):
    token = _current.set(display)
    try:
        yield
    finally:
        _current.reset(token)


def present_web_frame(frame):
    display = current_web_display()
    if display is not None and frame.get('type') in {'message', 'edit', 'delete', 'edit_markup'}:
        return {**frame, 'web_display': display, 'msg_type': frame.get('msg_type') or 'ai_reply'}
    return frame


def web_text_response(function):
    """Final Markdown senders may split HTML, but keep one semantic Web reply."""
    @wraps(function)
    async def wrapped(context, chat_id, msg, response, *args, **kwargs):
        from .reply_presentation import reply_message_ids
        # Nested finalizers inside a stream must keep the WHOLE response, not just
        # the last Telegram fragment. The caller freezes its folding preference.
        display = current_web_display()
        if display is None:
            from .web_history import _section_ns
            ns = _section_ns()
            fold = ns['_should_hide_protocol_blocks']() if ns else True
            ids = reply_message_ids() or [getattr(msg, 'message_id', None)]
            display = build_web_display(response, fold=fold, reply_message_id=ids[0])
        with web_display_scope(display):
            return await function(context, chat_id, msg, response, *args, **kwargs)
    return wrapped


def web_stream_display(*, final=False, stopped=False):
    """Capture one renderer flush; inherited async edit tasks retain this snapshot."""
    def decorate(function):
        @wraps(function)
        async def wrapped(renderer, *args, **kwargs):
            text = ''.join(renderer.response_parts)
            if stopped:
                text += ('\n\n⏹️ 已停止，保留以上已生成内容。' if text.strip() else
                         '⏹️ 已停止，还没有生成可保留的内容。')
            display = build_web_display(text, fold=renderer._hide_blocks,
                                        hide_unclosed=not final,
                                        reply_message_id=renderer.message_ids[0])
            with web_display_scope(display):
                return await function(renderer, *args, **kwargs)
        return wrapped
    return decorate
