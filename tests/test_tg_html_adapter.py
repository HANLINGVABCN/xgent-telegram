"""Telegram 出口适配：标准折叠形态只在打到 Bot API 时才转成 Telegram 形态。"""

from __future__ import annotations

import asyncio
import unittest

from xgent_app.protocols import ProtocolParser
from xgent_app.tg_html_adapter import install_telegram_html_adapter
from tests.test_protocols import NONCE_A, protocol_block


def _canonical(body: str = "a <b>\nc") -> str:
    return ProtocolParser.render_folded_html(
        "说明\n" + protocol_block("run-x", body, NONCE_A), prose_renderer=lambda t: t, raw_copy=True)


class ToTelegramHtmlTests(unittest.TestCase):
    def test_canonical_form_has_pre_and_raw(self):
        html = _canonical()
        self.assertIn("<pre>", html)
        self.assertIn('data-raw="', html)

    def test_converts_to_telegram_form(self):
        tg = ProtocolParser.to_telegram_html(_canonical())
        self.assertIn("<blockquote expandable>", tg)
        self.assertNotIn("<pre>", tg)
        self.assertNotIn("data-raw", tg)
        self.assertNotIn(NONCE_A, tg)
        self.assertIn("a &lt;b&gt;\nc</blockquote>", tg)

    def test_idempotent_and_passthrough(self):
        tg = ProtocolParser.to_telegram_html(_canonical())
        self.assertEqual(tg, ProtocolParser.to_telegram_html(tg))
        plain = "<b>x</b><pre>code</pre>"
        self.assertEqual(plain, ProtocolParser.to_telegram_html(plain))  # 非折叠块的 pre 不动

    def test_long_block_label_kept(self):
        tg = ProtocolParser.to_telegram_html(_canonical("\n".join(str(i) for i in range(60))))
        self.assertTrue(tg.endswith("<i>已折叠 10 行</i></blockquote>"))


class _FakeBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        self.calls.append(("send", text))
        return None

    async def edit_message_text(self, text, chat_id=None, message_id=None, parse_mode=None, **kw):
        self.calls.append(("edit", text))
        return None


class AdapterTests(unittest.TestCase):
    def test_html_sends_are_converted_plain_untouched(self):
        bot = _FakeBot()
        install_telegram_html_adapter(bot)
        install_telegram_html_adapter(bot)  # 幂等
        html = _canonical()

        async def go():
            await bot.send_message(1, html, parse_mode="HTML")
            await bot.send_message(chat_id=1, text=html, parse_mode="HTML")
            await bot.edit_message_text(html, chat_id=1, message_id=2, parse_mode="HTML")
            await bot.edit_message_text(text=html, chat_id=1, message_id=2, parse_mode="HTML")
            await bot.send_message(1, html)  # 非 HTML：不动
        asyncio.run(go())
        for kind, text in bot.calls[:4]:
            self.assertNotIn("<pre>", text, kind)
            self.assertNotIn("data-raw", text, kind)
        self.assertEqual(html, bot.calls[4][1])


if __name__ == "__main__":
    unittest.main()
