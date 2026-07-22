from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from bot_app.agent_file_delivery import send_written_agent_file


class AgentFileDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "out.txt"
        self.path.write_text("hello", encoding="utf-8")
        self.context = Mock()
        self.context.bot.send_document = AsyncMock()
        self.safe_send = AsyncMock()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    def _written(self, *, size=5, filename="out.txt", existed=False):
        return {
            "path": str(self.path),
            "filename": filename,
            "size": size,
            "existed": existed,
        }

    async def test_text_file_keeps_caption_and_notice(self):
        notice = await send_written_agent_file(
            self._written(),
            protocol="file",
            context=self.context,
            chat_id=1,
            max_file_size=50,
            safe_send_message=self.safe_send,
            safe_text=str,
            html_parse_mode="HTML",
        )
        self.assertIn("[file结果]", notice)
        self.assertIn("新建", notice)
        self.assertEqual(
            self.context.bot.send_document.await_args.kwargs["caption"],
            "📄 已写入服务器并发送: out.txt",
        )

    async def test_base64_file_uses_base64_caption(self):
        await send_written_agent_file(
            self._written(),
            protocol="file:base64",
            context=self.context,
            chat_id=1,
            max_file_size=50,
            safe_send_message=self.safe_send,
            safe_text=str,
            html_parse_mode="HTML",
        )
        self.assertEqual(
            self.context.bot.send_document.await_args.kwargs["caption"],
            "📄 已写入服务器并发送 (base64): out.txt",
        )

    async def test_large_file_only_sends_over_limit_notice(self):
        notice = await send_written_agent_file(
            self._written(size=100),
            protocol="file:base64",
            context=self.context,
            chat_id=1,
            max_file_size=50,
            safe_send_message=self.safe_send,
            safe_text=str,
            html_parse_mode="HTML",
        )
        self.assertIn("[file:base64结果]", notice)
        self.context.bot.send_document.assert_not_awaited()
        self.safe_send.assert_awaited_once()
        self.assertIn("base64 文件已写入服务器", self.safe_send.await_args.args[2])


if __name__ == "__main__":
    unittest.main()
