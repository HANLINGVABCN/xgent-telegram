import unittest

from bot_app.protocols import ProtocolParser


class ProtocolParserTests(unittest.TestCase):
    def test_extracts_multiple_protocols_in_order(self):
        response = (
            "先说明\n"
            "```run-x\n"
            "echo ok\n"
            "```\n"
            "中间文字\n"
            "```read-x:/tmp/demo.txt:1-3\n"
            "```\n"
        )
        blocks = ProtocolParser.extract_protocol_blocks(response)
        self.assertEqual(["run", "read"], [block["type"] for block in blocks])
        self.assertEqual("echo ok", blocks[0]["body"])
        self.assertEqual("/tmp/demo.txt:1-3", blocks[1]["path"])

    def test_heredoc_file_can_contain_fences(self):
        response = (
            "```file-x:/tmp/demo.md <<END\n"
            "# title\n"
            "```python\n"
            "print('ok')\n"
            "```\n"
            "END\n"
        )
        block = ProtocolParser.extract_protocol_blocks(response)[0]
        self.assertEqual("file", block["type"])
        self.assertEqual("/tmp/demo.md", block["path"])
        self.assertIn("```python", block["body"])
        self.assertIn("print('ok')", block["body"])

    def test_strip_protocols_preserves_user_facing_text(self):
        response = "before\n```run-x\necho ok\n```\n\nafter"
        self.assertEqual("before\n\nafter", ProtocolParser.strip_protocol_blocks(response))

    def test_unclosed_protocol_is_left_as_plain_text(self):
        response = "before\n```run-x\necho ok"
        self.assertEqual([], ProtocolParser.extract_protocol_blocks(response))
        self.assertEqual(response, ProtocolParser.strip_protocol_blocks(response))


if __name__ == "__main__":
    unittest.main()

