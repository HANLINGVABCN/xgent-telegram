from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from xgent_app.agent_dispatch import dispatch_standard_protocol


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


    async def test_search_is_dispatched_with_configured_key(self):
        with patch(
            "xgent_app.agent_dispatch.run_search",
            new=AsyncMock(return_value={"success": True, "output": "[search结果] ok"}),
        ) as search:
            result = await dispatch_standard_protocol(
                {"type": "search", "body": "nginx 502"},
                executor=self.executor,
                provider_api_format="openai",
                stop_event_factory=self.stop_factory,
                logger=self.logger,
                search_api_key="tvly-key",
            )

        search.assert_awaited_once_with("nginx 502", "tvly-key")
        self.assertEqual(result["kind"], "search")
        self.stop_factory.assert_not_called()

    async def test_fetch_is_dispatched_with_configured_key(self):
        with patch(
            "xgent_app.agent_dispatch.run_fetch",
            new=AsyncMock(return_value={"success": True, "output": "[fetch结果] ok"}),
        ) as fetch:
            result = await dispatch_standard_protocol(
                {"type": "fetch", "body": "https://x.example"},
                executor=self.executor,
                provider_api_format="openai",
                stop_event_factory=self.stop_factory,
                logger=self.logger,
                search_api_key="tvly-key",
            )

        fetch.assert_awaited_once_with("https://x.example", "tvly-key")
        self.assertEqual(result["kind"], "fetch")

    async def test_search_exception_is_normalized_and_logged(self):
        with patch(
            "xgent_app.agent_dispatch.run_search",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = await dispatch_standard_protocol(
                {"type": "search", "body": "q"},
                executor=self.executor,
                provider_api_format="openai",
                stop_event_factory=self.stop_factory,
                logger=self.logger,
                search_api_key="tvly-key",
            )

        self.assertFalse(result["success"])
        self.assertIn("执行异常", result["notice"])
        self.logger.error.assert_called_once_with("Agent search 执行异常: boom")

    async def test_search_without_key_still_dispatches_for_guidance(self):
        """未配置 key 时也要进 run_search，由它返回配置指引而不是静默跳过。"""
        with patch(
            "xgent_app.agent_dispatch.run_search",
            new=AsyncMock(return_value={"success": False, "output": "需要 TAVILY_API_KEY"}),
        ) as search:
            result = await dispatch_standard_protocol(
                {"type": "search", "body": "q"},
                executor=self.executor,
                provider_api_format="openai",
                stop_event_factory=self.stop_factory,
                logger=self.logger,
            )

        search.assert_awaited_once_with("q", None)
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
