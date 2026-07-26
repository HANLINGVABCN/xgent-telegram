from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from xgent_app.agent_sendfile import execute_sendfile_protocol


class AgentSendfileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.context = Mock()
        self.context.bot.send_document = AsyncMock()
        self.context.bot.send_chat_action = AsyncMock()
        self.safe_send = AsyncMock()
        self.logger = Mock()
        self.cancel_task = AsyncMock(side_effect=self._cancel_task)
        self.executor = Mock()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def _cancel_task(self, task, timeout):
        del timeout
        task.cancel()
        try:
            await task
        except BaseException:
            pass

    def _call(self, path, *, api_base_url="", max_file_size=50 * 1024 * 1024):
        self.executor.resolve_file_path.return_value = str(path)
        return execute_sendfile_protocol(
            str(path),
            executor=self.executor,
            context=self.context,
            chat_id=7,
            api_base_url=api_base_url,
            local_api_host_data_dir=str(self.root / "api"),
            local_api_container_data_dir="/var/lib/telegram-bot-api",
            max_file_size=max_file_size,
            safe_send_message=self.safe_send,
            safe_text=str,
            logger=self.logger,
            cancel_task_quietly=self.cancel_task,
        )

    async def test_missing_file_preserves_notice_and_user_message(self):
        path = self.root / "missing.txt"
        notice = await self._call(path)
        self.assertIn("文件不存在", notice)
        self.safe_send.assert_awaited_once()
        self.context.bot.send_document.assert_not_awaited()

    async def test_small_file_uses_native_upload(self):
        path = self.root / "hello.txt"
        path.write_text("hello", encoding="utf-8")
        notice = await self._call(path)
        self.assertIn("已发送服务器文件给用户", notice)
        kwargs = self.context.bot.send_document.await_args.kwargs
        self.assertEqual(kwargs["filename"], "hello.txt")
        self.assertEqual(kwargs["read_timeout"], 120)
        self.assertFalse(self.safe_send.await_count)

    async def test_large_file_without_local_api_is_rejected(self):
        path = self.root / "large.bin"
        path.write_bytes(b"1234")
        notice = await self._call(path, max_file_size=1)
        self.assertIn("未启用本地 API", notice)
        self.safe_send.assert_awaited_once()
        self.context.bot.send_document.assert_not_awaited()

    async def test_large_file_local_api_uses_temp_exposure_and_cleans_up(self):
        path = self.root / "large.bin"
        path.write_bytes(b"1234")
        notice = await self._call(path, api_base_url="http://local", max_file_size=1)
        self.assertIn("本地API直发", notice)
        kwargs = self.context.bot.send_document.await_args.kwargs
        self.assertTrue(kwargs["document"].startswith("file:///var/lib/telegram-bot-api/large.bin.sendfile_"))
        self.assertFalse(list((self.root / "api").glob("*")))
        self.cancel_task.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
