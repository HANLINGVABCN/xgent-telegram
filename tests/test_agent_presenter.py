from __future__ import annotations

import re
import unittest

from xgent_app.agent_presenter import (
    build_edit_presentation,
    build_grep_presentation,
    build_run_presentation,
    build_shell_presentation,
    build_standard_operation_presentation,
    fold_output_block,
)
from xgent_app.cli_render import _FOLD_HEADER_RE


class AgentPresenterTests(unittest.TestCase):
    def test_fold_output_block_wraps_pre_in_expandable_blockquote(self):
        # 结果卡片里本来直接显示的 <pre> 代码块，被包成三端一致的可折叠块；
        # 块头 "{icon} {kind}-x · N 行" 对齐 CLI 折叠识别形态，正文转义原样保留。
        self.assertEqual(
            fold_output_block("a < b\nc", kind="run", icon="⌨️"),
            "<blockquote expandable><b>⌨️ run-x</b> · 2 行\n"
            "<pre>a &lt; b\nc</pre></blockquote>",
        )

    def test_fold_header_matches_cli_fold_detection_regex(self):
        # CLI 端（cli_tui._HEADER_RE ← cli_render._FOLD_HEADER_RE 单一真源）靠这个
        # 正则把块头认成可折叠块。直接引真源正则来断言，避免测试里再抄一份、
        # 三处失步却测不出来。块头纯文本（去掉 <b> 后）必须命中，否则 CLI 三端
        # 就不一致了。
        for kind, icon in [
            ("run", "⌨️"), ("shell", "🖥️"), ("grep", "🔎"),
            ("search", "🌐"), ("fetch", "📄"), ("edit", "✏️"),
        ]:
            html = fold_output_block("l1\nl2\nl3\nl4", kind=kind, icon=icon)
            header = re.sub(r"</?b>", "", html.split("\n", 1)[0])
            header = header.replace("<blockquote expandable>", "")
            self.assertRegex(header, _FOLD_HEADER_RE)

    def test_edit_presentation_folds_output_block(self):
        self.assertEqual(
            build_edit_presentation({"success": True, "notice": "a < b"}),
            "✏️ <b>Agent Edit</b>\n"
            "<blockquote expandable><b>✏️ edit-x</b> · 1 行\n"
            "<pre>a &lt; b</pre></blockquote>",
        )

    def test_grep_presentation_folds_output_and_keeps_limit(self):
        result = {"success": False, "notice": "x" * 2100, "hits": 4}
        text = build_grep_presentation(result)

        self.assertTrue(
            text.startswith(
                "⚠️ <b>Agent Grep</b> 命中 4 处\n"
                "<blockquote expandable><b>🔎 grep-x</b> · 1 行\n<pre>"
            )
        )
        self.assertIn("x" * 2100, text)  # 非 Telegram 通道不继承结果卡片的显示截断。
        self.assertTrue(text.endswith("</pre></blockquote>"))

    def test_standard_presentation_dispatches_visible_kinds_only(self):
        result = {
            "kind": "edit",
            "success": True,
            "notice": "done",
        }
        self.assertEqual(
            build_standard_operation_presentation(result),
            build_edit_presentation(result),
        )
        self.assertIsNone(
            build_standard_operation_presentation({"kind": "read"})
        )

    def test_shell_presentation_escapes_status_and_keeps_wait_note(self):
        self.assertEqual(
            build_shell_presentation(
                action_label="启动会话",
                shell_result={
                    "success": True,
                    "running": True,
                    "pty": True,
                    "status": "running&ok",
                    "waited_seconds": 2,
                },
                session_id="abc<1>",
                display_output="out",
                pause_note="\n正在等待。",
            ),
            (
                "🖥️ <b>Agent Shell 启动会话</b>\n"
                "会话: <code>abc&lt;1&gt;</code> · 运行中 · PTY\n"
                "✅ 状态: <code>running&amp;ok</code>\n"
                "本次等待/捕获耗时: 2 秒\n正在等待。\n"
                "<blockquote expandable><b>🖥️ shell-x</b> · 1 行\n"
                "<pre>out</pre></blockquote>"
            ),
        )

    def test_run_presentation_folds_output_block(self):
        self.assertEqual(
            build_run_presentation(
                {
                    "success": True,
                    "output": "<done>",
                    "return_code": 0,
                    "output_path": "/tmp/a&b.log",
                }
            ),
            (
                "⌨️ <b>Agent Run</b>\n"
                # 返回码 0 必须显示成 "0"。以前 escape_html 用 falsy 判断，
                # 成功命令的返回码会渲染成空的 <code></code>，
                # 而喂给模型的上下文里却是 "0"，两边对不上。
                "✅ 返回码: <code>0</code>\n"
                "完整输出: <code>/tmp/a&amp;b.log</code>\n"
                # 返回码/完整输出留在块外原样不动，只有命令输出的代码块被折叠。
                '<blockquote expandable data-output-path="/tmp/a&amp;b.log"><b>⌨️ run-x</b> · 1 行\n'
                "<pre>&lt;done&gt;</pre></blockquote>"
            ),
        )

    def test_truncated_archive_is_not_described_as_complete(self):
        rendered = build_run_presentation({
            "success": True, "output": "summary", "return_code": 0,
            "output_path": "/tmp/output.txt", "archive_truncated": True,
        })
        self.assertIn("输出存档（已截断）", rendered)
        self.assertNotIn("完整输出:", rendered)

    def test_run_presentation_reports_archive_failure(self):
        """存档失败时 output_path 是 None，不能显示成空白。"""
        rendered = build_run_presentation(
            {
                "success": True,
                "output": "ok",
                "return_code": 0,
                "output_path": None,
            }
        )
        self.assertIn("存档失败", rendered)


if __name__ == "__main__":
    unittest.main()
