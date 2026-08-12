"""install_tg_to_web_mirror 的类方法覆盖 + 转发 + 还原测试。

PTB ExtBot 用 __slots__ 且实例无 __dict__，实例覆盖方法不可行（连
object.__setattr__ 都被拒）。改在类上用 staticmethod 覆盖。这里验证：
install 后类方法被覆盖、调用经 wrapper 转发到原方法并推帧、restore 后恢复。
"""

from __future__ import annotations

import asyncio
import unittest

from telegram.ext import ExtBot

from xgent_app.web_bridge import install_tg_to_web_mirror, WebOutbox


class TgToWebMirrorTests(unittest.TestCase):
    def setUp(self):
        # 保存真实类方法，每个测试后无条件恢复，避免污染 PTB 全局类。
        self._orig_send = ExtBot.send_message
        self._orig_edit = ExtBot.edit_message_text

    def tearDown(self):
        ExtBot.send_message = self._orig_send
        ExtBot.edit_message_text = self._orig_edit

    def test_none_inputs_return_noop(self):
        restore = install_tg_to_web_mirror(None, WebOutbox())
        self.assertIsNone(restore())  # noop 不抛

    def test_patch_forward_and_emit(self):
        called = []

        async def fake_send(self, *a, **kw):
            called.append((a, kw))
            return type("R", (), {"message_id": 42})()

        ExtBot.send_message = fake_send
        bot = ExtBot("1:2")
        outbox = WebOutbox()
        restore = install_tg_to_web_mirror(bot, outbox)

        # 类方法已被 staticmethod 覆盖：实例访问不绑 self，args 不含 self。
        self.assertIsNot(ExtBot.send_message, fake_send)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                bot.send_message(123, "hi", parse_mode="HTML")
            )
        finally:
            loop.close()

        self.assertEqual(result.message_id, 42)
        self.assertTrue(called, "原方法应被转发调用")
        # fake_send(self=bot, 123, "hi") -> a=(123, "hi")
        self.assertEqual(called[0][0][0], 123)
        # wrapper 推了一帧到 outbox
        frame = outbox.get(timeout=0.5)
        self.assertEqual(frame["type"], "message")
        self.assertEqual(frame["message_id"], 42)
        self.assertEqual(frame["text"], "hi")

        restore()
        # restore 把类属性设回 saved（原函数 fake_send）
        self.assertIs(ExtBot.send_message, fake_send)

    def test_restore_recovers_class_method(self):
        async def fake_send(self, *a, **kw):
            return None

        ExtBot.send_message = fake_send
        bot = ExtBot("1:2")
        restore = install_tg_to_web_mirror(bot, WebOutbox())
        self.assertIsNot(ExtBot.send_message, fake_send)  # 被 staticmethod 覆盖
        restore()
        # restore 把类属性设回 saved（原函数 fake_send）
        self.assertIs(ExtBot.send_message, fake_send)  # 恢复


if __name__ == "__main__":
    unittest.main()
