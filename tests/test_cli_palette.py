"""`/` 命令面板的纯逻辑测试。

画屏和按键读取要真 pty 才能测，那部分不在这里；这里钉的是"什么时候该弹面板、
筛选和选择怎么变、编辑键改了什么、粘贴怎么落地"——这些是纯状态变换，也是最容易
在后续改动里被悄悄改坏的部分。
"""

import io
import os
import re
import sys
import threading
import time
import unittest

from xgent_app.cli_render import display_width
from xgent_app.cli_palette import (
    MAX_ROWS,
    SlashPalette,
    _normalize_paste,
    _PASTE_END,
    select,
)

COMMANDS = [
    "agent", "blacklist", "chat_model", "clear_memory", "config", "depth",
    "export", "models", "start", "status", "stream", "update", "web",
]


def make(buffer: str = "", history=None) -> SlashPalette:
    palette = SlashPalette(
        "> ",
        lambda prefix: [c for c in COMMANDS if c.startswith(prefix.lower())],
        lambda names, selected: [f"{'>' if i == selected else ' '} /{n}"
                                 for i, n in enumerate(names)],
        history=history,
    )
    palette.buffer = buffer
    palette.cursor = len(buffer)
    palette._refresh_matches()
    return palette


class MenuVisibilityTests(unittest.TestCase):
    def test_slash_at_line_start_opens_the_menu(self):
        self.assertTrue(make("/")._menu_open())
        self.assertEqual(COMMANDS, make("/").matches)

    def test_typing_narrows_the_matches(self):
        self.assertEqual(["start", "status", "stream"], make("/st").matches)
        self.assertEqual(["status"], make("/statu").matches)

    def test_path_in_the_middle_of_a_sentence_does_not_open_the_menu(self):
        # "看看 /home/x" 是在写参数，不是在挑命令。这条要是坏了，用户每次
        # 提到一个绝对路径都会被candidates糊一脸。
        for text in ("ls /tmp", "看看 /home/hanling", "/web on"):
            with self.subTest(text=text):
                self.assertFalse(make(text)._menu_open())
                self.assertEqual([], make(text).matches)

    def test_plain_text_never_opens_the_menu(self):
        self.assertEqual([], make("你好").matches)

    def test_unknown_command_yields_no_matches(self):
        self.assertEqual([], make("/zzzz").matches)


class SelectionTests(unittest.TestCase):
    def test_accept_fills_the_highlighted_entry(self):
        palette = make("/st")
        palette.selected = 1
        palette._accept_selection()
        self.assertEqual("/status", palette.buffer)
        self.assertEqual(len("/status"), palette.cursor)
        self.assertEqual([], palette.matches, "选完面板要收起")

    def test_selection_sticks_to_the_same_command_while_typing(self):
        # 选中 /stream 后再多打一个字符，高亮不该跳回第一条。
        palette = make("/st")
        palette.selected = 2
        self.assertEqual("stream", palette.matches[palette.selected])
        palette.buffer = "/str"
        palette._refresh_matches()
        self.assertEqual("stream", palette.matches[palette.selected])

    def test_selection_resets_when_previous_choice_filtered_out(self):
        palette = make("/st")
        palette.selected = 2
        palette.buffer = "/sta"
        palette._refresh_matches()
        self.assertEqual(0, palette.selected)

    def test_window_keeps_selection_visible(self):
        palette = make("/")
        palette.selected = len(COMMANDS) - 1
        window, local = palette._visible()
        self.assertEqual(MAX_ROWS, len(window))
        self.assertEqual(COMMANDS[-1], window[local])


class EditingTests(unittest.TestCase):
    def test_backspace_reopens_the_menu(self):
        palette = make("/status x")
        self.assertEqual([], palette.matches)
        for _ in range(2):
            palette._backspace()
        palette._refresh_matches()
        self.assertEqual(["status"], palette.matches)

    def test_cursor_aware_insert_and_delete(self):
        palette = make("/web")
        palette.cursor = 1
        palette._insert("a")
        self.assertEqual("/aweb", palette.buffer)
        palette._delete()
        self.assertEqual("/aeb", palette.buffer)

    def test_delete_word(self):
        palette = make("read the /etc/fstab file")
        palette._delete_word()
        self.assertEqual("read the /etc/fstab ", palette.buffer)

    def test_history_walks_backwards_and_restores_draft(self):
        palette = make("draf", history=["first", "second"])
        palette._history_move(-1)
        self.assertEqual("second", palette.buffer)
        palette._history_move(-1)
        self.assertEqual("first", palette.buffer)
        palette._history_move(1)
        self.assertEqual("second", palette.buffer)
        palette._history_move(1)
        self.assertEqual("draf", palette.buffer, "翻回底部要还回没写完的那行")

    def test_history_is_noop_without_entries(self):
        palette = make("hi")
        palette._history_move(-1)
        self.assertEqual("hi", palette.buffer)


