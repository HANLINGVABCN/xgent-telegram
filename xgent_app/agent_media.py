"""Lifecycle management for Agent external-media generation.

This module owns waiting, stop handling, typing indicators, and cleanup.  It
intentionally does not format results, send generated artifacts, or persist
history; those remain separate responsibilities.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional, TypedDict


class MediaGenerationExecution(TypedDict):
    stopped: bool
    result: Optional[dict[str, Any]]


async def execute_media_generation(
    prompt: str,
    *,
    context: Any,
    chat_id: Any,
    generate_media: Callable[[str], Awaitable[dict[str, Any]]],
    keep_typing: Callable[[Any, Any, asyncio.Event], Awaitable[None]],
    stop_event_factory: Callable[[], asyncio.Event],
    stop_requested: Callable[[], bool],
    build_stop_keyboard: Callable[[], Any],
    safe_edit_text: Callable[..., Awaitable[Any]],
    cancel_task_quietly: Callable[..., Awaitable[Any]],
) -> MediaGenerationExecution:
    """Generate media while preserving the existing Telegram stop behavior."""
    typing_stop = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(context, chat_id, typing_stop))
    drawing_message = None
    media_task = None
    stop_task = None
    stopped = False

    try:
        drawing_message = await context.bot.send_message(
            chat_id=chat_id,
            text="🎨 正在生成媒体... 请稍等",
            reply_markup=build_stop_keyboard(),
        )

        media_task = asyncio.create_task(generate_media(prompt))
        stop_task = asyncio.create_task(stop_event_factory().wait())
        done, _pending = await asyncio.wait(
            {media_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done and stop_requested():
            stopped = True
            await safe_edit_text(
                drawing_message,
                "⏹️ 媒体生成已停止。",
                reply_markup=None,
            )
            await cancel_task_quietly(media_task, timeout=1.0)
            return {"stopped": True, "result": None}

        await cancel_task_quietly(stop_task, timeout=0.2)
        return {"stopped": False, "result": await media_task}
    finally:
        if stopped and media_task is not None and not media_task.done():
            await cancel_task_quietly(media_task, timeout=1.0)
        if stop_task is not None and not stop_task.done():
            await cancel_task_quietly(stop_task, timeout=0.2)
        typing_stop.set()
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass

        if drawing_message is not None and not stopped:
            try:
                await drawing_message.delete()
            except Exception:
                pass
