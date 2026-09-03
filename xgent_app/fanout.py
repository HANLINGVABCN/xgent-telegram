"""按通道隔离的出站扇出层：一个通道打不通，不许拖慢别的通道。

**要解决的问题。** 此前对话核心每产出一条消息，MirrorBot 都是
`await 真实TG调用` 之后才 `_emit` 网页帧（web_bridge.py 的 8 个镜像方法一律
这个顺序）。而流式渲染每 0.35 秒编辑一次，于是"网页延迟"直接等于"Telegram
延迟"：TG 断连时每次编辑都要等满一个 read_timeout（30s，上传路径上甚至到
1800s），网页看起来就是卡死。

**这里的结构。** 一个出站通道 = 一条有界队列 + 一个 worker + 一个熔断器。
对话核心调 ``offer()``（同步、非阻塞、永不抛）就算"发出去了"，真正的网络
往返发生在 worker 里。单 worker 单队列天然保证同通道内 send→edit→delete 的
FIFO 顺序——这条顺序丢了就是历史上的"上一条消息无限刷屏"。

**为什么不用 asyncio.Queue。** 生产者不止事件循环一家：web_server 的 HTTP
工作线程、cli_bridge 的写库守护线程都可能间接产出帧。asyncio.Queue 不是线程
安全的，所以入队侧用 deque + threading.Lock（与 WebOutbox 同一个取舍），出队
侧才是 asyncio worker。

**投递不出去的怎么办。** 落库，通道恢复后按序重放（见 ChannelWorker 的
_defer / drain_store）。丢弃 + 恢复提示是更省事的做法，但那意味着 TG 断连
期间网页上的对话在 Telegram 上永久缺失——"三端同步"就成了一句空话。

本模块只用标准库，不 import telegram、也不依赖 sections 命名空间，可以直接单测。
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

CIRCUIT_CLOSED = "closed"
CIRCUIT_OPEN = "open"
CIRCUIT_HALF_OPEN = "half_open"


class OpNotDeliverable(Exception):
    """这条操作永远投不出去，重试没有意义（目标 bot 没有这个方法、要发的文件
    已经不存在……）。

    与网络失败必须区分开：网络失败要落待发库、要计入熔断；这类要直接丢掉，
    既不占待发库，也不能把熔断器推开——否则一个拼错的方法名会让整条 Telegram
    通道被判定为"断线"。
    """


class CircuitBreaker:
    """连续失败到阈值就开闸，冷却后放一次探针。线程安全。

    没有它的话，TG 断连期间每一条消息都要老老实实等满一次超时才失败——
    一轮 Agent 对话几百次编辑，累计等待是小时级的。开闸之后直接跳过，
    投递路径变成 O(1)。
    """

    def __init__(self, failure_threshold: int = 3, cooldown: float = 60.0):
        self._threshold = max(1, int(failure_threshold))
        self._cooldown = max(1.0, float(cooldown))
        self._lock = threading.Lock()
        self._state = CIRCUIT_CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._skipped = 0

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def allow(self) -> bool:
        """现在能不能试着投递。开闸且未过冷却期时返回 False 并计入跳过数。"""
        with self._lock:
            if self._state != CIRCUIT_OPEN:
                return True
            if time.time() - self._opened_at >= self._cooldown:
                # 冷却到了：放一次探针。探针失败会立刻回到 open 并重置冷却。
                self._state = CIRCUIT_HALF_OPEN
                return True
            self._skipped += 1
            return False

    def record_success(self) -> int:
        """记一次成功。真正从"坏"恢复时返回期间跳过的次数，否则返回 0。"""
        with self._lock:
            recovered = self._state != CIRCUIT_CLOSED or self._failures > 0
            skipped = self._skipped if recovered else 0
            self._state = CIRCUIT_CLOSED
            self._failures = 0
            self._skipped = 0
            return skipped

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == CIRCUIT_HALF_OPEN or self._failures >= self._threshold:
                self._state = CIRCUIT_OPEN
                self._opened_at = time.time()

    def reset(self) -> None:
        with self._lock:
            self._state = CIRCUIT_CLOSED
            self._failures = 0
            self._opened_at = 0.0
            self._skipped = 0

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            data: Dict[str, Any] = {"circuit": self._state, "failures": self._failures}
            if self._state == CIRCUIT_OPEN:
                data["cooldown_left"] = round(
                    max(0.0, self._cooldown - (time.time() - self._opened_at)), 1)
                data["skipped"] = self._skipped
            return data


# 操作类型。刻意与 cli_relay_ops 的 op 名一致——CLI 跨进程中继早就在用这套
# 名字序列化"一次 bot 调用"了，再造第二套命名只会让两处对不上。
OP_SEND = "send_message"
OP_EDIT = "edit_message_text"
OP_EDIT_MARKUP = "edit_message_reply_markup"
OP_DELETE = "delete_message"
OP_CHAT_ACTION = "send_chat_action"
OP_DOCUMENT = "send_document"
OP_PHOTO = "send_photo"

# 会产生一个新的原生消息 id 的操作。
CREATING_KINDS = frozenset({OP_SEND, OP_DOCUMENT, OP_PHOTO})
# 需要先知道目标原生 id 才能投递的操作。目标不存在时**丢弃**，绝不退化成
# "新发一条"——那正是"上一条消息无限刷屏"的成因。
TARGETING_KINDS = frozenset({OP_EDIT, OP_EDIT_MARKUP, OP_DELETE})
# 后来者可以整个盖掉前者的操作：同一条消息的连续编辑只有最后一次有信息量。
SUPERSEDABLE_KINDS = frozenset({OP_EDIT, OP_EDIT_MARKUP, OP_CHAT_ACTION})
# 大文件上传：合法地会超过普通超时（idle.py 里 read_timeout 最高开到 1800s），
# 不套统一的 wait_for，靠 PTB 自己的超时 + 熔断计数收口。
UPLOAD_KINDS = frozenset({OP_DOCUMENT, OP_PHOTO})


class Op:
    """一次待投递的 bot 调用。

    payload 必须是 JSON 可序列化的（要落库重放）；transient 放不能序列化的
    东西（打开的文件对象、真的 InlineKeyboardMarkup），只用于**首次**投递，
    从库里重放时它是空的——所以能落库的上传操作必须同时在 payload 里留下
    本地路径。
    """

    __slots__ = ("kind", "logical_id", "chat_id", "payload", "transient",
                 "durable", "seq", "created_at", "row_id", "attempts", "superseded")

    def __init__(self, kind: str, *, logical_id: Optional[int] = None,
                 chat_id: Optional[int] = None,
                 payload: Optional[Dict[str, Any]] = None,
                 transient: Optional[Dict[str, Any]] = None,
                 durable: bool = True, seq: int = 0,
                 created_at: Optional[float] = None,
                 row_id: Optional[int] = None, attempts: int = 0):
        self.kind = kind
        self.logical_id = logical_id
        self.chat_id = chat_id
        self.payload = payload or {}
        self.transient = transient or {}
        self.durable = bool(durable)
        self.seq = seq
        self.created_at = time.time() if created_at is None else float(created_at)
        self.row_id = row_id
        self.attempts = attempts
        self.superseded = False

    @property
    def supersede_key(self) -> Optional[str]:
        """可被后来者整个盖掉时的归并键；不可归并返回 None。"""
        if self.kind not in SUPERSEDABLE_KINDS:
            return None
        if self.kind == OP_CHAT_ACTION:
            return f"{self.kind}:{self.chat_id}"
        if self.logical_id is None:
            return None
        return f"{self.kind}:{self.logical_id}"

    def to_row(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "logical_id": self.logical_id,
            "chat_id": self.chat_id,
            "payload": json.dumps(self.payload, ensure_ascii=False),
            "created_at": self.created_at,
            "seq": self.seq,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Op":
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        return cls(
            str(row.get("kind") or ""),
            logical_id=row.get("logical_id"),
            chat_id=row.get("chat_id"),
            payload=payload if isinstance(payload, dict) else {},
            seq=int(row.get("seq") or 0),
            created_at=row.get("created_at"),
            row_id=row.get("id"),
            attempts=1,
        )

    def __repr__(self) -> str:  # pragma: no cover —— 只为调试日志好看
        return f"<Op {self.kind} logical={self.logical_id} seq={self.seq}>"


def collapse_ops(ops: List[Op]) -> "tuple[List[Op], int]":
    """重放前归并：同一条消息的连续编辑只保留最后一次，被删除的消息之前的编辑全丢。

    用户选的策略是"全部持久重放"——**内容**一条不丢，这里丢掉的只是被后来者
    完全覆盖掉的中间态。理由是物理性的：流式渲染每 0.35s 编辑一次，一轮对话
    几百次编辑打到同一条消息上，原样重放的话 Telegram 侧几乎全部撞
    "message is not modified"/429，而可见的最终状态与只放最后一次逐字相同。
    发送、文件、删除、以及每条消息最后那次编辑一律原样按序重放。

    返回 (要重放的列表, 被归并掉的条数)。入参必须已按投递顺序排好。
    """
    deleted: Set[int] = set()
    last_index: Dict[str, int] = {}
    for index, op in enumerate(ops):
        if op.kind == OP_DELETE and op.logical_id is not None:
            deleted.add(op.logical_id)
        key = op.supersede_key
        if key is not None:
            last_index[key] = index

    keep: List[Op] = []
    dropped = 0
    for index, op in enumerate(ops):
        key = op.supersede_key
        if key is not None and last_index.get(key) != index:
            dropped += 1
            continue
        # 消息最终被删掉了：删除之前对它的编辑没有任何可见效果。
        if (op.kind in (OP_EDIT, OP_EDIT_MARKUP) and op.logical_id in deleted):
            dropped += 1
            continue
        keep.append(op)
    return keep, dropped


class ChannelWorker:
    """一个出站通道：有界队列 + 单 worker + 熔断器 + 持久待发库。

    对话核心只调 ``offer()``，它是同步、非阻塞、永不抛的。真正的网络往返在
    worker 里发生——这就是"某个通道断了不影响别的通道"的结构性保证，而不是
    靠每个调用点自己记得包 try。

    ``deliver(op, native_id) -> Optional[int]`` 由调用方注入：本模块不认识
    Telegram，也不认识数据库。创建类操作要返回该通道的原生消息 id。
    """

    def __init__(self, name: str,
                 deliver: Callable[[Op, Optional[int]], Any], *,
                 is_configured: Optional[Callable[[], bool]] = None,
                 maxsize: int = 2000, timeout: float = 15.0,
                 breaker: Optional[CircuitBreaker] = None,
                 store: Optional[Any] = None,
                 on_recovered: Optional[Callable[[int, int], Any]] = None):
        self.name = name
        self._deliver = deliver
        self._is_configured = is_configured or (lambda: True)
        self._maxsize = max(16, int(maxsize))
        self._timeout = float(timeout)
        self._breaker = breaker or CircuitBreaker()
        self._store = store
        self._on_recovered = on_recovered

        self._lock = threading.Lock()
        self._queue: "collections.deque[Op]" = collections.deque()
        self._pending_supersede: Dict[str, Op] = {}
        self._seq = 0

        # logical_id -> 本通道的原生 message_id。这就是原来 MirrorBot._id_map，
        # 从"一个 bot 垫片"搬到"一个通道"上——每个通道各自维护自己的 id 空间。
        self._native: Dict[int, int] = {}
        # 反查：原生 id -> logical_id。少数调用点会直接拿着真实 message_id 回来
        # （转发-读取-删除那套 trick、MirrorMessage 包装真实消息），推网页帧时
        # 必须换回网页见过的那个 id，否则前端 byMessageId 落空、每次编辑新建一个
        # 气泡——就是"上一条消息无限刷屏"。
        self._logical: Dict[int, int] = {}
        # 本通道见过的逻辑 id（入队即记，不等投递）。用来判断"核心传进来的这个
        # message_id 到底是逻辑 id 还是别处拿到的原生 id"——不能靠数值大小去猜：
        # CLI 进程分配的逻辑 id 从 1 起，和真实 Telegram message_id 撞在同一区间。
        self._issued: Dict[int, float] = collections.OrderedDict()
        self._failed: Set[int] = set()      # 永久失败：后续 edit/delete 直接丢
        self._deferred: Set[int] = set()    # 有操作躺在库里等重放

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._event: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._closing = False
        self._draining = False
        self._in_flight = False

        self._delivered = 0
        self._dropped = 0
        self._orphaned = 0
        self._coalesced = 0
        self._deferred_rows = 0
        self._failures = 0
        self._last_error: Optional[str] = None

    # --- 生命周期 ---

    def ensure_started(self) -> bool:
        """确保 worker 在跑。同步版本，供 offer() 惰性拉起。

        惰性启动是刻意的：通道的使用点（MirrorBot、trigger 投递、CLI 中继回放）
        分布在很多地方，要求每处都先 await start() 只会漏掉一处，而漏掉的表现是
        "消息静默不发"。没有运行中的事件循环时返回 False。
        """
        if self._task is not None and not self._task.done():
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._closing = False
        self._loop = loop
        self._event = asyncio.Event()
        self._task = loop.create_task(self._run(), name=f"xgent-channel-{self.name}")
        if self._store is not None:
            # 上次进程留下的待发操作：worker 空闲时会去补投（见 _run）。
            self._deferred_rows = max(self._deferred_rows, 1)
        return True

    async def start(self) -> None:
        self.ensure_started()

    async def aclose(self) -> None:
        self._closing = True
        self._wake()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass

    # --- 入队（同步、非阻塞、永不抛） ---

    def offer(self, op: Op) -> bool:
        """把一次 bot 调用交给通道。返回 False 表示队列满且这条被丢弃。

        永远不抛异常：调用方是对话核心，它正握着全局对话锁，任何异常或阻塞都会
        变成"整轮对话卡住"。
        """
        try:
            with self._lock:
                self._seq += 1
                op.seq = self._seq
                if op.kind in CREATING_KINDS and op.logical_id is not None:
                    self._note_issued(op.logical_id)
                key = op.supersede_key
                if key is not None:
                    prev = self._pending_supersede.get(key)
                    if prev is not None and not prev.superseded:
                        # 队列里还压着同一条消息的上一次编辑：直接从队列里摘掉。
                        # 只打墓碑不摘除的话，死条目仍然占着 maxsize 名额——一轮
                        # 对话几百次编辑就会把队列挤爆，挤掉的恰恰是真正需要送达
                        # 的 send。摘除是 O(队列长度)，而正因为摘除了，队列在流式
                        # 场景下几乎恒定只有一条，实际开销可以忽略。
                        prev.superseded = True
                        with contextlib.suppress(ValueError):
                            self._queue.remove(prev)
                        self._coalesced += 1
                    self._pending_supersede[key] = op
                accepted = True
                if len(self._queue) >= self._maxsize:
                    # 与 WebOutbox._offer 同一取舍：丢最旧的，绝不阻塞生产者。
                    # 走到这里说明积压的是发送/文件/删除这类不可归并的操作，属于
                    # 真的洪峰，要打日志——被丢掉的 send 会让它后续的 edit 一起
                    # 失去目标（worker 会安全丢弃，不会误投到别的消息上）。
                    victim = self._queue.popleft()
                    self._dropped += 1
                    logger.warning("通道 %s 队列已满（%d），丢弃最旧的 %s",
                                   self.name, self._maxsize, victim.kind)
                self._queue.append(op)
            self._wake()
            return accepted
        except Exception:  # noqa: BLE001 —— offer 绝不能把异常带给对话核心
            logger.debug("通道 %s 入队失败", self.name, exc_info=True)
            return False

    def _wake(self) -> None:
        loop, event = self._loop, self._event
        if loop is None or event is None:
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass  # 循环已关，进程正在退出

    def _pop(self) -> Optional[Op]:
        with self._lock:
            if not self._queue:
                return None
            op = self._queue.popleft()
            key = op.supersede_key
            if key is not None and self._pending_supersede.get(key) is op:
                self._pending_supersede.pop(key, None)
            return op

    def _queue_len(self) -> int:
        with self._lock:
            return len(self._queue)

    async def _run(self) -> None:
        while not self._closing:
            op = self._pop()
            if op is None:
                if self._deferred_rows and self._is_configured() and self._breaker.allow():
                    # 队列空了才做补投：优先保证实时流量。
                    await self._drain_store(0)
                event = self._event
                if event is not None:
                    event.clear()
                    if self._queue_len() == 0:
                        try:
                            await asyncio.wait_for(event.wait(), timeout=1.0)
                        except asyncio.TimeoutError:
                            pass
                else:  # pragma: no cover —— start() 之前不会跑到这里
                    await asyncio.sleep(0.05)
                continue
            self._in_flight = True
            try:
                await self._handle(op)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 单条失败不能让 worker 死掉
                logger.warning("通道 %s 处理 %s 时异常", self.name, op.kind, exc_info=True)
            finally:
                self._in_flight = False

    # --- 投递 ---

    async def _handle(self, op: Op) -> None:
        if op.superseded:
            await self._forget(op)
            return
        if not self._is_configured():
            # 通道压根没配置（例如这台机器没有 BOT_TOKEN）：不落库、不重试。
            # 攒着只会在用户某天配上 token 时突然刷出几千条历史消息。
            await self._forget(op)
            return

        native: Optional[int] = None
        if op.kind in TARGETING_KINDS:
            if op.logical_id in self._failed:
                self._orphaned += 1
                await self._forget(op)
                return
            if op.logical_id in self._deferred:
                await self._defer(op)
                return
            native = self._resolve_native(op)
            if native is None:
                # 目标从来没在这个通道上成功发出去过。**丢弃**，不退化成新发
                # 一条——那正是"上一条消息无限刷屏"的成因。
                self._orphaned += 1
                await self._forget(op)
                return
        elif op.logical_id is not None and op.logical_id in self._deferred:
            # 这条消息的创建操作还躺在库里等重放，后续操作必须排在它后面。
            await self._defer(op)
            return

        if not self._breaker.allow():
            await self._defer(op)
            return

        try:
            # 大文件上传合法地会超过统一超时（idle.py 里 read_timeout 最高开到
            # 1800s），_deliver_one 对这类不套 wait_for，只靠熔断计数收口。
            result = await self._deliver_one(op, native)
        except asyncio.CancelledError:
            raise
        except OpNotDeliverable as exc:
            # 不是网络问题：直接丢，不落库、不动熔断器。
            self._orphaned += 1
            self._last_error = str(exc)[:200]
            if op.kind in CREATING_KINDS and op.logical_id is not None:
                # 这条消息在本通道上永远不会存在，后续 edit/delete 一律丢弃。
                self._failed.add(int(op.logical_id))
            logger.info("通道 %s 跳过不可投递的 %s: %s", self.name, op.kind, self._last_error)
            await self._forget(op)
            return
        except Exception as exc:  # noqa: BLE001
            self._breaker.record_failure()
            self._failures += 1
            self._last_error = str(exc)[:200]
            logger.warning("通道 %s 投递 %s 失败（转入待发）: %s",
                           self.name, op.kind, self._last_error)
            await self._defer(op)
            return

        if op.kind in CREATING_KINDS and op.logical_id is not None and result is not None:
            self._remember(op.logical_id, result)
        elif op.kind == OP_DELETE and op.logical_id is not None:
            self._forget_id(op.logical_id)
        self._delivered += 1
        await self._forget(op)

        skipped = self._breaker.record_success()
        if skipped or self._deferred_rows:
            await self._drain_store(skipped)

    async def _forget(self, op: Op) -> None:
        """这条操作已经不需要再投了：库里有行就删掉。"""
        if op.row_id is None or self._store is None:
            return
        try:
            await self._store.delete([op.row_id])
        except Exception:  # noqa: BLE001
            logger.debug("通道 %s 删除待发行失败", self.name, exc_info=True)

    async def _defer(self, op: Op) -> None:
        """投不出去：落库等通道恢复后重放。

        落不了库（没配持久层、或这条带着不可序列化的文件对象）时只能放弃——
        但要把 logical_id 标成永久失败，让后续 edit/delete 直接丢，绝不允许
        "编辑一条并不存在的消息"退化成"新发一条"。
        """
        if op.row_id is not None:
            # 本来就是从库里捞出来重放的，行还在，不用重复落库。
            if op.logical_id is not None:
                self._deferred.add(int(op.logical_id))
            self._deferred_rows = max(self._deferred_rows, 1)
            return
        if self._store is None or not op.durable:
            if op.kind in CREATING_KINDS and op.logical_id is not None:
                self._failed.add(int(op.logical_id))
            self._dropped += 1
            return
        try:
            await self._store.append(self.name, op)
        except Exception:  # noqa: BLE001
            if op.kind in CREATING_KINDS and op.logical_id is not None:
                self._failed.add(int(op.logical_id))
            self._dropped += 1
            logger.warning("通道 %s 待发落库失败，这条只能丢弃", self.name, exc_info=True)
            return
        if op.logical_id is not None:
            self._deferred.add(int(op.logical_id))
        self._deferred_rows += 1

    async def _deliver_one(self, op: Op, native: Optional[int]) -> Any:
        if op.kind in UPLOAD_KINDS:
            return await self._deliver(op, native)
        return await asyncio.wait_for(self._deliver(op, native), timeout=self._timeout)

    async def _drain_store(self, skipped: int) -> None:
        """通道恢复后按序补投待发库。

        这是"全部持久重放"的落点：**内容**一条不丢。只有被后来者完全覆盖掉的
        中间态编辑会被归并（见 collapse_ops 的说明），可见的最终状态与原样重放
        逐字相同。
        """
        if self._store is None or self._draining:
            self._deferred_rows = 0 if self._store is None else self._deferred_rows
            return
        self._draining = True
        try:
            try:
                rows = await self._store.fetch(self.name, limit=1000)
            except Exception:  # noqa: BLE001
                logger.debug("通道 %s 读待发库失败", self.name, exc_info=True)
                return
            if not rows:
                self._deferred_rows = 0
                self._deferred.clear()
                return
            ops = [Op.from_row(row) for row in rows]
            for row_op in ops:
                if row_op.kind in CREATING_KINDS and row_op.logical_id is not None:
                    # 进程重启后待发库是唯一的"我发过哪些逻辑 id"的记录。
                    self._note_issued(row_op.logical_id)
            keep, collapsed = collapse_ops(ops)
            keep_marks = {id(op) for op in keep}
            stale = [op.row_id for op in ops
                     if id(op) not in keep_marks and op.row_id is not None]
            if stale:
                with contextlib.suppress(Exception):
                    await self._store.delete(stale)
            logger.info("通道 %s 开始补投 %d 条待发操作（归并掉 %d 条被覆盖的中间编辑）",
                        self.name, len(keep), collapsed)

            replayed = 0
            for op in keep:
                if self._closing or not self._breaker.allow():
                    break
                native: Optional[int] = None
                if op.kind in TARGETING_KINDS:
                    native = self._resolve_native(op)
                    if native is None:
                        # 对应的创建操作也在这批里、但排在后面被截断了；或者它
                        # 早已被放弃。丢掉，绝不新发一条。
                        self._orphaned += 1
                        await self._forget(op)
                        continue
                try:
                    result = await self._deliver_one(op, native)
                except asyncio.CancelledError:
                    raise
                except OpNotDeliverable as exc:
                    self._orphaned += 1
                    self._last_error = str(exc)[:200]
                    if op.kind in CREATING_KINDS and op.logical_id is not None:
                        self._failed.add(int(op.logical_id))
                    await self._forget(op)
                    continue
                except Exception as exc:  # noqa: BLE001
                    self._breaker.record_failure()
                    self._failures += 1
                    self._last_error = str(exc)[:200]
                    logger.warning("通道 %s 补投中断，剩余留在待发库: %s",
                                   self.name, self._last_error)
                    break
                if op.kind in CREATING_KINDS and op.logical_id is not None and result is not None:
                    self._remember(op.logical_id, result)
                elif op.kind == OP_DELETE and op.logical_id is not None:
                    self._forget_id(op.logical_id)
                await self._forget(op)
                self._breaker.record_success()
                replayed += 1

            try:
                remaining = int(await self._store.count(self.name))
            except Exception:  # noqa: BLE001
                remaining = 0
            self._deferred_rows = remaining
            if not remaining:
                self._deferred.clear()
            if replayed and self._on_recovered is not None:
                with contextlib.suppress(Exception):
                    await self._on_recovered(replayed, skipped)
        finally:
            self._draining = False

    # --- 只读视图 ---

    _ISSUED_MAX = 8192

    def _note_issued(self, logical_id: Any) -> None:
        try:
            logical = int(logical_id)
        except (TypeError, ValueError):
            return
        self._issued[logical] = time.time()
        with contextlib.suppress(AttributeError, KeyError):
            self._issued.move_to_end(logical)  # type: ignore[attr-defined]
        while len(self._issued) > self._ISSUED_MAX:
            self._issued.pop(next(iter(self._issued)), None)

    def knows_logical(self, logical_id: Optional[int]) -> bool:
        """这个 id 是本通道发过（或正要发）的逻辑 id 吗？

        判据必须是"记录过"而不是"数值够大"：CLI 进程分配的逻辑 id 从 1 起，
        与真实 Telegram message_id 完全撞在一起，靠区间去猜必然错。
        """
        if logical_id is None:
            return False
        try:
            logical = int(logical_id)
        except (TypeError, ValueError):
            return False
        return logical in self._issued or logical in self._native

    def _resolve_native(self, op: Op) -> Optional[int]:
        """这条 edit/delete 该打到哪个原生 id 上。

        payload 里的 native_id 是显式指定：少数调用点手上拿到的本来就是通道的
        原生 id（转发-读取-删除那套取内容的 trick 里，被转发消息不是我们发的，
        通道里没有它的逻辑 id）。没有显式指定时才按 logical_id 查表。
        """
        explicit = (op.payload or {}).get("native_id")
        if explicit is not None:
            try:
                return int(explicit)
            except (TypeError, ValueError):
                return None
        try:
            return self._native.get(int(op.logical_id or 0))
        except (TypeError, ValueError):
            return None

    def _remember(self, logical_id: Any, native: Any) -> None:
        try:
            logical, native_int = int(logical_id), int(native)
        except (TypeError, ValueError):
            return
        self._native[logical] = native_int
        self._logical[native_int] = logical

    def _forget_id(self, logical_id: Any) -> None:
        try:
            logical = int(logical_id)
        except (TypeError, ValueError):
            return
        native = self._native.pop(logical, None)
        if native is not None:
            self._logical.pop(native, None)

    def logical_for_native(self, native_id: Optional[int]) -> Optional[int]:
        """原生 id -> logical_id 的反查；没记录返回 None。"""
        if native_id is None:
            return None
        try:
            return self._logical.get(int(native_id))
        except (TypeError, ValueError):
            return None

    async def wait_idle(self, timeout: float = 5.0) -> bool:
        """等到队列排空且没有在途投递。给测试和有序停机用。"""
        deadline = time.time() + max(0.0, float(timeout))
        while time.time() < deadline:
            if self._queue_len() == 0 and not self._in_flight:
                return True
            await asyncio.sleep(0.01)
        return self._queue_len() == 0 and not self._in_flight

    def native_id(self, logical_id: Optional[int]) -> Optional[int]:
        if logical_id is None:
            return None
        with contextlib.suppress(TypeError, ValueError):
            return self._native.get(int(logical_id))
        return None

    def stats(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "queued": self._queue_len(),
            "delivered": self._delivered,
            "dropped": self._dropped,
            "orphaned": self._orphaned,
            "coalesced": self._coalesced,
            "pending_store": self._deferred_rows,
            "failures": self._failures,
            "configured": bool(self._is_configured()),
            "running": self._task is not None and not self._task.done(),
        }
        data.update(self._breaker.snapshot())
        if self._last_error:
            data["last_error"] = self._last_error
        return data


class ChannelRegistry:
    """进程内按名字共享的通道表。

    "Telegram 现在通不通"是一个**进程级**事实，不是每轮对话各自的事实。每个
    MirrorBot 各带一个熔断器的话，每轮对话都要重新花 3×15s 去发现一次 TG 断了，
    熔断等于没有。所以 worker 按名字共享。

    但注册表是一个可实例化的对象、通过 get_channel_registry() 公开访问——不是
    藏在模块里、被别的模块用 `from x import _PRIVATE_GLOBAL` 在函数体里摸进去的
    全局变量（ef37cb9 就是那么写的，熔断状态因此成了隐式单例）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: Dict[str, ChannelWorker] = {}

    def get(self, name: str) -> Optional[ChannelWorker]:
        with self._lock:
            return self._workers.get(name)

    def register(self, worker: ChannelWorker) -> ChannelWorker:
        """登记；同名已存在时返回既有的那个（幂等，便于惰性创建）。"""
        with self._lock:
            existing = self._workers.get(worker.name)
            if existing is not None:
                return existing
            self._workers[worker.name] = worker
            return worker

    def names(self) -> List[str]:
        with self._lock:
            return list(self._workers)

    def stats(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            workers = dict(self._workers)
        return {name: worker.stats() for name, worker in workers.items()}

    async def aclose_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            with contextlib.suppress(Exception):
                await worker.aclose()


_CHANNEL_REGISTRY = ChannelRegistry()


def get_channel_registry() -> ChannelRegistry:
    return _CHANNEL_REGISTRY


__all__ = [
    "CIRCUIT_CLOSED",
    "CIRCUIT_HALF_OPEN",
    "CIRCUIT_OPEN",
    "CREATING_KINDS",
    "ChannelRegistry",
    "ChannelWorker",
    "CircuitBreaker",
    "Op",
    "OP_CHAT_ACTION",
    "OP_DELETE",
    "OP_DOCUMENT",
    "OP_EDIT",
    "OP_EDIT_MARKUP",
    "OP_PHOTO",
    "OP_SEND",
    "OpNotDeliverable",
    "SUPERSEDABLE_KINDS",
    "TARGETING_KINDS",
    "UPLOAD_KINDS",
    "collapse_ops",
    "get_channel_registry",
]
