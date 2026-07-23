from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from bot_app.agent_history import (
    persist_agent_result,
    persist_media_result,
    persist_standard_operation_result,
)


class AgentHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_recorder_before_conversation_history(self):
        calls = []
        recorder = AsyncMock()
        database = AsyncMock()
        recorder.record.side_effect = lambda **kwargs: calls.append(("record", kwargs))
        database.add_chat_message.side_effect = lambda *args: calls.append(("db", args))

        await persist_agent_result(
            recorder=recorder,
            message_type="agent-result",
            database=database,
            conversation_id=7,
            chat_id=9,
            notice="notice",
        )

        self.assertEqual([item[0] for item in calls], ["record", "db"])
        recorder.record.assert_awaited_once_with(
            msg_type="agent-result",
            role="system",
            content="notice",
            chat_id=9,
        )
        database.add_chat_message.assert_awaited_once_with(7, "user", "notice")

    async def test_standard_read_result_is_added_to_conversation_history(self):
        recorder = AsyncMock()
        database = AsyncMock()

        await persist_standard_operation_result(
            recorder=recorder,
            message_type="agent-result",
            database=database,
            conversation_id=7,
            chat_id=9,
            operation={"kind": "read", "notice": "read notice"},
        )

        recorder.record.assert_awaited_once()
        database.add_chat_message.assert_awaited_once_with(
            7, "user", "read notice"
        )

    async def test_standard_run_result_keeps_legacy_global_only_history(self):
        recorder = AsyncMock()
        database = AsyncMock()

        await persist_standard_operation_result(
            recorder=recorder,
            message_type="agent-result",
            database=database,
            conversation_id=7,
            chat_id=9,
            operation={"kind": "run", "notice": "run notice"},
        )

        recorder.record.assert_awaited_once()
        database.add_chat_message.assert_not_awaited()

    async def test_media_persistence_keeps_special_history_prefix(self):
        recorder = AsyncMock()
        database = AsyncMock()

        await persist_media_result(
            recorder=recorder,
            database=database,
            conversation_id=7,
            chat_id=9,
            notice="media",
        )

        recorder.record_media_reply.assert_awaited_once_with("media", 9)
        database.add_chat_message.assert_awaited_once_with(
            7, "user", "[外部媒体模块回复]\nmedia"
        )

    async def test_can_preserve_run_behavior_without_database_write(self):
        recorder = AsyncMock()
        database = AsyncMock()

        await persist_agent_result(
            recorder=recorder,
            message_type="agent-result",
            database=database,
            conversation_id=7,
            chat_id=9,
            notice="run notice",
            add_to_conversation=False,
        )

        recorder.record.assert_awaited_once()
        database.add_chat_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
