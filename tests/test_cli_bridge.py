"""CLI 客户端（cli_render + cli_bridge）的行为测试。

这两个模块刻意不 import telegram、也不依赖 sections 共享命名空间，所以可以
直接构造对象断言行为，不需要起 bot、不需要数据库。

重点覆盖三类容易回归的地方：
  1. HTML->ANSI：对话核心跨客户端边界传过来的是 Telegram HTML，漏渲染就会
     在终端里原样打印出 "<b>提供商管理</b>"（这是真实发生过的 bug）。
  2. 显示宽度与折行：中文占两列，用 len() 算会让排版全线错位；而原地重绘
     依赖"每行都在宽度内"这个前提，折行错了会擦掉别人的内容。
  3. 菜单注册表语义：Telegram 里旧消息上的按钮一直可点，所以后续的纯文本
     消息不能把当前菜单清掉。
"""

from __future__ import annotations

import asyncio
import io
import os
import tempfile
import unittest

from xgent_app import cli_bridge
from xgent_app.cli_render import (
    MessageRenderer,
    Palette,
    TerminalScreen,
    display_width,
    html_to_ansi,
    split_control_buttons,
    wrap_line,
)


def run(coro):
    return asyncio.run(coro)


class FakeMarkup:
    """最小的 InlineKeyboardMarkup 替身：只要有 inline_keyboard 就够。"""

    def __init__(self, rows):
        self.inline_keyboard = [
            [type("Btn", (), {"text": text, "callback_data": data})() for text, data in row]
            for row in rows
        ]


class DisplayWidthTests(unittest.TestCase):
    def test_cjk_counts_as_two_columns(self):
        self.assertEqual(display_width("abc"), 3)
        self.assertEqual(display_width("提供商"), 6)
        self.assertEqual(display_width("a提b"), 4)

    def test_ansi_escapes_are_zero_width(self):
        self.assertEqual(display_width("\x1b[1mabc\x1b[0m"), 3)

    def test_wrap_respects_display_width_not_len(self):
        # 10 个中文 = 20 列，宽度 10 必须折成 2 行；按 len() 算会只折 1 行。
        lines = wrap_line("中" * 10, 10)
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertLessEqual(display_width(line), 10)

    def test_wrap_never_exceeds_width(self):
        text = "这是一段很长的中文说明文字，中间还夹着 some english words 和一个很长的路径 /var/log/xgent/server.log"
        for width in (24, 40, 72):
            for line in wrap_line(text, width):
                self.assertLessEqual(display_width(line), width, f"width={width}")


class HtmlToAnsiTests(unittest.TestCase):
    def test_tags_are_stripped_when_color_disabled(self):
        out = html_to_ansi("<b>提供商管理</b>", Palette(False))
        self.assertEqual(out, "提供商管理")
        self.assertNotIn("<b>", out)

    def test_tags_become_ansi_when_color_enabled(self):
        out = html_to_ansi("<b>粗体</b>", Palette(True))
        self.assertIn("粗体", out)
        self.assertIn("\x1b[", out)
        self.assertNotIn("<b>", out)

    def test_entities_are_unescaped(self):
        self.assertEqual(html_to_ansi("a &lt; b &amp; c", Palette(False)), "a < b & c")

    def test_link_keeps_bare_url(self):
        out = html_to_ansi('<a href="https://example.com">示例</a>', Palette(False))
        self.assertIn("https://example.com", out)
        self.assertIn("示例", out)

    def test_unknown_tags_are_dropped(self):
        self.assertEqual(html_to_ansi("<span x=1>hi</span>", Palette(False)), "hi")


