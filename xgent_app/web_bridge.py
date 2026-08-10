"""Web Chat 与对话核心之间的鸭子类型垫片。

process_conversation() 只需要 update.effective_chat.id、
update.message.reply_text()、update.callback_query 以及 context.bot 上的
几个发送方法——它并不检查这些对象是不是真的 telegram 类型。

shell_triggers.py 里的 _SelfTriggerUpdate / _SelfTriggerContext 已经用同样
的方式把后台触发任务接进了对话核心，这里照抄那个形状，因此 Agent 模式、
协议执行、记忆写入、停止语义全部自动继承，无需在对话核心里加任何分支。

本模块不 import telegram，也不依赖 sections 命名空间，可以直接单测。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Dict, List, Optional


class WebOutbox:
    """asyncio 侧生产、HTTP 处理线程侧消费的帧队列。

    对话核心跑在 PTB 的事件循环里，SSE 响应写在 http.server 的工作线程里，
    两边跨线程，所以用线程安全的 queue.Queue 而不是 asyncio.Queue。
    """

    def __init__(self, maxsize: int = 1000):
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=maxsize)
        self._closed = threading.Event()

    def put(self, frame: Dict[str, Any]) -> None:
        """非阻塞投递。队列满时丢弃最旧的一帧，绝不阻塞对话核心。"""
        if self._closed.is_set():
            return
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            # 消费端跟不上（浏览器关了但连接没断干净）。丢最旧的一帧腾位置，
            # 宁可让页面少显示一段，也不能把持有全局对话锁的协程卡住。
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(frame)
            except (queue.Empty, queue.Full):
                pass

    def get(self, timeout: float = 25.0) -> Optional[Dict[str, Any]]:
        """取一帧；超时返回 None，供调用方发 SSE 心跳保活。"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._closed.set()
        # 塞一个 None 唤醒可能正在阻塞的消费者
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    @property
    def closed(self) -> bool:
        return self._closed.is_set()


class WebMessage:
    """假的 telegram Message：记录 message_id，支持后续编辑。"""

    def __init__(self, bot: "WebBot", message_id: int, chat_id: int, text: str = ""):
        self.bot = bot
        self.message_id = message_id
        self.chat_id = chat_id
        self.text = text
        # 对话核心里有 `update.message or update.callback_query.message` 的写法
        self.chat = type("WebChat", (), {"id": chat_id})()

    async def reply_text(self, text: str, **kwargs: Any) -> "WebMessage":
        return await self.bot.send_message(chat_id=self.chat_id, text=text, **kwargs)

    async def edit_text(self, text: str, **kwargs: Any) -> "WebMessage":
        return await self.bot.edit_message_text(
            chat_id=self.chat_id, message_id=self.message_id, text=text, **kwargs
        )

    async def delete(self) -> bool:
        return await self.bot.delete_message(chat_id=self.chat_id, message_id=self.message_id)


def _markup_to_frame(reply_markup: Any) -> Optional[List[List[Dict[str, str]]]]:
    """把 InlineKeyboardMarkup 拍平成可 JSON 化的结构。

    只取 text 和 callback_data——网页端唯一需要的按钮是"停止"。
    """
    if reply_markup is None:
        return None
    keyboard = getattr(reply_markup, "inline_keyboard", None)
    if not keyboard:
        return None
    rows: List[List[Dict[str, str]]] = []
    for row in keyboard:
        buttons: List[Dict[str, str]] = []
        for button in row:
            buttons.append({
                "text": str(getattr(button, "text", "")),
                "callback_data": str(getattr(button, "callback_data", "") or ""),
            })
        if buttons:
            rows.append(buttons)
    return rows or None


