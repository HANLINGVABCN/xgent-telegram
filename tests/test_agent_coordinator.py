from __future__ import annotations

import unittest

from xgent_app.agent_coordinator import plan_agent_round_transition
from xgent_app.agent_loop_state import AgentRoundState


class AgentCoordinatorTests(unittest.TestCase):
    def test_finished_round_uses_pause_message(self):
        state = AgentRoundState(pause_message="paused")
        decision = plan_agent_round_transition(
            state,
            stop_requested=False,
            agent_turn_history=[],
        )
        self.assertFalse(decision["continue_loop"])
        self.assertEqual(decision["status_text"], "paused")
        self.assertFalse(decision["send_stop_notice"])
        self.assertFalse(decision["show_completion_status"])

    def test_finished_stopped_round_uses_stop_status_without_duplicate_notice(self):
        state = AgentRoundState()
        decision = plan_agent_round_transition(
            state,
            stop_requested=True,
            agent_turn_history=[],
        )
        self.assertEqual(decision["status_text"], "⏹️ Agent 操作已停止。")
        self.assertFalse(decision["send_stop_notice"])

    def test_stop_after_results_requests_detailed_notice(self):
        state = AgentRoundState(should_continue=True)
        state.add_context({"role": "user", "content": "result"})
        decision = plan_agent_round_transition(
            state,
            stop_requested=True,
            agent_turn_history=[],
        )
        self.assertFalse(decision["continue_loop"])
        self.assertTrue(decision["send_stop_notice"])
        self.assertFalse(decision["show_completion_status"])

    def test_no_context_still_preserves_completion_status_edit(self):
        state = AgentRoundState(should_continue=True)
        decision = plan_agent_round_transition(
            state,
            stop_requested=False,
            agent_turn_history=[],
        )
        self.assertFalse(decision["continue_loop"])
        self.assertTrue(decision["show_completion_status"])
        self.assertIsNone(decision["status_text"])

    def test_continuation_copies_history_and_appends_context(self):
        original = {"role": "assistant", "content": "reply"}
        state = AgentRoundState(should_continue=True)
        state.add_context({"role": "user", "content": "result"})
        decision = plan_agent_round_transition(
            state,
            stop_requested=False,
            agent_turn_history=[original],
        )
        self.assertTrue(decision["continue_loop"])
        self.assertTrue(decision["show_completion_status"])
        self.assertEqual(
            decision["next_history"],
            [original, {"role": "user", "content": "result"}],
        )
        self.assertIsNot(decision["next_history"][0], original)


if __name__ == "__main__":
    unittest.main()