class MessageRenderTests(unittest.TestCase):
    def setUp(self):
        self.renderer = MessageRenderer(Palette(False), width=60)

    def test_html_body_has_no_raw_tags(self):
        lines = self.renderer.render_text("<b>标题</b>\n正文", parse_mode="HTML")
        joined = "\n".join(lines)
        self.assertIn("标题", joined)
        self.assertNotIn("<b>", joined)

    def test_plain_text_is_left_alone_without_parse_mode(self):
        # parse_mode 为 None 时对话核心发的就是字面文本，不该被当 HTML 解析
        # ——这与 Telegram 的行为一致（不带 parse_mode 就是纯文本）。
        lines = self.renderer.render_text("1 < 2 & 3 > 0", parse_mode=None)
        self.assertIn("1 < 2 & 3 > 0", "\n".join(lines))

    def test_pre_block_is_not_wrapped(self):
        table = "col_a | col_b | col_c | col_d | col_e | col_f | col_g | col_h"
        lines = self.renderer.render_text(f"<pre>{table}</pre>", parse_mode="HTML")
        # 表格是按等宽对齐过的，折行会毁掉对齐，所以整行保持完整。
        self.assertTrue(any(table in line for line in lines), lines)

    def test_buttons_are_numbered_in_order(self):
        buttons = [("添加", "a"), ("导出", "b"), ("导入", "c"), ("返回", "d")]
        lines = self.renderer.render_buttons(buttons)
        joined = "\n".join(lines)
        for index in range(1, 5):
            self.assertIn(str(index), joined)
        # 行优先：第一行应该同时出现第 1、2 项，而不是第 1、3 项。
        self.assertIn("添加", lines[0])
        self.assertIn("导出", lines[0])

    def test_single_column_when_terminal_is_narrow(self):
        narrow = MessageRenderer(Palette(False), width=28)
        lines = narrow.render_buttons([("一个比较长的按钮名", "a"), ("另一个长按钮", "b")])
        self.assertEqual(len(lines), 2)


class TerminalScreenTests(unittest.TestCase):
    def make_screen(self):
        stream = io.StringIO()
        stream.isatty = lambda: True  # type: ignore[attr-defined]
        return TerminalScreen(stream=stream, color=True, width=40), stream

    def test_update_block_redraws_in_place(self):
        screen, stream = self.make_screen()
        screen.print_block(["第一版"], message_id=7)
        stream.truncate(0), stream.seek(0)

        self.assertTrue(screen.update_block(["第二版"], 7))
        written = stream.getvalue()
        # 上移 + 清屏到末尾，才算真的原地重绘而不是又追加一份。
        self.assertIn("\x1b[2A", written)
        self.assertIn("\x1b[J", written)
        self.assertIn("第二版", written)

    def test_update_block_refuses_other_message(self):
        screen, _stream = self.make_screen()
        screen.print_block(["块"], message_id=7)
        self.assertFalse(screen.update_block(["别的"], 99))

    def test_update_block_refuses_after_other_output(self):
        screen, _stream = self.make_screen()
        screen.print_block(["块"], message_id=7)
        screen.print_plain("用户敲了别的东西")
        # 这条消息已经不是屏幕最后一块了，再上移会擦掉不属于自己的内容。
        self.assertFalse(screen.update_block(["新内容"], 7))

    def test_no_redraw_without_color_support(self):
        stream = io.StringIO()
        screen = TerminalScreen(stream=stream, color=False, width=40)
        screen.print_block(["块"], message_id=7)
        # 输出被重定向到文件时不能塞光标控制序列，否则日志变乱码。
        self.assertFalse(screen.update_block(["新"], 7))

    def test_overlong_lines_count_as_multiple_rows(self):
        screen, stream = self.make_screen()
        # 宽 40 的屏幕上，一行 100 列的内容会被终端软换行成 3 行；
        # 记账按 1 行算的话，重绘时上移距离就会不够。
        screen.print_block(["x" * 100], message_id=3)
        stream.truncate(0), stream.seek(0)
        screen.update_block(["y"], 3)
        self.assertIn("\x1b[4A", stream.getvalue())