class WebBot:
    """把 PTB Bot 的调用序列化成 JSON 帧推进 WebOutbox。

    只实现对话核心与 Agent 循环实际会用到的方法。缺方法会在运行时抛
    AttributeError 而不是静默失败，这是刻意的：宁可炸出来，也不要让用户
    以为消息发出去了。
    """

    def __init__(self, outbox: WebOutbox, chat_id: int):
        self.outbox = outbox
        self.chat_id = chat_id
        self._next_message_id = 1
        self._id_lock = threading.Lock()

    def _allocate_message_id(self) -> int:
        with self._id_lock:
            message_id = self._next_message_id
            self._next_message_id += 1
            return message_id

    def _emit(self, frame_type: str, **fields: Any) -> None:
        frame: Dict[str, Any] = {"type": frame_type, "ts": time.time()}
        frame.update(fields)
        self.outbox.put(frame)

    async def send_message(self, chat_id: int, text: str, reply_markup: Any = None,
                           parse_mode: Any = None, **kwargs: Any) -> WebMessage:
        message_id = self._allocate_message_id()
        self._emit(
            "message",
            message_id=message_id,
            text=str(text),
            parse_mode=str(parse_mode) if parse_mode else None,
            reply_markup=_markup_to_frame(reply_markup),
        )
        return WebMessage(self, message_id, chat_id, str(text))

    async def edit_message_text(self, text: str, chat_id: Optional[int] = None,
                                message_id: Optional[int] = None, reply_markup: Any = None,
                                parse_mode: Any = None, **kwargs: Any) -> WebMessage:
        self._emit(
            "edit",
            message_id=message_id,
            text=str(text),
            parse_mode=str(parse_mode) if parse_mode else None,
            reply_markup=_markup_to_frame(reply_markup),
        )
        return WebMessage(self, int(message_id or 0), int(chat_id or self.chat_id), str(text))

    async def edit_message_reply_markup(self, chat_id: Optional[int] = None,
                                        message_id: Optional[int] = None,
                                        reply_markup: Any = None, **kwargs: Any) -> bool:
        self._emit("edit_markup", message_id=message_id,
                   reply_markup=_markup_to_frame(reply_markup))
        return True

    async def delete_message(self, chat_id: Optional[int] = None,
                             message_id: Optional[int] = None, **kwargs: Any) -> bool:
        self._emit("delete", message_id=message_id)
        return True

    async def send_chat_action(self, chat_id: Optional[int] = None,
                               action: Any = None, **kwargs: Any) -> bool:
        self._emit("chat_action", action=str(action) if action else "typing")
        return True

    async def send_document(self, chat_id: Optional[int] = None, document: Any = None,
                            caption: Optional[str] = None, filename: Optional[str] = None,
                            **kwargs: Any) -> WebMessage:
        # 文件本体不走 SSE：二进制塞进 JSON 帧会把内存和带宽打爆。
        # 只报告文件名和标题，正文里已经有服务器路径，用户可以用文件管理器取。
        name = filename or getattr(document, "filename", None) or getattr(document, "name", None)
        message_id = self._allocate_message_id()
        self._emit("document", message_id=message_id,
                   filename=str(name) if name else "file",
                   caption=str(caption) if caption else None)
        return WebMessage(self, message_id, int(chat_id or self.chat_id), str(caption or ""))

    async def send_photo(self, chat_id: Optional[int] = None, photo: Any = None,
                         caption: Optional[str] = None, **kwargs: Any) -> WebMessage:
        message_id = self._allocate_message_id()
        self._emit("photo", message_id=message_id,
                   caption=str(caption) if caption else None)
        return WebMessage(self, message_id, int(chat_id or self.chat_id), str(caption or ""))

    async def get_file(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Web 端不支持下载 Telegram 文件")


class WebUpdate:
    """对照 shell_triggers.py 的 _SelfTriggerUpdate。"""

    def __init__(self, bot: WebBot, chat_id: int):
        self.effective_chat = type("WebChat", (), {"id": chat_id})()
        self.effective_user = type("WebUser", (), {
            "id": chat_id, "full_name": "Web", "username": None,
        })()
        self.message = WebMessage(bot, 0, chat_id)
        self.callback_query = None


class WebContext:
    """对照 shell_triggers.py 的 _SelfTriggerContext。"""

    def __init__(self, bot: WebBot):
        self.bot = bot


def build_web_conversation_objects(chat_id: int, outbox: WebOutbox):
    """一次造好三件套，调用方直接喂给 process_conversation。"""
    bot = WebBot(outbox, chat_id)
    return WebUpdate(bot, chat_id), WebContext(bot), bot


__all__ = [
    "WebOutbox",
    "WebMessage",
    "WebBot",
    "WebUpdate",
    "WebContext",
    "build_web_conversation_objects",
]
