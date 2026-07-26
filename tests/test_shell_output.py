import unittest

from xgent_app.shell_output import (
    build_run_notice,
    format_shell_context_output,
    format_shell_display_output,
)
from xgent_app.text_utils import clip_middle_text


class ShellOutputTests(unittest.TestCase):
    def test_clip_middle_text_keeps_both_ends(self):
        result = clip_middle_text("0123456789" * 20, 160, "输出")
        self.assertLessEqual(len(result), 160)
        self.assertIn("0123", result)
        self.assertIn("6789", result)
        self.assertIn("输出已省略", result)

    def test_running_display_keeps_latest_output(self):
        result = format_shell_display_output("old\n" * 1000 + "latest", True, 100)
        self.assertLessEqual(len(result), 140)
        self.assertIn("latest", result)
        self.assertIn("最新 shell 输出", result)

    def test_context_and_result_are_separate_formats(self):
        context = format_shell_context_output("x" * 13000, False, 12000)
        notice = build_run_notice({
            "command": "echo ok",
            "success": True,
            "return_code": 0,
            "output": "ok",
        })
        self.assertLess(len(context), 13000)
        self.assertIn("上下文输出:", notice)
        self.assertIn("echo ok", notice)


if __name__ == "__main__":
    unittest.main()