class CliBotRenderTests(unittest.TestCase):
    def setUp(self):
        cli_bridge.reset_menu_state()
        self.stream = io.StringIO()
        self.screen = TerminalScreen(stream=self.stream, color=False, width=60)
        cli_bridge.set_screen(self.screen)
        self.bot = cli_bridge.CliBot(1, self.screen)

    def tearDown(self):
        cli_bridge.set_screen(None)
        cli_bridge.reset_menu_state()

    def test_send_message_renders_html(self):
        run(self.bot.send_message(chat_id=1, text="<b>提供商管理</b>", parse_mode="HTML"))
        output = self.stream.getvalue()
        self.assertIn("提供商管理", output)
        self.assertNotIn("<b>", output)

    def test_menu_options_follow_display_order(self):
        markup = FakeMarkup([[("添加", "add"), ("导出", "export")], [("返回", "back")]])
        run(self.bot.send_message(chat_id=1, text="菜单", reply_markup=markup))
        self.assertEqual(cli_bridge.get_last_menu_options(), ["add", "export", "back"])

    def test_plain_message_does_not_clear_menu(self):
        markup = FakeMarkup([[("添加", "add")]])
        run(self.bot.send_message(chat_id=1, text="菜单", reply_markup=markup))
        # 对照 Telegram：随后再发一条纯文本，旧消息上的按钮依然可点。
        run(self.bot.send_message(chat_id=1, text="⏳ 正在获取模型列表..."))
        self.assertEqual(cli_bridge.get_last_menu_options(), ["add"])

    def test_editing_menu_away_clears_it(self):
        markup = FakeMarkup([[("添加", "add")]])
        msg = run(self.bot.send_message(chat_id=1, text="菜单", reply_markup=markup))
        # 这次是把那条消息本身的按钮去掉，等价于 TG 上按钮真的消失了。
        run(self.bot.edit_message_text(text="完成", chat_id=1, message_id=msg.message_id,
                                       reply_markup=None))
        self.assertEqual(cli_bridge.get_last_menu_options(), [])

    def test_url_only_buttons_are_skipped(self):
        markup = FakeMarkup([[("打开网页", ""), ("确定", "ok")]])
        run(self.bot.send_message(chat_id=1, text="菜单", reply_markup=markup))
        # 终端点不了 url 按钮，编号必须只覆盖真正能触发的项，否则编号会错位。
        self.assertEqual(cli_bridge.get_last_menu_options(), ["ok"])

    def test_send_document_shows_local_path_for_str_arg(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
            handle.write(b"hi")
            path = handle.name
        try:
            run(self.bot.send_document(chat_id=1, document=path))
            output = self.stream.getvalue()
            # 终端里打不开文件，路径就是唯一有用的信息。
            self.assertIn(os.path.basename(path), output)
            self.assertIn(os.path.abspath(path), output)
        finally:
            os.unlink(path)

    def test_streaming_edits_redraw_instead_of_reprinting(self):
        color_stream = io.StringIO()
        color_stream.isatty = lambda: True  # type: ignore[attr-defined]
        screen = TerminalScreen(stream=color_stream, color=True, width=60)
        bot = cli_bridge.CliBot(1, screen)
        msg = run(bot.send_message(chat_id=1, text="正在", parse_mode="HTML"))
        for text in ("正在生成", "正在生成回答", "正在生成回答内容"):
            run(bot.edit_message_text(text=text, chat_id=1, message_id=msg.message_id,
                                      parse_mode="HTML"))
        output = color_stream.getvalue()
        # 流式每 0.35 秒推一次累计全文；没有原地重绘的话中间态会全部堆在屏幕上。
        self.assertEqual(output.count("正在生成回答内容"), 1)
        self.assertIn("\x1b[J", output)


class CliObjectContractTests(unittest.TestCase):
    """对话核心真实访问到、而垫片曾经漏掉的属性/方法。"""

    def setUp(self):
        cli_bridge.reset_menu_state()
        self.screen = TerminalScreen(stream=io.StringIO(), color=False, width=60)
        cli_bridge.set_screen(self.screen)

    def tearDown(self):
        cli_bridge.set_screen(None)
        cli_bridge.reset_menu_state()

    def test_context_has_application_and_args(self):
        _update, context, _bot = cli_bridge.build_cli_conversation_objects(1, self.screen)
        # messages.py:880 的 restart_web_chat(context.application) 会读它；
        # None 正是 idle.py 里"纯 Web 模式没有 PTB Application"的合法取值。
        self.assertIsNone(context.application)
        # token_stats.cmd_token_stats 读 context.args。
        self.assertEqual(context.args, [])

    def test_command_objects_parse_args(self):
        _update, context, _bot = cli_bridge.build_cli_command_objects(1, "/stats 7", self.screen)
        self.assertEqual(context.args, ["7"])

    def test_message_exposes_methods_the_core_calls(self):
        update, _context, _bot = cli_bridge.build_cli_conversation_objects(1, self.screen)
        for name in ("reply_text", "edit_text", "delete", "reply_document",
                     "reply_photo", "reply_markdown", "edit_reply_markup"):
            self.assertTrue(callable(getattr(update.message, name, None)), name)

    def test_bot_exposes_methods_the_core_calls(self):
        _update, _context, bot = cli_bridge.build_cli_conversation_objects(1, self.screen)
        for name in ("send_message", "edit_message_text", "edit_message_reply_markup",
                     "delete_message", "send_chat_action", "send_document",
                     "send_photo", "forward_message"):
            self.assertTrue(callable(getattr(bot, name, None)), name)

    def test_callback_objects_have_no_message_but_have_query(self):
        update, _context, _bot = cli_bridge.build_cli_callback_objects(1, "menu_main", 0, self.screen)
        self.assertIsNone(update.message)
        self.assertEqual(update.callback_query.data, "menu_main")
        self.assertEqual(update.callback_query.from_user.id, 1)

    def test_bot_carries_web_bot_marker(self):
        # rendering.py 用这个标记跳过 Telegram 私有 Rich API 直连；
        # 掉了的话 CLI 会去打 Telegram 的 HTTP 接口。
        _update, _context, bot = cli_bridge.build_cli_conversation_objects(1, self.screen)
        self.assertTrue(getattr(bot, "_is_xgent_web_bot", False))


class ControlButtonTests(unittest.TestCase):
    """"停止回答"这类终端里点不了的按钮，不能当成编号菜单项渲染。

    Telegram 上它是一颗随时可点的按钮；终端上一轮对话跑起来之后输入循环就在
    await，用户敲不进任何编号。照直渲染成 " 1 ❌ 停止回答" 既是假承诺，
    又会让"正在运行"的状态行下面顶着一个通用的失败记号。
    """

    def setUp(self):
        cli_bridge.reset_menu_state()
        self.stream = io.StringIO()
        self.screen = TerminalScreen(stream=self.stream, color=False, width=80)

    def tearDown(self):
        cli_bridge.reset_menu_state()

    def test_stop_button_is_split_out_of_the_menu(self):
        menu, hints = split_control_buttons([("❌ 停止回答", "act_stop_generation")])
        self.assertEqual([], menu)
        self.assertEqual(1, len(hints))
        self.assertIn("C", hints[0][0])  # ⌃C

    def test_ordinary_buttons_are_left_alone(self):
        buttons = [("提供商", "menu_providers"), ("模型", "menu_models")]
        menu, hints = split_control_buttons(buttons)
        self.assertEqual(buttons, menu)
        self.assertEqual([], hints)

    def test_rendered_message_shows_keybinding_not_a_numbered_cross(self):
        renderer = MessageRenderer(Palette(False), 80)
        lines = renderer.render_message(
            "Agent 第 1 轮 · 1 个操作进行中",
            [("❌ 停止回答", "act_stop_generation")],
        )
        body = "\n".join(lines)
        self.assertIn("Agent 第 1 轮", body)
        self.assertIn("中断回答", body)
        self.assertNotIn("❌", body)
        self.assertNotIn("停止回答", body)
        # 上框线 + 正文 + 键位提示 + 下框线，正好四行：没有多出来的空行 +
        # 编号菜单块。
        self.assertEqual(4, len(lines), lines)

    def test_mixed_buttons_keep_contiguous_numbering(self):
        renderer = MessageRenderer(Palette(False), 80)
        lines = renderer.render_message(
            "正文",
            [("提供商", "menu_providers"), ("❌ 停止回答", "act_stop_generation"),
             ("模型", "menu_models")],
        )
        body = "\n".join(lines)
        self.assertIn(" 1 提供商", body)
        self.assertIn(" 2 模型", body)
        self.assertNotIn(" 3 ", body)

    def test_stop_button_never_becomes_the_active_menu(self):
        # 登记了就意味着这一轮结束后用户随手敲个裸数字会打到 act_stop_generation。
        bot = cli_bridge.CliBot(1, self.screen)
        run(bot.send_message(
            chat_id=1, text="Agent 第 1 轮",
            reply_markup=FakeMarkup([[("❌ 停止回答", "act_stop_generation")]]),
        ))
        self.assertEqual([], cli_bridge.get_last_menu_options())
        self.assertEqual([], cli_bridge.get_last_menu_labels())

    def test_registered_options_line_up_with_displayed_numbers(self):
        bot = cli_bridge.CliBot(1, self.screen)
        run(bot.send_message(
            chat_id=1, text="正文",
            reply_markup=FakeMarkup([
                [("提供商", "menu_providers")],
                [("❌ 停止回答", "act_stop_generation")],
                [("模型", "menu_models")],
            ]),
        ))
        # 屏幕上显示 1=提供商 2=模型，注册表必须一一对应，不能错开一格。
        self.assertEqual(
            ["menu_providers", "menu_models"], cli_bridge.get_last_menu_options()
        )
        self.assertEqual(["提供商", "模型"], cli_bridge.get_last_menu_labels())

    def test_reply_markup_edit_also_hides_the_stop_button(self):
        bot = cli_bridge.CliBot(1, self.screen)
        run(bot.edit_message_reply_markup(
            chat_id=1, message_id=7,
            reply_markup=FakeMarkup([[("❌ 停止回答", "act_stop_generation")]]),
        ))
        self.assertEqual([], cli_bridge.get_last_menu_options())
        self.assertNotIn("❌", self.stream.getvalue())
        self.assertIn("中断回答", self.stream.getvalue())


if __name__ == "__main__":
    unittest.main()
