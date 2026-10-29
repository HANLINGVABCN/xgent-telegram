from __future__ import annotations

import unittest

from xgent_app.agent_status import AgentTurnOrigin, build_agent_round_status, compact_agent_round_status


class AgentStatusTests(unittest.TestCase):
    def test_user_round_phases_are_unambiguous(self):
        self.assertEqual(
            build_agent_round_status(3, "running", operation_count=2),
            "Agent 第 3 轮 · 2 个操作进行中",
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
        self.assertEqual(
            build_agent_round_status(3, "completed", operation_count=3),
            "✅ Agent 第 3 轮 · 3 个操作已完成",
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
            "Agent 第 7 轮 · 后台任务 trg_demo · 1 个操作进行中",
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

    def test_compact_round_line_preserves_counts_without_inventing_completion(self):
        self.assertEqual('Agent 第 3 轮 · 2 个操作',
                         compact_agent_round_status(build_agent_round_status(3, 'completed', operation_count=2)))
        self.assertEqual('Agent 第 3 轮', compact_agent_round_status(build_agent_round_status(3, 'completed')))
        self.assertEqual('Agent 第 3 轮', compact_agent_round_status(build_agent_round_status(3, 'continued')))
        for phase in ('running', 'waiting_ai', 'stopped', 'failed', 'limit'):
            with self.subTest(phase=phase):
                self.assertIsNone(compact_agent_round_status(build_agent_round_status(3, phase)))


def test_background_progress_is_ai_status_even_after_an_operation():
    from types import SimpleNamespace
    from xgent_app.presentation import PresentationTurn, message_presentation
    from xgent_app.presentation_delivery import PresentationBot
    turn=PresentationTurn('folded')
    turn.active_operation={'kind':'run','output':'previous result'}
    bot=PresentationBot(SimpleNamespace(),turn)
    text='后台流式输出中... 已生成 120 字'
    with message_presentation({'kind':'ai_reply','purpose':'generation','transient':True}):
        assert bot._kind(text)=='ai_reply'
        assert bot._prepare(text,bot._kind(text),None)==text


if __name__ == "__main__":
    unittest.main()
