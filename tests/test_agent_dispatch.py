from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock

from bot_app.agent_dispatch import dispatch_standard_protocol


class AgentDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.executor = Mock()
        self.logger = Mock()
        self.stop_event = object()
        self.stop_factory = Mock(return_value=self.stop_event)

    async def test_unsupported_protocol_has_no_side_effects(self):
        result = await dispatch_standard_protocol(
            {"type": "file", "body": "x"},
            executor=self.executor,
            provider_api_format="openai",
            stop_event_factory=self.stop_factory,
            logger=self.logger,
        )

        self.assertIsNone(result)
        self.stop_factory.assert_not_called()

    async def test_read_path_uses_ranged_reader(self):
        self.executor.read_file_ranged = AsyncMock(
            return_value={"notice": "read", "message": {"role": "user", "content": "x"}}
        )

        result = await dispatch_standard_protocol(
            {"type": "read", "path": "/tmp/a:1-2", "body": ""},
            executor=self.executor,
            provider_api_format="openai",
            stop_event_factory=self.stop_factory,
            logger=self.logger,
        )

        self.executor.read_file_ranged.assert_awaited_once_with("/tmp/a:1-2")
        self.assertEqual(result["kind"], "read")
        self.stop_factory.assert_not_called()

    async def test_read_body_without_range_uses_model_reader(self):
        self.executor._split_read_range.return_value = ("/tmp/a", "")
        self.executor.read_path_for_model = AsyncMock(
            return_value={"notice": "read", "message": {"role": "user", "content": "x"}}
        )

        await dispatch_standard_protocol(
            {"type": "read", "body": "/tmp/a"},
            executor=self.executor,
            provider_api_format="gemini",
            stop_event_factory=self.stop_factory,
            logger=self.logger,
        )

        self.executor.read_path_for_model.assert_awaited_once_with("/tmp/a", "gemini")

    async def test_edit_exception_is_normalized_and_logged(self):
        self.executor.edit_file = AsyncMock(side_effect=RuntimeError("bad"))

        result = await dispatch_standard_protocol(
            {"type": "edit", "body": "payload"},
            executor=self.executor,
            provider_api_format="openai",
            stop_event_factory=self.stop_factory,
            logger=self.logger,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["notice"], "[edit结果] 执行异常: bad")
        self.logger.error.assert_called_once_with("Agent edit 执行异常: bad")

    async def test_run_creates_stop_event_only_when_dispatched(self):
        self.executor.run_command = AsyncMock(
            return_value={
                "success": True,
                "command": "echo ok",
                "output": "ok",
                "return_code": 0,
                "timed_out": False,
                "stopped": False,
                "output_path": "out.log",
                "output_bytes": 2,
                "elapsed_seconds": 0.1,
            }
        )

        result = await dispatch_standard_protocol(
            {"type": "run", "body": "echo ok"},
            executor=self.executor,
            provider_api_format="openai",
            stop_event_factory=self.stop_factory,
            logger=self.logger,
        )

        self.stop_factory.assert_called_once_with()
        self.executor.run_command.assert_awaited_once_with("echo ok", self.stop_event)
        self.assertEqual(result["kind"], "run")


if __name__ == "__main__":
    unittest.main()
