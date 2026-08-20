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
    print() 即可，不需要多订阅者广播机制。
  - 没有认证——本地终端场景下"能跑这个进程"本身就是权限凭证，与 Web 端
    需要密码墙的定位不同（Web 是给远程浏览器访问设计的）。
  - edit_text 没有真实的"原地编辑"能力：终端不像聊天气泡那样可以对某一条
    历史消息做原地替换。本模块选择了最简单的实现——重新打印一行标了
    "[更新]" 前缀的内容，不做 ANSI 光标控制去覆盖之前的输出。这是范围
    内的刻意简化（见 CLI 规划文档的"不做终端富文本渲染"边界），如果
    未来要做真正的原地刷新，需要引入光标控制或者一个基于 curses 的渲染层，
    这不是本次"验证接口能否解耦"这个任务要解决的问题。
  - send_photo/send_document 不能像 Web 那样生成 <img>/下载链接，只能打印
    文件保存路径——这和 Web 端"没有本地路径时"的降级路径是同一种处理，
    只是 CLI 端永远这样处理（终端没有内嵌显示图片、没有可点击链接的概念）。

本模块不 import telegram，不依赖 sections 命名空间，可以直接单测。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# CLI 侧假 message_id 全局计数器。CLI 是单进程单会话，不需要像 Web 那样
# 刻意避开真实 Telegram message_id 的取值范围（没有 TG->CLI 镜像这回事），
# 但仍然从 1 开始递增，保证同一次运行内 message_id 不重复，足够
# byMessageId 风格的查找（如果未来需要）使用。
_CLI_MESSAGE_ID_LOCK = threading.Lock()
_cli_message_id_counter = 1


def _allocate_cli_message_id() -> int:
    global _cli_message_id_counter
    with _CLI_MESSAGE_ID_LOCK:
        mid = _cli_message_id_counter
        _cli_message_id_counter += 1
        return mid


# 最近一次打印的菜单对应的 callback_data 列表，供 xgent_cli.py 的输入循环
# 把用户输入的编号翻译回真实的 callback_data。CLI 是单进程单会话，模块级
# 变量就够用，不需要 Web 端那种按 session/连接隔离的机制。每次
# CliBot.send_message/edit_message_text 打印新菜单时覆盖，没有菜单
# （reply_markup=None）时清空——避免用户在两轮不相关的对话之间误触发
# 上一次菜单的编号。
_last_menu_options: List[str] = []


def get_last_menu_options() -> List[str]:
    return list(_last_menu_options)


def _set_last_menu_options(reply_markup: Any) -> None:
    global _last_menu_options
    _last_menu_options = _extract_callback_options(reply_markup)


def _markup_to_lines(reply_markup: Any) -> Optional[List[str]]:
    """把 InlineKeyboardMarkup 转成终端可读的编号列表文本。

    对照 web_bridge.py 的 _markup_to_frame：那边拍平成 JSON 给前端渲染按钮，
    这里直接拍平成文本行，格式类似：
      [1] 按钮文字
      [2] 另一个按钮
    用户输入编号即可触发对应 callback_data，等价于点击。这一步也是本次
    任务要验证的关键点之一：如果 callbacks.py 的路由真的只靠 callback_data
    字符串匹配（前期审计已确认），这里只需要把编号映射回 callback_data
    字符串，不需要模拟任何 Telegram 消息对象的其它属性。
    """
    if reply_markup is None:
        return None
    keyboard = getattr(reply_markup, "inline_keyboard", None)
    if not keyboard:
        return None
    lines: List[str] = []
    idx = 1
    for row in keyboard:
        for button in row:
            callback_data = str(getattr(button, "callback_data", "") or "")
            if not callback_data:
                # url/web_app 按钮在终端里同样没有意义，跳过——与 Web 前端
                # renderButtons 里 "if (!btn.callback_data) return" 的处理
                # 完全对应（web_bridge.py 的 _markup_to_frame 保留了这些按钮，
                # 是前端 index.html 自己在渲染时跳过；CLI 没有独立的前端层，
                # 所以在这里就地跳过，效果一致）。
                continue
            text = str(getattr(button, "text", "") or "")
            lines.append(f"  [{idx}] {text}")
            idx += 1
    return lines or None


def _extract_callback_options(reply_markup: Any) -> List[str]:
    """按钮的 callback_data 列表，顺序与 _markup_to_lines 的编号一一对应。"""
    if reply_markup is None:
        return []
    keyboard = getattr(reply_markup, "inline_keyboard", None)
    if not keyboard:
        return []
    options: List[str] = []
    for row in keyboard:
        for button in row:
            callback_data = str(getattr(button, "callback_data", "") or "")
            if callback_data:
                options.append(callback_data)
    return options


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

    async def delete(self) -> bool:
        return await self.bot.delete_message(chat_id=self.chat_id, message_id=self.message_id)


