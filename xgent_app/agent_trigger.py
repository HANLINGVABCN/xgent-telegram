"""Execution boundary for Agent trigger protocols."""

from __future__ import annotations

from typing import Any, Mapping


async def execute_trigger_protocol(
    block: Mapping[str, Any],
    *,
    trigger_manager: Any,
    bot: Any,
    chat_id: Any,
    conversation_id: Any,
    original_text: str,
    response: str,
) -> str:
    """Run a trigger and preserve its existing failure notice format."""
    try:
        return await trigger_manager.handle_protocol(
            block.get("path") or "",
            block.get("body") or "",
            bot,
            chat_id,
            conversation_id,
            original_text,
            response,
        )
    except Exception as exc:
        return f"[trigger结果] 操作失败: {str(exc)[:300]}"
