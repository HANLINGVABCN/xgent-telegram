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
    content_width,
    display_width,
    fill_line,
    html_to_ansi,
    rule_block,
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


class MessagePresentationTests(unittest.TestCase):
    """消息呈现的四档样式：ai/user 双横线块、cmd 灰底、system 小字、token 裸行。

    这几条断言守的是"页面呈现"层的约定，不是实现细节：框线一旦重新长出
    左右竖线，终端窗口一拉窄整块就散架（这正是改成双横线的原因）；灰底
    一旦在行中断掉，命令输出看起来就像半截。
    """

    def setUp(self):
        cli_bridge.reset_menu_state()
        self.stream = io.StringIO()
        self.screen = TerminalScreen(stream=self.stream, color=True, width=60)
        cli_bridge.set_screen(self.screen)
        self.bot = cli_bridge.CliBot(1, self.screen)

    def tearDown(self):
        cli_bridge.set_turn_kind("chat")
        cli_bridge.set_screen(None)
        cli_bridge.reset_menu_state()

    # -- ai / user 块 ---------------------------------------------------
    def test_ai_block_has_no_vertical_border(self):
        renderer = MessageRenderer(Palette(False), 60)
        body = "\n".join(renderer.render_message("你好", style="ai"))
        self.assertIn("◆ XGent", body)
        for char in ("│", "╭", "╰", "╮", "╯"):
            self.assertNotIn(char, body, f"消息块不该再有 {char}：左右包边会被拉窄的窗口顶散架")

    def test_user_block_keeps_the_same_shape(self):
        renderer = MessageRenderer(Palette(False), 60)
        lines = renderer.render_message("你好", title="User", marker="❯", style="user")
        self.assertIn("❯ User", lines[0])
        self.assertTrue(set(lines[-1]) <= {"─"}, lines[-1])

    def test_rule_title_degrades_instead_of_overflowing(self):
        # 终端窄到放不下标题时退化成纯横线：硬塞会把上横线顶到第二行。
        lines = rule_block(["正文"], 12, "◆ XGent 一个很长的标题")
        self.assertEqual(12, display_width(lines[0]))
        self.assertEqual(12, display_width(lines[-1]))

    # -- cmd 灰底 -------------------------------------------------------
    def test_cmd_line_keeps_inline_color_and_refills_bg(self):
        pal = Palette(True)
        renderer = MessageRenderer(pal, 60)
        lines = renderer.render_message(
            "<b>可用命令</b>", parse_mode="HTML",
            title="命令", marker="◇", style="cmd",
        )
        body_lines = [line for line in lines[1:] if line.strip()]
        self.assertTrue(body_lines)
        body = "\n".join(body_lines)
        # 原色保留（bold 还在），灰底也在。
        self.assertIn(pal.bold, body)
        self.assertIn(pal.bg, body)
        # 每个 reset 后面都要立刻把灰底续上，否则底色在行中断掉。
        for line in body_lines:
            chunks = line.split(pal.reset)
            for chunk in chunks[1:-1]:
                self.assertTrue(chunk.startswith(pal.bg),
                                f"reset 之后没有补回灰底：{chunk!r}")

    def test_cmd_line_is_not_dimmed(self):
        # 要求 7：灰底上的字要正常亮度，dim 叠在 236 灰底上会糊成暗灰。
        pal = Palette(True)
        lines = MessageRenderer(pal, 60).render_message(
            "普通输出", title="命令", marker="◇", style="cmd")
        for line in lines[1:]:
            self.assertNotIn(pal.dim, line)

    def test_cmd_fill_survives_narrow_terminal(self):
        pal = Palette(True)
        long_line = "路径 " * 40
        for width in (40, 60, 100):
            with self.subTest(width=width):
                for line in fill_line(long_line, width, pal):
                    self.assertEqual(width, display_width(line))
                    self.assertTrue(line.startswith(pal.bg))
                    self.assertTrue(line.endswith(pal.reset))

    # -- system / token -------------------------------------------------
    def test_system_style_has_no_background(self):
        # 系统提示要白字：状态信息是要被读到的，dim 压暗等于让人看不见。
        pal = Palette(True)
        lines = MessageRenderer(pal, 60).render_message(
            "请输入 API Key", title="系统", marker="◇", style="system")
        self.assertIn("◇", lines[0])
        self.assertIn("系统", lines[0])
        body = "\n".join(lines[1:])
        self.assertNotIn(pal.bg, body)
        self.assertNotIn(pal.dim, body)

    def test_token_style_has_no_title_and_no_rule(self):
        lines = MessageRenderer(Palette(False), 60).render_message(
            "↑ 21825 tokens · ⚡ 43 tokens/s", style="token")
        body = "\n".join(lines)
        self.assertIn("tokens/s", body)
        self.assertNotIn("─", body)
        self.assertNotIn("◆", body)
        self.assertNotIn("XGent", body)

    def test_token_line_hugs_the_reply_above_it(self):
        # 要求 5：token 行紧贴上一块，行距放到它下面。
        run(self.bot.send_message(chat_id=1, text="回复正文"))
        self.stream.truncate(0)
        self.stream.seek(0)
        run(self.bot.send_message(chat_id=1, text="↑ 100 tokens · ⚡ 43 tokens/s"))
        output = self.stream.getvalue()
        self.assertFalse(output.startswith("\n"), "token 行前面不该再空一行")
        self.assertTrue(output.endswith("\n\n"), "行距要放到 token 行下面")

    # -- 轮次分类跨 send/edit 一致 --------------------------------------
    def test_edit_keeps_cmd_style(self):
        cli_bridge.set_turn_kind("cmd")
        message = run(self.bot.send_message(chat_id=1, text="正在执行…"))
        first = self.stream.getvalue()
        self.stream.truncate(0)
        self.stream.seek(0)
        run(self.bot.edit_message_text(
            text="执行完成", chat_id=1, message_id=message.message_id))
        second = self.stream.getvalue()
        # 同一条消息不能在 edit 那一刻从"◇ 命令"翻成"◆ XGent"。
        for output in (first, second):
            self.assertIn("◇", output)
            self.assertIn("命令", output)
        self.assertNotIn("XGent", second)

    def test_chat_turn_still_edits_as_ai_block(self):
        cli_bridge.set_turn_kind("chat")
        run(self.bot.edit_message_text(text="AI 回复", chat_id=1, message_id=42))
        self.assertIn("XGent", self.stream.getvalue())


