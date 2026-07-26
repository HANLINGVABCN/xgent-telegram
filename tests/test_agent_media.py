from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from xgent_app.agent_media import execute_media_generation


class _Message:
    def __init__(self):
        self.delete = AsyncMock()


class AgentMediaTests(unittest.IsolatedAsyncioTestCase):
    def _dependencies(self):
        message = _Message()
        context = Mock()
        context.bot.send_message = AsyncMock(return_value=message)

        async def keep_typing(_context, _chat_id, stop_event):
            await stop_event.wait()

        async def cancel_quietly(task, timeout):
            del timeout
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        return context, message, keep_typing, cancel_quietly

    async def test_success_returns_result_and_deletes_progress_message(self):
        context, message, keep_typing, cancel_quietly = self._dependencies()
        stop_event = asyncio.Event()
        result_payload = {"success": True, "paths": ["a.png"]}

        result = await execute_media_generation(
            "draw",
            context=context,
            chat_id=123,
            generate_media=AsyncMock(return_value=result_payload),
            keep_typing=keep_typing,
            stop_event_factory=Mock(return_value=stop_event),
            stop_requested=Mock(return_value=False),
            build_stop_keyboard=Mock(return_value="keyboard"),
            safe_edit_text=AsyncMock(),
            cancel_task_quietly=cancel_quietly,
        )

        self.assertEqual(result, {"stopped": False, "result": result_payload})
        context.bot.send_message.assert_awaited_once_with(
            chat_id=123,
            text="🎨 正在生成媒体... 请稍等",
            reply_markup="keyboard",
        )
        message.delete.assert_awaited_once_with()

    async def test_stop_edits_message_cancels_generation_and_keeps_notice(self):
        context, message, keep_typing, cancel_quietly = self._dependencies()
        stop_event = asyncio.Event()
        stop_event.set()
        safe_edit = AsyncMock()
        generation_cancelled = asyncio.Event()

        async def generate(_prompt):
            try:
                await asyncio.Event().wait()
            finally:
                generation_cancelled.set()

        result = await execute_media_generation(
            "draw",
            context=context,
            chat_id=123,
            generate_media=generate,
            keep_typing=keep_typing,
            stop_event_factory=Mock(return_value=stop_event),
            stop_requested=Mock(return_value=True),
            build_stop_keyboard=Mock(return_value="keyboard"),
            safe_edit_text=safe_edit,
            cancel_task_quietly=cancel_quietly,
        )

        self.assertEqual(result, {"stopped": True, "result": None})
        self.assertTrue(generation_cancelled.is_set())
        safe_edit.assert_awaited_once_with(
            message, "⏹️ 媒体生成已停止。", reply_markup=None
        )
        message.delete.assert_not_awaited()

    async def test_stop_edit_error_still_cancels_generation(self):
        context, _message, keep_typing, cancel_quietly = self._dependencies()
        stop_event = asyncio.Event()
        stop_event.set()
        generation_cancelled = asyncio.Event()

        async def generate(_prompt):
            try:
                await asyncio.Event().wait()
            finally:
                generation_cancelled.set()

        with self.assertRaisesRegex(RuntimeError, "edit failed"):
            await execute_media_generation(
                "draw",
                context=context,
                chat_id=123,
                generate_media=generate,
                keep_typing=keep_typing,
                stop_event_factory=Mock(return_value=stop_event),
                stop_requested=Mock(return_value=True),
                build_stop_keyboard=Mock(return_value="keyboard"),
                safe_edit_text=AsyncMock(side_effect=RuntimeError("edit failed")),
                cancel_task_quietly=cancel_quietly,
            )

        self.assertTrue(generation_cancelled.is_set())

    async def test_generation_error_still_cleans_typing_and_progress_message(self):
        context, message, keep_typing, cancel_quietly = self._dependencies()
        stop_event = asyncio.Event()

        async def fail(_prompt):
            raise RuntimeError("failed")

        with self.assertRaisesRegex(RuntimeError, "failed"):
            await execute_media_generation(
                "draw",
                context=context,
                chat_id=123,
                generate_media=fail,
                keep_typing=keep_typing,
                stop_event_factory=Mock(return_value=stop_event),
                stop_requested=Mock(return_value=False),
                build_stop_keyboard=Mock(return_value="keyboard"),
                safe_edit_text=AsyncMock(),
                cancel_task_quietly=cancel_quietly,
            )

        message.delete.assert_awaited_once_with()

    async def test_progress_message_error_does_not_leak_typing_task(self):
        context, _message, keep_typing, cancel_quietly = self._dependencies()
        context.bot.send_message.side_effect = RuntimeError("send failed")

        with self.assertRaisesRegex(RuntimeError, "send failed"):
            await execute_media_generation(
                "draw",
                context=context,
                chat_id=123,
                generate_media=AsyncMock(),
                keep_typing=keep_typing,
                stop_event_factory=Mock(return_value=asyncio.Event()),
                stop_requested=Mock(return_value=False),
                build_stop_keyboard=Mock(return_value="keyboard"),
                safe_edit_text=AsyncMock(),
                cancel_task_quietly=cancel_quietly,
            )


if __name__ == "__main__":
    unittest.main()
