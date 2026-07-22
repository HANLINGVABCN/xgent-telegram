"""Persistence helpers for Agent operation history.

The Agent loop decides *what* should be stored; this module owns the repeated
ordering and call shape for recorder and conversation-history writes.
"""

from __future__ import annotations

from typing import Any


async def persist_agent_result(
    *,
    recorder: Any,
    message_type: Any,
    database: Any,
    conversation_id: Any,
    chat_id: Any,
    notice: str,
    add_to_conversation: bool = True,
) -> None:
    """Record an Agent result, then optionally append it to model history."""
    await recorder.record(
        msg_type=message_type,
        role="system",
        content=notice,
        chat_id=chat_id,
    )
    if add_to_conversation:
        await database.add_chat_message(conversation_id, "user", notice)


async def persist_media_result(
    *,
    recorder: Any,
    database: Any,
    conversation_id: Any,
    chat_id: Any,
    notice: str,
) -> None:
    """Preserve the media-specific recorder and conversation-history format."""
    await recorder.record_media_reply(notice, chat_id)
    await database.add_chat_message(
        conversation_id, "user", f"[外部媒体模块回复]\n{notice}"
    )
