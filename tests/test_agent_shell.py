from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock

from xgent_app.agent_shell import execute_shell_protocol


class AgentShellTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_type_has_no_side_effects(self):
        manager = Mock()
        result = await execute_shell_protocol(
            {"type": "run", "body": "echo"},
            shell_manager=manager,
            executor=Mock(),
            stop_event_factory=Mock(),
            stop_requested=Mock(return_value=False),
        )
        self.assertIsNone(result)

    async def test_shell_start_preserves_command_and_output(self):
        manager = Mock()
        manager.start = AsyncMock(
            return_value={
                "success": True,
                "session_id": "s1",
                "command": "top",
                "output": "out",
                "running": True,
            }
        )
        event = object()
        factory = Mock(return_value=event)

        result = await execute_shell_protocol(
            {"type": "shell", "body": "top"},
            shell_manager=manager,
            executor=Mock(),
            stop_event_factory=factory,
            stop_requested=Mock(return_value=False),
        )

        manager.start.assert_awaited_once_with("top", event)
        self.assertEqual(result["session_id"], "s1")
        self.assertEqual(result["command"], "top")
        self.assertEqual(result["output"], "out")

    async def test_stop_closes_running_session_and_appends_notice(self):
        manager = Mock()
        manager.start = AsyncMock(
            return_value={
                "success": True,
                "session_id": "s1",
                "command": "top",
                "output": "out",
                "running": True,
            }
        )
        manager.kill = AsyncMock(return_value={})

        result = await execute_shell_protocol(
            {"type": "shell", "body": "top"},
            shell_manager=manager,
            executor=Mock(),
            stop_event_factory=Mock(return_value=object()),
            stop_requested=Mock(return_value=True),
        )

        manager.kill.assert_awaited_once_with("s1")
        self.assertFalse(result["result"]["running"])
        self.assertEqual(result["result"]["status"], "stopped")
        self.assertIn("会话已随当前回合停止而关闭", result["output"])

    async def test_stdin_parse_error_does_not_create_stop_event(self):
        manager = Mock()
        executor = Mock()
        executor.parse_stdin_macro.side_effect = ValueError("bad")
        factory = Mock()

        result = await execute_shell_protocol(
            {"type": "stdin", "path": "s1", "body": "bad"},
            shell_manager=manager,
            executor=executor,
            stop_event_factory=factory,
            stop_requested=Mock(return_value=False),
        )

        factory.assert_not_called()
        self.assertFalse(result["result"]["success"])
        self.assertEqual(result["result"]["status"], "parse_error")


if __name__ == "__main__":
    unittest.main()
