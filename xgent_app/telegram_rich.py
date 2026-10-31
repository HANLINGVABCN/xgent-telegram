"""Telegram Rich HTML projection. Source text and other channels stay untouched."""
from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
import re

from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.tasklists import tasklists_plugin

from .protocols import ProtocolParser

TEXT_BUDGET = 32000
BLOCK_BUDGET = 480
MAX_COLUMNS = 20
MAX_DEPTH = 16


@dataclass
class Block:
    tag: str = ''
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    text: str = ''
    # text is HTML only when it was emitted by the inline renderer below.
    inline: bool = False

    def html(self):
        value = ''.join(child.html() for child in self.children) if self.children else (
            self.text if self.inline else escape(self.text, quote=False))
        if not self.tag:
            return value
        attrs = ''.join(' ' + name + ('' if value is True else '="' + escape(str(value), quote=True) + '"')
                        for name, value in self.attrs.items())
        return '<' + self.tag + attrs + '>' + value + '</' + self.tag + '>'

    def size(self):
        # Source-HTML length is conservative: entities/tags cannot cause an
        # undercount of Telegram's parsed text or formula-source budget.
        return len(self.html())

    def count(self):
        structural = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre', 'footer', 'ul', 'ol',
                      'li', 'blockquote', 'aside', 'table', 'tr', 'details', 'tg-math-block'}
        return (1 if self.tag in structural else 0) + sum(child.count() for child in self.children)


def _parser():
    return (MarkdownIt('commonmark', {'html': False, 'maxNesting': 1024}).enable(['table', 'strikethrough'])
            .use(dollarmath_plugin).use(footnote_plugin).use(tasklists_plugin))


def inline_html(tokens):
    result = []
    opened = []
    pairs = {'strong': 'b', 'em': 'i', 's': 's', 'link': 'a'}
    for token in tokens or []:
        kind = token.type
        if kind == 'text':
            result.append(escape(token.content, quote=False))
        elif kind == 'code_inline':
            result.append('<code>' + escape(token.content, quote=False) + '</code>')
        elif kind in {'softbreak', 'hardbreak'}:
            result.append('<br>')
        elif kind == 'math_inline':
            result.append('<tg-math>' + escape(token.content, quote=False) + '</tg-math>')
        elif kind == 'footnote_ref':
            index = int(token.meta['id']) + 1
            result.append('<a href="#fn-' + str(index) + '"><sup>' + str(index) + '</sup></a>')
        elif kind == 'image':
            # Media are delivered by the original attachment pipeline. Rendering
            # a model-authored URL must not trigger a fresh server-side upload.
            result.append(escape(token.content, quote=False))
        elif kind == 'html_inline':
            checkbox = re.fullmatch(r'<input\b[^>]*type="checkbox"[^>]*>', token.content)
            if checkbox:
                checked = ' checked' if re.search(r'\bchecked(?:[ =])', token.content) else ''
                result.append('<input type="checkbox"' + checked + '>')
            else:
                result.append(escape(token.content, quote=False))
        elif kind.endswith('_open') and kind[:-5] in pairs:
            tag = pairs[kind[:-5]]
            allowed = len(opened) < 8
            opened.append((tag, allowed))
            if not allowed:
                continue
            attrs = ''
            if tag == 'a':
                url = token.attrGet('href') or ''
                attrs = ' href="' + escape(url, quote=True) + '"'
            result.append('<' + tag + attrs + '>')
        elif kind.endswith('_close') and kind[:-6] in pairs:
            tag, allowed = opened.pop() if opened else (pairs[kind[:-6]], False)
            if allowed:
                result.append('</' + tag + '>')
        else:
            result.append(escape(token.content, quote=False))
    return ''.join(result)


def markdown_blocks(text):
    root = Block()
    stack = [root]
    for token in _parser().parse(str(text)):
        kind = token.type
        if kind == 'inline':
            stack[-1].children.append(Block(text=inline_html(token.children), inline=True))
        elif kind in {'fence', 'code_block'}:
            code = Block('code', text=token.content)
            language = token.info.strip().split(' ', 1)[0] if token.info else ''
            if re.fullmatch(r'[A-Za-z0-9_+.-]+', language):
                code.attrs['class'] = 'language-' + language
            stack[-1].children.append(Block('pre', children=[code]))
        elif kind == 'math_block':
            stack[-1].children.append(Block('tg-math-block', text=token.content))
        elif kind == 'hr':
            stack[-1].children.append(Block(text='<hr>', inline=True))
        elif kind == 'footnote_block_open':
            section = Block()
            stack[-1].children.append(section)
            stack.append(section)
        elif kind == 'footnote_open':
            index = int(token.meta['id']) + 1
            node = Block('div', children=[Block(text='<a name="fn-' + str(index) + '"></a>', inline=True)])
            # Rich HTML has no div: use a paragraph-compatible container.
            node.tag = ''
            stack[-1].children.append(node)
            stack.append(node)
        elif kind == 'footnote_anchor':
            continue
        elif kind.endswith('_open'):
            tag = token.tag
            if tag in {'thead', 'tbody'}:
                tag = ''
            attrs = {}
            if tag == 'table':
                attrs['compact'] = True
            elif tag in {'td', 'th'}:
                alignment = token.attrGet('style') or ''
                match = re.fullmatch(r'text-align:(left|center|right)', alignment)
                if match:
                    attrs['align'] = match[1]
            elif tag == 'ol' and token.attrGet('start'):
                attrs['start'] = token.attrGet('start')
            node = Block(tag, attrs=attrs)
            stack[-1].children.append(node)
            stack.append(node)
        elif kind.endswith('_close'):
            if len(stack) > 1:
                stack.pop()
        elif token.content:
            stack[-1].children.append(Block('p', text=token.content))
    return [_bounded(block) for block in root.children]


