"""毒药消息阻塞修复回归测试。

验证三道防线：
  1. _target_ids() 不把 message_id <= 0 当作原生 ID
  2. deliver_op_to_bot() 把永久性 BadRequest 翻译成 OpNotDeliverable
  3. _resolve_native() 拒绝 native_id <= 0
  4. _drain_store() 遇到毒药消息 continue 而非 break
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from xgent_app.fanout import (
    ChannelWorker,
    Op,
    OP_EDIT,
    OP_EDIT_MARKUP,
    OP_SEND,
    OpNotDeliverable,
)
from xgent_app.web_bridge import deliver_op_to_bot, MirrorBot


# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------

class _FakeStore:
    """内存待发库替身。"""

    def __init__(self):
        self.rows = []
        self._next_id = 1

    async def append(self, channel, op):
        row = op.to_row()
        row["id"] = self._next_id
        row["channel"] = channel
        self._next_id += 1
        self.rows.append(row)

    async def fetch(self, channel, limit=1000):
        return [r for r in self.rows if r["channel"] == channel][:limit]

    async def count(self, channel):
        return sum(1 for r in self.rows if r["channel"] == channel)

    async def delete(self, row_ids):
        self.rows = [r for r in self.rows if r["id"] not in row_ids]


class _FakeBadRequest(Exception):
    """模拟 telegram.error.BadRequest。"""
    pass


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 防线 1：_target_ids 对 message_id <= 0 的守卫
# ---------------------------------------------------------------------------

class TestTargetIdsZeroGuard(unittest.TestCase):
    """_target_ids(0) 不应返回 native_id=0。"""

    def _make_mirror_bot(self):
        """构造一个最小 MirrorBot 以调用 _target_ids。"""
        outbox = MagicMock()
        bot = MirrorBot.__new__(MirrorBot)
        bot.chat_id = 12345
        bot.real_bot = MagicMock()
        bot._outbox = outbox
        bot._offer_fn = lambda *a, **kw: None
        # 模拟 _tg_channel 方法
        fake_channel = MagicMock()
        fake_channel.knows_logical = MagicMock(return_value=False)
        fake_channel.logical_for_native = MagicMock(return_value=None)
        bot._tg_channel = MagicMock(return_value=fake_channel)
        return bot

    def test_zero_returns_none_native(self):
        bot = self._make_mirror_bot()
        logical, native = bot._target_ids(0)
        self.assertIsNone(native,
                          "message_id=0 不应被当作原生 ID，native 应为 None")

    def test_negative_returns_none_native(self):
        bot = self._make_mirror_bot()
        logical, native = bot._target_ids(-5)
        self.assertIsNone(native,
                          "message_id=-5 不应被当作原生 ID，native 应为 None")

    def test_positive_unknown_still_works(self):
        """正数且不在 _issued/_native 中的消息 ID 仍应被视为原生 ID（情况 3）。"""
        bot = self._make_mirror_bot()
        logical, native = bot._target_ids(42)
        self.assertEqual(native, 42,
                         "正数未知 ID 仍应走情况 3 返回 native=42")

    def test_none_passthrough(self):
        bot = self._make_mirror_bot()
        logical, native = bot._target_ids(None)
        self.assertIsNone(logical)
        self.assertIsNone(native)


# ---------------------------------------------------------------------------
# 防线 2：deliver_op_to_bot 的 BadRequest 异常翻译
# ---------------------------------------------------------------------------

class TestBadRequestTranslation(unittest.TestCase):
    """永久性 BadRequest 应被翻译成 OpNotDeliverable。"""

    def _make_edit_op(self, native_id=42):
        return Op(kind=OP_EDIT, chat_id=12345, logical_id=1000000,
                  payload={"text": "hello", "native_id": native_id})

    def test_message_to_edit_not_found(self):
        """'Message to edit not found' 是永久性错误，应翻译。"""
        import xgent_app.web_bridge as wb
        original_cls = wb._TgBadRequest
        try:
            wb._TgBadRequest = _FakeBadRequest
            bot = MagicMock()
            bot.edit_message_text = AsyncMock(
                side_effect=_FakeBadRequest("Message to edit not found"))
            op = self._make_edit_op()
            with self.assertRaises(OpNotDeliverable) as cm:
                _run(deliver_op_to_bot(bot, op, 42))
            self.assertIn("永久拒绝", str(cm.exception))
        finally:
            wb._TgBadRequest = original_cls

    def test_message_not_modified(self):
        """'Message is not modified' 是永久性错误，应翻译。"""
        import xgent_app.web_bridge as wb
        original_cls = wb._TgBadRequest
        try:
            wb._TgBadRequest = _FakeBadRequest
            bot = MagicMock()
            bot.edit_message_text = AsyncMock(
                side_effect=_FakeBadRequest("Message is not modified"))
            op = self._make_edit_op()
            with self.assertRaises(OpNotDeliverable):
                _run(deliver_op_to_bot(bot, op, 42))
        finally:
            wb._TgBadRequest = original_cls

    def test_network_error_not_translated(self):
        """普通网络错误不应被翻译，应原样上抛。"""
        import xgent_app.web_bridge as wb
        original_cls = wb._TgBadRequest
        try:
            wb._TgBadRequest = _FakeBadRequest
            bot = MagicMock()
            bot.edit_message_text = AsyncMock(
                side_effect=ConnectionError("Network unreachable"))
            op = self._make_edit_op()
            with self.assertRaises(ConnectionError):
                _run(deliver_op_to_bot(bot, op, 42))
        finally:
            wb._TgBadRequest = original_cls

    def test_unknown_badrequest_not_translated(self):
        """不在 _PERM_PATTERNS 中的 BadRequest 不应被翻译。"""
        import xgent_app.web_bridge as wb
        original_cls = wb._TgBadRequest
        try:
            wb._TgBadRequest = _FakeBadRequest
            bot = MagicMock()
            bot.edit_message_text = AsyncMock(
                side_effect=_FakeBadRequest("Some unknown bad request reason"))
            op = self._make_edit_op()
            with self.assertRaises(_FakeBadRequest):
                _run(deliver_op_to_bot(bot, op, 42))
        finally:
            wb._TgBadRequest = original_cls


# ---------------------------------------------------------------------------
# 防线 3：_resolve_native 拒绝 native_id <= 0
# ---------------------------------------------------------------------------

class TestResolveNativeZeroGuard(unittest.TestCase):
    """_resolve_native 对 native_id=0 应返回 None。"""

    def _make_worker(self):
        deliver = AsyncMock(return_value=None)
        worker = ChannelWorker("telegram", deliver, is_configured=lambda: True)
        return worker

    def test_explicit_zero_returns_none(self):
        worker = self._make_worker()
        op = Op(kind=OP_EDIT, chat_id=12345, logical_id=1000000,
                payload={"native_id": 0})
        result = worker._resolve_native(op)
        self.assertIsNone(result,
                          "native_id=0 应返回 None（无效占位值）")

    def test_explicit_negative_returns_none(self):
        worker = self._make_worker()
        op = Op(kind=OP_EDIT, chat_id=12345, logical_id=1000000,
                payload={"native_id": -1})
        result = worker._resolve_native(op)
        self.assertIsNone(result,
                          "native_id=-1 应返回 None（无效占位值）")

    def test_explicit_positive_returns_value(self):
        worker = self._make_worker()
        op = Op(kind=OP_EDIT, chat_id=12345, logical_id=1000000,
                payload={"native_id": 123})
        result = worker._resolve_native(op)
        self.assertEqual(result, 123,
                         "native_id=123 应正常返回")


# ---------------------------------------------------------------------------
# 集成场景：毒药消息不阻塞队列补投
# ---------------------------------------------------------------------------

class TestDrainStoreSkipsPoisonMessage(unittest.TestCase):
    """_drain_store 遇到 native_id=0 的毒药消息时，应跳过并继续投递后续消息。"""

    def test_poison_message_skipped_during_drain(self):
        """毒药消息（native_id=0）应被跳过，后续正常 send 操作应成功投递。"""
        delivered = []

        async def fake_deliver(op, native):
            delivered.append(op.kind)
            return 999  # 模拟返回原生 message_id

        store = _FakeStore()
        worker = ChannelWorker("telegram", fake_deliver,
                               is_configured=lambda: True, store=store)

        async def run():
            # 先写入一条毒药消息（edit with native_id=0）
            poison = Op(kind=OP_EDIT, chat_id=12345, logical_id=1000000,
                        payload={"text": "poison", "native_id": 0})
            await store.append("telegram", poison)

            # 再写入一条正常的 send 消息
            normal = Op(kind=OP_SEND, chat_id=12345, logical_id=1000001,
                        payload={"text": "hello"})
            await store.append("telegram", normal)

            # 触发补投
            await worker._drain_store(0)

        _run(run())

        # 毒药消息应被丢弃（native_id=0 → _resolve_native 返回 None → orphaned）
        # 正常消息应被投递
        self.assertIn(OP_SEND, delivered,
                      "正常 send 操作应在毒药消息被跳过后成功投递")
        # store 中应该没有残留
        remaining = _run(store.count("telegram"))
        self.assertEqual(remaining, 0,
                         "补投完成后 store 中不应有残留记录")


if __name__ == "__main__":
    unittest.main()
