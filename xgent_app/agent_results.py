"""Normalized results for Agent protocol operations.

The legacy Agent executor returns plain dictionaries whose fields vary by
protocol.  This module adds a small, compatibility-friendly boundary around
those dictionaries.  It does not execute operations, send Telegram messages,
write history, or change the executor's original fields.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, TypedDict

from xgent_app.agent_context import (
    AgentMessage,
    build_edit_context_message,
    build_grep_context_message,
    build_read_context_message,
    build_run_context_message,
)
from xgent_app.shell_output import build_run_notice


class AgentOperationResult(TypedDict, total=False):
    """Common result contract, while retaining legacy executor fields."""

    success: bool
    kind: str
    notice: str
    output: str
    user_message: Optional[str]
    context_message: Optional[AgentMessage]
    should_continue: bool
    metadata: Dict[str, Any]


def _copy_raw(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy only the top-level mapping so legacy values remain untouched."""
    return dict(raw)


def _normalize(
    raw: Mapping[str, Any],
    *,
    kind: str,
    notice: str,
    output: str,
    context_message: Optional[AgentMessage],
    metadata: Optional[Dict[str, Any]] = None,
) -> AgentOperationResult:
    """Add the common contract without removing any legacy keys."""
    result: AgentOperationResult = _copy_raw(raw)  # type: ignore[assignment]
    result.update(
        {
            "success": bool(raw.get("success", True)),
            "kind": kind,
            "notice": notice,
            "output": output,
            "user_message": None,
            "context_message": context_message,
            "should_continue": True,
            "metadata": metadata if metadata is not None else {},
        }
    )
    return result


def normalize_read_result(raw: Mapping[str, Any]) -> AgentOperationResult:
    """Normalize a read executor result without flattening multimodal content."""
    notice = str(raw["notice"])
    return _normalize(
        raw,
        kind="read",
        notice=notice,
        output=str(raw.get("output") or notice),
        context_message=build_read_context_message(raw),
    )


def normalize_edit_result(raw: Mapping[str, Any]) -> AgentOperationResult:
    """Normalize an edit executor result."""
    notice = str(raw.get("output") or raw.get("notice") or "")
    return _normalize(
        raw,
        kind="edit",
        notice=notice,
        output=notice,
        context_message=build_edit_context_message(notice),
    )


def normalize_grep_result(raw: Mapping[str, Any]) -> AgentOperationResult:
    """Normalize a grep executor result."""
    notice = str(raw.get("output") or raw.get("notice") or "")
    return _normalize(
        raw,
        kind="grep",
        notice=notice,
        output=notice,
        context_message=build_grep_context_message(notice),
    )


def normalize_run_result(raw: Mapping[str, Any]) -> AgentOperationResult:
    """Normalize a run executor result and preserve its detailed fields."""
    notice = build_run_notice(dict(raw))
    return _normalize(
        raw,
        kind="run",
        notice=notice,
        output=str(raw.get("output") or "(无输出)"),
        context_message=build_run_context_message(notice),
    )


def failed_result(kind: str, message: str) -> AgentOperationResult:
    """Create the same shape used for executor exceptions."""
    notice = str(message)
    return _normalize(
        {"success": False, "output": notice, "notice": notice},
        kind=kind,
        notice=notice,
        output=notice,
        context_message=(
            build_read_context_message({"notice": notice})
            if kind == "read"
            else (
                build_edit_context_message(notice)
                if kind == "edit"
                else (
                    build_grep_context_message(notice)
                    if kind == "grep"
                    else build_run_context_message(notice)
                )
            )
        ),
    )
