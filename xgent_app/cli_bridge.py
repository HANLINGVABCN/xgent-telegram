"""CLI（本地终端）与对话核心之间的鸭子类型垫片。

process_conversation() 只需要 update.effective_chat.id、
update.message.reply_text()、update.callback_query 以及 context.bot 上的
几个发送方法——它并不检查这些对象是不是真的 telegram 类型。web_bridge.py
已经用这套鸭子类型把 Web 端接进了对话核心；本模块照抄同样的形状，把本地
终端也接进去。这不是巧合的重复代码，而是刻意的验证：如果 CLI 也能顺利
套进 web_bridge.py 已经验证过的接口形状里，就说明 process_conversation
的抽象接口不是"看起来通用、实际上是 Telegram/Web 形状的伪抽象"。

与 web_bridge.py 的关键差异（都是范式差异，不是偷懒）：
  - 没有 SSE/outbox 广播——终端只有一个"连接"（当前进程的 stdout），直接
    写 stdout 即可，不需要多订阅者广播机制。
  - 没有认证——本地终端场景下"能跑这个进程"本身就是权限凭证，与 Web 端
    需要密码墙的定位不同（Web 是给远程浏览器访问设计的）。
  - "编辑历史消息"这个 Telegram/Web 的核心范式，在终端上靠 ANSI 光标控制
    实现（cli_render.TerminalScreen.update_block）。这不是模拟，是真的原地
    重绘：流式回复每 0.35 秒就把累计全文重发一次，没有原地重绘的话终端会
    被同一段文字刷屏上百次。
  - 排版交给 cli_render.py（CLI 的"前端层"，对应 Web 的 index.html
    renderText）：HTML->ANSI、按显示宽度折行、菜单编号化都在那边。

本模块不 import telegram，也不依赖 sections 共享命名空间，可以直接单测。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

from xgent_app.cli_render import (
    MessageRenderer,
    TerminalScreen,
    buttons_from_markup,
    split_control_buttons,
)
# 从磁盘路径参数里挖本地路径的逻辑与 Web 端完全一样（同样要处理"传字符串
# 路径"和"传打开的文件对象"两种调用形式），直接复用 web_bridge 里那一份，
# 不在这里抄第二遍——两边判定一旦分裂就会出现"Web 能显示路径、CLI 不能"
# 这类只在某个客户端复现的 bug。web_bridge 只依赖标准库，导入无副作用。
from xgent_app.web_bridge import _local_path_from_send_arg

logger = logging.getLogger(__name__)

# CLI 侧假 message_id 全局计数器。CLI 是单进程单会话，不需要像 Web 那样
# 刻意避开真实 Telegram message_id 的取值范围（没有 TG->CLI 镜像这回事），
# 但仍然从 1 开始递增，保证同一次运行内 message_id 不重复——原地重绘就是
# 靠 message_id 认领"屏幕上最后那个块是不是我的"。
_CLI_MESSAGE_ID_LOCK = threading.Lock()
_cli_message_id_counter = 1


def _allocate_cli_message_id() -> int:
    global _cli_message_id_counter
    with _CLI_MESSAGE_ID_LOCK:
        mid = _cli_message_id_counter
        _cli_message_id_counter += 1
        return mid


# --------------------------------------------------------------------------
# 菜单注册表
# --------------------------------------------------------------------------
# 记录每条消息上挂着哪些按钮，供 xgent_cli.py 的输入循环把用户输入的编号
# 翻译回真实 callback_data。
#
# 语义刻意对齐 Telegram，而不是"最后打印的东西说了算"：在 Telegram 里，
# 一条带按钮的消息发出去之后，即使后面又来了几条纯文本消息，那些按钮**仍然
# 可以点**。早先的实现是每次 send_message 都覆盖/清空，于是像
# callbacks.py:1784 那样"先发一条无按钮的『正在获取模型列表…』"就会把用户
# 手上的菜单直接抹掉，只能重敲 /providers 从头来。
#
# 现在的规则：
#   - 带按钮的消息 -> 登记到该 message_id，并成为"当前可选菜单"；
#   - 不带按钮的**新**消息 -> 不动当前菜单（对应 TG 里旧按钮依然可点）；
#   - 对某条消息的编辑带 reply_markup=None -> 那条消息的按钮真的被移除了，
#     若它正是当前菜单则清空。
_MENU_LOCK = threading.Lock()
_menu_by_message: Dict[int, List[Tuple[str, str]]] = {}
_active_menu_message_id: Optional[int] = None
# 「菜单导航中」：最近一次交互输出是带按钮的菜单。区别于 _active_menu_message_id
# （Telegram 语义里旧按钮永远可点），这个标志只看"最后展示的是不是菜单"，
# 供 CLI 决定提示符颜色（黄 ❯）和 Ctrl+C 语义（关菜单而不是退出）。
# 用户和 AI 正常聊天（纯文本输出）后即回到 False，避免黄灯常亮失去信号意义。
_menu_top = False


def _register_menu(message_id: Optional[int], buttons: Sequence[Tuple[str, str]],
                   is_edit: bool = False) -> None:
    """登记可点按钮。

    登记的必须是 cli_render.split_control_buttons() 摘掉控制按钮之后的那一份
    ——屏幕上不显示编号的按钮如果还留在这张表里，编号就会和显示错开一格，
    而且"停止回答"会以一颗隐形按钮的身份赖在当前菜单上：这一轮结束后用户
    敲个裸数字本来是想点别的，结果打到了 act_stop_generation。
    """
    global _active_menu_message_id, _menu_top
    with _MENU_LOCK:
        if buttons:
            if message_id is not None:
                _menu_by_message[message_id] = list(buttons)
                _active_menu_message_id = message_id
            _menu_top = True
            return
        if not is_edit:
            # 新的纯文本消息：菜单注册表按 Telegram 语义保留（旧按钮仍可点），
            # 但"导航中"信号结束——屏幕上最后一层已经不是菜单了。
            _menu_top = False
            return
        if message_id is not None:
            _menu_by_message.pop(message_id, None)
            if _active_menu_message_id == message_id:
                # 只有当前菜单自己的按钮被移除（编辑/删除）才算退出导航；
                # 删一条无关的旧消息（如 /export 完删状态提示）不该误伤信号。
                _active_menu_message_id = None
                _menu_top = False


def menu_is_top() -> bool:
    """最近一次交互输出是否是菜单（CLI 的"菜单导航中"信号）。"""
    with _MENU_LOCK:
        return _menu_top


def dismiss_menu() -> bool:
    """用户显式关闭当前菜单导航（CLI 的 Ctrl+C）。

    与 Telegram 的"按钮永远可点"语义不同：终端里用户说"我要退出这个菜单"，
    就真的清掉——编号不再生效，提示符恢复空闲色。返回是否确有菜单被关掉。
    """
    global _menu_top, _active_menu_message_id
    with _MENU_LOCK:
        had = _menu_top or _active_menu_message_id is not None
        if _active_menu_message_id is not None:
            _menu_by_message.pop(_active_menu_message_id, None)
        _active_menu_message_id = None
        _menu_top = False
        return had


def get_last_menu_options() -> List[str]:
    """当前可选菜单的 callback_data 列表，顺序与显示编号一一对应。"""
    with _MENU_LOCK:
        if _active_menu_message_id is None:
            return []
        return [data for _text, data in _menu_by_message.get(_active_menu_message_id, [])]


def get_last_menu_labels() -> List[str]:
    """当前可选菜单的按钮文字，供提示用户"当前菜单是什么"。"""
    with _MENU_LOCK:
        if _active_menu_message_id is None:
            return []
        return [text for text, _data in _menu_by_message.get(_active_menu_message_id, [])]


def reset_menu_state() -> None:
    """清空菜单注册表（单测隔离用）。"""
    global _active_menu_message_id, _menu_top
    with _MENU_LOCK:
        _menu_by_message.clear()
        _active_menu_message_id = None
        _menu_top = False


# --------------------------------------------------------------------------
# 共享屏幕
# --------------------------------------------------------------------------
# CLI 是单进程单会话，屏幕只有一块。CliBot 每次命令/回调/对话都会新建实例
# （与 WebBot 一致），但原地重绘需要跨实例记住"屏幕上最后那个块"，所以屏幕
# 状态必须放在模块级。
_shared_screen: Optional[TerminalScreen] = None


def get_screen() -> TerminalScreen:
    global _shared_screen
    if _shared_screen is None:
        _shared_screen = TerminalScreen()
    return _shared_screen


def set_screen(screen: Optional[TerminalScreen]) -> None:
    """替换共享屏幕（单测里换成写入 StringIO 的假屏幕）。"""
    global _shared_screen
    _shared_screen = screen


class CliMessage:
    """假的 telegram Message：对应 web_bridge.WebMessage。"""

    def __init__(self, bot: "CliBot", message_id: int, chat_id: int, text: str = ""):
        self.bot = bot
        self.message_id = message_id
        self.chat_id = chat_id
        self.text = text
        self.caption: Optional[str] = None
        self.chat = type("CliChat", (), {"id": chat_id})()

    async def reply_text(self, text: str, **kwargs: Any) -> "CliMessage":
        return await self.bot.send_message(chat_id=self.chat_id, text=text, **kwargs)

    async def edit_text(self, text: str, **kwargs: Any) -> "CliMessage":
        return await self.bot.edit_message_text(
            chat_id=self.chat_id, message_id=self.message_id, text=text, **kwargs
        )

    async def edit_reply_markup(self, reply_markup: Any = None, **kwargs: Any) -> bool:
        """只换按钮不动正文。

        callbacks.py:1983 的模型列表翻页就走这里，而且外面只 try/except 记一条
        warning——早先 CliMessage 没有这个方法，CLI 上点"下一页"是**静默**没
        反应，连报错都看不到。
        """
        return await self.bot.edit_message_reply_markup(
            chat_id=self.chat_id, message_id=self.message_id,
            reply_markup=reply_markup, **kwargs
        )

    async def reply_document(self, document: Any = None, caption: Optional[str] = None,
                             **kwargs: Any) -> "CliMessage":
        """callbacks.py:1091 的"下载提示词"按钮直接调这个。缺了会 AttributeError，
        并且被 callbacks.py 的兜底 except 吞成一句无信息的"操作失败"。"""
        return await self.bot.send_document(
            chat_id=self.chat_id, document=document, caption=caption, **kwargs
        )

    async def reply_photo(self, photo: Any = None, caption: Optional[str] = None,
                          **kwargs: Any) -> "CliMessage":
        return await self.bot.send_photo(
            chat_id=self.chat_id, photo=photo, caption=caption, **kwargs
        )

    async def reply_markdown(self, text: str, **kwargs: Any) -> "CliMessage":
        kwargs.pop("parse_mode", None)
        return await self.bot.send_message(chat_id=self.chat_id, text=text, **kwargs)

    async def delete(self) -> bool:
        return await self.bot.delete_message(chat_id=self.chat_id, message_id=self.message_id)


class CliBot:
    """把对话核心的调用渲染到终端。

    对照 web_bridge.WebBot——同样的方法集合，同样的 _is_xgent_web_bot 标记
    （复用这个既有的判别点，rendering.py 里检查它来跳过 TelegramRichAPI
    的私有协议直连、跳过 TG->Web 镜像安装，CLI 需要同样的行为，没有必要
    另开一个 _is_xgent_cli_bot 标记再改一遍所有判断点）。
    """

    _is_xgent_web_bot = True
    # CLI 专属标记：commands.py 的 /restart 据此区分"当前进程是被托管的
    # 服务"和"另起的 CLI 会话"——后者不该被 sys.exit 带走。
    _is_xgent_cli_bot = True

    def __init__(self, chat_id: int, screen: Optional[TerminalScreen] = None):
        self.chat_id = chat_id
        self.screen = screen or get_screen()

    def _allocate_message_id(self) -> int:
        return _allocate_cli_message_id()

    def _renderer(self) -> MessageRenderer:
        return self.screen.renderer()

    async def send_message(self, chat_id: int, text: str, reply_markup: Any = None,
                           parse_mode: Any = None, **kwargs: Any) -> CliMessage:
        message_id = self._allocate_message_id()
        buttons = buttons_from_markup(reply_markup)
        lines = self._renderer().render_message(str(text), buttons, parse_mode)
        self.screen.print_block(lines, message_id=message_id)
        _register_menu(message_id, split_control_buttons(buttons)[0], is_edit=False)
        return CliMessage(self, message_id, chat_id, str(text))

    async def edit_message_text(self, text: str, chat_id: Optional[int] = None,
                                message_id: Optional[int] = None, reply_markup: Any = None,
                                parse_mode: Any = None, **kwargs: Any) -> CliMessage:
        """原地重绘目标消息；重绘不可行时追加一份。

        重绘不可行的情况：中间夹了别的输出（这条已经不是屏幕最后一块）、块比
        终端还高、或者输出不是 tty（重定向到文件）。这几种情况下宁可多打一份
        也不能上移光标去擦不属于自己的内容。
        """
        target_id = int(message_id or 0)
        buttons = buttons_from_markup(reply_markup)
        lines = self._renderer().render_message(str(text), buttons, parse_mode)
        if not self.screen.update_block(lines, target_id):
            self.screen.print_block(lines, message_id=target_id)
        _register_menu(target_id, split_control_buttons(buttons)[0], is_edit=True)
        return CliMessage(self, target_id, int(chat_id or self.chat_id), str(text))

    async def edit_message_reply_markup(self, chat_id: Optional[int] = None,
                                        message_id: Optional[int] = None,
                                        reply_markup: Any = None, **kwargs: Any) -> bool:
        """只换按钮。终端没法单独重画消息尾部，所以把新按钮作为独立一块打印。"""
        target_id = int(message_id or 0)
        buttons = buttons_from_markup(reply_markup)
        menu_buttons, hints = split_control_buttons(buttons)
        _register_menu(target_id, menu_buttons, is_edit=True)
        if not buttons:
            return True
        renderer = self._renderer()
        lines = renderer.render_buttons(menu_buttons) + renderer.render_hints(hints)
        if not lines:
            return True
        self.screen.print_block(lines, message_id=None)
        return True

    async def delete_message(self, chat_id: Optional[int] = None,
                             message_id: Optional[int] = None, **kwargs: Any) -> bool:
        # 终端没有"删除已滚出去的历史输出"的概念，静默忽略——这与 Web 端
        # emit("delete", ...) 交给前端处理 DOM 节点是同一类"平台特有能力，
        # CLI 没有对应物就降级为空操作"的取舍。
        _register_menu(int(message_id or 0), (), is_edit=True)
        return True

    async def send_chat_action(self, chat_id: Optional[int] = None,
                               action: Any = None, **kwargs: Any) -> bool:
        # "typing" 状态在终端里没有直接对应物，且高频调用
        # （keep_typing_while_waiting 每隔几秒就调一次）——打印出来只会刷屏。
        return True

    def _file_lines(self, icon: str, headline: str, path: Optional[str],
                    caption: Optional[str], parse_mode: Any) -> List[str]:
        renderer = self._renderer()
        pal = self.screen.palette
        lines = [f"  {icon} {pal.paint(headline, pal.ok, pal.bold)}"]
        if path:
            lines.append(f"     {pal.paint(path, pal.muted, pal.underline)}")
        if caption:
            lines.extend(renderer.render_text(caption, parse_mode, indent="     "))
        return lines

    async def send_document(self, chat_id: Optional[int] = None, document: Any = None,
                            caption: Optional[str] = None, filename: Optional[str] = None,
                            parse_mode: Any = None, **kwargs: Any) -> CliMessage:
        local_path = _local_path_from_send_arg(document)
        name = filename or getattr(document, "filename", None)
        if not name and local_path:
            name = os.path.basename(local_path)
        if not name:
            name = getattr(document, "name", None)
        display_name = str(name) if name else "文件"
        message_id = self._allocate_message_id()
        lines = self._file_lines("📎", f"文件：{display_name}", local_path, caption, parse_mode)
        self.screen.print_block(lines, message_id=message_id)
        return CliMessage(self, message_id, int(chat_id or self.chat_id), str(caption or ""))

    async def send_photo(self, chat_id: Optional[int] = None, photo: Any = None,
                         caption: Optional[str] = None, parse_mode: Any = None,
                         **kwargs: Any) -> CliMessage:
        # 终端显示不了图片，能给的最有用信息就是本地路径——用户可以直接
        # 复制去打开。这也是 send_document/send_photo 必须挖出本地路径的原因。
        local_path = _local_path_from_send_arg(photo)
        message_id = self._allocate_message_id()
        lines = self._file_lines("🖼", "图片已生成", local_path, caption, parse_mode)
        self.screen.print_block(lines, message_id=message_id)
        return CliMessage(self, message_id, int(chat_id or self.chat_id), str(caption or ""))

    async def get_file(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("CLI 端不支持下载 Telegram 文件")

    async def forward_message(self, chat_id: Optional[int] = None,
                              from_chat_id: Optional[int] = None,
                              message_id: Optional[int] = None,
                              **kwargs: Any) -> CliMessage:
        # 与 web_bridge.WebBot.forward_message 同理：CLI 没有"转发"语义，
        # 直接返回空文本占位消息，调用方的 try/except 会自然走向下一个
        # fallback 分支。
        message_id_val = self._allocate_message_id()
        target = int(chat_id) if chat_id is not None else self.chat_id
        return CliMessage(self, message_id_val, target, "")


class CliUpdate:
    """对照 web_bridge.WebUpdate / shell_triggers._SelfTriggerUpdate。"""

    def __init__(self, bot: CliBot, chat_id: int):
        self.effective_chat = type("CliChat", (), {"id": chat_id})()
        self.effective_user = type("CliUser", (), {
            "id": chat_id, "full_name": "CLI", "username": None,
        })()
        self.message = CliMessage(bot, 0, chat_id)
        self.callback_query = None


class CliContext:
    """对照 web_bridge.WebContext / shell_triggers._SelfTriggerContext。"""

    def __init__(self, bot: CliBot):
        self.bot = bot
        # 配置状态机会调 restart_web_chat(context.application)（messages.py:880/900）、
        # /web 菜单会调 start_web_chat_if_enabled(context.application)
        # （callbacks.py:700/810）。CLI 进程根本没有 PTB Application，而 None
        # 恰好就是这两个函数明确支持的取值——idle.py:826-832 的 docstring 写死了
        # "app 为 None 时代表纯 Web 模式（未配置 BOT_TOKEN，没有 PTB Application）"，
        # 下游 _web_real_bot=getattr(app,"bot",None) 也原生容忍 None。
        # 不补这个属性的话，CLI 里设 Web 密码/端口会直接 AttributeError（实测过）。
        self.application = None
        # token_stats.cmd_token_stats 会读 context.args（/stats 7 的过滤条件）。
        # 空列表等价于"不带参数的命令"；带参数时由 build_cli_command_objects 覆盖。
        self.args: List[str] = []


class CliCallbackQuery:
    """对照 web_bridge.WebCallbackQuery：只实现对话核心/回调路由用到的部分。

    CLI 场景下"点击按钮"的等价操作是"输入编号"——由调用方（xgent_cli.py
    的输入循环）把编号翻译成 callback_data 字符串后，构造这个对象。
    answer() 把提示文本打印出来，等价于 Web/Telegram 端弹 toast。
    """

    def __init__(self, bot: CliBot, message: CliMessage, data: str, user_id: int):
        self.bot = bot
        self.message = message
        self.data = data
        self.from_user = type("CliUser", (), {
            "id": user_id, "full_name": "CLI", "username": None,
        })()
        self.id = "cli-callback"

    async def answer(self, text: Optional[str] = None, show_alert: bool = False,
                     **kwargs: Any) -> bool:
        if text:
            self.bot.screen.notice(str(text), "warn" if show_alert else "info")
        return True


def build_cli_conversation_objects(chat_id: int, screen: Optional[TerminalScreen] = None):
    """一次造好三件套，调用方直接喂给 process_conversation。

    对照 web_bridge.build_web_conversation_objects。CLI 没有 outbox 概念
    （没有多订阅者广播需要），所以签名比 Web 那边少一个参数。
    """
    bot = CliBot(chat_id, screen)
    return CliUpdate(bot, chat_id), CliContext(bot), bot


def build_cli_callback_objects(chat_id: int, callback_data: str, message_id: int,
                               screen: Optional[TerminalScreen] = None):
    """对照 web_bridge.build_web_callback_objects：CLI 按钮点击（编号输入）三件套。"""
    bot = CliBot(chat_id, screen)
    msg = CliMessage(bot, int(message_id or 0), chat_id)
    query = CliCallbackQuery(bot, msg, str(callback_data or ""), chat_id)
    update = CliUpdate(bot, chat_id)
    update.callback_query = query
    update.message = None
    return update, CliContext(bot), bot


def build_cli_command_objects(chat_id: int, command_text: str,
                              screen: Optional[TerminalScreen] = None):
    """对照 web_bridge.build_web_command_objects：CLI /命令 三件套。

    额外解析 context.args：PTB 的 CommandHandler 会把命令后面的空白分隔片段
    塞进 context.args，cmd_token_stats（/stats 7、/stats 2025-01-01 …）这类
    handler 直接读它。不解析的话参数会被静默丢掉，"/stats 7" 退化成 "/stats"。
    """
    bot = CliBot(chat_id, screen)
    update = CliUpdate(bot, chat_id)
    text = str(command_text or "")
    update.message.text = text
    context = CliContext(bot)
    context.args = text.split()[1:]
    return update, context, bot


__all__ = [
    "CliMessage",
    "CliBot",
    "CliUpdate",
    "CliContext",
    "CliCallbackQuery",
    "build_cli_conversation_objects",
    "build_cli_callback_objects",
    "build_cli_command_objects",
    "get_last_menu_options",
    "get_last_menu_labels",
    "menu_is_top",
    "dismiss_menu",
    "get_screen",
    "set_screen",
    "reset_menu_state",
]
