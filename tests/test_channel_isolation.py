"""通道隔离：一个通道打不通，不许拖慢/污染别的通道。

这是"三端彻底解耦"在**输出路径**上的回归测试。修之前对话核心每产出一条消息
都是 `await 真实TG调用` 成功之后才推网页帧，于是网页延迟直接等于 Telegram
延迟——TG 断连时流式渲染每 0.35s 一次编辑，每次都要等满 read_timeout。

这里的用例全部是纯单元测试（只碰 fanout + web_bridge，不加载 sections），
所以跑得很快，可以放心在每次改动后都跑。
"""

import asyncio
import time
import unittest

from xgent_app.fanout import (
    CIRCUIT_OPEN,
    ChannelWorker,
    CircuitBreaker,
    Op,
    OP_DELETE,
    OP_EDIT,
    OP_SEND,
    OpNotDeliverable,
    collapse_ops,
)
from xgent_app.web_bridge import MirrorBot, WebOutbox


class _FakeStore:
    """把待发操作放在内存里的持久层替身，接口与 idle._ChannelOutboxStore 一致。"""

    def __init__(self):
        self.rows = []
        self._next_id = 1

    async def append(self, channel, op):
        row = op.to_row()
        row["id"] = self._next_id
        row["channel"] = channel
        self._next_id += 1
        self.rows.append(row)
        return row["id"]

    async def fetch(self, channel, limit=1000):
        return [row for row in self.rows if row["channel"] == channel][:limit]

    async def delete(self, row_ids):
        dead = {int(value) for value in row_ids}
        self.rows = [row for row in self.rows if row["id"] not in dead]

    async def count(self, channel):
        return len([row for row in self.rows if row["channel"] == channel])


def _frames_sink():
    frames = []
    outbox = WebOutbox()
    outbox.put = frames.append          # 直接截获帧，不用起订阅者
    return frames, outbox


class TelegramBlackholeTests(unittest.TestCase):
    """TG 变黑洞（连上了但永不返回）时，网页必须完全不受影响。"""

    def test_blackholed_telegram_does_not_delay_web(self):
        frames, outbox = _frames_sink()

        async def hang(op, native):
            await asyncio.sleep(3600)    # 黑洞：永不返回

        async def scenario():
            worker = ChannelWorker(
                "tg-hang", hang, timeout=0.05,
                breaker=CircuitBreaker(failure_threshold=3, cooldown=60.0),
            )
            bot = MirrorBot(outbox, 42, real_bot=object(), channel=worker)
            started = time.perf_counter()
            for index in range(50):
                await bot.send_message(chat_id=42, text=f"流式第 {index} 块")
            elapsed = time.perf_counter() - started
            # 让 worker 有机会去撞几次超时，把熔断打开
            await asyncio.sleep(0.6)
            stats = worker.stats()
            await worker.aclose()
            return elapsed, stats

        elapsed, stats = asyncio.run(scenario())
        self.assertEqual(50, len(frames), "网页帧必须一条不少")
        self.assertLess(elapsed, 0.5,
                        "50 次发送必须在毫秒级返回——网页延迟不许等于 Telegram 延迟")
        self.assertEqual(CIRCUIT_OPEN, stats["circuit"],
                         "连续超时之后熔断要打开，后续调用变成 O(1) 跳过")

    def test_missing_bot_method_does_not_trip_the_breaker(self):
        """垫片 bot 少一个方法不是网络故障，不该把整条通道判成断线。"""
        async def scenario():
            calls = []

            async def deliver(op, native):
                calls.append(op.kind)
                raise OpNotDeliverable("bot 没有方法 send_message")

            worker = ChannelWorker("tg-shim", deliver, timeout=1.0,
                                   breaker=CircuitBreaker(failure_threshold=2))
            worker.ensure_started()
            for _ in range(5):
                worker.offer(Op(OP_SEND, logical_id=1, chat_id=1, payload={"text": "x"}))
            await worker.wait_idle(timeout=3.0)
            stats = worker.stats()
            await worker.aclose()
            return len(calls), stats

        attempts, stats = asyncio.run(scenario())
        self.assertEqual(5, attempts, "每一条都该被尝试，而不是被熔断挡住")
        self.assertNotEqual(CIRCUIT_OPEN, stats["circuit"])
        self.assertEqual(0, stats["failures"], "不可投递不计入网络失败")


