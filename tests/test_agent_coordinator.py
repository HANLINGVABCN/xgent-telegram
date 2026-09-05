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


class OverProtocolTests(unittest.TestCase):
    """over 协议：拦掉"再叫一次模型"这一个动作，别的什么都不动。

    结果落库是各执行分支里 persist_* 干的事，和这一层无关——所以这些用例只钉
    "回灌载荷有没有被丢掉"和"协调器接着走哪条分支"。
    """

    def _round(self, *, over_text="任务完成楼，主人~"):
        state = AgentRoundState(should_continue=True, over_text=over_text)
        state.add_context({"role": "user", "content": "[run结果] 返回码 0"})
        return state

    def test_successful_run_with_over_stops_before_calling_the_model(self):
        state = self._round()
        state.note_over_eligibility("run")
        self.assertTrue(state.apply_over())
        self.assertFalse(state.has_context)

        decision = plan_agent_round_transition(
            state,
            stop_requested=False,
            agent_turn_history=[{"role": "assistant", "content": "reply"}],
        )
        # 落到"没有新上下文"那条既有分支：正常收尾、不再调模型。
        self.assertFalse(decision["continue_loop"])
        self.assertTrue(decision["show_completion_status"])
        self.assertIsNone(decision["status_text"])

    def test_failed_operation_voids_over_and_feeds_the_result_back(self):
        # run 返回码非零、shellkill 会话不存在、edit 匹配不唯一，在循环里都归到
        # 同一件事：success 为假 → over_blocked。
        cases = [
            ("run", "[run结果] 失败: 返回码 1"),
            ("shellkill", "[shellkill结果] 会话不存在: demo"),
            ("edit", "[edit结果] 匹配不唯一"),
        ]
        for op_name, error_msg in cases:
            with self.subTest(operation=op_name):
                state = AgentRoundState(should_continue=True, over_text="任务完成楼，主人~")
                state.add_context({"role": "user", "content": error_msg})
                state.note_over_eligibility(op_name)
                state.over_blocked = True

                self.assertFalse(state.apply_over())
                decision = plan_agent_round_transition(
                    state,
                    stop_requested=False,
                    agent_turn_history=[{"role": "assistant", "content": "reply"}],
                )
                self.assertTrue(decision["continue_loop"])
                self.assertEqual(2, len(decision["next_history"]))

    def test_whitelisted_types_do_not_block_over(self):
        state = self._round()
        for block_type in ("run", "edit", "shellkill", "file", "file_base64", "over"):
            state.note_over_eligibility(block_type)
        self.assertFalse(state.over_blocked)
        self.assertTrue(state.apply_over())

    def test_output_bearing_protocol_voids_over(self):
        # 这些协议的结果本身就是模型要读的东西，over 一律失效。
        for block_type in (
            "read", "grep", "search", "fetch", "media",
            "shell", "stdin", "shellread", "trigger", "sendfile",
        ):
            with self.subTest(block_type=block_type):
                state = self._round()
                state.note_over_eligibility("run")
                state.note_over_eligibility(block_type)
                self.assertTrue(state.over_blocked)
                self.assertFalse(state.apply_over())
                self.assertTrue(state.has_context)

    def test_without_an_over_block_nothing_changes(self):
        state = self._round(over_text=None)
        state.note_over_eligibility("run")
        self.assertFalse(state.apply_over())
        self.assertTrue(state.has_context)


if __name__ == "__main__":
    unittest.main()
