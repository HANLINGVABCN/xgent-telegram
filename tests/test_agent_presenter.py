from __future__ import annotations

import unittest

from xgent_app.agent_presenter import (
    build_edit_presentation,
    build_grep_presentation,
    build_run_presentation,
    build_shell_presentation,
    build_standard_operation_presentation,
)


class AgentPresenterTests(unittest.TestCase):
    def test_edit_presentation_matches_legacy_html(self):
        self.assertEqual(
            build_edit_presentation({"success": True, "notice": "a < b"}),
            "✏️ <b>Agent Edit</b>\n<pre>a &lt; b</pre>",
        )

    def test_grep_presentation_matches_legacy_html_and_limit(self):
        result = {"success": False, "notice": "x" * 2100, "hits": 4}
        text = build_grep_presentation(result)

        self.assertTrue(text.startswith("⚠️ <b>Agent Grep</b> 命中 4 处\n<pre>"))
        self.assertEqual(text.count("x"), 2000)
        self.assertTrue(text.endswith("</pre>"))

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
                "<pre>out</pre>"
            ),
        )

    def test_run_presentation_matches_legacy_html(self):
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
                "<pre>&lt;done&gt;</pre>"
            ),
        )

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
