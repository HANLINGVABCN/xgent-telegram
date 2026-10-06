"""TUI 增量布局的结构性回归：约束扫描/重排次数，不依赖机器跑分。"""
import unittest
from unittest import mock

from xgent_app import cli_tui
from xgent_app.cli_render import MessageRenderer, Palette


def folded_lines(index=0, count=50):
    return [f'message {index}', f'  🔧 run-x · {count} 行',
            *[f'  │ command {index}: line {line}' for line in range(count)]]


class IncrementalLayoutTests(unittest.TestCase):
    def model_with_history(self, count=300):
        model = cli_tui.MessageModel()
        for index in range(count):
            model.upsert(index, folded_lines(index))
        model.render_rows()
        return model

    def test_block_metadata_is_not_reparsed_on_read(self):
        model = self.model_with_history(1)
        block = next(b for b in model.messages[0].blocks if b.foldable)
        with mock.patch.object(cli_tui, '_is_folded_label', wraps=cli_tui._is_folded_label) as scan:
            for _ in range(100):
                self.assertTrue(block.foldable)
                self.assertEqual(50, len(block.code_lines))
                self.assertEqual(50, block.total_lines)
            scan.assert_not_called()

    def test_stream_update_only_rebuilds_changed_message(self):
        model = self.model_with_history()
        with mock.patch.object(model, '_message_rows', wraps=model._message_rows) as render:
            model.upsert(299, folded_lines(299) + ['new tail'])
            rows = model.render_rows()
            self.assertEqual(1, render.call_count)
            self.assertEqual(299, render.call_args.args[0].key)
            self.assertIn(('new tail', None), rows)
        with mock.patch.object(cli_tui.Block, 'foldable', new_callable=mock.PropertyMock) as scan:
            for _ in range(20):
                self.assertEqual((300, 0), model.counts())
            scan.assert_not_called()

    def test_selection_only_rebuilds_old_and_new_selected_message(self):
        model = self.model_with_history()
        targets = model.foldable_targets()
        model.render_rows(targets[20])
        with mock.patch.object(model, '_message_rows', wraps=model._message_rows) as render:
            rows = model.render_rows(targets[80])
            self.assertEqual(2, render.call_count)
            self.assertEqual({20, 80}, {call.args[0].key for call in render.call_args_list})
            selected = [(text, target) for text, target in rows if '❯' in text]
            self.assertEqual(1, len(selected))
            self.assertEqual(targets[80], selected[0][1])

    def test_counts_and_rows_follow_toggle_replace_remove(self):
        model = self.model_with_history(3)
        target = model.foldable_targets()[1]
        model.toggle(*target)
        self.assertEqual((3, 1), model.counts())
        with mock.patch.object(model, '_message_rows', wraps=model._message_rows) as render:
            model.render_rows()
            self.assertEqual(1, render.call_count)
        model.upsert(1, folded_lines(1, count=60))
        self.assertTrue(model.is_expanded(model.foldable_targets()[1]))
        self.assertEqual((3, 1), model.counts())
        model.upsert(1, ['plain replacement'])
        self.assertEqual((2, 0), model.counts())
        self.assertEqual(2, len(model.foldable_targets()))
        model.remove(0)
        self.assertEqual((1, 0), model.counts())
        model.set_all_expanded(True)
        self.assertEqual((1, 1), model.counts())
        model.set_all_expanded(False)
        self.assertEqual((1, 0), model.counts())

    def test_tui_collapses_large_blocks_to_header(self):
        screen = cli_tui.PtScreen(Palette(False), width=80)
        screen.print_block(folded_lines(), message_id=1)
        rows = [text for text, _ in screen.model.render_rows()]
        self.assertFalse(any('line 0' in text for text in rows))
        self.assertEqual(2, len(rows))  # 一行散文 + 一行工具块头
        screen.model.toggle(*screen.model.foldable_targets()[0])
        self.assertTrue(any('line 0' in text for text, _ in screen.model.render_rows()))

    def test_error_is_expanded_once_but_user_can_collapse_it(self):
        screen = cli_tui.PtScreen(Palette(False), width=80)
        screen.print_block(folded_lines(), message_id=1)
        screen.update_block(['❌ 返回码: 1'] + folded_lines(), message_id=1)
        target = screen.model.foldable_targets()[0]
        self.assertTrue(screen.model.is_expanded(target))
        screen.model.toggle(*target)
        screen.update_block(['❌ 返回码: 1'] + folded_lines() + ['details'], message_id=1)
        self.assertFalse(screen.model.is_expanded(target))
        screen.print_block(['⚠️ 错误'] + folded_lines(), message_id=2)
        self.assertTrue(screen.model.is_expanded(screen.model.foldable_targets()[-1]))


class CompactRendererTests(unittest.TestCase):
    def setUp(self):
        cli_tui._cached_paragraph.cache_clear()

    def test_completed_paragraphs_are_reused_on_stream_update(self):
        renderer = cli_tui.PtScreen(Palette(True), width=80).renderer()
        text = '\n'.join(f'稳定段落 {i}：这是已收到的内容。 English words.' for i in range(200))
        renderer.render_text(text, 'HTML')
        with mock.patch.object(cli_tui, 'wrap_line', wraps=cli_tui.wrap_line) as wrap:
            lines = renderer.render_text(text + '\n刚收到的新段落。', 'HTML')
            self.assertEqual(1, wrap.call_count)
            self.assertTrue(any('刚收到' in line for line in lines))

    def test_wrap_cache_obeys_width_and_does_not_retain_huge_paragraphs(self):
        renderer = cli_tui.PtScreen(Palette(False), width=80).renderer()
        raw = 'word ' * 20
        wide = renderer.render_text(raw)
        narrow = cli_tui.TuiMessageRenderer(Palette(False), 30).render_text(raw)
        self.assertGreater(len(narrow), len(wide))
        cli_tui._cached_paragraph.cache_clear()
        renderer.render_text('x' * 10000)
        self.assertEqual(0, cli_tui._cached_paragraph.cache_info().currsize)
        for i in range(1200):
            cli_tui._tui_wrap_text(f'paragraph {i}', 80)
        self.assertLessEqual(cli_tui._cached_paragraph.cache_info().currsize, 1024)

    def test_compact_style_does_not_change_legacy_or_mutate_palette(self):
        palette = Palette(True)
        original = vars(palette).copy()
        legacy_before = MessageRenderer(palette, 80).render_message('hello')
        screen = cli_tui.PtScreen(palette, width=80)
        rendered = screen.renderer().render_message('hello', [('Run', 'run')], 'HTML')
        text = '\n'.join(rendered)
        self.assertNotIn('═', text)
        self.assertIn('hello', text)
        self.assertIn('Run', text)
        command = '\n'.join(screen.renderer().render_message('hello', style='cmd', title='命令'))
        self.assertNotIn('\x1b[48;', command)
        legacy = '\n'.join(MessageRenderer(palette, 80).render_message('hello'))
        self.assertEqual('\n'.join(legacy_before), legacy)
        self.assertIn('─' * 8, legacy)
        self.assertEqual(original, vars(palette))

    def test_compact_hints_are_short_and_keep_help_discoverable(self):
        for mode in ('typing', 'input', 'busy', 'browse', 'native'):
            self.assertIn('F1', cli_tui.compact_hint_text(mode))
            self.assertLess(len(cli_tui.compact_hint_text(mode)), 55)


if __name__ == '__main__':
    unittest.main()
