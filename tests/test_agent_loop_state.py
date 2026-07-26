from __future__ import annotations

import unittest

from xgent_app.agent_loop_state import AgentRoundState


class AgentRoundStateTests(unittest.TestCase):
    def test_context_is_copied_and_round_starts_without_continue(self):
        state = AgentRoundState()
        message = {"role": "user", "content": "notice"}

        state.add_context(message)
        message["content"] = "changed"

        self.assertTrue(state.has_context)
        self.assertFalse(state.should_continue)
        self.assertEqual(state.continuation_messages[0]["content"], "notice")

    def test_pause_message_is_independent_round_state(self):
        state = AgentRoundState(pause_message="等待")
        self.assertEqual(state.pause_message, "等待")
        self.assertFalse(state.has_context)


if __name__ == "__main__":
    unittest.main()
