"""Pure Telegram presentation builders for Agent operation results.

This module formats user-visible text only.  It does not execute protocols,
send messages, persist history, or build model continuation context.
"""

from __future__ import annotations

import html
from typing import Any, Mapping, Optional

from bot_app.shell_output import format_shell_display_output


def escape_html(value: Any) -> str:
    """Match the legacy ``safe_text`` behavior used by Agent messages."""
    return html.escape(str(value)) if value else ""


def build_edit_presentation(result: Mapping[str, Any]) -> str:
    notice = str(result.get("notice") or result.get("output") or "")
    emoji = "✏️" if result.get("success") else "⚠️"
    return f"{emoji} <b>Agent Edit</b>\n<pre>{escape_html(notice[:1500])}</pre>"


def build_grep_presentation(result: Mapping[str, Any]) -> str:
    notice = str(result.get("notice") or result.get("output") or "")
    emoji = "🔎" if result.get("success") else "⚠️"
    hits = result.get("hits", 0)
    return (
        f"{emoji} <b>Agent Grep</b> 命中 {hits} 处\n"
        f"<pre>{escape_html(notice[:2000])}</pre>"
    )


def build_run_presentation(result: Mapping[str, Any]) -> str:
    display_output = format_shell_display_output(
        str(result.get("output") or "(无输出)"),
        running=False,
    )
    status_emoji = "✅" if result.get("success") else "❌"
    return (
        "⌨️ <b>Agent Run</b>\n"
        f"{status_emoji} 返回码: <code>{escape_html(result.get('return_code'))}</code>\n"
        f"完整输出: <code>{escape_html(result.get('output_path'))}</code>\n"
        f"<pre>{escape_html(display_output)}</pre>"
    )



def build_standard_operation_presentation(
    result: Mapping[str, Any],
) -> Optional[str]:
    """Select the existing presentation for a normalized standard result."""
    builders = {
        "edit": build_edit_presentation,
        "grep": build_grep_presentation,
        "run": build_run_presentation,
    }
    builder = builders.get(str(result.get("kind") or ""))
    return builder(result) if builder is not None else None


def build_shell_presentation(
    *,
    action_label: str,
    shell_result: Mapping[str, Any],
    session_id: Any,
    display_output: str,
    pause_note: str,
) -> str:
    """Format the existing Shell status message without performing I/O."""
    status_emoji = "✅" if shell_result.get("success") else "❌"
    running_note = "运行中" if shell_result.get("running") else "已结束"
    if not shell_result.get("success"):
        running_note = "失败"
    pty_note = "PTY" if shell_result.get("pty") else "pipe"
    wait_seconds = shell_result.get("waited_seconds")
    wait_note = ""
    if wait_seconds is not None:
        wait_note = f"\n本次等待/捕获耗时: {escape_html(wait_seconds)} 秒"
    return (
        f"🖥️ <b>Agent Shell {escape_html(action_label)}</b>\n"
        f"会话: <code>{escape_html(session_id)}</code> · {escape_html(running_note)} · {escape_html(pty_note)}\n"
        f"{status_emoji} 状态: <code>{escape_html(shell_result.get('status') or shell_result.get('return_code') or '')}</code>"
        f"{wait_note}{escape_html(pause_note)}\n"
        f"<pre>{escape_html(display_output)}</pre>"
    )