class PasteTests(unittest.TestCase):
    """粘贴。这组测试守的是那个真实 bug：往输入行贴一段多行日志，每个换行都被
    当成回车，一次粘贴变成几十条消息逐条发给 AI。"""

    def test_short_single_line_paste_goes_straight_into_the_line(self):
        palette = make("看看 ")
        palette._apply_paste("/var/log/syslog")
        self.assertEqual("看看 /var/log/syslog", palette.buffer)
        self.assertEqual({}, dict(palette._pastes))

    def test_multiline_paste_becomes_one_placeholder(self):
        palette = make("这段日志什么意思 ")
        palette._apply_paste("line1\nline2\nline3")
        self.assertEqual(1, len(palette._pastes))
        marker = next(iter(palette._pastes))
        self.assertIn("3 行", marker)
        self.assertEqual(f"这段日志什么意思 {marker}", palette.buffer)
        self.assertNotIn("\n", palette.buffer, "输入行里绝不能出现换行")

    def test_submitting_restores_the_pasted_body(self):
        palette = make("这段日志什么意思 ")
        palette._apply_paste("line1\nline2\nline3")
        self.assertEqual(
            "这段日志什么意思 line1\nline2\nline3",
            palette._expand_pastes(palette.buffer),
        )

    def test_two_identical_pastes_restore_independently(self):
        palette = make("")
        palette._apply_paste("a\nb")
        palette._insert(" 和 ")
        palette._apply_paste("a\nb")
        self.assertEqual(2, len(palette._pastes), "同样内容也要各自一个占位符")
        self.assertEqual("a\nb 和 a\nb", palette._expand_pastes(palette.buffer))

    def test_long_single_line_paste_also_uses_a_placeholder(self):
        palette = make("")
        body = "x" * 500
        palette._apply_paste(body)
        self.assertEqual(1, len(palette._pastes))
        self.assertIn("500 字", palette.buffer)
        self.assertEqual(body, palette._expand_pastes(palette.buffer))

    def test_backspace_removes_the_whole_placeholder(self):
        palette = make("问题 ")
        palette._apply_paste("line1\nline2")
        palette._backspace()
        self.assertEqual("问题 ", palette.buffer)
        self.assertEqual({}, dict(palette._pastes), "删掉的占位符不该还留着正文")

    def test_backspace_inside_normal_text_is_unaffected(self):
        palette = make("")
        palette._apply_paste("line1\nline2")
        palette._insert("ab")
        palette._backspace()
        self.assertTrue(palette.buffer.endswith("a"))
        self.assertEqual(1, len(palette._pastes))

    def test_empty_paste_changes_nothing(self):
        palette = make("hi")
        palette._apply_paste("")
        self.assertEqual("hi", palette.buffer)
        self.assertEqual({}, dict(palette._pastes))


class NormalizePasteTests(unittest.TestCase):
    def test_line_endings_are_unified(self):
        self.assertEqual("a\nb\nc", _normalize_paste(b"a\r\nb\rc"))

    def test_trailing_newline_is_dropped(self):
        # 复制整行时终端通常带一个行尾换行，用户不是想让消息以空行结束。
        self.assertEqual("a\nb", _normalize_paste(b"a\nb\n\n"))

    def test_control_junk_is_stripped_but_tabs_survive(self):
        self.assertEqual("a\tb", _normalize_paste(b"a\tb\x00\x07"))

    def test_utf8_split_bytes_do_not_raise(self):
        self.assertIsInstance(_normalize_paste("中文".encode()[:-1]), str)


@unittest.skipIf(select is None, "需要 POSIX select")
class ReadPasteTests(unittest.TestCase):
    """从字节流里切出粘贴正文。用管道喂，覆盖"结束标记被拆到两次 read"。"""

    def _drive(self, *chunks: bytes):
        """把 chunks 分几次写进管道（中间留点间隔，逼出多次 read），返回
        (粘贴正文, 读完后退回去的剩余字节)。"""
        read_fd, write_fd = os.pipe()
        palette = make("")

        def feed() -> None:
            for i, chunk in enumerate(chunks):
                if i:
                    time.sleep(0.05)
                os.write(write_fd, chunk)

        worker = threading.Thread(target=feed)
        worker.start()
        try:
            body = palette._read_paste(read_fd)
        finally:
            worker.join()
            os.close(read_fd)
            os.close(write_fd)
        return body, bytes(palette._pending)

    def test_reads_until_the_end_marker(self):
        body, _ = self._drive(b"hello\nworld" + _PASTE_END.encode())
        self.assertEqual("hello\nworld", body)

    def test_end_marker_split_across_reads(self):
        body, _ = self._drive(b"hello\nworld\x1b[20", b"1~")
        self.assertEqual("hello\nworld", body)

    def test_keys_typed_right_after_the_paste_are_not_swallowed(self):
        body, rest = self._drive(b"log" + _PASTE_END.encode() + b"\r")
        self.assertEqual("log", body)
        self.assertEqual(b"\r", rest, "粘贴后紧跟的回车得留给下一次读键")

    def test_missing_end_marker_gives_up_instead_of_hanging(self):
        body, _ = self._drive(b"half a paste")
        self.assertEqual("half a paste", body)


