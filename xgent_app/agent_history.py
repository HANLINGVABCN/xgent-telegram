"""Persistence helpers for Agent operation history.

The Agent loop decides *what* should be stored; this module owns the repeated
ordering and call shape for recorder and conversation-history writes.
"""

from __future__ import annotations

from typing import Any, Optional

from xgent_app.attachments import AttachmentContextError


async def persist_agent_result(
    *,
    recorder: Any,
    message_type: Any,
    database: Any,
    conversation_id: Any,
    chat_id: Any,
    notice: str,
    display_content: Optional[str] = None,
) -> None:
    """Record an Agent result in both global and conversation history.

    ``notice`` is the plain-text form fed to the model as conversation
    context. ``display_content`` is what gets stored for *display* (global
    history / web+CLI cross-sync) — pass it when the live send used a richer
    presentation (e.g. an HTML card via build_standard_operation_presentation)
    than the plain notice, so a page refresh renders the same thing the user
    already saw live instead of falling back to bare notice text. Defaults to
    ``notice`` when the caller has no separate presentation.
    """
    await recorder.record(
        msg_type=message_type,
        role="system",
        content=notice if display_content is None else display_content,
        chat_id=chat_id,
    )
    await database.add_chat_message(conversation_id, "user", notice)


async def persist_standard_operation_result(
    *,
    recorder: Any,
    message_type: Any,
    database: Any,
    conversation_id: Any,
    chat_id: Any,
    operation: dict[str, Any],
    presentation: Optional[str] = None,
) -> None:
    """Persist every normalized standard operation in both history views.

    ``presentation`` is the HTML card actually sent to the user live (from
    ``build_standard_operation_presentation``); passing it keeps refreshed
    history visually identical to the live message instead of degrading to
    the plain-text notice (e.g. "Agent Run" losing its emoji/bold/<pre> box
    and turning into a wall of plain text after a page reload).
    """
    await persist_agent_result(
        recorder=recorder,
        message_type=message_type,
        database=database,
        conversation_id=conversation_id,
        chat_id=chat_id,
        notice=operation["notice"],
        display_content=presentation,
    )


async def persist_media_result(
    *,
    recorder: Any,
    database: Any,
    conversation_id: Any,
    chat_id: Any,
    notice: str,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Preserve the media-specific recorder and conversation-history format."""
    if metadata is None:
        await recorder.record_media_reply(notice, chat_id)
    else:
        row_id = await recorder.record_media_reply(notice, chat_id, metadata=metadata)
        if metadata.get("attachments") and row_id is None:
            raise AttachmentContextError("Generated-image associations were not saved")
    kwargs = {}
    if metadata and "attachment_generation" in metadata:
        kwargs["attachment_generation"] = metadata["attachment_generation"]
    try:
        await database.add_chat_message(
            conversation_id, "user", f"[外部媒体模块回复]\n{notice}", **kwargs,
        )
    except Exception as exc:
        if kwargs:
            raise AttachmentContextError(f"Generated-image history sync failed: {exc}") from exc
        raise
