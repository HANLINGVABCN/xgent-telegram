"""Structured, display-only conversation presentation shared by all clients.

Raw model text and tool continuation messages are never rewritten here. Live
delivery and history share one compact, expandable presentation.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import html
import json
import re
import time
from typing import Any

from .protocols import ProtocolParser

FOLDED = 'folded'
_turn = ContextVar('conversation_presentation', default=None)
_message = ContextVar('message_presentation', default=None)
LABELS = {
    'run': ('⌨️', '执行命令'), 'read': ('📖', '读取文件'),
    'edit': ('✏️', '修改文件'), 'grep': ('🔎', '查找内容'),
    'intel': ('🧠', '代码分析'), 'search': ('🌐', '搜索'),
    'fetch': ('📄', '读取网页'), 'shell': ('🖥️', '启动终端'),
    'stdin': ('⌨️', '输入终端'), 'shellkill': ('⏹️', '关闭终端'),
    'file': ('📄', '写入文件'), 'file_base64': ('📄', '写入文件'),
    'sendfile': ('📎', '发送文件'), 'media': ('🖼️', '生成媒体'),
    'ask': ('📝', '需要你的输入'),
}
_STATUS_RE = re.compile(r'Agent\s*第\s*\d+\s*轮')


def normalize_mode(value: Any) -> str:
    """Accept retired client/settings values without restoring another view."""
    return FOLDED


def current_turn():
    return _turn.get()


@contextmanager
def presentation_turn(turn):
    token = _turn.set(turn)
    try:
        yield turn
    finally:
        _turn.reset(token)


@contextmanager
def message_presentation(value):
    token = _message.set(value)
    try:
        yield
    finally:
        _message.reset(token)


def current_message_presentation():
    return _message.get()


def metadata_dict(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return dict(result) if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def technical_notice(text):
    return bool(_STATUS_RE.search(str(text))) or str(text).startswith('系统提示：⚠️ 协议块 nonce')


def successful_status(text):
    text = str(text)
    return bool(_STATUS_RE.search(text)) and not any(word in text for word in ('失败', '停止', '超过', '超限'))


def approximate_seconds(value):
    if value is None:
        return '未采集'
    seconds = max(0.0, float(value))
    return f'约 {seconds:.1f} 秒' if seconds < 10 else f'约 {seconds:.0f} 秒'


def _number(value):
    return f'{value:,}' if isinstance(value, int) and not isinstance(value, bool) else '未提供'


def usage_line(reply):
    usage = reply.get('usage') or {}
    if not usage and reply.get('elapsed_seconds') is None:
        return ''
    return (f"↑ {_number(usage.get('input_tokens'))} · "
            f"↓ {_number(usage.get('output_tokens'))} · cache {_number(usage.get('cached_tokens'))} · "
            f"{approximate_seconds(reply['elapsed_seconds']) if reply.get('elapsed_seconds') is not None else '耗时未记录'}")


def usage_details(reply):
    usage = reply.get('usage') or {}
    elapsed = reply.get('elapsed_seconds')
    output = usage.get('output_tokens')
    rate = f'{output / elapsed:.1f} tokens/s' if isinstance(output, int) and elapsed and elapsed > 0 else '未提供'
    rows = [
        f"轮次：R{reply.get('round', '?')}", f"模型：{reply.get('model') or '未提供'}",
        f"输入 Token：{_number(usage.get('input_tokens'))}",
        f"缓存 Token：{_number(usage.get('cached_tokens'))}",
        f"输出 Token：{_number(output)}", f"思考 Token：{_number(usage.get('reasoning_tokens'))}",
        f"可见输出 Token：{_number(usage.get('visible_output_tokens'))}",
        f"总 Token：{_number(usage.get('total_tokens'))}",
        f"平均响应速率：{rate}（输出量 / 本轮响应耗时）",
        '首字延迟：未采集', f"本轮响应耗时：{elapsed:.3f} 秒" if elapsed is not None else '本轮响应耗时：未采集',
    ]
    return '\n'.join(rows)


def summary_line(summary):
    text = f"共执行 {summary['rounds']} 轮，{summary['operations']} 个操作 · 总耗时{approximate_seconds(summary.get('elapsed_seconds'))}"
    if summary.get('reason'):
        text = str(summary['reason']) + '\n' + text
    return text


def footer_html(presentation, *, expanded=False):
    pieces = []
    line = usage_line(presentation)
    if line:
        pieces.append('<i>' + html.escape(line) + '</i>')
    if presentation.get('summary'):
        pieces.append('<i>' + html.escape(summary_line(presentation['summary'])) + '</i>')
    if expanded and line:
        pieces.append('<pre>' + html.escape(usage_details(presentation)) + '</pre>')
    return '\n'.join(pieces)


def protocol_block_html(part, mode):
    if part.get('type') == 'over':
        return ''  # Its prose is delivered once by the existing over executor.
    icon, label = LABELS.get(part.get('type'), ('🔧', '执行操作'))
    if part.get('generating'):
        return f'<blockquote>⚡ {icon} {label} · 生成中…</blockquote>'
    attrs = ' expandable data-compact="1"'
    if part.get('raw'):
        attrs += ' data-raw="' + html.escape(part['raw'], quote=True).replace('\n', '&#10;') + '"'
    return (f'<blockquote{attrs}><b>{icon} {label}</b> · {part.get("line_count", 0)} 行\n'
            f'<pre>{html.escape(part.get("body") or "", quote=False)}</pre></blockquote>')


def ai_segments(raw, mode, *, hide_unclosed=False):
    return [
        {'kind': 'markdown', 'text': part['text']} if part['kind'] == 'prose' else
        {'kind': 'html', 'text': protocol_block_html(part, mode)}
        for part in ProtocolParser.scan_folded_segments(raw, hide_unclosed=hide_unclosed)
        if part['kind'] == 'prose' or not (mode == FOLDED and part.get('type') == 'over')
    ]


def ai_html(raw, mode, prose_renderer, *, hide_unclosed=False):
    return '\n'.join(prose_renderer(part['text']) if part['kind'] == 'markdown' else part['text']
                     for part in ai_segments(raw, mode, hide_unclosed=hide_unclosed))


def operation_html(operation, *, detailed=False):
    kind = operation.get('kind') or 'run'
    icon, label = LABELS.get(kind, ('🔧', '操作'))
    if kind == 'run':
        icon, label = '📥', '命令输出'
    elif kind != 'ask':
        label += '结果'
    body = str(operation.get('output') or operation.get('notice') or '(无输出)')
    prefix = ''
    if operation.get('success') is False:
        prefix = '⚠️ 操作失败'
        if operation.get('return_code') is not None:
            prefix += f" · 返回码 {operation['return_code']}"
        reason = body.splitlines()[0].strip() if body.splitlines() else ''
        if reason and not reason.startswith('<'):
            prefix += ' · ' + html.escape(reason[:100])
        prefix += '\n'
    elif operation.get('running'):
        prefix = '🖥️ 终端仍在运行，等待后续输入或关闭。\n'
    attrs = ' expandable data-compact="1"'
    if operation.get('output_path'):
        attrs += ' data-output-path="' + html.escape(str(operation['output_path']), quote=True) + '"'
    return (prefix + f'<blockquote{attrs}><b>{icon} {label}</b> · {body.count(chr(10)) + 1} 行\n'
            '<pre>' + html.escape(body, quote=False) + '</pre></blockquote>')


def legacy_operation(content, metadata=None):
    """Recover only explicit legacy tool facts; never infer missing original output."""
    metadata = metadata_dict(metadata)
    source = (metadata.get('display') or {}).get('content') or str(content or '')
    match = re.search(r'<b>\s*Agent\s+(Run|Edit|Grep|Search|Fetch|Shell)\b', source, re.I)
    body = re.search(r'<pre>(.*?)</pre>', source, re.S)
    if match is None or body is None:
        return None
    result = {'kind': match[1].lower(), 'output': html.unescape(body[1]), 'detailed_html': source}
    code = re.search(r'返回码:\s*<code>(-?\d+)</code>', source)
    if code:
        result.update(return_code=int(code[1]), success=int(code[1]) == 0)
    path = re.search(r'data-output-path="([^"]+)"', source)
    if path is None:
        path = re.search(r'(?:完整输出|输出存档（已截断）):\s*<code>([^<]+)</code>', source)
    if path:
        result['output_path'] = html.unescape(path[1])
    return result


@dataclass
class PresentationTurn:
    mode: str
    started_at: float = field(default_factory=time.monotonic)
    replies: list = field(default_factory=list)
    operations: list = field(default_factory=list)
    active_reply: dict | None = None
    active_operation: dict | None = None
    reason: str = ''
    last_message: Any = None
    last_record_id: int | None = None
    operation_messages: dict = field(default_factory=dict)

    def __post_init__(self):
        self.mode = FOLDED

    @property
    def folded(self):
        return self.mode == FOLDED

    def start_reply(self, reply, ids):
        self.active_operation = None
        self.active_reply = {'version': 1, 'mode': self.mode, 'kind': 'ai_reply',
            'round': len(self.replies) + 1, 'model': getattr(reply, 'presentation_model', ''),
            'message_ids': ids, 'record_id': None, 'execution_schema': 1}

    def saved_reply(self, record_id):
        if self.active_reply is not None and self.active_reply.get('record_id') is None and record_id is not None:
            self.active_reply['record_id'] = record_id
            self.replies.append(self.active_reply)
        self.last_record_id = record_id

    def start_operation(self, block):
        self.active_reply = None
        if block['type'] == 'over':
            self.active_operation = None
            return
        self.active_operation = {'kind': block['type'], 'sequence': len(self.operations) + 1,
                                 'started_at': time.monotonic(), 'command': str(block.get('body') or ''),
                                 'path': block.get('path'), 'state': 'running',
                                 'reply_record_id': self.replies[-1].get('record_id') if self.replies else None}
        self.operations.append(self.active_operation)

    def observe_operation(self, result, detailed_html=None):
        if self.active_operation is not None:
            for key in ('kind', 'success', 'output', 'notice', 'return_code', 'output_path',
                        'archive_truncated', 'running', 'status', 'session_id', 'waited_seconds', 'stopped'):
                if result.get(key) is not None:
                    self.active_operation[key] = result[key]
            self.active_operation['elapsed_seconds'] = time.monotonic() - self.active_operation['started_at']
            if detailed_html is not None:
                self.active_operation['detailed_html'] = detailed_html
            from .web_display import operation_state
            if result.get('stopped'):
                self.active_operation['state'] = 'stopped'
            self.active_operation['state'] = operation_state(self.active_operation)

    def summary(self):
        return {'rounds': len(self.replies), 'operations': len(self.operations),
                'elapsed_seconds': time.monotonic() - self.started_at, 'reason': self.reason,
                'items': [{k: v for k, v in op.items() if k != 'started_at'} for op in self.operations]}


def record_presentation(msg_type, content):
    turn = current_turn()
    if turn is None:
        return None
    result = {'version': 1, 'mode': turn.mode, 'kind': msg_type}
    if msg_type == 'ai_reply' and turn.active_reply:
        result.update({k: v for k, v in turn.active_reply.items() if k not in {'record_id', 'message_ids'}})
    elif msg_type == 'token_usage' and turn.active_reply:
        result['reply_record_id'] = turn.active_reply.get('record_id')
    elif msg_type == 'agent_result' and turn.active_operation and not technical_notice(content):
        result['operation'] = {k: v for k, v in turn.active_operation.items() if k != 'started_at'}
        result['operation'].setdefault('notice', str(content))
    if msg_type == 'agent_status' or technical_notice(content):
        result['technical'] = True
        result['anchor_record_id'] = turn.last_record_id
    return result


def link_usage_records(rows):
    """Join old and new usage rows without altering their durable content or order."""
    replies = {}
    previous = None
    for row in rows:
        metadata = metadata_dict(row.get('metadata'))
        if row.get('msg_type') == 'ai_reply':
            previous = row
            replies[row.get('id')] = row
        elif row.get('msg_type') == 'token_usage':
            link = (metadata.get('presentation') or {}).get('reply_record_id')
            parent = replies.get(link) if link is not None else previous
            if parent is not None and isinstance(metadata.get('usage'), dict):
                parent_meta = metadata_dict(parent.get('metadata'))
                presentation = parent_meta.setdefault('presentation', {'version': 1, 'kind': 'ai_reply'})
                presentation.setdefault('round', '?')  # Legacy records have no reliable task boundary.
                presentation.setdefault('model', metadata.get('model') or '')
                presentation.setdefault('usage', metadata['usage'])
                parent['metadata'] = parent_meta
    return rows