def _flatten(block):
    from html.parser import HTMLParser

    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

        def handle_starttag(self, tag, attrs):
            if tag in {'br', 'p', 'li', 'tr', 'pre'}:
                self.parts.append('\n')
    stack, values = [(block, False)], []
    while stack:
        node, closing = stack.pop()
        if closing:
            if node.tag in {'p', 'li', 'tr', 'pre'}:
                values.append('\n')
        elif node.children:
            if node.tag == 'li':
                values.append('• ')
            stack.append((node, True))
            stack.extend((child, False) for child in reversed(node.children))
        elif node.inline:
            parser = Text(); parser.feed(node.text); parser.close()
            values.extend(parser.parts)
        else:
            values.append(node.text)
    return Block('p', text=''.join(values))


def _bounded(block, depth=0):
    if depth >= 7:
        return _flatten(block)
    block.children = [_bounded(child, depth + bool(block.tag)) for child in block.children]
    return block


def _rows(block):
    result = []
    for child in block.children:
        if child.tag == 'tr':
            result.append(child)
        else:
            result.extend(_rows(child))
    return result


def _tables(block):
    if block.tag != 'table':
        block.children = [piece for child in block.children for piece in _tables(child)]
        return [block]
    rows = _rows(block)
    columns = max((len(row.children) for row in rows), default=0)
    if columns <= MAX_COLUMNS:
        block.children = rows
        return [block]
    groups = [list(range(MAX_COLUMNS))]
    for start in range(MAX_COLUMNS, columns, MAX_COLUMNS - 1):
        groups.append([0, *range(start, min(start + MAX_COLUMNS - 1, columns))])
    return [Block('table', attrs=block.attrs, children=[Block('tr', children=[
        row.children[index] if index < len(row.children) else Block('td') for index in group]) for row in rows])
        for group in groups]


def rich_blocks(text, *, generating=False):
    result = []
    for segment in ProtocolParser.scan_folded_segments(str(text), hide_unclosed=generating):
        if segment['kind'] == 'prose':
            result.extend(piece for block in markdown_blocks(segment['text']) for piece in _tables(block))
        elif segment.get('type') != 'over':
            protocol = ('file-x' if segment['type'] == 'file_base64' else segment['type'] + '-x')
            path = segment.get('path') or ''
            if segment.get('generating'):
                result.append(Block('p', text=protocol + ' · 生成中…'))
            else:
                preview, note = ProtocolParser._telegram_preview(segment.get('body') or '')
                header = '<b>' + escape(protocol + (' ' + path if path else '')) + '</b> · '
                header += str((segment.get('body') or '').count('\n') + 1) + ' 行<br>'
                result.append(Block('blockquote', attrs={'expandable': True}, children=[
                    Block(text=header, inline=True), Block('code', children=[Block(text=preview, inline=True)]),
                    Block(text=note, inline=True)]))
    return result


