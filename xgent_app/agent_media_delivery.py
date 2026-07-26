"""Telegram delivery for completed Agent media generation results."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping, Sequence


async def send_media_generation_result(
    result: Mapping[str, Any],
    artifacts: Sequence[Any],
    notice: str,
    *,
    context: Any,
    chat_id: Any,
    send_artifacts: Callable[..., Awaitable[Any]],
    safe_send_message: Callable[..., Awaitable[Any]],
    safe_text: Callable[[Any], str],
    logger: Any,
) -> None:
    """Send generated media or the existing user-visible failure message."""
    if result.get("success"):
        try:
            await send_artifacts(
                context,
                chat_id,
                artifacts,
                caption=notice,
            )
        except Exception as error:
            logger.error("发送生成媒体失败: %s", error)
            await safe_send_message(
                context,
                chat_id,
                f"⚠️ 媒体已经生成，但发送给用户时出了点问题: {safe_text(str(error)[:200])}",
            )
        return

    await safe_send_message(
        context,
        chat_id,
        f"⚠️ 媒体生成失败: {safe_text(str(result.get('error') or '未知错误'))}",
    )
