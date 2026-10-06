"""Full local presentation, Telegram-only budgets, and paged archive safety."""
import html
import os
from pathlib import Path
import re
import tempfile
import unittest

from xgent_app.protocols import ProtocolParser
from xgent_app.agent_presenter import (build_run_presentation, build_grep_presentation,
    build_edit_presentation, build_search_presentation, build_fetch_presentation, build_shell_presentation)
from xgent_app.cli_tui import PtScreen, block_text
from xgent_app.cli_render import Palette
from xgent_app.output_archive import read_output_page, OutputArchiveError, OUTPUT_PAGE_BYTES
from xgent_app.web_history import build_history_message


def protocol(body, tag='run-x'):
    return f'```{tag}\n<<BEGIN_FULL12345\n{body}\n<<END_FULL12345\n```'


def body_of(text):
    return html.unescape(re.search(r'<pre>(.*?)</pre>', text, re.S)[1])


class FullPresentationTests(unittest.TestCase):
    def test_long_protocol_body_and_path_stay_complete(self):
        body = '\n'.join(f'{i}: <tag> & 中文 ' + 'long ' * 20 for i in range(300))
        path = '/tmp/' + 'deep/' * 300 + 'test.py'
        raw = protocol(body, 'file-x:' + path)
        canonical = ProtocolParser.render_folded_html(raw, raw_copy=True)
        self.assertEqual(body, body_of(canonical))
        self.assertIn(path, canonical)
        self.assertNotIn('已折叠', canonical)
        telegram = ProtocolParser.to_telegram_html(canonical)
        self.assertLess(len(telegram), 3900)
        self.assertNotIn('299:', telegram)
        self.assertNotIn('data-raw', telegram)
        self.assertNotIn('<pre>', telegram)
        self.assertIn('已折叠', telegram)
        self.assertEqual(telegram, ProtocolParser.to_telegram_html(telegram))
        self.assertEqual(raw, protocol(body, 'file-x:' + path))

    def test_single_huge_escaped_line_is_complete_locally_and_safe_for_telegram(self):
        body = '<>&中文' * 5000 + 'END_MARKER'
        canonical = ProtocolParser.render_folded_html(protocol(body))
        self.assertEqual(body, body_of(canonical))
        telegram = ProtocolParser.to_telegram_html(canonical)
        self.assertLess(len(telegram), 3900)
        self.assertIn('部分字符已省略', telegram)
        self.assertNotIn('END_MARKER', telegram)

    def test_telegram_emoji_budgets_count_utf16_not_python_codepoints(self):
        body = "😀" * 3500
        path = "/tmp/" + "😀" * 800 + "/last.py"
        canonical = ProtocolParser.render_folded_html(protocol(body, "file-x:" + path))
        telegram = ProtocolParser.to_telegram_html(canonical)
        self.assertEqual(body, body_of(canonical))
        self.assertLess(len(telegram.encode("utf-16-le")) // 2, 3900)

    def test_old_result_path_gets_paged_view_without_claiming_missing_text_is_restored(self):
        old = '<b>Agent Run</b>\n完整输出: <code>/tmp/xgent_storage/command_outputs/a.txt</code>\n' + (
            '<blockquote expandable><b>run-x</b> · 1 行\n<pre>old summary</pre></blockquote>')
        record = {"id": 1, "msg_type": "agent_result", "content": "context", "metadata": {
            "display": {"content": old, "parse_mode": "HTML"}}}
        display = build_history_message(record, "/tmp/xgent_storage", "/tmp/workspace")["content"]
        self.assertIn('data-output-path="/tmp/xgent_storage/command_outputs/a.txt"', display)
        self.assertEqual("old summary", body_of(display))

    def test_result_presenters_keep_full_received_output(self):
        text = '中文<script>not code</script>\n' * 600 + 'LAST_RESULT'
        for builder in (build_run_presentation, build_edit_presentation, build_grep_presentation,
                        build_search_presentation, build_fetch_presentation):
            with self.subTest(builder=builder.__name__):
                result = builder({'output': text, 'success': True, 'return_code': 0})
                self.assertEqual(text, body_of(result))
                self.assertLess(len(ProtocolParser.to_telegram_html(result)), 4000)
        shell = build_shell_presentation(action_label='read', shell_result={'success': True},
            session_id='abc', display_output=text, pause_note='')
        self.assertEqual(text, body_of(shell))

    def test_tui_expand_and_copy_preserve_all_lines_and_original_long_line(self):
        body = ('long-word-' * 90) + '\n' + '\n'.join(f'row {i}' for i in range(130))
        screen = PtScreen(Palette(False), width=50)
        text = ProtocolParser.render_folded_html(protocol(body), raw_copy=True)
        screen.print_block(screen.renderer().render_text(text, 'HTML'), message_id=1)
        target = screen.model.foldable_targets()[0]
        self.assertNotIn('row 129', '\n'.join(row for row, _ in screen.model.render_rows()))
        screen.model.toggle(*target)
        self.assertIn('row 129', '\n'.join(row for row, _ in screen.model.render_rows()))
        self.assertEqual(body, block_text(screen.model._block(target), whole=False))

    def test_old_preview_with_raw_is_restored_but_missing_raw_is_not_invented(self):
        body = '\n'.join(f'line{i}' for i in range(90))
        old = ProtocolParser.render_folded_html(protocol(body), max_lines=50, raw_copy=True)
        self.assertNotIn('line89', body_of(old))
        restored = ProtocolParser.restore_folded_html(old)
        self.assertEqual(body, body_of(restored))
        record = {'id': 1, 'msg_type': 'agent_result', 'content': 'model context',
                  'metadata': {'display': {'content': old, 'parse_mode': 'HTML'}}}
        message = build_history_message(record, '/tmp/storage', '/tmp/workspace')
        self.assertEqual(body, body_of(message['content']))
        no_raw = ProtocolParser.render_folded_html(protocol(body), max_lines=50)
        self.assertEqual(no_raw, ProtocolParser.restore_folded_html(no_raw))

    def test_incomplete_protocol_does_not_leak_body_to_copy_attributes(self):
        raw = '```run-x\n<<BEGIN_FULL12345\nprivate-unfinished-body'
        rendered = ProtocolParser.render_folded_html(raw, hide_unclosed=True, raw_copy=True)
        self.assertNotIn('private-unfinished-body', rendered)
        self.assertNotIn('data-raw', rendered)
        self.assertIn('生成中', rendered)
        final = ProtocolParser.render_folded_html(raw, hide_unclosed=False)
        self.assertEqual(raw, final)

    def test_archive_metadata_survives_tui_wrapping_but_not_telegram(self):
        path = '/tmp/xgent_storage/command_outputs/' + 'a' * 150 + '.txt'
        text = build_run_presentation({'output': 'ok', 'output_path': path})
        screen = PtScreen(Palette(False), width=40)
        screen.print_block(screen.renderer().render_message(text, parse_mode='HTML'), message_id=3)
        self.assertEqual(path, screen.model.messages[0].output_path)
        self.assertNotIn('data-output-path', ProtocolParser.to_telegram_html(text))


class OutputArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='xgent-output-page-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'command_outputs'
        self.root.mkdir()
        self.path = self.root / 'result.txt'

    def read(self, path=None, offset=0):
        return read_output_page(str(path or self.path), offset, roots=[self.root])

    def test_utf8_pages_reconstruct_exact_text_and_are_bounded(self):
        text = ('123456中文😀\n' * 15000) + '<script>never executed</script>END'
        self.path.write_bytes(text.encode('utf-8'))
        parts = []
        offset = 0
        while True:
            page = self.read(offset=offset)
            self.assertLessEqual(len(page['text'].encode('utf-8')), OUTPUT_PAGE_BYTES)
            self.assertEqual(offset, page['offset'])
            parts.append(page['text'])
            if page['eof']: break
            self.assertGreater(page['next_offset'], offset)
            offset = page['next_offset']
        self.assertEqual(text, ''.join(parts))
        self.assertEqual(self.path.stat().st_size, page['next_offset'])
        self.assertEqual(parts[0], self.read()['text'])

    def test_empty_missing_invalid_and_outside_paths(self):
        self.path.write_text('', encoding='utf-8')
        self.assertTrue(self.read()['eof'])
        self.assertEqual('', self.read()['text'])
        outside = Path(self.temp.name) / 'secret.txt'
        outside.write_text('not for this endpoint', encoding='utf-8')
        invalid = self.root / 'binary.bin'; invalid.write_bytes(b'abc')
        for path in (outside, self.root, invalid, self.root / 'missing.txt'):
            with self.subTest(path=path), self.assertRaises(OutputArchiveError): self.read(path)
        for offset in (-1, '0', True, 1):
            with self.subTest(offset=offset), self.assertRaises(OutputArchiveError): self.read(offset=offset)
        with self.assertRaises(OutputArchiveError):
            read_output_page(str(self.path), roots=[])

    def test_symlink_outside_root_is_rejected(self):
        outside = Path(self.temp.name) / 'outside.txt'; outside.write_text('private', encoding='utf-8')
        try: self.path.symlink_to(outside)
        except OSError: self.skipTest('symlinks not permitted')
        with self.assertRaises(OutputArchiveError): self.read()

    @unittest.skipUnless(os.name == 'posix', 'FIFO is POSIX-only')
    def test_fifo_is_rejected_without_blocking(self):
        os.mkfifo(self.path)
        with self.assertRaises(OutputArchiveError): self.read()


if __name__ == '__main__': unittest.main()
