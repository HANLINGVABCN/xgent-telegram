import os
import unittest

os.environ.setdefault("BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
os.environ.setdefault("AUTHORIZED_USER_ID", "1")

from bot_server import (
    AgentExecutor,
    TEXT_STITCH_MODE_AUTO,
    TEXT_STITCH_MODE_FORCE,
    TEXT_STITCH_MODE_OFF,
    TEXT_STITCH_SPLIT_HINT_CHARS,
    merge_text_conversation_parts,
    should_stitch_text_message,
)


def payloads(macro):
    out = []
    for step in AgentExecutor.parse_stdin_macro(macro):
        if step["type"] == "bytes":
            out.append(step.get("payload"))
        else:
            out.append(("wait", step.get("seconds")))
    return out


class StdinSyntaxTests(unittest.TestCase):
    def test_plain_text_and_enter(self):
        self.assertEqual(payloads("npm run dev\nkey: [enter]"), [b"npm run dev", b"\r"])

    def test_text_prefix_is_plain_text(self):
        self.assertEqual(payloads("text: hello\ntype: world"), [b"text: hellotype: world"])

    def test_line_and_escaped_paste(self):
        self.assertEqual(payloads("line: hello\npaste: a\\[b\\]\\\\c"), [b"hello\r", b"a[b]\\c"])
        self.assertEqual(payloads("\\paste: literal"), [b"paste: literal"])
        self.assertEqual(payloads("line:  indented\npaste:\tTabbed"), [b" indented\r", b"Tabbed"])

    def test_paste_heredoc_is_literal_multiline_text(self):
        macro = "\n".join([
            "paste: <<EOF",
            "line 1",
            "key: [enter] is literal here",
            "text: stays text",
            "EOF",
            "key: [enter]",
        ])
        self.assertEqual(
            payloads(macro),
            [b"line 1\nkey: [enter] is literal here\ntext: stays text\n", b"\r"],
        )

    def test_paste_heredoc_can_contain_empty_lines_and_prefixes(self):
        macro = "paste: <<BLOCK\n\n\\key: literal\npaste: literal too\nBLOCK"
        self.assertEqual(payloads(macro), [b"\n\\key: literal\npaste: literal too\n"])
        self.assertEqual(payloads("paste: <<123\nok\n123"), [b"ok\n"])

    def test_paste_heredoc_requires_closing_marker(self):
        with self.assertRaisesRegex(ValueError, "未找到结束标记"):
            AgentExecutor.parse_stdin_macro("paste: <<EOF\nhello")

    def test_empty_paste_heredoc_writes_no_bytes(self):
        self.assertEqual(payloads("paste: <<EOF\nEOF"), [])

    def test_escaped_paste_heredoc_header_is_plain_text(self):
        self.assertEqual(payloads("\\paste: <<EOF"), [b"paste: <<EOF"])

    def test_top_level_inline_is_plain_text(self):
        self.assertEqual(payloads("npm run dev [enter]"), [b"npm run dev [enter]"])

    def test_unknown_prefix_is_plain_text(self):
        self.assertEqual(payloads("foo: bar\njson: {\"a\":1}"), [b"foo: barjson: {\"a\":1}"])

    def test_indented_control_prefix_is_plain_text(self):
        self.assertEqual(payloads("  key: [enter]\n\twait: 1s"), [b"  key: [enter]\twait: 1s"])

    def test_escaped_control_prefix_is_plain_text(self):
        self.assertEqual(payloads("\\key: [enter]\n\\wait: 1s\n\\repeat: 3 [up]"), [b"key: [enter]wait: 1srepeat: 3 [up]"])

    def test_key_sequences(self):
        self.assertEqual(payloads("key: ctrl+a b"), [b"\x01b"])
        self.assertEqual(payloads("key: [ctrl]+[a] [ctrl]+[c]"), [b"\x01\x03"])
        self.assertEqual(payloads("key: [ ]"), [b" "])
        self.assertEqual(payloads("key: ["), [b"["])
        self.assertEqual(payloads("key: ]"), [b"]"])

    def test_key_aliases_for_human_terminal_actions(self):
        self.assertEqual(payloads("key: eof interrupt cancel"), [b"\x04\x03\x03"])
        self.assertEqual(payloads("key: [ctrl]+[d] [ctrl]+[c] [alt]+[left] [shift]+[tab]"), [b"\x04\x03\x1b[1;3D\x1b[Z"])
        self.assertEqual(payloads("key: f13 f20 f24 kp-enter kp-plus numpad9"), [b"\x1b[25~\x1b[34~\x1b[1;2S\r+9"])

    def test_key_repeat_and_repeat_command(self):
        self.assertEqual(payloads("key: [up]*2"), [b"\x1b[A\x1b[A"])
        self.assertEqual(payloads("repeat: 2 [up] [enter]"), [b"\x1b[A", b"\r", b"\x1b[A", b"\r"])

    def test_exact_bytes(self):
        self.assertEqual(payloads("raw: \\r\\n\nhex: 1b5b41\nbase64: SGk=\nbytes: 0x21 10"), [b"\r\n\x1b[AHi!\n"])

    def test_every_byte_is_representable(self):
        macro = "bytes: " + " ".join(str(i) for i in range(256))
        self.assertEqual(payloads(macro), [bytes(range(256))])

    def test_complex_ai_friendly_scenario(self):
        macro = "\n".join([
            "line: python - <<'PY'",
            "paste: <<PY",
            "print('key: [enter] is text')",
            "print('a+a+d is text')",
            "PY",
            "key: [ctrl]+[d]",
            "wait: 10ms",
            "raw: \\x1b[31mred\\x1b[0m",
            "hex: 0a",
        ])
        self.assertEqual(
            payloads(macro),
            [
                b"python - <<'PY'\r",
                b"print('key: [enter] is text')\nprint('a+a+d is text')\n\x04",
                ("wait", 0.01),
                b"\x1b[31mred\x1b[0m\n",
            ],
        )

    def test_wait(self):
        self.assertEqual(payloads("wait: 250ms\nsleep: 1s\ndelay: 500"), [("wait", 0.25), ("wait", 1.0), ("wait", 0.5)])

    def test_invalid_key_combination_still_errors(self):
        with self.assertRaisesRegex(ValueError, "多个普通键"):
            AgentExecutor.parse_stdin_macro("key: ctrl+a+c")

    def test_limits_reject_too_many_steps(self):
        macro = "\n".join("wait: 0" for _ in range(AgentExecutor.MAX_STDIN_MACRO_STEPS + 1))
        with self.assertRaisesRegex(ValueError, "步骤数"):
            AgentExecutor.parse_stdin_macro(macro)

    def test_limits_reject_too_many_bytes(self):
        macro = "x" * (AgentExecutor.MAX_STDIN_MACRO_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "输入不能超过"):
            AgentExecutor.parse_stdin_macro(macro)

    def test_extract_stdin_preserves_body_whitespace(self):
        blocks = AgentExecutor.extract_protocol_blocks("```stdin:s1\n  hello  \nkey: [enter]\n```")
        self.assertEqual(blocks[0]["body"], "  hello  \nkey: [enter]")

    def test_rejects_bad_exact_byte_payloads(self):
        with self.assertRaises(ValueError):
            AgentExecutor.parse_stdin_macro("raw: \\xG0")
        with self.assertRaises(ValueError):
            AgentExecutor.parse_stdin_macro("bytes: 256")
        with self.assertRaises(ValueError):
            AgentExecutor.parse_stdin_macro("base64: !!!")

    def test_wait_limits(self):
        with self.assertRaisesRegex(ValueError, "单次等待"):
            AgentExecutor.parse_stdin_macro("wait: 61s")
        macro = "\n".join("wait: 60s" for _ in range(3))
        with self.assertRaisesRegex(ValueError, "总等待"):
            AgentExecutor.parse_stdin_macro(macro)


class TextConversationBufferTests(unittest.TestCase):
    def test_merges_short_separate_messages_with_blank_line(self):
        self.assertEqual(
            merge_text_conversation_parts(["first paragraph", "second paragraph"]),
            "first paragraph\n\nsecond paragraph",
        )

    def test_merges_near_limit_telegram_split_without_extra_separator(self):
        first_part = "a" * TEXT_STITCH_SPLIT_HINT_CHARS
        self.assertEqual(
            merge_text_conversation_parts([first_part, "tail"]),
            first_part + "tail",
        )

    def test_ignores_empty_parts(self):
        self.assertEqual(
            merge_text_conversation_parts(["", "  ", "actual text"]),
            "actual text",
        )

    def test_auto_mode_only_stitches_near_limit_messages(self):
        self.assertFalse(should_stitch_text_message("short", TEXT_STITCH_MODE_AUTO))
        self.assertTrue(
            should_stitch_text_message("a" * TEXT_STITCH_SPLIT_HINT_CHARS, TEXT_STITCH_MODE_AUTO)
        )

    def test_force_and_off_modes_override_length(self):
        self.assertTrue(should_stitch_text_message("short", TEXT_STITCH_MODE_FORCE))
        self.assertFalse(
            should_stitch_text_message("a" * TEXT_STITCH_SPLIT_HINT_CHARS, TEXT_STITCH_MODE_OFF)
        )


if __name__ == "__main__":
    unittest.main()
