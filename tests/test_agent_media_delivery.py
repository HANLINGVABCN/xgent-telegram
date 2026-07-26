from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock

from xgent_app.agent_media_delivery import send_media_generation_result


class AgentMediaDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_sends_artifacts_with_notice_caption(self):
        send_artifacts = AsyncMock()
        safe_send = AsyncMock()
        context = Mock()
        artifacts = ["a.png"]

        await send_media_generation_result(
            {"success": True},
            artifacts,
            "notice",
            context=context,
            chat_id=8,
            send_artifacts=send_artifacts,
            safe_send_message=safe_send,
            safe_text=str,
            logger=Mock(),
        )

        send_artifacts.assert_awaited_once_with(
            context, 8, artifacts, caption="notice"
        )
        safe_send.assert_not_awaited()

    async def test_delivery_error_keeps_existing_warning(self):
        safe_send = AsyncMock()
        logger = Mock()

        await send_media_generation_result(
            {"success": True},
            [],
            "notice",
            context=Mock(),
            chat_id=8,
            send_artifacts=AsyncMock(side_effect=RuntimeError("boom")),
            safe_send_message=safe_send,
            safe_text=str,
            logger=logger,
        )

        self.assertEqual(
            safe_send.await_args.args[2],
            "⚠️ 媒体已经生成，但发送给用户时出了点问题: boom",
        )
        logger.error.assert_called_once()

    async def test_generation_failure_keeps_existing_warning(self):
        safe_send = AsyncMock()

        await send_media_generation_result(
            {"success": False, "error": "bad"},
            [],
            "notice",
            context=Mock(),
            chat_id=8,
            send_artifacts=AsyncMock(),
            safe_send_message=safe_send,
            safe_text=str,
            logger=Mock(),
        )

        self.assertEqual(
            safe_send.await_args.args[2],
            "⚠️ 媒体生成失败: bad",
        )


if __name__ == "__main__":
    unittest.main()
