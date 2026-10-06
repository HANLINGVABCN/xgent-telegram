"""全屏 CLI TUI（cli_tui）的行为测试。

折叠语义与 Telegram/网页一致：收起 = 只显示「▸ 块头」；展开 = 块头 + 前 50 行
(+「已折叠 K 行」纯文字)。纯逻辑（分段/模型/屏幕/补全/底栏）脱离终端测；pt 装了
再用 PipeInput + DummyOutput 端到端驱动真 App（选块、展开、滚动、补全、退出）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import unittest
from unittest import mock

from xgent_app import cli_tui
from xgent_app.cli_render import MessageRenderer, Palette
from xgent_app.protocols import ProtocolParser
from tests.test_protocols import NONCE_A, protocol_block


def _folded_lines(body_lines: int, width: int = 80, color: bool = False) -> list:
    """造一条真实的折叠回复：散文 + 大块(run) + 散文 + 小块(edit)，经真渲染器出行。"""
    big = protocol_block("run-x", "\n".join(f"l{i}" for i in range(body_lines)), NONCE_A)
    small = protocol_block("edit-x:/etc/a.py", "x\ny", "NONCEB12")
    html = ProtocolParser.render_folded_html(
        "说明\n" + big + "\n中间\n" + small, prose_renderer=lambda t: t)
    return MessageRenderer(Palette(color), width).render_text(html, "HTML")


def _plain(rows) -> list:
    return [cli_tui._strip_ansi(t) for t, _ in rows]


class SegmentLinesTests(unittest.TestCase):
    def test_folded_reply_splits_into_prose_and_fold_blocks(self):
        blocks = cli_tui.segment_lines(_folded_lines(55))
        folds = [b for b in blocks if b.collapsible]
        self.assertEqual(2, len(folds))
        self.assertIn("run-x · 55 行", cli_tui._strip_ansi(folds[0].header))  # 保留 -x
        self.assertEqual(55, len(folds[0].lines))
        self.assertEqual("│ l54", cli_tui._strip_ansi(folds[0].lines[-1]).strip())
        self.assertTrue(folds[0].foldable)
        self.assertEqual(2, len(folds[1].lines))            # ≤3 行：无标签、不折叠
        self.assertFalse(folds[1].foldable)

    def test_colored_output_segments_the_same(self):
        folds = [b for b in cli_tui.segment_lines(_folded_lines(55, color=True)) if b.collapsible]
        self.assertEqual(2, len(folds))

    def test_code_without_header_is_prose(self):
        # 折叠关 / 普通 markdown 代码：没有块头 → 原样显示，不折
        blocks = cli_tui.segment_lines(["  hi", "  │ a", "  │ b"])
        self.assertEqual(1, len(blocks))
        self.assertFalse(blocks[0].collapsible)

    def test_header_like_prose_without_code_is_prose(self):
        blocks = cli_tui.segment_lines(["  统计 · 3 行", "  普通文字"])
        self.assertFalse(any(b.collapsible for b in blocks))


RUN_HEAD = "[🔧 run-x] · 55 行"
EDIT_HEAD = "[📝 edit-x /etc/a.py] · 2 行"


class MessageModelTests(unittest.TestCase):
    def setUp(self):
        self.m = cli_tui.MessageModel(Palette(False))
        self.m.upsert(1, _folded_lines(55))

    def _idx(self, rows, head):
        return [i for i, r in enumerate(rows) if r.endswith(head)][0]

    def test_default_collapsed_shows_header_plus_3_line_preview(self):
        rows = _plain(self.m.render_rows())
        hi = self._idx(rows, RUN_HEAD)
        self.assertEqual("  ▸ " + RUN_HEAD, rows[hi])
        self.assertEqual(["  │ l0", "  │ l1", "  │ l2"], rows[hi + 1:hi + 4])
        self.assertNotIn("  │ l3", rows)
        self.assertFalse(any("已折叠" in r for r in rows))

    def test_expand_shows_complete_body(self):
        run = self.m.foldable_targets()[0]
        self.assertTrue(self.m.toggle(*run))
        rows = _plain(self.m.render_rows())
        hi = self._idx(rows, RUN_HEAD)
        self.assertEqual("  ▾ " + RUN_HEAD, rows[hi])
        self.assertEqual("  │ l0", rows[hi + 1])
        self.assertEqual("  │ l49", rows[hi + 50])
        self.assertEqual("  │ l50", rows[hi + 51])
        self.assertEqual("  │ l54", rows[hi + 55])
        self.assertFalse(any("已折叠" in r for r in rows))

    def test_label_is_not_clickable(self):
        run = self.m.foldable_targets()[0]
        self.m.toggle(*run)
        for text, target in self.m.render_rows():
            plain = cli_tui._strip_ansi(text)
            if "已折叠" in plain:
                self.assertIsNone(target)
            if plain.endswith(RUN_HEAD):
                self.assertEqual(run, target)

    def test_short_block_shown_whole_and_not_foldable(self):
        self.assertEqual(1, len(self.m.foldable_targets()))
        rows = _plain(self.m.render_rows())
        hi = self._idx(rows, EDIT_HEAD)
        self.assertEqual("  ■ " + EDIT_HEAD, rows[hi])
        self.assertEqual(["  │ x", "  │ y"], rows[hi + 1:hi + 3])
        bi = [i for i, b in enumerate(self.m.messages[0].blocks) if b.collapsible][1]
        self.assertFalse(self.m.toggle(1, bi))

    def test_toggle_again_collapses(self):
        run = self.m.foldable_targets()[0]
        self.m.toggle(*run)
        self.m.toggle(*run)
        self.assertFalse(self.m.is_expanded(run))

    def test_toggle_rejects_prose(self):
        self.assertFalse(self.m.toggle(1, 0))
        self.assertFalse(self.m.toggle(99, 0))

    def test_upsert_keeps_expand_state(self):
        run = self.m.foldable_targets()[0]
        self.m.toggle(*run)
        self.m.upsert(1, _folded_lines(60))
        self.assertTrue(self.m.is_expanded(self.m.foldable_targets()[0]))

    def test_counts_and_set_all(self):
        self.assertEqual((1, 0), self.m.counts())
        self.m.set_all_expanded(True)
        self.assertEqual((1, 1), self.m.counts())
        self.m.set_all_expanded(False)
        self.assertEqual((1, 0), self.m.counts())

    def test_selected_header_marked(self):
        run = self.m.foldable_targets()[0]
        rows = _plain(self.m.render_rows(run))
        self.assertIn("❯ ▸ " + RUN_HEAD, rows)

    def test_block_text_for_copy(self):
        blk = [b for b in self.m.messages[0].blocks if b.collapsible][0]
        code = cli_tui.block_text(blk, whole=False).split("\n")
        self.assertEqual("l0", code[0])
        self.assertEqual(55, len(code))                   # 完整复制，不继承 TG 50 行上限
        self.assertTrue(cli_tui.block_text(blk).startswith("🔧 run-x · 55 行\nl0"))

    def test_leading_blank_between_messages(self):
        m = cli_tui.MessageModel()
        m.upsert(None, ["a"], leading_blank=False)
        m.upsert(None, ["b"])
        m.upsert(None, ["c"], leading_blank=False)
        self.assertEqual(["a", "", "b", "c"], _plain(m.render_rows()))

    def test_revision_and_remove(self):
        rev = self.m.revision
        self.assertTrue(self.m.remove(1))
        self.assertGreater(self.m.revision, rev)
        self.assertFalse(self.m.has(1))
        self.assertFalse(self.m.remove(1))


class PtScreenTests(unittest.TestCase):
    def setUp(self):
        self.s = cli_tui.PtScreen(palette=Palette(False), width=80)
        self.calls = []
        self.s.on_change = lambda: self.calls.append(1)

    def test_print_and_update(self):
        self.s.print_block(["a"], message_id=5)
        self.assertTrue(self.s.update_block(["b"], 5))
        self.assertFalse(self.s.update_block(["c"], 6))
        self.assertEqual(2, len(self.calls))
        self.assertEqual(["b"], _plain(self.s.model.render_rows()))

    def test_notice_marker(self):
        self.s.notice("done", "ok")
        self.assertEqual(["✓ done"], _plain(self.s.model.render_rows()))

    def test_width_forced(self):
        self.assertEqual(80, self.s.width)


class SlashAndHintTests(unittest.TestCase):
    def test_slash_prefix(self):
        out = cli_tui.slash_completions("/st", ["start", "stop", "help"], lambda n: n.upper())
        self.assertEqual([("/start", "START"), ("/stop", "STOP")], out)
        self.assertEqual([], cli_tui.slash_completions("st", ["start"], str))
        self.assertEqual([], cli_tui.slash_completions("/start x", ["start"], str))

    def test_start_is_first_for_s_prefix_without_reordering_other_commands(self):
        names = ["search", "skills", "start", "stats", "stop", "help"]
        for prefix in ("/s", "/S", "/st", "/sta"):
            with self.subTest(prefix=prefix):
                result = cli_tui.slash_completions(prefix, names, lambda name: name.upper())
                self.assertEqual(("/start", "START"), result[0])
                expected_rest = ["/" + name for name in names
                                 if name != "start" and name.startswith(prefix[1:].lower())]
                self.assertEqual(expected_rest, [name for name, _ in result[1:]])
        self.assertEqual(names, [name[1:] for name, _ in cli_tui.slash_completions("/", names, str)])
        self.assertEqual([("/stats", "stats")], cli_tui.slash_completions("/stat", names, str))
        self.assertEqual([("/search", "search"), ("/skills", "skills")],
                         cli_tui.slash_completions("/s", ["search", "skills"], str))

    def test_hints_per_mode(self):
        self.assertIn("点击代码块展开", cli_tui.hint_text("input"))
        self.assertIn("拖选自动复制", cli_tui.hint_text("input"))
        self.assertIn("Enter 发送", cli_tui.hint_text("typing"))
        self.assertIn("↑↓ 输入历史", cli_tui.hint_text("typing"))
        self.assertIn("Ctrl+C 中断", cli_tui.hint_text("busy"))
        self.assertIn("y 复制", cli_tui.hint_text("browse"))
        self.assertIn("再按一次", cli_tui.hint_text("exit"))


class TuiEnabledTests(unittest.TestCase):
    def _run(self, env, stdin_tty=True, stdout_tty=True, pt_ok=True):
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("sys.stdin") as si, mock.patch("sys.stdout") as so, \
                mock.patch.object(cli_tui, "_pt_available", return_value=pt_ok):
            si.isatty.return_value = stdin_tty
            so.isatty.return_value = stdout_tty
            return cli_tui.tui_enabled()

    def test_enabled_when_all_conditions_met(self):
        self.assertTrue(self._run({"XGENT_CLI_TUI": "1"}))

    def test_disabled_without_optin(self):
        self.assertFalse(self._run({}))

    def test_disabled_by_no_tui_override(self):
        self.assertFalse(self._run({"XGENT_CLI_TUI": "1", "XGENT_CLI_NO_TUI": "1"}))

    def test_disabled_without_tty(self):
        self.assertFalse(self._run({"XGENT_CLI_TUI": "1"}, stdout_tty=False))

    def test_disabled_when_pt_missing(self):
        self.assertFalse(self._run({"XGENT_CLI_TUI": "1"}, pt_ok=False))

    def test_truthy_variants(self):
        for val in ("1", "true", "YES", "on", "  On  "):
            self.assertTrue(self._run({"XGENT_CLI_TUI": val}), val)
        for val in ("0", "no", "off", ""):
            self.assertFalse(self._run({"XGENT_CLI_TUI": val}), val)


class FragmentCacheTests(unittest.TestCase):
    def setUp(self):
        if importlib.util.find_spec("prompt_toolkit") is None:
            self.skipTest("prompt_toolkit 未安装")
        cli_tui._line_fragments.cache_clear()

    def test_ansi_characters_are_coalesced_without_losing_styles(self):
        text = "\x1b[31m" + "中x" * 1000 + "\x1b[0m tail"
        fragments = cli_tui._line_fragments(text)
        self.assertEqual("中x" * 1000 + " tail", "".join(t for _, t in fragments))
        self.assertLessEqual(len(fragments), 3)
        self.assertNotEqual(fragments[0][0], fragments[-1][0])
        self.assertIs(fragments, cli_tui._line_fragments(text))

    def test_cache_is_bounded_without_wholesale_eviction(self):
        for i in range(5000):
            cli_tui._line_fragments(f"line {i}")
        info = cli_tui._line_fragments.cache_info()
        self.assertEqual(4096, info.currsize)
        cli_tui._line_fragments("line 4999")
        self.assertEqual(info.hits + 1, cli_tui._line_fragments.cache_info().hits)

    def test_fold_counts_are_cached_until_model_revision_changes(self):
        model = cli_tui.MessageModel()
        model.upsert(1, _folded_lines(10))
        with mock.patch.object(cli_tui.Block, "foldable", new_callable=mock.PropertyMock,
                               return_value=True) as foldable:
            expected = model.counts()
            calls = foldable.call_count
            self.assertEqual(expected, model.counts())
            self.assertEqual(calls, foldable.call_count)
            model.upsert(2, ["new message"])
            model.counts()
            self.assertGreater(foldable.call_count, calls)


class PtEndToEndTests(unittest.TestCase):
    """pt 装了才跑：真 App + 管道输入，按键驱动整条交互链。"""

    def setUp(self):
        if importlib.util.find_spec("prompt_toolkit") is None:
            self.skipTest("prompt_toolkit 未安装")

    def _run(self, scenario):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.application.current import get_app
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        screen = cli_tui.PtScreen(palette=Palette(True), width=80)
        sent = []

        async def _dispatch(text):
            sent.append(text)
            return False

        hooks = cli_tui.TuiHooks(dispatch=_dispatch, command_names=lambda: ("help", "hide"))

        async def _drive():
            with create_pipe_input() as pinp:
                with create_app_session(input=pinp, output=DummyOutput()):
                    task = asyncio.ensure_future(cli_tui.run_tui(screen, hooks))
                    await asyncio.sleep(0.3)
                    app = get_app()
                    win = [w for w in app.layout.find_all_windows() if hasattr(w, "scroll_by")][0]

                    async def keys(text, wait=0.25):
                        pinp.send_text(text)
                        await asyncio.sleep(wait)

                    await scenario(screen, app, win, keys, sent)
                    await keys("\x03\x03")          # 空闲 Ctrl+C 两次退出
                    await asyncio.wait_for(task, timeout=5)

        asyncio.run(_drive())

    def test_browse_expand_and_scroll(self):
        async def scenario(screen, app, win, keys, sent):
            for i in range(80):
                screen.print_plain(f"history {i}")
            screen.print_block(screen.renderer().render_text(
                ProtocolParser.render_folded_html(
                    "说明\n" + protocol_block("run-x", "\n".join(f"l{i}" for i in range(55)), NONCE_A),
                    prose_renderer=lambda t: t), "HTML"), message_id=7)
            await keys("")
            self.assertTrue(win.follow)                       # 默认贴底
            self.assertFalse(app.layout.has_focus(win))       # 一进来就聚焦输入框
            await keys("\x1b", 0.8)                          # Esc 到输出区
            await keys("\t")                                 # Tab 选中最新折叠块
            await keys("\r")                                 # Enter 原地展开
            target = screen.model.foldable_targets()[-1]
            self.assertTrue(screen.model.is_expanded(target))
            rows = _plain(screen.model.render_rows())
            hi = [i for i, r in enumerate(rows) if "run-x" in r and "55 行" in r][0]
            self.assertTrue(win.vertical_scroll <= hi < win.vertical_scroll + 30)  # 块头在视口里
            await keys("\x1b", 0.8)                          # Esc 取消选中
            before = win.vertical_scroll
            await keys("\x1b[A")                             # ↑ 只管输入框：回到输入框，不翻页
            self.assertFalse(app.layout.has_focus(win))
            self.assertEqual(before, win.vertical_scroll)
            await keys("\x1b[1;5F")                          # Ctrl+End 回底
            await keys("\x1b[5~")                            # PgUp 翻页
            self.assertFalse(win.follow)
            top = win.vertical_scroll
            screen.print_plain("new line")
            await keys("")
            self.assertEqual(top, win.vertical_scroll)        # 新内容不把人拽回底
            await keys("\x1b[1;5F")                          # Ctrl+End 回底跟随
            self.assertTrue(win.follow)
            await keys("\x0f")                               # Ctrl+O 收起最新块
            self.assertFalse(screen.model.is_expanded(target))

        self._run(scenario)

    def test_input_stays_at_bottom_and_help_preserves_draft(self):
        from prompt_toolkit.layout.controls import BufferControl
        from prompt_toolkit.data_structures import Size

        async def scenario(screen, app, win, keys, sent):
            input_window = app.layout.current_window
            self.assertIsInstance(input_window.content, BufferControl)
            def cursor_y():
                return app.renderer._last_screen.cursor_positions[input_window].y
            def snapshot_text():
                return "\n".join("".join(cell.char for _, cell in sorted(row.items()))
                                  for _, row in sorted(app.renderer._last_screen.data_buffer.items()))
            screen.print_block(["short conversation"], message_id=910)
            await keys("")
            self.assertEqual(app.output.get_size().rows - 2, cursor_y())
            await keys("draft text")
            screen.print_block([f"history line {i}" for i in range(200)], message_id=911)
            await keys("")
            self.assertEqual(app.output.get_size().rows - 2, cursor_y())
            await keys("\x1bOP")  # F1
            self.assertIn("操作帮助", snapshot_text())
            self.assertEqual("draft text", input_window.content.buffer.text)
            self.assertEqual(app.output.get_size().rows - 2, cursor_y())
            with mock.patch.object(app.output, "get_size", return_value=Size(rows=18, columns=48)):
                await keys("")
                app.invalidate()
                await keys("")
                self.assertEqual(16, cursor_y())
                self.assertEqual("draft text", input_window.content.buffer.text)
            await keys("\x1bOP")
            self.assertNotIn("操作帮助", snapshot_text())
            self.assertEqual("draft text", input_window.content.buffer.text)
            input_window.content.buffer.reset()  # 测试完草稿保留后，交还退出夹具。

        self._run(scenario)

    def test_output_archive_pages_preserve_draft_and_do_not_execute_content(self):
        import tempfile
        from pathlib import Path
        from xgent_app import output_archive
        from xgent_app.agent_presenter import build_run_presentation

        with tempfile.TemporaryDirectory(prefix="xgent-tui-archive-") as temp:
            root = Path(temp)
            archive = root / "result.txt"
            text = "FIRST_PAGE\n" + "中" * 30000 + "\nLAST_PAGE <script>not executed</script>"
            archive.write_text(text, encoding="utf-8")
            async def scenario(screen, app, win, keys, sent):
                draft = app.current_buffer
                await keys("draft preserved")
                result = build_run_presentation({"output": "summary", "output_path": str(archive)})
                screen.print_block(screen.renderer().render_message(result, parse_mode="HTML"), message_id=51)
                await keys("\x1bOR", .4)  # F3
                self.assertIsNot(app.current_buffer, draft)
                self.assertIn("FIRST_PAGE", app.current_buffer.text)
                self.assertNotIn("LAST_PAGE", app.current_buffer.text)
                await keys("\x1b[17~", .4)  # F6
                self.assertIn("LAST_PAGE", app.current_buffer.text)
                self.assertNotIn("FIRST_PAGE", app.current_buffer.text)
                await keys("\x1b[15~", .4)  # F5
                self.assertIn("FIRST_PAGE", app.current_buffer.text)
                self.assertLessEqual(len(app.current_buffer.text.encode("utf-8")), output_archive.OUTPUT_PAGE_BYTES)
                await keys("\x1bOR")
                self.assertIs(app.current_buffer, draft)
                self.assertEqual("draft preserved", draft.text)
                self.assertEqual([], sent)
                draft.reset()
            with mock.patch.object(output_archive, 'DEFAULT_OUTPUT_ROOT', root):
                self._run(scenario)

    def test_getchat_history_block_expands(self):
        async def scenario(screen, app, win, keys, sent):
            # /getchat 的历史行没有 message_id，照样能选中展开
            screen.print_block(screen.renderer().render_text(
                ProtocolParser.render_folded_html(
                    protocol_block("run-x", "\n".join(f"l{i}" for i in range(10)), NONCE_A),
                    prose_renderer=lambda t: t), "HTML"))
            await keys("\x1b", 0.8)                          # Esc 到输出区
            await keys("\t")
            await keys("\r")
            self.assertTrue(screen.model.is_expanded(screen.model.foldable_targets()[-1]))
            await keys("", 0.8)                          # Esc 取消选中

        self._run(scenario)

    def test_typing_enters_input_and_submit_returns_to_browse(self):
        async def scenario(screen, app, win, keys, sent):
            self.assertFalse(app.layout.has_focus(win))       # 一进来就在输入框
            await keys("/he", 0.4)
            state = app.current_buffer.complete_state
            self.assertEqual(["/help"], [c.text for c in state.completions])
            await keys("\x15hello\x1b\r world", 0.8)       # Alt+Enter 换行
            self.assertEqual("hello\n world", app.current_buffer.text)
            await keys("\r", 0.3)
            self.assertEqual(["hello\n world"], sent)
            self.assertFalse(app.layout.has_focus(win))       # 发送后仍在输入框

        self._run(scenario)




    def test_f2_native_mode_supports_arrows_pages_and_preserves_drafts(self):
        async def scenario(screen, app, win, keys, sent):
            for i in range(200):
                screen.print_plain(f"history {i}")
            await keys("")
            input_window = app.layout.current_window
            input_buffer = app.current_buffer
            input_buffer.history.append_string("previous input")
            with mock.patch.object(app.output, "write_raw", wraps=app.output.write_raw) as raw:
                await keys("\x1bOQ")
                self.assertFalse(app.mouse_support())
                self.assertTrue(app.layout.has_focus(win))
                self.assertIn(mock.call("\x1b[?1007h"), raw.call_args_list)
                top = win.vertical_scroll
                await keys("\x1b[A")
                self.assertEqual(top - 1, win.vertical_scroll)
                top = win.vertical_scroll
                await keys("\x1b[5~")
                self.assertLess(win.vertical_scroll, top)
                top = win.vertical_scroll
                await keys("\x1b[6~")
                self.assertGreater(win.vertical_scroll, top)
                await keys("unsent draft")
                self.assertTrue(app.layout.has_focus(input_window))
                top = win.vertical_scroll
                await keys("\x1bOA")
                self.assertEqual(top - 1, win.vertical_scroll)
                self.assertEqual("unsent draft", input_buffer.text)
                await keys("\x1bOB")
                self.assertEqual(top, win.vertical_scroll)
                await keys("\x1bOQ")
                self.assertTrue(app.mouse_support())
                self.assertIn(mock.call("\x1b[?1007l"), raw.call_args_list)
                self.assertEqual("unsent draft", input_buffer.text)
            input_buffer.reset()  # End-to-end harness exits with two idle Ctrl+C keys.

        self._run(scenario)


class PtMouseTests(unittest.TestCase):
    """真 App + 管道输入里的 SGR 鼠标序列：单击展开、拖选复制、滚轮滚动。"""

    def setUp(self):
        if importlib.util.find_spec("prompt_toolkit") is None:
            self.skipTest("prompt_toolkit 未安装")

    def _run(self, scenario, turn_active=lambda: False):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.application.current import get_app
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        screen = cli_tui.PtScreen(palette=Palette(True), width=80)

        async def _dispatch(_text):
            return False

        hooks = cli_tui.TuiHooks(dispatch=_dispatch, turn_active=turn_active)
        copied = []

        async def _drive():
            with create_pipe_input() as pinp:
                with create_app_session(input=pinp, output=DummyOutput()):
                    task = asyncio.ensure_future(cli_tui.run_tui(screen, hooks))
                    await asyncio.sleep(0.3)
                    app = get_app()
                    win = [w for w in app.layout.find_all_windows() if hasattr(w, "scroll_by")][0]

                    async def keys(text, wait=0.25):
                        pinp.send_text(text)
                        await asyncio.sleep(wait)

                    def at(row):   # 内容行号 → 屏幕行（1 起，顶栏占第 1 行）
                        top = win.max_top if win.follow else win.vertical_scroll
                        return row - top + 2

                    await scenario(screen, app, win, keys, at)
                    await keys("\x1b", 0.8)
                    await keys("\x03\x03")
                    await asyncio.wait_for(task, timeout=5)

        with mock.patch.object(cli_tui, "copy_to_clipboard",
                               side_effect=lambda text, *a, **k: copied.append(text) or True):
            asyncio.run(_drive())
        return copied

    def _block(self, screen, n=10, mid=7):
        screen.print_block(screen.renderer().render_text(
            ProtocolParser.render_folded_html(
                "说明\n" + protocol_block("run-x", "\n".join(f"line{i}" for i in range(n)), NONCE_A),
                prose_renderer=lambda t: t), "HTML"), message_id=mid)

    def test_click_block_toggles_in_place(self):
        async def scenario(screen, app, win, keys, at):
            self._block(screen)
            await keys("")
            rows = _plain(screen.model.render_rows())
            hi = [i for i, r in enumerate(rows) if "run-x" in r][0]
            y = at(hi)
            await keys(f"\x1b[<0;6;{y}M\x1b[<0;6;{y}m")     # 单击块头
            target = screen.model.foldable_targets()[0]
            self.assertTrue(screen.model.is_expanded(target))
            y = at(hi + 5)
            await keys(f"\x1b[<0;6;{y}M\x1b[<0;6;{y}m")     # 单击展开后的正文 → 收起
            self.assertFalse(screen.model.is_expanded(target))

        self._run(scenario)

    def test_drag_select_copies_text(self):
        async def scenario(screen, app, win, keys, at):
            self._block(screen)
            screen.model.toggle(*screen.model.foldable_targets()[0])  # 紧凑模式先展开再拖选
            await keys("")
            rows = _plain(screen.model.render_rows())
            r0 = [i for i, r in enumerate(rows) if r.strip() == "│ line0"][0]
            y0, y1 = at(r0), at(r0 + 1)
            await keys(f"\x1b[<0;5;{y0}M\x1b[<32;9;{y0}M\x1b[<32;9;{y1}M\x1b[<0;9;{y1}m")
            self.assertIsNone(screen.model.foldable_targets() and None)
            self.assertTrue(screen.model.is_expanded(screen.model.foldable_targets()[0]))  # 拖选不触发折叠

        copied = self._run(scenario)
        self.assertEqual(1, len(copied))
        self.assertTrue(copied[0].startswith("line0"))
        self.assertIn("\nline1", copied[0])            # 第二行不带「  │ 」装饰

    def test_wheel_scrolls_output(self):
        async def scenario(screen, app, win, keys, at):
            for i in range(120):
                screen.print_plain(f"row {i}")
            await keys("")
            self.assertTrue(win.follow)
            await keys("\x1b[<64;5;10M\x1b[<64;5;10M")      # 滚轮上滚两格
            self.assertFalse(win.follow)
            top = win.vertical_scroll
            await keys("\x1b[<65;5;10M")                      # 下滚一格
            self.assertEqual(top + 3, win.vertical_scroll)

        self._run(scenario)


    def test_drag_wheel_and_spinner_never_rebuild_unchanged_history(self):
        activity = {"busy": False}
        async def scenario(screen, app, win, keys, at):
            lines = [f"\x1b[32mrow {i}: 中文 mixed text\x1b[0m" for i in range(10000)]
            screen.print_block(lines, message_id=901)
            await keys("")
            with mock.patch.object(screen.model, "render_rows", wraps=screen.model.render_rows) as flatten, \
                 mock.patch.object(cli_tui, "_line_fragments", wraps=cli_tui._line_fragments) as parse:
                await keys("\x1b[<0;5;6M")
                self.assertFalse(win.follow)
                await keys("\x1b[<32;12;7M\x1b[<32;15;8M")
                await keys("\x1b[<64;5;10M")
                top = win.vertical_scroll
                activity["busy"] = True
                await keys("", 0.35)
                self.assertEqual(top, win.vertical_scroll)
                await keys("\x1b[<0;15;8m")
                activity["busy"] = False
                await keys("")
                flatten.assert_not_called()
                parsed_lines = {call.args[0] for call in parse.call_args_list}
                self.assertLess(len(parsed_lines), 200)
                self.assertTrue(parsed_lines)
            with mock.patch.object(screen.model, "render_rows", wraps=screen.model.render_rows) as flatten:
                screen.update_block(lines + ["new tail"], message_id=901)
                await keys("")
                flatten.assert_called_once()

        self._run(scenario, turn_active=lambda: activity["busy"])

    def test_slow_clipboard_does_not_block_scrolling(self):
        import threading
        started, release = threading.Event(), threading.Event()
        async def scenario(screen, app, win, keys, at):
            screen.print_block([f"row {i}" for i in range(200)], message_id=902)
            await keys("")
            def slow_copy(text):
                started.set()
                release.wait(timeout=3)
                return True
            with mock.patch.object(cli_tui, "copy_to_clipboard", side_effect=slow_copy):
                try:
                    await keys("\x1b[<0;3;6M\x1b[<32;6;7M\x1b[<0;6;7m", 0.1)
                    self.assertTrue(started.is_set())
                    top = win.vertical_scroll
                    await keys("\x1b[<64;5;10M", 0.1)
                    self.assertLess(win.vertical_scroll, top)
                    self.assertFalse(release.is_set())
                finally:
                    release.set()
                    await keys("")

        self._run(scenario)


    def test_chinese_drag_selection_keeps_character_boundaries(self):
        async def scenario(screen, app, win, keys, at):
            screen.print_block(["  中文 abc", "second line"], message_id=903)
            await keys("")
            y = at(0)
            await keys(f"\x1b[<0;3;{y}M\x1b[<32;5;{y}M\x1b[<0;5;{y}m")
        self.assertEqual(["中文"], self._run(scenario))

    def test_wrapped_line_drag_does_not_trigger_spurious_edge_scroll(self):
        async def scenario(screen, app, win, keys, at):
            screen.print_block(["0123456789" * 40, "next row"], message_id=904)
            await keys("")
            info = win.render_info
            wrapped = [(screen_y, column) for screen_y, (row, column)
                       in info.visible_line_to_row_col.items() if row == 0 and screen_y > 0]
            self.assertGreaterEqual(len(wrapped), 2)
            screen_y, column = wrapped[0]
            y = screen_y + 2
            await keys(f"\x1b[<0;3;{y}M\x1b[<32;8;{y}M", 0.15)
            await keys(f"\x1b[<0;8;{y}m")
            return column
        copied = self._run(scenario)
        self.assertEqual(1, len(copied))
        self.assertEqual(6, len(copied[0]))


if __name__ == "__main__":
    unittest.main()