class CliBot:
    """把对话核心的调用直接打印到终端。

    对照 web_bridge.WebBot——同样的方法集合，同样的 _is_xgent_web_bot 标记
    （复用这个既有的判别点，rendering.py 里检查它来跳过 TelegramRichAPI
    的私有协议直连、跳过 TG->Web 镜像安装，CLI 需要同样的行为，没有必要
    另开一个 _is_xgent_cli_bot 标记再改一遍判断点）。

    只实现对话核心与 Agent 循环实际会用到的方法。缺方法会在运行时抛
    AttributeError 而不是静默失败——与 WebBot 的设计取舍一致：宁可炸出来，
    也不要让用户以为消息发出去了。
    """

    _is_xgent_web_bot = True

    def __init__(self, chat_id: int):
        self.chat_id = chat_id

    def _allocate_message_id(self) -> int:
        return _allocate_cli_message_id()

    def _print(self, text: str, prefix: str = "") -> None:
        rendered = text if not prefix else f"{prefix}{text}"
        print(rendered)

    async def send_message(self, chat_id: int, text: str, reply_markup: Any = None,
                           parse_mode: Any = None, **kwargs: Any) -> CliMessage:
        message_id = self._allocate_message_id()
        self._print(str(text), prefix="\n\U0001f916 ")
        _set_last_menu_options(reply_markup)
        lines = _markup_to_lines(reply_markup)
        if lines:
            print("\n".join(lines))
        return CliMessage(self, message_id, chat_id, str(text))

    async def edit_message_text(self, text: str, chat_id: Optional[int] = None,
                                message_id: Optional[int] = None, reply_markup: Any = None,
                                parse_mode: Any = None, **kwargs: Any) -> CliMessage:
        # 终端没有"原地编辑历史某一行"的能力，见模块顶部说明：这里选择
        # 最简单的实现——重新打印一份标了前缀的内容，不做光标控制。
        self._print(str(text), prefix="\n\U0001f504 [更新] ")
        _set_last_menu_options(reply_markup)
        lines = _markup_to_lines(reply_markup)
        if lines:
            print("\n".join(lines))
        return CliMessage(self, int(message_id or 0), int(chat_id or self.chat_id), str(text))

    async def edit_message_reply_markup(self, chat_id: Optional[int] = None,
                                        message_id: Optional[int] = None,
                                        reply_markup: Any = None, **kwargs: Any) -> bool:
        _set_last_menu_options(reply_markup)
        lines = _markup_to_lines(reply_markup)
        if lines:
            print("\n".join(lines))
        return True

    async def delete_message(self, chat_id: Optional[int] = None,
                             message_id: Optional[int] = None, **kwargs: Any) -> bool:
        # 终端没有"删除历史输出"的概念，静默忽略即可——这与 Web 端
        # emit("delete", ...) 交给前端处理 DOM 节点是同一类"平台特有能力，
        # CLI 没有对应物就降级为空操作"的取舍。
        return True

    async def send_chat_action(self, chat_id: Optional[int] = None,
                               action: Any = None, **kwargs: Any) -> bool:
        # "typing" 状态在终端里没有直接对应物，且高频调用（keep_typing_while_waiting
        # 每隔几秒就调一次）——打印出来只会刷屏，直接忽略。
        return True

    async def send_document(self, chat_id: Optional[int] = None, document: Any = None,
                            caption: Optional[str] = None, filename: Optional[str] = None,
                            **kwargs: Any) -> CliMessage:
        name = filename or getattr(document, "filename", None) or getattr(document, "name", None)
        display_name = str(name) if name else "file"
        message_id = self._allocate_message_id()
        local_path = getattr(document, "name", None) if hasattr(document, "name") else None
        path_hint = f"（本地路径：{local_path}）" if isinstance(local_path, str) else ""
        self._print(f"\U0001f4ce 已保存文件：{display_name}{path_hint}"
                    + (f"\n   {caption}" if caption else ""))
        return CliMessage(self, message_id, int(chat_id or self.chat_id), str(caption or ""))

    async def send_photo(self, chat_id: Optional[int] = None, photo: Any = None,
                         caption: Optional[str] = None, **kwargs: Any) -> CliMessage:
        message_id = self._allocate_message_id()
        local_path = getattr(photo, "name", None) if hasattr(photo, "name") else None
        path_hint = f"（本地路径：{local_path}）" if isinstance(local_path, str) else ""
        self._print(f"\U0001f5bc️ 已生成图片{path_hint}"
                    + (f"\n   {caption}" if caption else ""))
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


class CliCallbackQuery:
    """对照 web_bridge.WebCallbackQuery：只实现对话核心/回调路由用到的部分。

    CLI 场景下"点击按钮"的等价操作是"输入编号"——由调用方（xgent_cli.py
    的输入循环）把编号翻译成 callback_data 字符串后，构造这个对象。
    answer() 把提示文本直接打印，等价于 Web 端弹 toast。
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
            prefix = "⚠️ " if show_alert else "ℹ️ "
            print(f"{prefix}{text}")
        return True


def build_cli_conversation_objects(chat_id: int):
    """一次造好三件套，调用方直接喂给 process_conversation。

    对照 web_bridge.build_web_conversation_objects。CLI 没有 outbox 概念
    （没有多订阅者广播需要），所以签名比 Web 那边少一个参数。
    """
    bot = CliBot(chat_id)
    return CliUpdate(bot, chat_id), CliContext(bot), bot


def build_cli_callback_objects(chat_id: int, callback_data: str, message_id: int):
    """对照 web_bridge.build_web_callback_objects：CLI 按钮点击（编号输入）三件套。"""
    bot = CliBot(chat_id)
    msg = CliMessage(bot, int(message_id or 0), chat_id)
    query = CliCallbackQuery(bot, msg, str(callback_data or ""), chat_id)
    update = CliUpdate(bot, chat_id)
    update.callback_query = query
    update.message = None
    return update, CliContext(bot), bot


def build_cli_command_objects(chat_id: int, command_text: str):
    """对照 web_bridge.build_web_command_objects：CLI /命令 三件套。"""
    bot = CliBot(chat_id)
    update = CliUpdate(bot, chat_id)
    update.message.text = str(command_text or "")
    return update, CliContext(bot), bot


__all__ = [
    "CliMessage",
    "CliBot",
    "CliUpdate",
    "CliContext",
    "CliCallbackQuery",
    "build_cli_conversation_objects",
    "build_cli_callback_objects",
    "build_cli_command_objects",
]
