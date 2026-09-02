"""Telegram 镜像熔断器的行为测试。

钉的是那个真实故障：TG 断联时 MirrorBot 每次镜像都要等满超时，一轮流式
回复几百次 edit 把对话拖成小时级、锁被长期占住，网页端跟着不可用。熔断器
要在连续失败后跳过 TG 调用（网页帧照发、对话不被拖慢），冷却期满放一个
试探，恢复后自动闭合并补一条"断连期间跳过多少条"的汇总提示。

时间相关的行为（冷却期）不真睡：把 _last_failure_time 倒拨一个周期即可，
冷却逻辑只看这个时间戳。
"""

import asyncio
import time
import unittest

from xgent_app import web_bridge
from xgent_app.web_bridge import MirrorBot, TgCircuitBreaker, WebOutbox


class CircuitBreakerTests(unittest.TestCase):
    """纯状态机：closed → open → half-open → closed/open 的每条边。"""

    def test_fresh_breaker_is_closed_and_allows(self):
        breaker = TgCircuitBreaker()
        self.assertTrue(breaker.allow())

    def test_failures_below_threshold_do_not_open(self):
        breaker = TgCircuitBreaker(failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        self.assertTrue(breaker.allow(), "未到阈值不该开闸")

    def test_success_resets_the_failure_streak(self):
        # "连续"是关键词：失败被成功打断后重新数，偶发抖动不该把闸打开。
        breaker = TgCircuitBreaker(failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        breaker.record_failure()
        self.assertTrue(breaker.allow())

    def test_consecutive_failures_open_the_circuit(self):
        breaker = TgCircuitBreaker(failure_threshold=3)
        for _ in range(3):
            breaker.record_failure()
        self.assertFalse(breaker.allow())
        self.assertFalse(breaker.allow(), "开闸期间要持续拒绝")

    def test_cooldown_expiry_moves_to_half_open(self):
        breaker = TgCircuitBreaker(failure_threshold=1, cooldown=60.0)
        breaker.record_failure()
        self.assertFalse(breaker.allow())
        breaker._last_failure_time = time.time() - 61.0  # 倒拨，不真睡 60s
        self.assertTrue(breaker.allow(), "冷却期满应放一个试探")
        self.assertEqual("half-open", breaker._state)

    def test_half_open_success_closes_and_reports_skipped(self):
        breaker = TgCircuitBreaker(failure_threshold=1, cooldown=60.0)
        breaker.record_failure()
        breaker.allow()  # 开闸期间跳过，累计 1 次
        breaker.allow()  # 累计 2 次
        breaker._last_failure_time = time.time() - 61.0
        self.assertTrue(breaker.allow())  # half-open 试探
        self.assertEqual(2, breaker.record_success(),
                         "恢复时要报出断连期间跳过的操作数")
        self.assertEqual("closed", breaker._state)
        self.assertEqual(0, breaker.record_success(), "平常的成功不该再报数")

    def test_half_open_failure_reopens(self):
        breaker = TgCircuitBreaker(failure_threshold=3, cooldown=60.0)
        for _ in range(3):
            breaker.record_failure()
        breaker._last_failure_time = time.time() - 61.0
        self.assertTrue(breaker.allow())  # half-open 试探
        breaker.record_failure()
        self.assertFalse(breaker.allow(), "试探失败应重新开闸")


class MirrorBotCircuitTests(unittest.TestCase):
    """_tg_call 与熔断器的集成：TG 挂了，网页帧照发、对话不被拖住。"""

    def setUp(self):
        # 换一个全新的熔断器和 50ms 的镜像超时，不污染模块级状态、不真等 15s。
        self._saved_circuit = web_bridge._TG_CIRCUIT
        self._saved_timeout = web_bridge._MIRROR_TG_CALL_TIMEOUT
        self.breaker = TgCircuitBreaker(failure_threshold=3, cooldown=60.0)
        web_bridge._TG_CIRCUIT = self.breaker
        web_bridge._MIRROR_TG_CALL_TIMEOUT = 0.05

    def tearDown(self):
        web_bridge._TG_CIRCUIT = self._saved_circuit
        web_bridge._MIRROR_TG_CALL_TIMEOUT = self._saved_timeout

    def _mirror(self, real_bot):
        outbox = WebOutbox()
        frames = []
        outbox.put = frames.append  # 直接截获帧，不起 SSE 线程
        return MirrorBot(outbox, 42, real_bot), frames

    def test_hanging_tg_opens_the_circuit_and_web_frames_keep_flowing(self):
        class HangingBot:
            def __init__(self):
                self.calls = 0

            async def send_message(self, *args, **kwargs):
                self.calls += 1
                await asyncio.sleep(30)  # 模拟 TG 断联：永远不回来

        bot = HangingBot()
        mirror, frames = self._mirror(bot)

        async def scenario():
            for _ in range(3):  # 连续 3 次超时 → 开闸
                await mirror.send_message(chat_id=42, text="流式块")
            started = time.monotonic()
            await mirror.send_message(chat_id=42, text="开闸后的块")
            return time.monotonic() - started

        elapsed = asyncio.run(scenario())
        self.assertEqual(3, bot.calls, "开闸后的调用不该再打到 TG")
        self.assertLess(elapsed, 1.0, "开闸后镜像必须立刻跳过，不能等超时")
        self.assertEqual(4, len(frames), "TG 全挂，网页帧一条不能少")

    def test_tg_exception_also_counts_toward_the_circuit(self):
        class BrokenBot:
            async def send_message(self, *args, **kwargs):
                raise RuntimeError("Timed out")  # PTB 的 TimedOut 走同一条路

        mirror, frames = self._mirror(BrokenBot())

        async def scenario():
            for _ in range(3):
                await mirror.send_message(chat_id=42, text="hi")
            return await mirror.send_message(chat_id=42, text="again")

        asyncio.run(scenario())
        self.assertEqual("open", self.breaker._state)
        self.assertEqual(4, len(frames), "异常同样不挡网页帧")

    def test_missing_method_is_not_a_connectivity_failure(self):
        # 垫片 bot 没实现全量 PTB 接口：缺方法是编程问题，不计入熔断——
        # 误开闸会让正常的镜像被平白跳过。
        class BareBot:
            pass

        mirror, frames = self._mirror(BareBot())
        asyncio.run(mirror.edit_message_reply_markup(chat_id=42, message_id=1))
        self.assertEqual("closed", self.breaker._state)
        self.assertEqual(0, self.breaker._failure_count)
        self.assertEqual(1, len(frames), "缺方法的网页帧照发")

    def test_recovery_after_cooldown_sends_a_summary_notice(self):
        class FlakyBot:
            def __init__(self):
                self.fail = True
                self.texts = []

            async def send_message(self, *args, **kwargs):
                if self.fail:
                    raise RuntimeError("Timed out")
                self.texts.append(str(kwargs.get("text") or ""))
                return None

        bot = FlakyBot()
        mirror, frames = self._mirror(bot)

        async def scenario():
            for _ in range(3):
                await mirror.send_message(chat_id=42, text="断连期间")
            await mirror.send_message(chat_id=42, text="被跳过")  # skipped=1
            bot.fail = False
            self.breaker._last_failure_time = time.time() - 61.0  # 冷却期满
            await mirror.send_message(chat_id=42, text="恢复")  # half-open 试探
            await asyncio.sleep(0.01)  # 让恢复提示的 create_task 跑完
            return None

        asyncio.run(scenario())
        self.assertEqual("closed", self.breaker._state)
        notices = [t for t in bot.texts if "连接恢复" in t]
        self.assertEqual(1, len(notices), notices)
        self.assertIn("1 条", notices[0], "提示里要带断连期间跳过的条数")


if __name__ == "__main__":
    unittest.main()
