from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from xgent_app.agent_history import (
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

    async def test_standard_run_result_is_also_added_to_conversation_history(self):
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
        database.add_chat_message.assert_awaited_once_with(
            7, "user", "run notice"
        )

    async def test_presentation_html_is_what_gets_stored_for_display(self):
        """实时发送用的是 build_standard_operation_presentation 产出的 HTML 卡片
        （emoji/加粗/<pre> 代码框），不是 operation['notice'] 纯文本；落库给显示历史
        用的内容必须是同一份 HTML，否则刷新后 run/edit/grep/search/fetch 结果从
        带样式的卡片退化成一大段纯文本——这正是本用例要卡住的回归。
        模型上下文（add_chat_message）仍然收纯文本 notice，不受影响。
        """
        recorder = AsyncMock()
        database = AsyncMock()
        html_card = "⌨️ <b>Agent Run</b>\n<pre>0</pre>"

        await persist_standard_operation_result(
            recorder=recorder,
            message_type="agent-result",
            database=database,
            conversation_id=7,
            chat_id=9,
            operation={"kind": "run", "notice": "[Agent run]\n纯文本 notice"},
            presentation=html_card,
        )

        recorder.record.assert_awaited_once_with(
            msg_type="agent-result",
            role="system",
            content=html_card,
            chat_id=9,
        )
        database.add_chat_message.assert_awaited_once_with(
            7, "user", "[Agent run]\n纯文本 notice"
        )

    async def test_presentation_omitted_falls_back_to_notice(self):
        """有的协议（如 read）没有对应的 HTML presentation——不传 presentation 时
        必须退回旧行为（落库内容 = notice），不能因为新增参数破坏原有调用点。"""
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

        recorder.record.assert_awaited_once_with(
            msg_type="agent-result",
            role="system",
            content="read notice",
            chat_id=9,
        )

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


if __name__ == "__main__":
    unittest.main()
