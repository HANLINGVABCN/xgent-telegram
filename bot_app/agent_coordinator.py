"""Pure transition planning for the Agent operation loop."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, TypedDict

from bot_app.agent_loop_state import AgentRoundState


class AgentRoundDecision(TypedDict):
    continue_loop: bool
    next_history: Optional[List[Dict[str, Any]]]
    status_text: Optional[str]
    send_stop_notice: bool
    show_completion_status: bool


def plan_agent_round_transition(
    round_state: AgentRoundState,
    *,
    stop_requested: bool,
    agent_turn_history: Sequence[Mapping[str, Any]],
) -> AgentRoundDecision:
    """Plan the next loop action without Telegram, database, or model I/O."""
    if not round_state.should_continue:
        status_text = (
            "⏹️ Agent 操作已停止。"
            if stop_requested
            else (round_state.pause_message or "🛠️ Agent 操作阶段已结束。")
        )
        return {
            "continue_loop": False,
            "next_history": None,
            "status_text": status_text,
            "send_stop_notice": False,
            "show_completion_status": False,
        }

    if stop_requested:
        return {
            "continue_loop": False,
            "next_history": None,
            "status_text": "⏹️ Agent 操作已停止。",
            "send_stop_notice": True,
            "show_completion_status": False,
        }

    if not round_state.has_context:
        return {
            "continue_loop": False,
            "next_history": None,
            "status_text": None,
            "send_stop_notice": False,
            "show_completion_status": True,
        }

    next_history = [dict(message) for message in agent_turn_history]
    next_history.extend(round_state.continuation_messages)
    return {
        "continue_loop": True,
        "next_history": next_history,
        "status_text": None,
        "send_stop_notice": False,
        "show_completion_status": True,
    }
