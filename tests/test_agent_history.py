from __future__ import annotations

import unittest
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock

from xgent_app.agent_history import (
    persist_agent_result,
    persist_media_result,
    persist_standard_operation_result,
)
from xgent_app.attachments import AttachmentContextError


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

    async def test_display_metadata_does_not_change_shell_notice(self):
        recorder, database = AsyncMock(), AsyncMock()
        metadata = {"display": {"content": "<b>Shell</b>\n<pre>a\nb</pre>", "parse_mode": "HTML"}}
        await persist_agent_result(
            recorder=recorder, message_type="agent_result", database=database,
            conversation_id=7, chat_id=9, notice="shell notice", display_metadata=metadata,
        )
        self.assertEqual("shell notice", recorder.record.await_args.kwargs["content"])
        self.assertEqual(metadata, recorder.record.await_args.kwargs["metadata"])
        database.add_chat_message.assert_awaited_once_with(7, "user", "shell notice")

    async def test_sent_file_reference_survives_html_presentation(self):
        recorder, database = AsyncMock(), AsyncMock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder).resolve() / "report without extension"
            path.write_bytes(b"report")
            notice = (
                "[sendfile\u7ed3\u679c] \u5df2\u53d1\u9001\u670d\u52a1\u5668\u6587\u4ef6"
                f"\u7ed9\u7528\u6237: {path} (6 bytes)"
            )
            await persist_agent_result(
                recorder=recorder, message_type="agent_result", database=database,
                conversation_id=7, chat_id=9, notice=notice, display_content="<b>Sent</b>",
            )
            recorded = recorder.record.await_args.kwargs
            self.assertEqual("<b>Sent</b>", recorded["content"])
            self.assertEqual(str(path), recorded["metadata"]["display_media"][0]["path"])
            database.add_chat_message.assert_awaited_once_with(7, "user", notice)

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

    async def test_media_associations_are_saved_before_guarded_history_mirror(self):
        recorder, database = AsyncMock(), AsyncMock()
        calls = []
        metadata = {"attachments": [{"id": "image"}], "attachment_generation": 4}
        recorder.record_media_reply.side_effect = lambda *a, **k: calls.append("record") or 12
        database.add_chat_message.side_effect = lambda *a, **k: calls.append("mirror")
        await persist_media_result(
            recorder=recorder, database=database, conversation_id=7,
            chat_id=9, notice="media", metadata=metadata,
        )
        self.assertEqual(["record", "mirror"], calls)
        recorder.record_media_reply.assert_awaited_once_with("media", 9, metadata=metadata)
        database.add_chat_message.assert_awaited_once_with(
            7, "user", "[外部媒体模块回复]\nmedia", attachment_generation=4,
        )

    async def test_failed_media_association_is_not_mirrored_as_success(self):
        recorder, database = AsyncMock(), AsyncMock()
        recorder.record_media_reply.return_value = None
        with self.assertRaises(AttachmentContextError):
            await persist_media_result(
                recorder=recorder, database=database, conversation_id=7,
                chat_id=9, notice="media", metadata={"attachments": [{"id": "image"}]},
            )
        database.add_chat_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
