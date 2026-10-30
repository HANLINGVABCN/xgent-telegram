"""Native table fidelity, splitting, safe HTML, and unchanged protocol previews."""
from html import unescape
from html.parser import HTMLParser
import re

from xgent_app.telegram_rich import BLOCK_BUDGET, render_rich, output_rich


class CheckHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors, self.text, self.max_depth = [], [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag not in {'br', 'hr', 'input'}:
            self.stack.append(tag); self.max_depth = max(self.max_depth, len(self.stack))
        assert all(name in {'compact','expandable','align','class','href','name','type','checked','start'} for name, _ in attrs), attrs

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(tag)

    def handle_data(self, data):
        self.text.append(data)


def parsed(value):
    parser = CheckHTML(); parser.feed(value); parser.close()
    assert not parser.stack and not parser.errors, (parser.stack, parser.errors)
    assert parser.max_depth <= 16
    return ''.join(parser.text)


def test_table_keeps_alignment_links_code_and_escaped_pipe():
    raw = '| 项目 | 结果 |\n| :--- | ---: |\n| `a\\|b` | [可用](https://example.com) **正常** |'
    value = ''.join(render_rich(raw))
    assert '<table compact>' in value and '<pre>' not in value
    assert '<td align="left"><code>a|b</code></td>' in value
    assert '<td align="right"><a href="https://example.com">可用</a> <b>正常</b></td>' in value
    assert 'a|b可用 正常' in parsed(value)
    assert '\\|' in raw  # Source was not normalized or rewritten.


def test_code_and_protocol_pipes_are_not_tables_and_preview_stays_50_lines():
    raw = '```shell\necho a|b\n```\n```run-x\n<<BEGIN_RICHPROBE0001\n' + '\n'.join('LINE'+str(i) for i in range(57)) + '\n<<END_RICHPROBE0001\n```'
    value = ''.join(render_rich(raw))
    assert '<code class="language-shell">echo a|b\n</code>' in value
    assert '<table' not in value and '<blockquote expandable>' in value
    assert 'LINE49' in value and 'LINE50' not in value and '已折叠 7 行' in value
    parsed(value)


def test_math_footnotes_lists_and_untrusted_html_are_supported_safely():
    raw = '# 标题\n\n- [x] 完成\n- [ ] 等待\n\n公式 $x^2$。说明[^a]\n\n[^a]: 注释\n\n<script>no</script>'
    value = ''.join(render_rich(raw))
    assert '<h1>标题</h1>' in value
    assert '<tg-math>x^2</tg-math>' in value
    assert '<a href="#fn-1"><sup>1</sup></a>' in value
    assert '<a name="fn-1"></a>' in value
    assert '<input type="checkbox" checked>' in value
    assert '<script>' not in value and '&lt;script&gt;' in value
    parsed(value)


def test_wide_table_repeats_first_column_and_row_splits_repeat_header():
    columns = [f'C{i}' for i in range(26)]
    raw = '| ' + ' | '.join(columns) + ' |\n|' + '|'.join(['---']*26) + '|\n'
    raw += '\n'.join('|' + '|'.join(f'R{i}C{j}' for j in range(26)) + '|' for i in range(170))
    chunks = render_rich(raw)
    assert len(chunks) > 1
    for value in chunks:
        assert len(value) <= 32000
        parsed(value)
        for row in re.findall(r'<tr>(.*?)</tr>', value, re.S):
            assert len(re.findall(r'<t[dh](?:\s|>)', row)) <= 20
        for table in re.findall(r'<table compact>(.*?)</table>', value, re.S):
            assert '<th>C0</th>' in table
    joined = ''.join(chunks)
    assert all(f'R{i}C{j}' in joined for i in range(170) for j in range(26))


def test_large_formatted_words_and_long_code_split_without_losing_text():
    word = '甲&🙂' * 14000
    for raw in ('**' + word + '**', '```python\n' + word + '\n```'):
        chunks = render_rich(raw)
        assert len(chunks) > 1 and all(len(value) <= 32000 for value in chunks)
        assert ''.join(parsed(value) for value in chunks).strip() == word


def test_output_status_preview_and_archive_position_are_preserved():
    value = ''.join(output_rich({'kind':'run','success':False,'output':'\n'.join(str(i) for i in range(57)),
                               'output_path':'/tmp/结果.txt'}))
    assert '🔴 run-x · 输出 · 失败' in parsed(value)
    assert '<pre>' in value and '已折叠 7 行 · 存档：/tmp/结果.txt' in parsed(value)
    assert '50\n' not in parsed(value)


def test_deep_lists_and_many_rows_stay_inside_native_limits():
    value = ''.join(render_rich('\n'.join('  '*i+'- ITEM'+str(i) for i in range(30))))
    assert all('ITEM'+str(i) in parsed(value) for i in range(30))
    raw = '| A | B |\n|---|---|\n' + '\n'.join(f'|{i}|值{i}|' for i in range(1000))
    chunks = render_rich(raw)
    assert len(chunks) > 1
    for chunk in chunks:
        parsed(chunk)
        assert chunk.count('<tr>') < BLOCK_BUDGET
