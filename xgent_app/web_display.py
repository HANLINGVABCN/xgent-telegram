"""Optional Web presentation, independent of Telegram's reduced HTML dialect.

Only display frames/JSON metadata carry this information. Model text, Telegram
payloads, CLI output and the protocol execution path are never rewritten here.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import html

from .protocols import ProtocolParser
from .presentation import LABELS, current_turn, current_message_presentation, normalize_mode, footer_html, usage_details

_current = ContextVar('web_display', default=None)

PROTOCOL_NAMES = {kind: kind + '-x' for kind in LABELS}
PROTOCOL_NAMES['file_base64'] = 'file-x'


def operation_state(operation):
    if operation.get('status') in {'stopped', 'cancelled', 'interrupted'} or operation.get('state') == 'stopped':
        return 'stopped'
    if operation.get('running'):
        return 'running'
    if operation.get('success') is False:
        return 'failed'
    if operation.get('success') is True:
        return 'success'
    return operation.get('state') or 'unknown'


def web_block_html(body, *, protocol=None, state='unknown', raw=None, path=None, output_path=None, details=None):
    attrs = {'data-web-fold': '1', 'data-state': state}
    if protocol:
        attrs['data-protocol'] = protocol
    if path:
        attrs['data-preview'] = path
    if raw:
        attrs['data-raw'] = raw
    if output_path:
        attrs['data-output-path'] = output_path
    if details:
        attrs['data-operation-details'] = details
    encoded = ''.join(' ' + key + '="' + html.escape(str(value), quote=True).replace('\n', '&#10;') + '"'
                      for key, value in attrs.items())
    label = protocol or '输出'
    return '<blockquote expandable' + encoded + '><b>' + label + '</b><pre>' + html.escape(str(body), quote=False) + '</pre></blockquote>'


def web_ai_segments(text, *, hide_unclosed=False, presentation=None):
    operations = list((presentation or {}).get('operations') or [])
    segments = []
    for part in ProtocolParser.scan_folded_segments(text, hide_unclosed=hide_unclosed):
        if part['kind'] == 'prose':
            segments.append({'kind': 'markdown', 'text': part['text']})
            continue
        if part.get('type') == 'over':
            continue
        matched = next((op for op in operations if op.get('kind') == part.get('type')
                        and op.get('command') == part.get('body') and (op.get('path') or '') == (part.get('path') or '')), None)
        if matched:
            operations.remove(matched)
        state = operation_state(matched) if matched else 'generating' if part.get('generating') else (presentation or {}).get('default_protocol_state', 'pending')
        segments.append({'kind': 'html', 'text': web_block_html(
            part.get('body') or '', protocol=PROTOCOL_NAMES.get(part['type'], part['type'] + '-x'),
            state=state, raw=part.get('raw'), path=part.get('path'))})
    return segments


def build_operation_display(operation, mode, presentation=None):
    mode = normalize_mode(mode)
    details = '\n'.join(f'{label}：{operation[key]}' for key, label in (
        ('return_code', '返回码'), ('elapsed_seconds', '耗时（秒）'),
        ('session_id', '终端会话'), ('output_path', '输出存档')) if operation.get(key) is not None)
    body = operation.get('output') or operation.get('notice') or ('等待命令返回…' if operation_state(operation) == 'running' else '(无输出)')
    segments = [{'kind': 'html', 'text': web_block_html(body, state=operation_state(operation),
                 output_path=operation.get('output_path'), details=details)}]
    footer = footer_html(presentation or {})
    if footer:
        segments.append({'kind': 'html', 'text': footer, 'footer': True})
    return {'version': 1, 'segments': segments, 'presentation_mode': mode, 'operation_details': details}


def build_web_display(text: str, *, fold: bool = True, hide_unclosed: bool = False,
                      reply_message_id=None, mode=None, presentation=None) -> dict:
    turn = current_turn()
    mode = normalize_mode(mode or (turn.mode if turn is not None else 'folded'))
    if fold:
        segments = web_ai_segments(text, hide_unclosed=hide_unclosed, presentation=presentation)
    else:
        segments = [{'kind': 'markdown', 'text': text}]
    if presentation:
        footer = footer_html(presentation)
        if footer:
            segments.append({'kind': 'html', 'text': footer, 'footer': True})
    result = {'version': 1, 'segments': segments, 'presentation_mode': mode}
    if presentation and presentation.get('usage'):
        result['usage_details'] = usage_details(presentation)
    if presentation and presentation.get('summary'):
        result['task_details'] = '\n\n'.join(
            f"{item.get('sequence', '?')}. {LABELS.get(item.get('kind'), ('', item.get('kind', '操作')))[1]}\n"
            f"{item.get('command', '')}\n状态：{ {'success': '已运行', 'failed': '失败', 'stopped': '已停止', 'running': '运行中', 'pending': '待执行'}.get(operation_state(item), '未确认') }"
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
        result = {**result, 'msg_type': hint['kind'], 'presentation_mode': normalize_mode(hint.get('mode'))}
        if hint.get('record_id') is not None:
            result['record_id'] = hint['record_id']
            if hint['kind'] == 'agent_result' and frame.get('message_id') is not None:
                result['replace_message_ids'] = [frame['message_id']]
        if hint['kind'] == 'token_usage':
            result['hidden'] = True
        turn = current_turn()
        if hint.get('ask_id'):
            from .agent_ask import PENDING_ASKS
            pending = PENDING_ASKS.get(hint['ask_id'])
            if pending:
                result['ask_form'] = pending.web_snapshot()
        if hint['kind'] == 'agent_result' and turn and turn.active_operation and turn.active_operation['kind'] != 'ask':
            result['web_display'] = build_operation_display(turn.active_operation, turn.mode)
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
            turn = current_turn()
            mode = ns['_presentation_mode']() if ns and '_presentation_mode' in ns else (turn.mode if turn is not None else 'folded')
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
                                        mode=getattr(renderer, '_presentation_mode', 'folded'))
            with web_display_scope(display):
                return await function(renderer, *args, **kwargs)
        return wrapped
    return decorate
