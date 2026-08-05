import unittest

from xgent_app.protocols import ProtocolParser


NONCE_A = "0123456789AB"
NONCE_B = "FEDCBA987654"


def protocol_block(tag: str, body: str, nonce: str) -> str:
    return (
        f"```{tag} <<AGENT_BEGIN_{nonce}\n"
        f"{body}\n"
        f"AGENT_END_{nonce}\n"
        "```"
    )


class ProtocolParserTests(unittest.TestCase):
    def test_extracts_multiple_protocols_in_order(self):
        response = (
            "先说明\n"
            f"{protocol_block('run-x', 'echo ok', NONCE_A)}\n"
            "中间文字\n"
            f"{protocol_block('read-x:/tmp/demo.txt:1-3', '', NONCE_B)}\n"
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(["run", "read"], [block["type"] for block in blocks])
        self.assertEqual("echo ok", blocks[0]["body"])
        self.assertEqual("/tmp/demo.txt:1-3", blocks[1]["path"])

    def test_search_and_fetch_blocks_keep_multiline_body(self):
        # 反斜杠不能出现在 f-string 表达式里（Python 3.12 前）。
        search_body = "nginx 502\nmax: 3"
        response = (
            f"{protocol_block('search-x', search_body, NONCE_A)}\n"
            f"{protocol_block('fetch-x', 'https://x.example', NONCE_B)}\n"
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(["search", "fetch"], [block["type"] for block in blocks])
        self.assertEqual(search_body, blocks[0]["body"])
        self.assertEqual("https://x.example", blocks[1]["body"])

    def test_nonce_accepts_arbitrary_characters(self):
        for label, nonce in [
            ("字母数字", "abc123XYZ"),
            ("中文", "随机标记甲乙丙"),
            ("emoji", "🎲🎯🎨🎪🎭🎬"),
            ("符号", "a!@#$%^&*()b"),
            ("点与括号", "a.b[c]d+e*f"),
            ("下划线连字符", "old_style-nonce"),
        ]:
            with self.subTest(label):
                blocks = ProtocolParser.extract_protocol_blocks(
                    protocol_block("run-x", "echo ok", nonce)
                )
                self.assertEqual(1, len(blocks), label)
                self.assertEqual("echo ok", blocks[0]["body"])

    def test_nonce_rejects_whitespace_and_backticks(self):
        # 空白会让结束标记无法独占一行精确比较；反引号与围栏语法冲突。
        for label, nonce in [
            ("含空格", "abc def"),
            ("含反引号", "abc```def"),
            ("全反引号", "``````"),
            ("含制表符", "abc\tdef"),
        ]:
            with self.subTest(label):
                self.assertEqual(
                    [],
                    ProtocolParser.extract_protocol_blocks(
                        protocol_block("run-x", "echo ok", nonce)
                    ),
                    label,
                )

    def test_mismatched_nonce_does_not_close_block(self):
        response = (
            "```run-x <<AGENT_BEGIN_中文标记甲乙丙\n"
            "echo ok\n"
            "AGENT_END_中文标记丁戊己\n"          # 不同标记，不能闭合
            "```"
        )
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))

    def test_body_is_opaque_until_matching_end_sequence(self):
        response = protocol_block(
            "run-x",
            "python - <<'PY'\n"
            "```read-x <<AGENT_BEGIN_111111111111\n"
            "/tmp/should-not-run\n"
            "AGENT_END_111111111111\n"
            "```",
            NONCE_A,
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(1, len(blocks))
        self.assertEqual("run", blocks[0]["type"])
        self.assertIn("```read-x", blocks[0]["body"])
        self.assertIn("/tmp/should-not-run", blocks[0]["body"])

    def test_incomplete_outer_protocol_does_not_expose_nested_protocol(self):
        response = (
            f"```run-x <<AGENT_BEGIN_{NONCE_A}\n"
            "outer body\n"
            f"{protocol_block('read-x', '/tmp/should-not-run', NONCE_B)}"
        )
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))

    def test_duplicate_nonce_block_is_stripped_from_visible_text(self):
        """重复 nonce 的块不执行，但也不能把原始协议标记漏给用户看。"""
        response = (
            "前言\n"
            f"{protocol_block('run-x', 'echo one', NONCE_A)}\n"
            f"{protocol_block('run-x', 'echo two', NONCE_A)}\n"
            "结尾"
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(1, len(blocks), "重复 nonce 的块不应该被执行")
        self.assertEqual("echo one", blocks[0]["body"])

        stripped = ProtocolParser.strip_protocol_blocks(response)
        self.assertNotIn("AGENT_BEGIN", stripped, "重复块的原始标记泄漏给了用户")
        self.assertNotIn("echo two", stripped)
        self.assertIn("前言", stripped)
        self.assertIn("结尾", stripped)

    def test_has_unclosed_block_detects_missing_end_marker(self):
        """未闭合会让后续协议全部不执行，必须能被检测到并提示。"""
        response = (
            f"```run-x <<AGENT_BEGIN_{NONCE_A}\n"
            "echo ok\n"
            "（这里少了结束标记）"
        )
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertTrue(ProtocolParser.has_unclosed_block(response))

    def test_has_unclosed_block_false_for_well_formed(self):
        response = (
            "文字\n"
            f"{protocol_block('run-x', 'echo ok', NONCE_A)}\n"
            f"{protocol_block('read-x', '/tmp/a', NONCE_B)}"
        )
        self.assertFalse(ProtocolParser.has_unclosed_block(response))

    def test_has_unclosed_block_false_for_plain_text(self):
        self.assertFalse(ProtocolParser.has_unclosed_block("普通回复，没有协议。"))

    def test_end_marker_without_closing_fence_remains_body(self):
        end_marker = f"AGENT_END_{NONCE_A}"
        response = (
            f"```file-x:/tmp/demo.md <<AGENT_BEGIN_{NONCE_A}\n"
            "before\n"
            f"{end_marker}\n"
            "not-a-closing-fence\n"
            "after\n"
            f"{end_marker}\n"
            "```\n"
        )
        block = ProtocolParser.extract_protocol_blocks(response)[0]
        self.assertEqual("file", block["type"])
        self.assertEqual("/tmp/demo.md", block["path"])
        self.assertIn(f"{end_marker}\nnot-a-closing-fence", block["body"])

    def test_all_protocol_tags_keep_existing_execution_contract(self):
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
            nonce = f"{index:012X}"
            response = protocol_block(tag, body, nonce)
            with self.subTest(tag=tag):
                block = ProtocolParser.extract_protocol_blocks(response)[0]
                self.assertEqual(expected_type, block["type"])
                self.assertEqual(expected_path, block["path"])
                self.assertEqual(expected_body, block["body"])

    def test_begin_and_end_nonce_must_match(self):
        response = (
            f"```run-x <<AGENT_BEGIN_{NONCE_A}\n"
            "echo unsafe\n"
            f"AGENT_END_{NONCE_B}\n"
            "```"
        )
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))

    def test_begin_without_matching_end_is_not_executable(self):
        response = f"before\n```run-x <<AGENT_BEGIN_{NONCE_A}\necho ok"
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))

    def test_end_without_final_fence_is_not_executable(self):
        response = (
            f"before\n```run-x <<AGENT_BEGIN_{NONCE_A}\n"
            f"echo ok\nAGENT_END_{NONCE_A}"
        )
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))

    def test_old_single_end_marker_header_is_not_executable(self):
        response = (
            f"```run-x <<AGENT_END_{NONCE_A}\n"
            f"echo unsafe\nAGENT_END_{NONCE_A}\n```"
        )
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))

    def test_old_fenced_protocol_is_not_executable(self):
        response = "before\n```run-x\necho unsafe\n```\nafter"
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))

    def test_old_file_heredoc_marker_is_not_executable(self):
        response = "```file-x:/tmp/demo.txt <<EOF\ncontent\nEOF\n```"
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))

    def test_nonce_with_5_characters_is_rejected(self):
        response = protocol_block("run-x", "echo unsafe", "12345")
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))

    def test_nonce_with_6_characters_is_accepted(self):
        response = protocol_block("run-x", "echo ok", "A23_-B")
        self.assertEqual("echo ok", ProtocolParser.extract_protocol_blocks(response)[0]["body"])

    def test_nonce_with_32_characters_is_accepted(self):
        nonce = "A" + "1" * 29 + "_-"
        self.assertEqual(32, len(nonce))
        response = protocol_block("run-x", "echo ok", nonce)
        self.assertEqual("echo ok", ProtocolParser.extract_protocol_blocks(response)[0]["body"])

    def test_nonce_with_33_characters_is_rejected(self):
        nonce = "A" * 33
        response = protocol_block("run-x", "echo unsafe", nonce)
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))

    def test_duplicate_nonce_is_rejected_after_first_block(self):
        response = (
            f"{protocol_block('run-x', 'first', NONCE_A)}\n"
            f"{protocol_block('run-x', 'second', NONCE_A)}"
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(1, len(blocks))
        self.assertEqual("first", blocks[0]["body"])

    def test_duplicate_block_body_remains_opaque(self):
        duplicate_body = protocol_block("read-x", "/tmp/should-not-run", NONCE_B)
        response = (
            f"{protocol_block('run-x', 'first', NONCE_A)}\n"
            f"{protocol_block('run-x', duplicate_body, NONCE_A)}"
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(1, len(blocks))
        self.assertEqual("first", blocks[0]["body"])

    def test_strip_protocols_preserves_user_facing_text(self):
        response = (
            "before\n"
            f"{protocol_block('run-x', 'echo ok', NONCE_A)}\n\n"
            "after"
        )
        self.assertEqual("before\n\nafter", ProtocolParser.strip_protocol_blocks(response))

    def test_strip_only_removes_complete_new_protocols(self):
        old_block = (
            f"```run-x <<AGENT_END_{NONCE_B}\n"
            f"echo old\nAGENT_END_{NONCE_B}\n```"
        )
        response = f"before\n{protocol_block('run-x', 'echo ok', NONCE_A)}\n{old_block}\nafter"
        self.assertEqual(f"before\n{old_block}\nafter", ProtocolParser.strip_protocol_blocks(response))

    def test_windows_line_endings_are_supported(self):
        response = (
            f"```run-x <<AGENT_BEGIN_{NONCE_A}\r\n"
            "echo ok\r\n"
            f"AGENT_END_{NONCE_A}\r\n"
            "```"
        )
        block = ProtocolParser.extract_protocol_blocks(response)[0]
        self.assertEqual("run", block["type"])
        self.assertEqual("echo ok", block["body"])


if __name__ == "__main__":
    unittest.main()
