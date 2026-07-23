import unittest

from bot_app.protocols import ProtocolParser


MARKER_A = "AGENT_END_0123456789ABCDEF"
MARKER_B = "AGENT_END_FEDCBA9876543210"


class ProtocolParserTests(unittest.TestCase):
    def test_extracts_multiple_v2_protocols_in_order(self):
        response = (
            "先说明\n"
            f"```run-x <<{MARKER_A}\n"
            "echo ok\n"
            f"{MARKER_A}\n"
            "```\n"
            "中间文字\n"
            f"```read-x:/tmp/demo.txt:1-3 <<{MARKER_B}\n"
            f"{MARKER_B}\n"
            "```\n"
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(["run", "read"], [block["type"] for block in blocks])
        self.assertEqual("echo ok", blocks[0]["body"])
        self.assertEqual("/tmp/demo.txt:1-3", blocks[1]["path"])

    def test_body_is_opaque_until_matching_end_sequence(self):
        response = (
            f"```run-x <<{MARKER_A}\n"
            "python - <<'PY'\n"
            "```read-x <<AGENT_END_1111111111111111\n"
            "/tmp/should-not-run\n"
            "AGENT_END_1111111111111111\n"
            "```\n"
            f"{MARKER_A}\n"
            "```\n"
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(1, len(blocks))
        self.assertEqual("run", blocks[0]["type"])
        self.assertIn("```read-x", blocks[0]["body"])
        self.assertIn("/tmp/should-not-run", blocks[0]["body"])

    def test_marker_without_closing_fence_remains_body(self):
        response = (
            f"```file-x:/tmp/demo.md <<{MARKER_A}\n"
            "before\n"
            f"{MARKER_A}\n"
            "not-a-closing-fence\n"
            "after\n"
            f"{MARKER_A}\n"
            "```\n"
        )
        block = ProtocolParser.extract_protocol_blocks(response)[0]
        self.assertEqual("file", block["type"])
        self.assertEqual("/tmp/demo.md", block["path"])
        self.assertIn(f"{MARKER_A}\nnot-a-closing-fence", block["body"])

    def test_all_v2_protocol_tags_keep_existing_execution_contract(self):
        cases = [
            ("run-x", "echo ok", "run", "", "echo ok"),
            ("shell-x", "sleep 1", "shell", "", "sleep 1"),
            ("stdin-x:shell_1", "line: echo ok", "stdin", "shell_1", "line: echo ok"),
            ("shellread-x:shell_1", "check", "shellread", "shell_1", "check"),
            ("shellkill-x:shell_1", "done", "shellkill", "shell_1", "done"),
            ("trigger-x:show", "", "trigger", "show", ""),
            ("sendfile-x", "/tmp/demo.txt", "sendfile", "", "/tmp/demo.txt"),
            ("read-x", "/tmp/demo.txt:1-2", "read", "", "/tmp/demo.txt:1-2"),
            ("read-x:/tmp/demo.txt:1-2", "", "read", "/tmp/demo.txt:1-2", ""),
            ("edit-x", "edit body", "edit", "", "edit body"),
            ("grep-x", "grep body", "grep", "", "grep body"),
            ("media-x", "draw image", "media", "", "draw image"),
            ("file-x:/tmp/demo.txt", "file body", "file", "/tmp/demo.txt", "file body"),
            ("file-x:base64:/tmp/demo.bin", "SGVsbG8=", "file_base64", "/tmp/demo.bin", "SGVsbG8="),
        ]
        for index, (tag, body, expected_type, expected_path, expected_body) in enumerate(cases):
            marker = f"AGENT_END_{index:016X}"
            response = f"```{tag} <<{marker}\n{body}\n{marker}\n```"
            with self.subTest(tag=tag):
                block = ProtocolParser.extract_protocol_blocks(response)[0]
                self.assertEqual(expected_type, block["type"])
                self.assertEqual(expected_path, block["path"])
                self.assertEqual(expected_body, block["body"])

    def test_duplicate_marker_is_rejected_after_first_block(self):
        response = (
            f"```run-x <<{MARKER_A}\nfirst\n{MARKER_A}\n```\n"
            f"```run-x <<{MARKER_A}\nsecond\n{MARKER_A}\n```"
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(1, len(blocks))
        self.assertEqual("first", blocks[0]["body"])

    def test_old_fenced_protocol_is_not_executable(self):
        response = "before\n```run-x\necho unsafe\n```\nafter"
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))

    def test_old_file_heredoc_marker_is_not_executable(self):
        response = "```file-x:/tmp/demo.txt <<EOF\ncontent\nEOF\n```"
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))

    def test_short_or_common_marker_is_rejected(self):
        response = "```run-x <<EOF\necho unsafe\nEOF\n```"
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))

    def test_strip_protocols_preserves_user_facing_text(self):
        response = (
            "before\n"
            f"```run-x <<{MARKER_A}\n"
            "echo ok\n"
            f"{MARKER_A}\n"
            "```\n\n"
            "after"
        )
        self.assertEqual("before\n\nafter", ProtocolParser.strip_protocol_blocks(response))

    def test_incomplete_v2_protocol_is_left_as_plain_text(self):
        response = f"before\n```run-x <<{MARKER_A}\necho ok"
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))

    def test_marker_without_final_fence_is_left_as_plain_text(self):
        response = f"before\n```run-x <<{MARKER_A}\necho ok\n{MARKER_A}"
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))

    def test_windows_line_endings_are_supported(self):
        response = (
            f"```run-x <<{MARKER_A}\r\n"
            "echo ok\r\n"
            f"{MARKER_A}\r\n"
            "```"
        )
        block = ProtocolParser.extract_protocol_blocks(response)[0]
        self.assertEqual("run", block["type"])
        self.assertEqual("echo ok", block["body"])


if __name__ == "__main__":
    unittest.main()
