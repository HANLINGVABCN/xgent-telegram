from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock

from xgent_app.agent_trigger import execute_trigger_protocol


class AgentTriggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_protocol_arguments(self):
        manager = Mock()
        manager.handle_protocol = AsyncMock(return_value="ok")
        bot = object()

        result = await execute_trigger_protocol(
            {"path": "name", "body": "payload"},
            trigger_manager=manager,
            bot=bot,
            chat_id=1,
            conversation_id=2,
            original_text="user",
            response="ai",
        )

        self.assertEqual(result, "ok")
        manager.handle_protocol.assert_awaited_once_with(
            "name", "payload", bot, 1, 2, "user", "ai"
        )

    async def test_failure_keeps_existing_notice(self):
        manager = Mock()
        manager.handle_protocol = AsyncMock(side_effect=RuntimeError("bad"))

        result = await execute_trigger_protocol(
            {},
            trigger_manager=manager,
            bot=object(),
            chat_id=1,
            conversation_id=2,
            original_text="user",
            response="ai",
        )

        self.assertEqual(result, "[trigger结果] 操作失败: bad")


if __name__ == "__main__":
    unittest.main()
