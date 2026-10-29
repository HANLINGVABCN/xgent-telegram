"""Optional Web presentation, independent of Telegram's reduced HTML dialect.

Only display frames/JSON metadata carry this information. Model text, Telegram
payloads, CLI output and the protocol execution path are never rewritten here.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .protocols import ProtocolParser
from .presentation import LABELS, ai_segments, current_turn, current_message_presentation, normalize_mode, footer_html, usage_details

_current = ContextVar('web_display', default=None)


def build_web_display(text: str, *, fold: bool = True, hide_unclosed: bool = False,
                      reply_message_id=None, mode=None, presentation=None) -> dict:
    turn = current_turn()
    mode = normalize_mode(mode or (turn.mode if turn is not None else 'detailed'))
    if fold:
        segments = ai_segments(text, mode, hide_unclosed=hide_unclosed)
    else:
        segments = [{'kind': 'markdown', 'text': text}]
    if presentation and mode == 'folded':
        footer = footer_html(presentation)
        if footer:
            segments.append({'kind': 'html', 'text': footer, 'footer': True})
    result = {'version': 1, 'segments': segments, 'presentation_mode': mode}
    if presentation and presentation.get('usage'):
        result['usage_details'] = usage_details(presentation)
    if presentation and presentation.get('summary'):
        result['task_details'] = '\n\n'.join(
            f"{item.get('sequence', '?')}. {LABELS.get(item.get('kind'), ('', item.get('kind', '操作')))[1]}\n"
            f"{item.get('command', '')}\n状态：{'失败' if item.get('success') is False else '已执行'}"
            for item in presentation['summary'].get('items', []))
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
    result = frame
    if display is not None and frame.get('type') in {'message', 'edit', 'delete', 'edit_markup'}:
        result = {**frame, 'web_display': display, 'msg_type': frame.get('msg_type') or 'ai_reply'}
    hint = current_message_presentation()
    if hint and frame.get('type') in {'message', 'edit', 'edit_markup'}:
        result = {**result, 'msg_type': hint['kind'], 'presentation_mode': hint.get('mode')}
        if hint.get('record_id') is not None:
            result['record_id'] = hint['record_id']
    return result


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
            mode = ns['_presentation_mode']() if ns and '_presentation_mode' in ns else 'folded'
            display = build_web_display(response, fold=fold, reply_message_id=ids[0], mode=mode)
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
                                        reply_message_id=renderer.message_ids[0],
                                        mode=getattr(renderer, '_presentation_mode', 'detailed'))
            with web_display_scope(display):
                return await function(renderer, *args, **kwargs)
        return wrapped
    return decorate
