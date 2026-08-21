"""`/` 命令面板的纯逻辑测试。

画屏和按键读取要真 pty 才能测，那部分不在这里；这里钉的是"什么时候该弹面板、
筛选和选择怎么变、编辑键改了什么"——这些是纯状态变换，也是最容易在后续改动里
被悄悄改坏的部分。
"""

import unittest

from xgent_app.cli_palette import MAX_ROWS, SlashPalette

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


if __name__ == "__main__":
    unittest.main()
