from __future__ import annotations

import unittest

from bot_app.agent_status import AgentTurnOrigin, build_agent_round_status


class AgentStatusTests(unittest.TestCase):
    def test_user_round_phases_are_unambiguous(self):
        self.assertEqual(
            build_agent_round_status(3, "running", operation_count=2),
            "🛠️ Agent 第 3 轮 · 正在执行 2 个操作",
        )
        self.assertEqual(
            build_agent_round_status(3, "waiting_ai"),
            "🧠 Agent 第 3 轮 · 操作结果已返回，正在继续处理",
        )
        self.assertEqual(
            build_agent_round_status(3, "continued", next_iteration=4),
            "✅ Agent 第 3 轮 · 已完成，进入第 4 轮",
        )
        self.assertEqual(
            build_agent_round_status(3, "completed"),
            "✅ Agent 第 3 轮 · 本轮处理完成",
        )

    def test_trigger_origin_changes_label_not_iteration(self):
        origin = AgentTurnOrigin.trigger("trg_demo", "trun_demo")
        self.assertTrue(origin.is_trigger)
        self.assertEqual(origin.run_id, "trun_demo")
        self.assertEqual(
            build_agent_round_status(7, "waiting_ai", origin=origin),
            "🔔 Agent 第 7 轮 · 后台任务 trg_demo 已返回，正在继续处理",
        )
        self.assertEqual(
            build_agent_round_status(7, "running", origin=origin, operation_count=1),
            "🛠️ Agent 第 7 轮 · 后台任务 trg_demo · 正在执行 1 个操作",
        )

    def test_terminal_states_are_explicit(self):
        origin = AgentTurnOrigin.trigger("trg_demo", "trun_demo")
        self.assertIn("本轮处理完成", build_agent_round_status(8, "completed", origin=origin))
        self.assertIn("已停止", build_agent_round_status(8, "stopped", origin=origin))
        self.assertIn("处理失败", build_agent_round_status(8, "failed", origin=origin))
        self.assertEqual(
            build_agent_round_status(21, "limit", origin=origin, max_iterations=20),
            "⛔ Agent 第 21 轮 · 后台任务 trg_demo · 已超过最大 20 轮，结果已记录但不会继续调用 AI",
        )

    def test_unknown_phase_is_rejected(self):
        with self.assertRaises(ValueError):
            build_agent_round_status(1, "mystery")


if __name__ == "__main__":
    unittest.main()
