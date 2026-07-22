from __future__ import annotations

import unittest

from bot_app.agent_context import (
    build_edit_context_message,
    build_grep_context_message,
    build_read_context_message,
    build_run_context_message,
)
from bot_app.agent_results import (
    failed_result,
    normalize_edit_result,
    normalize_grep_result,
    normalize_read_result,
    normalize_run_result,
)
from bot_app.shell_output import build_run_notice


class AgentResultsTests(unittest.TestCase):
    def test_normalization_keeps_legacy_fields_and_adds_contract(self):
        raw = {"success": True, "output": "changed", "path": "/tmp/a.py"}
        result = normalize_edit_result(raw)

        self.assertEqual(result["path"], "/tmp/a.py")
        self.assertEqual(result["kind"], "edit")
        self.assertEqual(result["notice"], "changed")
        self.assertTrue(result["should_continue"])
        self.assertEqual(
            result["context_message"], build_edit_context_message("changed")
        )
        self.assertEqual(raw, {"success": True, "output": "changed", "path": "/tmp/a.py"})

    def test_read_preserves_multimodal_message(self):
        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": "notice"},
                {"type": "image", "mime_type": "image/png", "data": "abc"},
            ],
        }
        result = normalize_read_result({"notice": "notice", "message": message})

        self.assertEqual(result["context_message"], build_read_context_message({
            "notice": "notice", "message": message
        }))
        self.assertEqual(result["kind"], "read")

    def test_grep_keeps_hits_for_presenter(self):
        result = normalize_grep_result(
            {"success": True, "output": "a.py:1:hit", "hits": 1}
        )

        self.assertEqual(result["hits"], 1)
        self.assertEqual(result["notice"], "a.py:1:hit")
        self.assertEqual(
            result["context_message"],
            build_grep_context_message("a.py:1:hit"),
        )

    def test_run_keeps_command_fields_and_builds_context(self):
        raw = {
            "success": True,
            "command": "echo hi",
            "output": "hi",
            "return_code": 0,
            "timed_out": False,
            "stopped": False,
            "output_path": "output.log",
            "output_bytes": 3,
            "elapsed_seconds": 0.01,
        }
        result = normalize_run_result(raw)
        expected_notice = build_run_notice(raw)

        self.assertEqual(result["command"], "echo hi")
        self.assertEqual(result["notice"], expected_notice)
        self.assertEqual(
            result["context_message"],
            build_run_context_message(expected_notice),
        )

    def test_failed_result_is_also_continuation_safe(self):
        result = failed_result("grep", "[grep结果] 执行异常: bad")

        self.assertFalse(result["success"])
        self.assertEqual(result["output"], "[grep结果] 执行异常: bad")
        self.assertEqual(result["context_message"]["role"], "user")


if __name__ == "__main__":
    unittest.main()