class MessageKindClassificationTests(unittest.TestCase):
    """一次对话轮里，只有"AI 说的那段话"配得上横线块。

    这是改造里最容易搞错的一处：状态行、工具输出和最终回复都走同一个
    send_message、都发生在"对话轮"里。只按轮次分类的话，它们会全部被套上
    ◆ XGent 横线块——屏幕上就是一屏的框，而框���该只圈住"我问的"和"它答的"。
    """

    def setUp(self):
        cli_bridge.reset_menu_state()
        self.stream = io.StringIO()
        self.screen = TerminalScreen(stream=self.stream, color=False, width=100)
        cli_bridge.set_screen(self.screen)
        self.bot = cli_bridge.CliBot(1, self.screen)
        cli_bridge.set_turn_kind("chat")

    def tearDown(self):
        cli_bridge.set_turn_kind("chat")
        cli_bridge.set_screen(None)
        cli_bridge.reset_menu_state()

    def send(self, text, parse_mode=None):
        self.stream.truncate(0)
        self.stream.seek(0)
        run(self.bot.send_message(chat_id=1, text=text, parse_mode=parse_mode))
        return self.stream.getvalue()

    def test_agent_status_line_is_system_not_a_boxed_reply(self):
        for text in ("Agent 第 1 轮 · 1 个操作进行中",
                     "✅ Agent 第 3 轮 · 1 个操作已完成",
                     "⏹️ Agent 第 2 轮 · 已停止"):
            with self.subTest(text=text):
                out = self.send(text)
                self.assertIn("◇", out)
                self.assertIn("系统", out)
                self.assertNotIn("XGent", out)
                self.assertNotIn("─", out)

    def test_agent_tool_result_is_command_output(self):
        for text in ("⌨️ <b>Agent Run</b>\n✅ 返回码: <code>0</code>",
                     "🔎 <b>Agent Grep</b> 命中 3 处",
                     "🌐 <b>Agent Search</b> 命中 2 条"):
            with self.subTest(text=text):
                out = self.send(text, "HTML")
                self.assertIn("◇", out)
                self.assertIn("命令", out)
                self.assertNotIn("XGent", out)
                self.assertNotIn("─", out)

    def test_the_actual_reply_still_gets_its_block(self):
        out = self.send("系统与网络环境信息如下：测试正常。", "HTML")
        self.assertIn("◆", out)
        self.assertIn("XGent", out)
        self.assertIn("─", out)

    def test_only_one_kind_of_block_in_a_whole_turn(self):
        # 走一遍真实对话轮：状态 → 工具输出 → 状态 → 正文 → token。
        # 整轮下来横线块只该出现一次（那段正文）。
        turn = [
            ("Agent 第 1 轮 · 1 个操作进行中", None),
            ("⌨️ <b>Agent Run</b>\n✅ 返回码: <code>0</code>", "HTML"),
            ("✅ Agent 第 1 轮 · 1 个操作已完成", None),
            ("测试正常，随时可以开始。", "HTML"),
            ("↑ 5459 tokens · ↓ 117 tokens · ⚡ 30 tokens/s", None),
        ]
        self.stream.truncate(0)
        self.stream.seek(0)
        for text, parse_mode in turn:
            run(self.bot.send_message(chat_id=1, text=text, parse_mode=parse_mode))
        output = self.stream.getvalue()
        self.assertEqual(1, output.count("◆ XGent"),
                         f"一轮里横线块只该有一个（AI 正文）：\n{output}")
        # 横线只来自那一个块：上下各一条。
        rule_lines = [ln for ln in output.split("\n") if ln and set(ln) <= {"─"}]
        self.assertEqual(1, len(rule_lines), rule_lines)

    def test_every_line_stays_within_the_content_width(self):
        # 要求 9：满宽横线扛不住 resize，所以一律按 content_width 画。
        cap = content_width(200)
        for text, parse_mode in (("Agent 第 1 轮 · 进行中", None),
                                 ("⌨️ <b>Agent Run</b>", "HTML"),
                                 ("一段普通的 AI 回复", "HTML")):
            with self.subTest(text=text):
                for line in self.send(text, parse_mode).split("\n"):
                    self.assertLessEqual(display_width(line), cap, repr(line))


if __name__ == "__main__":
    unittest.main()
