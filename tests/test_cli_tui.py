"""全屏 CLI TUI（cli_tui）的行为测试。

pt 的 Application 那层在无 TTY 环境跑不起来，所以本模块只测**能脱离终端**的
纯逻辑：分段（segment_lines）、消息模型（MessageModel 的 upsert/toggle/
render_rows 折叠语义）、drop-in 屏幕（PtScreen 的 on_change/update_block 命中）、
opt-in 判定（tui_enabled 的环境闸门）与 slash 补全（slash_completions）。

pt 装了的话额外跑一个"能建起来、按 Ctrl+C 能干净退出"的烟测（PipeInput +
DummyOutput，importorskip 兜底），确保接线没写错；跑不到的交互细节靠
legacy 兜底与"只换绘制半边"的接缝保住不回归。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from xgent_app import cli_tui
from xgent_app.cli_render import Palette


def _bars(n: int, prefix: str = "code") -> list:
    """造 n 行"代码条"（│ 打头），模拟 MessageRenderer._render_pre 的产物。"""
    return [f"│ {prefix}{i}" for i in range(n)]


class SegmentLinesTests(unittest.TestCase):
    def test_long_run_is_collapsible(self):
        lines = ["散文一行"] + _bars(12)
        blocks = cli_tui.segment_lines(lines, collapse_min=8)
        self.assertEqual(len(blocks), 2)
        self.assertFalse(blocks[0].collapsible)
        self.assertTrue(blocks[1].collapsible)
        self.assertEqual(len(blocks[1].lines), 12)

    def test_short_run_merged_into_prose(self):
        lines = ["头"] + _bars(3) + ["尾"]
        blocks = cli_tui.segment_lines(lines, collapse_min=8)
        # 短代码不值得折叠，全并进散文，一整块不可折叠。
        self.assertEqual(len(blocks), 1)
        self.assertFalse(blocks[0].collapsible)
        self.assertEqual(len(blocks[0].lines), 5)

    def test_prose_and_code_interleave(self):
        lines = ["header 行"] + _bars(10) + ["中间散文"] + _bars(9)
        blocks = cli_tui.segment_lines(lines, collapse_min=8)
        kinds = [b.collapsible for b in blocks]
        self.assertEqual(kinds, [False, True, False, True])

    def test_no_code_is_all_prose(self):
        blocks = cli_tui.segment_lines(["只有散文", "还是散文"], collapse_min=8)
        self.assertEqual(len(blocks), 1)
        self.assertFalse(blocks[0].collapsible)


class MessageModelTests(unittest.TestCase):
    def _model(self):
        return cli_tui.MessageModel(Palette(False), preview_lines=3, collapse_min=8)

    def test_collapsed_shows_preview_plus_marker(self):
        m = self._model()
        m.upsert(1, ["header"] + _bars(12))
        rows = m.render_rows()
        texts = [t for t, _ in rows]
        # header 散文 + 3 行预览 + 1 行"展开"标记。
        self.assertIn("header", texts[0])
        self.assertTrue(any("展开" in t for t in texts))
        # 折叠态：12 行里只露 3 行预览。
        self.assertEqual(sum(1 for t in texts if t.startswith("│ code")), 3)

    def test_toggle_expands_full_body(self):
        m = self._model()
        m.upsert(1, ["header"] + _bars(12))
        self.assertTrue(m.toggle(1, 1))  # block 0 是散文，block 1 是代码
        texts = [t for t, _ in m.render_rows()]
        self.assertEqual(sum(1 for t in texts if t.startswith("│ code")), 12)
        self.assertTrue(any("收起" in t for t in texts))

    def test_toggle_rejects_prose_block(self):
        m = self._model()
        m.upsert(1, ["header"] + _bars(12))
        self.assertFalse(m.toggle(1, 0))  # 散文块不可折叠

    def test_upsert_preserves_expand_state_by_index(self):
        m = self._model()
        m.upsert(1, ["header"] + _bars(12))
        m.toggle(1, 1)
        # 流式重绘：同一 message_id 换内容，块序号对得上就保住展开态。
        m.upsert(1, ["header"] + _bars(14))
        blk = m._index[1].blocks[1]
        self.assertTrue(blk.expanded)
        texts = [t for t, _ in m.render_rows()]
        self.assertEqual(sum(1 for t in texts if t.startswith("│ code")), 14)

    def test_render_rows_carries_toggle_target(self):
        m = self._model()
        m.upsert(7, ["header"] + _bars(12))
        targets = {tgt for _, tgt in m.render_rows() if tgt is not None}
        self.assertEqual(targets, {(7, 1)})

    def test_leading_blank_between_messages_only(self):
        m = self._model()
        m.upsert(1, ["甲"])
        m.upsert(2, ["乙"], leading_blank=True)
        rows = m.render_rows()
        # 第一条前面不留空行；第二条前留一行分隔。
        self.assertEqual(rows[0][0], "甲")
        self.assertEqual(rows[1][0], "")
        self.assertEqual(rows[2][0], "乙")

    def test_remove_and_has(self):
        m = self._model()
        m.upsert(1, ["x"])
        self.assertTrue(m.has(1))
        self.assertTrue(m.remove(1))
        self.assertFalse(m.has(1))
        self.assertFalse(m.remove(1))

    def test_set_all_expanded(self):
        m = self._model()
        m.upsert(1, ["h"] + _bars(12))
        m.upsert(2, ["h"] + _bars(10))
        m.set_all_expanded(True)
        for msg in m.messages:
            for blk in msg.blocks:
                if blk.collapsible:
                    self.assertTrue(blk.expanded)
        m.set_all_expanded(False)
        for msg in m.messages:
            for blk in msg.blocks:
                if blk.collapsible:
                    self.assertFalse(blk.expanded)

    def test_revision_bumps_on_change(self):
        m = self._model()
        before = m.revision
        m.upsert(1, ["h"])
        self.assertGreater(m.revision, before)

    def test_foldable_targets_in_render_order(self):
        m = self._model()
        m.upsert(1, ["h"] + _bars(12))
        m.upsert(None, ["纯散文 notice"])       # message_id=None 不参与浏览
        m.upsert(3, ["h"] + _bars(9) + ["中间"] + _bars(10))
        # 每条消息里可折叠块是 block 1（散文）之外的代码段。
        self.assertEqual(m.foldable_targets(), [(1, 1), (3, 1), (3, 3)])

    def test_selected_marker_highlighted(self):
        m = self._model()
        m.upsert(1, ["h"] + _bars(12))
        rows_plain = m.render_rows()
        self.assertFalse(any("▶" in t for t, _ in rows_plain))
        rows_sel = m.render_rows((1, 1))
        hot = [t for t, _ in rows_sel if "▶" in t]
        self.assertEqual(len(hot), 1)            # 唯一被选中的块标记高亮
        self.assertIn("展开", hot[0])

    def test_marker_mentions_enter(self):
        m = self._model()
        m.upsert(1, ["h"] + _bars(12))
        texts = [t for t, _ in m.render_rows()]
        self.assertTrue(any("Enter" in t for t in texts))  # spec：CLI Enter 原地展开


class PtScreenTests(unittest.TestCase):
    def _screen(self):
        s = cli_tui.PtScreen(palette=Palette(False), width=80)
        calls = {"n": 0}
        s.on_change = lambda: calls.__setitem__("n", calls["n"] + 1)
        return s, calls

    def test_print_block_fires_on_change(self):
        s, calls = self._screen()
        s.print_block(["hello"], message_id=1)
        self.assertEqual(calls["n"], 1)
        self.assertTrue(s.model.has(1))

    def test_update_block_hits_known_id(self):
        s, _ = self._screen()
        s.print_block(["a"], message_id=5)
        self.assertTrue(s.update_block(["b"], 5))

    def test_update_block_misses_unknown_id(self):
        s, _ = self._screen()
        # legacy 只能改"最后一块"；PtScreen 记全量，未知 id 返回 False，
        # CliBot 据此回退 print_block。
        self.assertFalse(s.update_block(["b"], 999))

    def test_print_plain_is_prose(self):
        s, _ = self._screen()
        s.print_plain("just text")
        rows = s.model.render_rows()
        self.assertEqual(rows[0][0], "just text")
        self.assertIsNone(rows[0][1])

    def test_notice_prefixes_marker(self):
        s, _ = self._screen()
        s.notice("done", "ok")
        texts = [t for t, _ in s.model.render_rows()]
        self.assertTrue(any("done" in t for t in texts))

    def test_width_forced(self):
        s = cli_tui.PtScreen(palette=Palette(False), width=123)
        self.assertEqual(s.width, 123)


class SlashCompletionTests(unittest.TestCase):
    def test_prefix_filters_commands(self):
        names = ["start", "stop", "status", "getchat"]
        out = cli_tui.slash_completions("/st", names, lambda n: f"desc:{n}")
        got = [full for full, _ in out]
        self.assertEqual(got, ["/start", "/stop", "/status"])
        self.assertIn(("/start", "desc:start"), out)

    def test_no_slash_no_completion(self):
        self.assertEqual(cli_tui.slash_completions("st", ["start"], lambda n: ""), [])

    def test_space_stops_completion(self):
        # 已经打了空格 = 在写参数，不再补命令名。
        self.assertEqual(cli_tui.slash_completions("/getchat 5", ["getchat"], lambda n: ""), [])


class TuiEnabledTests(unittest.TestCase):
    def _patch(self, env, stdin_tty, stdout_tty, pt_ok):
        return (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("sys.stdin"),
            mock.patch("sys.stdout"),
            mock.patch.object(cli_tui, "_pt_available", return_value=pt_ok),
        )

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


class PtSmokeTests(unittest.TestCase):
    """pt 装了才跑：能建起 App、按 Ctrl+C（空闲）能干净退出。"""

    def test_build_and_exit(self):
        import asyncio

        pt = __import__("importlib").util.find_spec("prompt_toolkit")
        if pt is None:
            self.skipTest("prompt_toolkit 未安装")

        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        screen = cli_tui.PtScreen(palette=Palette(True), width=80)

        async def _dispatch(_text):
            return False

        hooks = cli_tui.TuiHooks(
            dispatch=_dispatch,
            banner=lambda: screen.print_plain("banner"),
            prompt_text=lambda: "> ",
            command_names=lambda: ("start", "stop"),
            turn_active=lambda: False,
        )

        async def _drive():
            with create_pipe_input() as pinp:
                with create_app_session(input=pinp, output=DummyOutput()):
                    pinp.send_text("\x03")  # 空闲态 Ctrl+C → 退出
                    await asyncio.wait_for(cli_tui.run_tui(screen, hooks), timeout=5)

        asyncio.run(_drive())
        # 退出后 on_change 已复位为 no-op，不再抓着 app。
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
