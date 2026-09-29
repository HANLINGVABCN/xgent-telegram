"""Pure Telegram presentation builders for Agent operation results.

This module formats user-visible text only.  It does not execute protocols,
send messages, persist history, or build model continuation context.
"""

from __future__ import annotations

import html
from typing import Any, Mapping, Optional

from xgent_app.shell_output import format_shell_display_output


def escape_html(value: Any) -> str:
    """Match the legacy ``safe_text`` behavior used by Agent messages.

    Only ``None`` renders as empty.  A plain falsy check would turn the integer
    ``0`` into an empty string, so a successful command's ``return_code`` showed
    up as an empty ``<code></code>`` while the model-facing context said ``0``.
    """
    if value is None:
        return ""
    return html.escape(str(value))


def fold_output_block(body: str, *, kind: str, icon: str) -> str:
    """把结果卡片里本来直接显示的 <pre> 代码块，包成三端一致的可折叠 <blockquote expandable>。

    只改这段命令/工具输出的呈现（可收起/展开）；返回码、完整输出路径、命中数等卡片其余
    内容都在块外、原样不动。正文沿用调用方既有的截断与转义（不再二次删改），块头
    ``{icon} {kind}-x · N 行`` 刻意对齐 protocols/CLI 的折叠识别形态，使 Telegram、
    网页、CLI 三端拿同一份 HTML 折叠一致——协议块本就是这么折的（见 [[protocols]] 的
    ``_render_block_blockquote`` 与 CLI 的 ``_HEADER_RE``）。

    正文 ≤ 3 行时三端都直接展示、不出折叠箭头（与协议块一致），所以短输出无副作用。
    """
    line_count = 0 if not body else body.count("\n") + 1
    header = f"<b>{escape_html(f'{icon} {kind}-x')}</b> · {line_count} 行"
    return (
        f"<blockquote expandable>{header}\n"
        f"<pre>{escape_html(body)}</pre></blockquote>"
    )


def build_edit_presentation(result: Mapping[str, Any]) -> str:
    notice = str(result.get("notice") or result.get("output") or "")
    emoji = "✏️" if result.get("success") else "⚠️"
    fold = fold_output_block(notice[:1500], kind="edit", icon="✏️")
    return f"{emoji} <b>Agent Edit</b>\n{fold}"


def build_grep_presentation(result: Mapping[str, Any]) -> str:
    notice = str(result.get("notice") or result.get("output") or "")
    emoji = "🔎" if result.get("success") else "⚠️"
    hits = result.get("hits", 0)
    fold = fold_output_block(notice[:2000], kind="grep", icon="🔎")
    return f"{emoji} <b>Agent Grep</b> 命中 {hits} 处\n{fold}"


def build_run_presentation(result: Mapping[str, Any]) -> str:
    display_output = format_shell_display_output(
        str(result.get("output") or "(无输出)"),
        running=False,
    )
    status_emoji = "✅" if result.get("success") else "❌"
    output_path = result.get("output_path")
    path_line = (
        f"完整输出: <code>{escape_html(output_path)}</code>\n"
        if output_path else "完整输出: <i>存档失败，仅保留上方内容</i>\n"
    )
    fold = fold_output_block(display_output, kind="run", icon="⌨️")
    return (
        "⌨️ <b>Agent Run</b>\n"
        f"{status_emoji} 返回码: <code>{escape_html(result.get('return_code'))}</code>\n"
        f"{path_line}"
        f"{fold}"
    )



def build_search_presentation(result: Mapping[str, Any]) -> str:
    notice = str(result.get("notice") or result.get("output") or "")
    emoji = "🌐" if result.get("success") else "⚠️"
    hits = len(result.get("results") or [])
    fold = fold_output_block(notice[:2500], kind="search", icon="🌐")
    return f"{emoji} <b>Agent Search</b> 命中 {hits} 条\n{fold}"


def build_fetch_presentation(result: Mapping[str, Any]) -> str:
    notice = str(result.get("notice") or result.get("output") or "")
    emoji = "📄" if result.get("success") else "⚠️"
    pages = len(result.get("results") or [])
    fold = fold_output_block(notice[:2500], kind="fetch", icon="📄")
    return f"{emoji} <b>Agent Fetch</b> 抓取 {pages} 个页面\n{fold}"


def build_standard_operation_presentation(
    result: Mapping[str, Any],
) -> Optional[str]:
    """Select the existing presentation for a normalized standard result."""
    builders = {
        "edit": build_edit_presentation,
        "grep": build_grep_presentation,
        "run": build_run_presentation,
        "search": build_search_presentation,
        "fetch": build_fetch_presentation,
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
    fold = fold_output_block(display_output, kind="shell", icon="🖥️")
    return (
        f"🖥️ <b>Agent Shell {escape_html(action_label)}</b>\n"
        f"会话: <code>{escape_html(session_id)}</code> · {escape_html(running_note)} · {escape_html(pty_note)}\n"
        f"{status_emoji} 状态: <code>{escape_html(shell_result.get('status') or shell_result.get('return_code') or '')}</code>"
        f"{wait_note}{escape_html(pause_note)}\n"
        f"{fold}"
    )