@unittest.skipIf(select is None, "需要 POSIX select")
class BurstTests(unittest.TestCase):
    """不支持括号粘贴的终端：靠"回车后面还紧跟着字节"识别粘贴。"""

    def _burst(self, *chunks: bytes):
        read_fd, write_fd = os.pipe()
        palette = make("")
        for chunk in chunks:
            os.write(write_fd, chunk)
        try:
            return palette._read_burst(read_fd)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_lone_enter_is_not_a_burst(self):
        # 管道里什么都没有 = 用户真的只是按了回车，必须放行去提交。
        self.assertIsNone(self._burst())

    def test_rest_of_the_paste_is_swallowed_as_one_block(self):
        self.assertEqual("b\nc", self._burst(b"b\nc\n"))

    def test_only_newlines_after_enter_reads_as_empty(self):
        # 归一化后是空串 -> 调用方当普通回车提交，不会生成一个空占位符。
        self.assertFalse(self._burst(b"\n\n"))


class InputBoxTests(unittest.TestCase):
    """输入框：框内折行、光标落点、放不下时的兜底。

    画屏本身要真终端才能看，但"内容切成几行、光标在第几行第几列"是纯计算，
    也正是最容易被后续改动悄悄改坏的地方——光标算错一列，用户就会看到自己
    敲的字出现在框线上。
    """

    def test_wrapping_counts_cjk_as_two_columns(self):
        palette = make("")
        palette.buffer = "中文中文ab"   # 4*2 + 2 = 10 列
        palette.cursor = len(palette.buffer)
        rows, _, _ = palette._layout(6)
        self.assertEqual(["中文中", "文ab"], rows)

    def test_cursor_lands_where_the_character_is(self):
        palette = make("")
        palette.buffer = "abcdef"
        palette.cursor = 4
        rows, row, col = palette._layout(3)
        self.assertEqual(["abc", "def"], rows)
        self.assertEqual((1, 1), (row, col), "第 5 个字符在第二行第 2 列")

    def test_cursor_at_a_full_row_end_moves_to_the_next_row(self):
        # 不换行的话光标会压在右框线上，下一次重画的行数也会算少一行。
        palette = make("")
        palette.buffer = "abc"
        palette.cursor = 3
        rows, row, col = palette._layout(3)
        self.assertEqual(["abc", ""], rows)
        self.assertEqual((1, 0), (row, col))

    def test_double_width_char_does_not_straddle_the_border(self):
        palette = make("")
        palette.buffer = "ab中"
        palette.cursor = 3
        rows, _, _ = palette._layout(3)
        self.assertEqual(["ab", "中"], rows, "放不下的宽字符整个挪到下一行")

    def test_rows_beyond_the_budget_window_around_the_cursor(self):
        palette = make("")
        rows = [str(i) for i in range(10)]
        windowed, cursor = palette._fit_rows(rows, 9, 3)
        self.assertEqual(["7", "8", "9"], windowed)
        self.assertEqual(2, cursor, "光标行要跟着窗口平移")

    def test_fit_rows_is_a_noop_when_it_all_fits(self):
        palette = make("")
        rows = ["a", "b"]
        self.assertEqual((rows, 1), palette._fit_rows(rows, 1, 5))


class BoxWidthTests(unittest.TestCase):
    """框线对齐。差一列就会看到右框线在那儿抖。"""

    def _frame(self, buffer: str, width: int) -> list:
        os.environ["COLUMNS"], os.environ["LINES"] = str(width), "24"
        palette = make(buffer)
        palette.prompt = "❯ "
        captured = io.StringIO()
        real, sys.stdout = sys.stdout, captured
        try:
            palette._render()
        finally:
            sys.stdout = real
        drawn = captured.getvalue().split("\x1b[J", 1)[1]
        drawn = re.split(r"\x1b\[[0-9]+[AG]", drawn)[0]
        return drawn.split("\n")

    def test_every_line_is_exactly_the_terminal_width(self):
        for width in (40, 56, 80):
            for buffer in ("", "abc", "你好，帮我看看服务器负载", "x" * 120):
                with self.subTest(width=width, buffer=buffer[:8]):
                    widths = {display_width(line) for line in self._frame(buffer, width)}
                    self.assertEqual({width}, widths)

    def test_box_has_a_top_and_a_bottom(self):
        lines = self._frame("hi", 40)
        self.assertTrue(lines[0].startswith("╭"))
        self.assertTrue(lines[-1].startswith("╰"))
        self.assertIn("hi", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