class OrderingAndTargetingTests(unittest.TestCase):
    def test_edit_after_failed_send_is_dropped_not_misrouted(self):
        """send 失败之后的 edit 必须被丢掉。

        绝不允许退化成"新发一条"——那正是历史上"上一条消息无限刷屏"的成因；
        也绝不允许把 message_id=None 传给 API。
        """
        async def scenario():
            seen = []

            async def deliver(op, native):
                seen.append((op.kind, native))
                if op.kind == OP_SEND:
                    raise RuntimeError("network down")
                return None

            # store=None：落不了库，只能放弃这条 send，并把它的 logical_id 标死
            worker = ChannelWorker("tg-fail", deliver, timeout=1.0)
            worker.ensure_started()
            worker.offer(Op(OP_SEND, logical_id=7, chat_id=1, payload={"text": "hi"}))
            await worker.wait_idle(timeout=3.0)
            worker.offer(Op(OP_EDIT, logical_id=7, chat_id=1, payload={"text": "edited"}))
            worker.offer(Op(OP_DELETE, logical_id=7, chat_id=1, payload={}))
            await worker.wait_idle(timeout=3.0)
            await worker.aclose()
            return seen

        seen = asyncio.run(scenario())
        self.assertEqual([(OP_SEND, None)], seen,
                         "只有那次 send 被尝试过；后续 edit/delete 必须一次都没打出去")

    def test_per_channel_fifo_under_interleave(self):
        """两条消息交错的 send/edit/delete 必须按投递顺序到达同一个通道。"""
        async def scenario():
            seen = []
            native_by_logical = {101: 5001, 202: 5002}

            async def deliver(op, native):
                seen.append((op.kind, op.logical_id, native))
                return native_by_logical.get(op.logical_id)

            worker = ChannelWorker("tg-fifo", deliver, timeout=1.0)
            worker.ensure_started()
            plan = [
                (OP_SEND, 101), (OP_SEND, 202), (OP_EDIT, 101),
                (OP_EDIT, 202), (OP_DELETE, 101), (OP_DELETE, 202),
            ]
            for kind, logical in plan:
                worker.offer(Op(kind, logical_id=logical, chat_id=1,
                                payload={"text": f"{kind}-{logical}"}))
                # 逐条等：编辑必须能查到前面那次发送记下的原生 id
                await worker.wait_idle(timeout=3.0)
            await worker.aclose()
            return seen

        seen = asyncio.run(scenario())
        self.assertEqual([
            (OP_SEND, 101, None), (OP_SEND, 202, None),
            (OP_EDIT, 101, 5001), (OP_EDIT, 202, 5002),
            (OP_DELETE, 101, 5001), (OP_DELETE, 202, 5002),
        ], seen)


class CoalescingTests(unittest.TestCase):
    def test_collapse_keeps_last_edit_and_drops_edits_before_delete(self):
        ops = [
            Op(OP_SEND, logical_id=1, payload={"text": "a"}),
            Op(OP_EDIT, logical_id=1, payload={"text": "b"}),
            Op(OP_EDIT, logical_id=1, payload={"text": "c"}),
            Op(OP_SEND, logical_id=2, payload={"text": "x"}),
            Op(OP_EDIT, logical_id=2, payload={"text": "y"}),
            Op(OP_DELETE, logical_id=2, payload={}),
        ]
        keep, dropped = collapse_ops(ops)
        kinds = [(op.kind, op.logical_id, op.payload.get("text")) for op in keep]
        self.assertEqual([
            (OP_SEND, 1, "a"),
            (OP_EDIT, 1, "c"),        # 只留最后一次编辑
            (OP_SEND, 2, "x"),
            (OP_DELETE, 2, None),     # 被删掉的消息，之前的编辑没有可见效果
        ], kinds)
        self.assertEqual(2, dropped)

    def test_queued_edits_coalesce_under_stream_load(self):
        """流式编辑打到同一条消息上时，队列里只需要留最后一次。

        没有归并的话，一次 Agent 对话几百次编辑会把有界队列挤爆，挤掉的是
        真正需要送达的 send。
        """
        async def scenario():
            seen = []
            gate = asyncio.Event()

            async def deliver(op, native):
                await gate.wait()        # 先卡住，让编辑在队列里堆起来
                seen.append(op.payload.get("text"))
                return 1

            worker = ChannelWorker("tg-coalesce", deliver, timeout=5.0, maxsize=64)
            worker.ensure_started()
            worker.offer(Op(OP_SEND, logical_id=9, chat_id=1, payload={"text": "首块"}))
            for index in range(500):
                worker.offer(Op(OP_EDIT, logical_id=9, chat_id=1,
                                payload={"text": f"第 {index} 次编辑"}))
            queued = worker.stats()["queued"]
            coalesced = worker.stats()["coalesced"]
            gate.set()
            await worker.wait_idle(timeout=5.0)
            stats = worker.stats()
            await worker.aclose()
            return seen, queued, coalesced, stats

        seen, queued, coalesced, stats = asyncio.run(scenario())
        self.assertGreater(coalesced, 400, "绝大多数中间编辑应被归并掉")
        self.assertEqual(0, stats["dropped"],
                         "归并之后不该有任何一条因为队列满而被丢弃")
        self.assertIn("首块", seen)
        self.assertEqual("第 499 次编辑", seen[-1],
                         "最终可见状态必须是最后那次编辑")


