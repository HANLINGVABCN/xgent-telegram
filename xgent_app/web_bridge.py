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

import contextlib
import logging
import queue
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 网页侧假 message_id 全局计数器。起始放到 1,000,000 以上，刻意避开真实
# Telegram message_id（通常是小整数），让前端 byMessageId 不会把 TG->Web 镜像
# 帧和网页原生帧搞混。WebBot / MirrorBot 共用这一个计数器，保证命令、按钮、
# 对话各自新建实例时 id 仍然唯一。
_WEB_MESSAGE_ID_LOCK = threading.Lock()
_web_message_id_counter = 1_000_000


def _allocate_web_message_id() -> int:
    global _web_message_id_counter
    with _WEB_MESSAGE_ID_LOCK:
        mid = _web_message_id_counter
        _web_message_id_counter += 1
        return mid


# TG->Web 镜像补丁的重入保护。key = id(ExtBot 类)，value = [depth, restore_fn]。
# 补丁是类级全局的：一旦某次 install 后 restore 没按预期成对执行（重入交错、
# install 与 try 之间抛异常），残留的 wrapper 会在下次 install 时被 getattr(cls)
# 当作“原方法”再次包裹——新 wrapper 转发 saved(real_bot, chat_id=..., text=...)，
# 而旧 wrapper 形参是 (*args, **kwargs)，会把 real_bot 收进 args[0]，再转发时
# real_bot 就落进原方法签名的 chat_id 位置，与关键字 chat_id 冲突，报
# “got multiple values for argument 'chat_id'”。用引用计数保证：已打补丁时只
# 增计数不重裹，最后一个 release 才真正还原类，从根上杜绝双层包裹。
_ACTIVE_MIRRORS: Dict[int, List[Any]] = {}


def _offer(q: "queue.Queue[Optional[Dict[str, Any]]]", item: Optional[Dict[str, Any]]) -> None:
    """向单条队列投递；满了就丢它自己最旧的一帧腾位置。绝不阻塞。"""
    try:
        q.put_nowait(item)
    except queue.Full:
        # 这个消费端跟不上（浏览器关了但连接还没断干净）。丢最旧的一帧，
        # 宁可让那个页面少显示一段，也不能把持有全局对话锁的协程卡住。
        try:
            q.get_nowait()
            q.put_nowait(item)
        except (queue.Empty, queue.Full):
            pass


class WebOutbox:
    """帧广播总线：asyncio 侧生产，N 个 SSE 线程各自消费一份**完整**的帧流。

    对话核心跑在 PTB 的事件循环里，SSE 响应写在 http.server 的工作线程里，
    两边跨线程，所以用线程安全的 queue.Queue 而不是 asyncio.Queue。

    早期实现是**单个** queue.Queue、所有 SSE 连接共用，这是个严重的错误：
    queue.get() 是「取走」而不是「广播」，于是每一帧只会落到其中一个连接上。
    后果是——
      - 开两个标签页（或 Telegram 内嵌 WebApp + 桌面浏览器）会互相抢消息，
        谁都收不全；
      - 前端断线重连后，旧连接的服务端线程还阻塞在 get(timeout=25) 里，最长
        25 秒内它会继续抢帧、再写进已经死掉的 socket，这些帧就此**永久丢失**。
    表现就是网页消息缺失、错乱、不即时，必须手动刷新（重新拉 history）才恢复。

    现在每个订阅者持有一条独立队列，put() 向所有订阅者各投一份，订阅者退出时
    自行摘除。没有订阅者时 put() 直接丢弃：没人在线就不该攒帧，前端连上后会
    重新拉一次 history 作为真相源，攒下来的旧帧反而会盖在新历史上造成错乱。

    只提供 subscribe() 而不提供 outbox 级的 get()：广播总线上「先 put 再 get」
    本来就收不到，留一个看起来能用的 get() 只会把这类 bug 引回来。
    """

    def __init__(self, maxsize: int = 1000):
        self._maxsize = max(1, int(maxsize))
        self._lock = threading.Lock()
        self._queues: List["queue.Queue[Optional[Dict[str, Any]]]"] = []
        self._closed = threading.Event()

    def subscribe(self) -> "WebSubscription":
        """注册一个订阅者，返回它的私有帧视图。

        用 with 语句，或手动 close()，否则队列会留在广播列表里被一直投递。
        """
        q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._queues.append(q)
        if self._closed.is_set():
            # 在 close() 之后才订阅：立刻塞 None 唤醒，别让消费者干等一个 timeout。
            _offer(q, None)
        return WebSubscription(self, q)

    def _drop(self, q: "queue.Queue[Optional[Dict[str, Any]]]") -> None:
        with self._lock:
            with contextlib.suppress(ValueError):
                self._queues.remove(q)

    def put(self, frame: Dict[str, Any]) -> None:
        """非阻塞广播给当前所有订阅者。"""
        if self._closed.is_set():
            return
        with self._lock:
            targets = list(self._queues)
        for q in targets:
            _offer(q, frame)

    def close(self) -> None:
        self._closed.set()
        # 给每个订阅者塞一个 None，唤醒可能正在阻塞的消费者
        with self._lock:
            targets = list(self._queues)
        for q in targets:
            _offer(q, None)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def subscriber_count(self) -> int:
        """当前在线的 SSE 连接数，供测试与排查用。"""
        with self._lock:
            return len(self._queues)