def _split_block(block, budget, block_budget):
    if block.size() <= budget and block.count() <= block_budget:
        return [block]
    if block.tag == 'table':
        rows = _rows(block)
        head = rows[:1]
        if head and Block('table', block.attrs, head).size() > budget:
            result = []
            for row_index, row in enumerate(rows):
                for index, cell in enumerate(row.children):
                    label = ('表头' if row_index == 0 else '第' + str(row_index) + '行') + ' · ' + str(index + 1) + '：'
                    result.extend(_split_block(Block('p', children=[Block(text=label), *cell.children]), budget, block_budget))
            return result
        parts, current = [], list(head)
        for row in rows[1:]:
            candidate = Block('table', block.attrs, current + [row])
            if (candidate.size() > budget or candidate.count() > block_budget) and len(current) > len(head):
                parts.append(Block('table', block.attrs, current))
                current = list(head)
            if Block('table', block.attrs, head + [row]).size() > budget:
                # Oversize cells remain text, never disappear or produce invalid
                # half-table HTML. Each row gets a complete continuation.
                if len(current) > len(head):
                    parts.append(Block('table', block.attrs, current)); current = list(head)
                for index, cell in enumerate(row.children):
                    label = head[0].children[index].html() if head and index < len(head[0].children) else str(index + 1)
                    parts.extend(_split_block(Block('p', children=[Block(text=label + ': ', inline=True), *cell.children]), budget, block_budget))
            else:
                current.append(row)
        if len(current) > len(head) or not parts:
            parts.append(Block('table', block.attrs, current))
        return parts
    if block.children:
        envelope = len(Block(block.tag, block.attrs).html())
        parts, current = [], []
        for child in block.children:
            for piece in _split_block(child, max(64, budget - envelope), max(2, block_budget - 1)):
                candidate = Block(block.tag, block.attrs, current + [piece])
                if current and (candidate.size() > budget or candidate.count() > block_budget):
                    parts.append(Block(block.tag, block.attrs, current)); current = []
                current.append(piece)
        if current:
            parts.append(Block(block.tag, block.attrs, current))
        return parts
    if block.inline:
        # Inline fragments contain complete formatting tags. Split through a
        # small HTML tree rather than cutting tags/entities at byte offsets.
        return _split_block(html_block(block.html()), budget, block_budget)
    envelope = len(Block(block.tag, block.attrs).html())
    width = max(1, (budget - envelope) // 6)
    return [Block(block.tag, block.attrs, text=block.text[index:index + width])
            for index in range(0, len(block.text), width)] or [block]


def html_block(value):
    from html.parser import HTMLParser

    class Tree(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.root = Block()
            self.stack = [self.root]

        def handle_starttag(self, tag, attrs):
            node = Block(tag, {key: True if val is None else val for key, val in attrs})
            if tag in {'br', 'hr', 'input'}:
                node = Block(text='<' + tag + ''.join(' ' + key + ('' if val is None else '="' + escape(val, quote=True) + '"') for key, val in attrs) + '>', inline=True)
            self.stack[-1].children.append(node)
            if tag not in {'br', 'hr', 'input'}:
                self.stack.append(node)

        def handle_endtag(self, tag):
            if len(self.stack) > 1 and self.stack[-1].tag == tag:
                self.stack.pop()

        def handle_data(self, data):
            self.stack[-1].children.append(Block(text=data))

    parser = Tree(); parser.feed(value); parser.close()
    return parser.root


def split_blocks(blocks, *, budget=TEXT_BUDGET, block_budget=BLOCK_BUDGET):
    chunks, current, size, count = [], [], 0, 0
    for block in blocks:
        for part in _split_block(block, budget, block_budget):
            value = part.html()
            if current and (size + len(value) > budget or count + part.count() > block_budget):
                chunks.append(''.join(current)); current, size, count = [], 0, 0
            current.append(value); size += len(value); count += part.count()
    if current:
        chunks.append(''.join(current))
    return chunks or [' ']


def render_rich(text, *, generating=False, details=None):
    blocks = rich_blocks(text, generating=generating)
    if details:
        blocks.append(html_block(details))
    return split_blocks(blocks)


def legacy_chunks(text, *, generating=False):
    """The existing conservative fallback remains a transport capability choice."""
    from .web_history import _section_ns
    ns = _section_ns()
    if ns:
        value = ns['_folded_display_html'](str(text), hide_unclosed=generating)
        return ns['split_html_for_telegram'](value or ' ', 3900)
    from .telegram_chat import escaped_chunks
    return escaped_chunks(str(text) or ' ', 3800)


def output_rich(operation):
    from .web_display import PROTOCOL_NAMES, operation_state
    state = operation_state(operation)
    protocol = operation.get('protocol_name') or PROTOCOL_NAMES.get(operation.get('kind'), 'run-x')
    dot = {'success': '🟢', 'failed': '🔴'}.get(state, '⚪')
    label = {'running': '执行中', 'stopped': '输出 · 已停止', 'failed': '输出 · 失败',
             'success': '输出'}.get(state, '输出 · 未确认')
    body = str(operation.get('output') or operation.get('notice') or '(无输出)')
    preview, note = ProtocolParser._telegram_preview(body)
    header = '<b>' + escape(dot + ' ' + protocol + ' · ' + label) + '</b> · ' + str(body.count('\n') + 1) + ' 行<br>'
    path = operation.get('output_path')
    suffix = ((' · ' if note else '<br>') + '存档：' + escape(str(path), quote=False)) if path else ''
    return split_blocks([Block('blockquote', {'expandable': True}, children=[
        Block(text=header, inline=True), Block('code', children=[Block(text=preview, inline=True)]),
        Block(text=note + suffix, inline=True)])])