class DurableReplayTests(unittest.TestCase):
    """断连期间的操作要落库，通道恢复后按序补投。

    用户明确要的是"全部持久重放"：**内容**一条不丢。被后来者完全覆盖掉的中间态
    编辑会在补投前归并（见 fanout.collapse_ops），可见的最终状态与原样重放逐字
    相同——原样重放几百次历史编辑只会在 Telegram 侧撞满 429。
    """

    def test_offline_ops_replay_in_order_after_recovery(self):
        async def scenario():
            store = _FakeStore()
            delivered = []
            down = {"value": True}

            async def deliver(op, native):
                if down["value"]:
                    raise RuntimeError("Telegram unreachable")
                delivered.append((op.kind, op.logical_id, native, op.payload.get("text")))
                return 6001 if op.kind == OP_SEND else None

            recovered = []

            async def on_recovered(replayed, skipped):
                recovered.append(replayed)

            worker = ChannelWorker(
                "tg-durable", deliver, timeout=1.0, store=store,
                breaker=CircuitBreaker(failure_threshold=2, cooldown=0.2),
                on_recovered=on_recovered,
            )
            worker.ensure_started()
            worker.offer(Op(OP_SEND, logical_id=11, chat_id=1, payload={"text": "占位"}))
            for index in range(6):
                worker.offer(Op(OP_EDIT, logical_id=11, chat_id=1,
                                payload={"text": f"流式 {index}"}))
            await asyncio.sleep(0.4)
            pending_while_down = await store.count("tg-durable")

            down["value"] = False        # Telegram 回来了
            deadline = time.time() + 6.0
            while time.time() < deadline and await store.count("tg-durable"):
                await asyncio.sleep(0.05)
            remaining = await store.count("tg-durable")
            await worker.aclose()
            return delivered, pending_while_down, remaining, recovered

        delivered, pending_while_down, remaining, recovered = asyncio.run(scenario())
        self.assertGreater(pending_while_down, 0, "断连期间的操作必须落库")
        self.assertEqual(0, remaining, "补投完待发库要清空")
        self.assertEqual(OP_SEND, delivered[0][0], "先补发送，后补编辑")
        self.assertEqual(6001, delivered[1][2],
                         "补投的编辑必须打在补发时新拿到的真实 id 上")
        self.assertEqual("流式 5", delivered[-1][3], "最终可见状态是最后那次编辑")
        self.assertTrue(recovered, "补投完要通知调用方（用户会收到一条恢复提示）")

    def test_chat_action_is_not_persisted(self):
        """"正在输入"是瞬时状态：补发一个三小时前的输入提示毫无意义。"""
        async def scenario():
            store = _FakeStore()

            async def deliver(op, native):
                raise RuntimeError("down")

            worker = ChannelWorker("tg-transient", deliver, timeout=1.0, store=store)
            worker.ensure_started()
            worker.offer(Op("send_chat_action", chat_id=1,
                            payload={"action": "typing"}, durable=False))
            await worker.wait_idle(timeout=3.0)
            count = await store.count("tg-transient")
            await worker.aclose()
            return count

        self.assertEqual(0, asyncio.run(scenario()))


if __name__ == "__main__":
    unittest.main()