class WebSubscription:
    """单个 SSE 连接的私有帧视图。每个订阅者都能看到全量帧流。"""

    def __init__(self, outbox: "WebOutbox", q: "queue.Queue[Optional[Dict[str, Any]]]"):
        self._outbox = outbox
        self._queue = q

    def get(self, timeout: float = 25.0) -> Optional[Dict[str, Any]]:
        """取一帧；超时返回 None，供调用方发 SSE 心跳保活。"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._outbox._drop(self._queue)

    def __enter__(self) -> "WebSubscription":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False


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

    # 标记位：process_conversation 据此识别"本轮来自网页"，跳过 TG->Web 镜像安装。
    _is_xgent_web_bot = True

    def __init__(self, outbox: WebOutbox, chat_id: int):
        self.outbox = outbox
        self.chat_id = chat_id
        self._next_message_id = 1
        self._id_lock = threading.Lock()

    def _allocate_message_id(self) -> int:
        # 用全局计数器，避免不同 WebBot 实例（每次命令/回调都新建一个）id 重复。
        return _allocate_web_message_id()

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


class MirrorBot:
    """双通道输出 Bot：每次发送既推帧给网页（SSE），又发给真实 Telegram bot。

    网页版和 Telegram 共用同一套对话核心，但默认各走各的输出通道——网页发的
    Telegram 看不到，反之亦然。MirrorBot 把两条通道并起来：对话核心调用
    send_message / edit_message_text 等方法时，这里同时：

      1. 调真实 PTB bot 对应方法（消息进 Telegram 聊天）；
      2. 推一帧到 WebOutbox（消息进网页 SSE）。

    real_bot 为 None 时退化为纯网页输出，行为等价于 WebBot。维护 fake_id ->
    real_id 映射，让后续 edit/delete 能定位到 Telegram 里的真实消息。

    Telegram 侧调用失败（消息超长、HTML 解析失败等）只记日志，不抛出——网页
    那一帧照发，避免一边坏了把整轮对话拖崩。这会让两端偶尔不一致，但比直接
    中断对话可控。
    """

    # 标记位：process_conversation 据此识别"本轮来自网页"，跳过 TG->Web 镜像安装。
    _is_xgent_web_bot = True

    def __init__(self, outbox: WebOutbox, chat_id: int, real_bot: Any = None):
        self.outbox = outbox
        self.chat_id = chat_id
        self.real_bot = real_bot
        self._next_message_id = 1
        self._id_lock = threading.Lock()
        self._id_map: Dict[int, int] = {}  # fake_id -> real Telegram message_id

    def _allocate_message_id(self) -> int:
        return _allocate_web_message_id()

    def _emit(self, frame_type: str, **fields: Any) -> None:
        frame: Dict[str, Any] = {"type": frame_type, "ts": time.time()}
        frame.update(fields)
        self.outbox.put(frame)

    def _resolve_real_id(self, message_id: Optional[int]) -> Optional[int]:
        """fake_id -> real_id。没映射就当它本身就是真实 id（部分代码会直接传 real id）。"""
        if message_id is None:
            return None
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return None
        return self._id_map.get(mid, mid)

    async def _tg_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self.real_bot is None:
            return None
        try:
            return await getattr(self.real_bot, method)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 —— 刻意宽，TG 失败不能拖垮网页
            logger.warning("MirrorBot Telegram %s 失败: %s", method, exc)
            return None

    async def send_message(self, chat_id: Optional[int] = None, text: str = "",
                           reply_markup: Any = None, parse_mode: Any = None,
                           **kwargs: Any) -> WebMessage:
        target = int(chat_id) if chat_id is not None else self.chat_id
        message_id = self._allocate_message_id()
        real = await self._tg_call(
            "send_message", chat_id=target, text=str(text),
            reply_markup=reply_markup, parse_mode=parse_mode, **kwargs,
        )
        if real is not None and getattr(real, "message_id", None) is not None:
            self._id_map[message_id] = real.message_id
        self._emit(
            "message", message_id=message_id, text=str(text),
            parse_mode=str(parse_mode) if parse_mode else None,
            reply_markup=_markup_to_frame(reply_markup),
        )
        return WebMessage(self, message_id, target, str(text))

    async def edit_message_text(self, text: str, chat_id: Optional[int] = None,
                                message_id: Optional[int] = None, reply_markup: Any = None,
                                parse_mode: Any = None, **kwargs: Any) -> WebMessage:
        target = int(chat_id) if chat_id is not None else self.chat_id
        real_id = self._resolve_real_id(message_id)
        if real_id is not None:
            await self._tg_call(
                "edit_message_text", chat_id=target, message_id=real_id,
                text=str(text), reply_markup=reply_markup, parse_mode=parse_mode, **kwargs,
            )
        self._emit(
            "edit", message_id=message_id, text=str(text),
            parse_mode=str(parse_mode) if parse_mode else None,
            reply_markup=_markup_to_frame(reply_markup),
        )
        return WebMessage(self, int(message_id or 0), target, str(text))

    async def edit_message_reply_markup(self, chat_id: Optional[int] = None,
                                        message_id: Optional[int] = None,
                                        reply_markup: Any = None, **kwargs: Any) -> bool:
        target = int(chat_id) if chat_id is not None else self.chat_id
        real_id = self._resolve_real_id(message_id)
        if real_id is not None:
            await self._tg_call(
                "edit_message_reply_markup", chat_id=target, message_id=real_id,
                reply_markup=reply_markup, **kwargs,
            )
        self._emit("edit_markup", message_id=message_id,
                   reply_markup=_markup_to_frame(reply_markup))
        return True

    async def delete_message(self, chat_id: Optional[int] = None,
                             message_id: Optional[int] = None, **kwargs: Any) -> bool:
        target = int(chat_id) if chat_id is not None else self.chat_id
        real_id = self._resolve_real_id(message_id)
        if real_id is not None:
            await self._tg_call("delete_message", chat_id=target, message_id=real_id, **kwargs)
        self._emit("delete", message_id=message_id)
        return True

    async def send_chat_action(self, chat_id: Optional[int] = None,
                               action: Any = None, **kwargs: Any) -> bool:
        if self.real_bot is not None:
            await self._tg_call(
                "send_chat_action", chat_id=int(chat_id) if chat_id is not None else self.chat_id,
                action=action or "typing", **kwargs,
            )
        self._emit("chat_action", action=str(action) if action else "typing")
        return True

    async def send_document(self, chat_id: Optional[int] = None, document: Any = None,
                            caption: Optional[str] = None, filename: Optional[str] = None,
                            **kwargs: Any) -> WebMessage:
        target = int(chat_id) if chat_id is not None else self.chat_id
        name = filename or getattr(document, "filename", None) or getattr(document, "name", None)
        message_id = self._allocate_message_id()
        real = await self._tg_call(
            "send_document", chat_id=target, document=document,
            caption=caption, filename=filename, **kwargs,
        )
        if real is not None and getattr(real, "message_id", None) is not None:
            self._id_map[message_id] = real.message_id
        self._emit("document", message_id=message_id,
                   filename=str(name) if name else "file",
                   caption=str(caption) if caption else None)
        return WebMessage(self, message_id, target, str(caption or ""))

    async def send_photo(self, chat_id: Optional[int] = None, photo: Any = None,
                         caption: Optional[str] = None, **kwargs: Any) -> WebMessage:
        target = int(chat_id) if chat_id is not None else self.chat_id
        message_id = self._allocate_message_id()
        real = await self._tg_call(
            "send_photo", chat_id=target, photo=photo, caption=caption, **kwargs,
        )
        if real is not None and getattr(real, "message_id", None) is not None:
            self._id_map[message_id] = real.message_id
        self._emit("photo", message_id=message_id,
                   caption=str(caption) if caption else None)
        return WebMessage(self, message_id, target, str(caption or ""))

    async def get_file(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Web 端不支持下载 Telegram 文件")


class MirrorMessage:
    """代理真实 PTB Message：数据属性透传，发送/编辑方法走 MirrorBot 以镜像到网页。

    Telegram 侧对话核心大量使用 ``update.message.reply_text`` / ``status_msg.edit_text``
    这类方法。直接把这些调用喂给真实 Message 只会进 Telegram，网页看不到。本类把
    这几个写方法改路由到 MirrorBot，其余属性（text / from_user / chat / message_id /
    photo / document 等）原样透传给真实对象，保证读取语义不变。
    """

    def __init__(self, real_message: Any, bot: MirrorBot):
        object.__setattr__(self, "_real", real_message)
        object.__setattr__(self, "_bot", bot)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    @property
    def chat_id(self) -> Any:
        return getattr(self._real, "chat_id", None)

    async def reply_text(self, text: str, **kwargs: Any) -> WebMessage:
        return await self._bot.send_message(chat_id=self.chat_id, text=text, **kwargs)

    async def reply_markdown(self, text: str, **kwargs: Any) -> WebMessage:
        return await self._bot.send_message(chat_id=self.chat_id, text=text, **kwargs)

    async def reply_document(self, document: Any = None, **kwargs: Any) -> WebMessage:
        return await self._bot.send_document(chat_id=self.chat_id, document=document, **kwargs)

    async def reply_photo(self, photo: Any = None, **kwargs: Any) -> WebMessage:
        return await self._bot.send_photo(chat_id=self.chat_id, photo=photo, **kwargs)

    async def edit_text(self, text: str, **kwargs: Any) -> WebMessage:
        return await self._bot.edit_message_text(
            chat_id=self.chat_id, message_id=self._real.message_id, text=text, **kwargs,
        )

    async def edit_reply_markup(self, reply_markup: Any = None, **kwargs: Any) -> bool:
        return await self._bot.edit_message_reply_markup(
            chat_id=self.chat_id, message_id=self._real.message_id, reply_markup=reply_markup, **kwargs,
        )

    async def delete(self) -> bool:
        return await self._bot.delete_message(
            chat_id=self.chat_id, message_id=self._real.message_id,
        )


class WebCallbackQuery:
    """对照 PTB CallbackQuery：只实现对话核心/回调路由用到的部分。

    answer() 把提示文本作为 callback_answer 帧推给网页，由前端弹 toast/弹窗。
    message 用 WebMessage（绑定 WebBot），edit_text/reply_text 走网页帧。
    """

    def __init__(self, bot: WebBot, message: WebMessage, data: str, user_id: int):
        self.bot = bot
        self.message = message
        self.data = data
        self.from_user = type("WebUser", (), {
            "id": user_id, "full_name": "Web", "username": None,
        })()
        self.id = "web-callback"

    async def answer(self, text: Optional[str] = None, show_alert: bool = False,
                     **kwargs: Any) -> bool:
        if text:
            self.bot._emit("callback_answer", text=str(text), show_alert=bool(show_alert))
        return True


def build_web_conversation_objects(chat_id: int, outbox: WebOutbox):
    """一次造好三件套，调用方直接喂给 process_conversation。"""
    bot = WebBot(outbox, chat_id)
    return WebUpdate(bot, chat_id), WebContext(bot), bot


def build_web_mirror_objects(chat_id: int, outbox: WebOutbox, real_bot: Any):
    """带 Telegram 镜像的对话三件套：网页消息同时进 Telegram。

    用于网页发起的普通对话（非 /命令、非按钮）。/命令和按钮走
    build_web_command_objects / build_web_callback_objects（纯网页，不回灌 TG），
    避免在网页点菜单时往 Telegram 刷一堆菜单消息。
    """
    bot = MirrorBot(outbox, chat_id, real_bot)
    update = WebUpdate(bot, chat_id)
    return update, WebContext(bot), bot


def build_web_callback_objects(chat_id: int, outbox: WebOutbox,
                               callback_data: str, message_id: int):
    """网页按钮点击三件套：复用 Telegram 的 handle_button_click 回调路由。"""
    bot = WebBot(outbox, chat_id)
    msg = WebMessage(bot, int(message_id or 0), chat_id)
    query = WebCallbackQuery(bot, msg, str(callback_data or ""), chat_id)
    update = WebUpdate(bot, chat_id)
    update.callback_query = query
    update.message = None
    return update, WebContext(bot), bot


def build_web_command_objects(chat_id: int, outbox: WebOutbox, command_text: str):
    """网页 /命令三件套：复用 Telegram 的 cmd_* 命令处理函数。"""
    bot = WebBot(outbox, chat_id)
    update = WebUpdate(bot, chat_id)
    update.message.text = str(command_text or "")
    return update, WebContext(bot), bot


def install_tg_to_web_mirror(real_bot: Any, outbox: WebOutbox):
    """TG -> Web 镜像：临时把真实 bot 的发送方法包成「发 TG + 推网页帧」。

    PTB 的 ExtBot 用 __slots__ 且实例无 __dict__，无法在实例上覆盖方法；
    TelegramObject 的 __setattr__ 也禁止实例属性赋值（连 object.__setattr__
    都被拒——send_message 是 read-only descriptor）。因此改为在**类**上用
    staticmethod 临时覆盖方法：staticmethod 不绑 self，wrapper 收到的 args
    不含 self，转发时显式把 real_bot 作为第一个参数传给原 function。

    类级覆盖是全局的，但全局对话锁保证同一时刻只有一轮对话在跑；并发按钮
    点击即使顺带被镜像，也只是让网页多看到一些菜单更新，可接受。返回的
    restore() 必须在 finally 里调用，否则真实 bot 会一直带着包装。

    与 MirrorBot（Web->TG）的区别：这里用的是真实 message_id，因为 Telegram 侧
    全程直接用真实 id；网页前端按帧里的 message_id 跟踪气泡，两套 id 互不冲突。
    """
    if real_bot is None or outbox is None:
        return lambda: None

    cls = type(real_bot)
    key = id(cls)

    # 重入保护：已有活跃补丁时复用，仅增计数、不再包裹，防止双层 wrapper 把
    # real_bot 注入成 chat_id（见模块顶部 _ACTIVE_MIRRORS 注释）。
    state = _ACTIVE_MIRRORS.get(key)
    if state is not None:
        state[0] += 1
        return _make_mirror_release(key)

    saved: Dict[str, Any] = {}

    def _patch(name: str, wrapper: Any) -> None:
        # 存类上的原 function（未绑定），用 staticmethod 覆盖类属性。
        # staticmethod 不绑 self，wrapper 的 args 不含 self，与原实例覆盖
        # 方案一致；转发时显式传 real_bot 给原 function。
        saved[name] = getattr(cls, name)
        setattr(cls, name, staticmethod(wrapper))

    def emit(frame_type: str, **fields: Any) -> None:
        frame: Dict[str, Any] = {"type": frame_type, "ts": time.time()}
        frame.update(fields)
        outbox.put(frame)

    def _kw_text(kwargs: Dict[str, Any], args: tuple, send: bool = False) -> str:
        text = kwargs.get("text")
        if text is not None:
            return str(text)
        if send and len(args) >= 2:  # send_message(chat_id, text, ...)
            return str(args[1])
        if args:  # edit_message_text(text, ...)
            return str(args[0])
        return ""

    # send_message
    async def send_message(*args: Any, **kwargs: Any) -> Any:
        result = await saved["send_message"](real_bot, *args, **kwargs)
        try:
            emit("message", message_id=getattr(result, "message_id", 0),
                 text=_kw_text(kwargs, args, send=True),
                 parse_mode=str(kwargs.get("parse_mode")) if kwargs.get("parse_mode") else None,
                 reply_markup=_markup_to_frame(kwargs.get("reply_markup")))
        except Exception:  # noqa: BLE001
            pass
        return result
    _patch("send_message", send_message)

    # edit_message_text(text, chat_id=None, message_id=None, ...)
    async def edit_message_text(*args: Any, **kwargs: Any) -> Any:
        result = await saved["edit_message_text"](real_bot, *args, **kwargs)
        try:
            mid = kwargs.get("message_id")
            if mid is None and len(args) >= 3:
                mid = args[2]
            emit("edit", message_id=mid, text=_kw_text(kwargs, args),
                 parse_mode=str(kwargs.get("parse_mode")) if kwargs.get("parse_mode") else None,
                 reply_markup=_markup_to_frame(kwargs.get("reply_markup")))
        except Exception:  # noqa: BLE001
            pass
        return result
    _patch("edit_message_text", edit_message_text)

    # edit_message_reply_markup
    async def edit_message_reply_markup(*args: Any, **kwargs: Any) -> Any:
        result = await saved["edit_message_reply_markup"](real_bot, *args, **kwargs)
        try:
            mid = kwargs.get("message_id")
            if mid is None and len(args) >= 2:
                mid = args[1]
            emit("edit_markup", message_id=mid,
                 reply_markup=_markup_to_frame(kwargs.get("reply_markup")))
        except Exception:  # noqa: BLE001
            pass
        return result
    _patch("edit_message_reply_markup", edit_message_reply_markup)

    # delete_message
    async def delete_message(*args: Any, **kwargs: Any) -> Any:
        result = await saved["delete_message"](real_bot, *args, **kwargs)
        try:
            mid = kwargs.get("message_id")
            if mid is None and len(args) >= 2:
                mid = args[1]
            emit("delete", message_id=mid)
        except Exception:  # noqa: BLE001
            pass
        return result
    _patch("delete_message", delete_message)

    # send_chat_action
    async def send_chat_action(*args: Any, **kwargs: Any) -> Any:
        result = await saved["send_chat_action"](real_bot, *args, **kwargs)
        try:
            emit("chat_action", action=str(kwargs.get("action") or "typing"))
        except Exception:  # noqa: BLE001
            pass
        return result
    _patch("send_chat_action", send_chat_action)

    # send_document
    async def send_document(*args: Any, **kwargs: Any) -> Any:
        result = await saved["send_document"](real_bot, *args, **kwargs)
        try:
            doc = kwargs.get("document")
            name = (kwargs.get("filename")
                    or getattr(doc, "filename", None)
                    or getattr(doc, "name", None)
                    or "file")
            emit("document", message_id=getattr(result, "message_id", 0),
                 filename=str(name),
                 caption=str(kwargs.get("caption")) if kwargs.get("caption") else None)
        except Exception:  # noqa: BLE001
            pass
        return result
    _patch("send_document", send_document)

    # send_photo
    async def send_photo(*args: Any, **kwargs: Any) -> Any:
        result = await saved["send_photo"](real_bot, *args, **kwargs)
        try:
            emit("photo", message_id=getattr(result, "message_id", 0),
                 caption=str(kwargs.get("caption")) if kwargs.get("caption") else None)
        except Exception:  # noqa: BLE001
            pass
        return result
    _patch("send_photo", send_photo)

    def restore() -> None:
        # 类属性设回原 function（non-data descriptor，访问实例时自动绑 self）。
        for name, fn in saved.items():
            try:
                setattr(cls, name, fn)
            except Exception:  # noqa: BLE001
                pass

    _ACTIVE_MIRRORS[key] = [1, restore]
    return _make_mirror_release(key)


def _make_mirror_release(key: int) -> Any:
    """引用计数 release：只有最后一个释放者才真正还原类属性。

    这样即使两个 process_conversation 交错（A 先 release、B 后 release），
    也不会在 A release 时就把类还原、再被 B 当作“原方法”重新包裹——只要还有
    任何一轮对话在用，补丁就保持原样；全都没了才一次性还原到真正的原始方法。
    """
    def release() -> None:
        state = _ACTIVE_MIRRORS.get(key)
        if state is None:
            return
        state[0] -= 1
        if state[0] <= 0:
            _ACTIVE_MIRRORS.pop(key, None)
            try:
                state[1]()
            except Exception:  # noqa: BLE001
                pass
    return release


__all__ = [
    "WebOutbox",
    "WebSubscription",
    "WebMessage",
    "WebBot",
    "WebUpdate",
    "WebContext",
    "WebCallbackQuery",
    "MirrorBot",
    "MirrorMessage",
    "install_tg_to_web_mirror",
    "build_web_conversation_objects",
    "build_web_mirror_objects",
    "build_web_callback_objects",
    "build_web_command_objects",
]
