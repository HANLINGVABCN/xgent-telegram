"""Telegram delivery helpers for files produced by Agent file protocols."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping


async def send_written_agent_file(
    written_file: Mapping[str, Any],
    *,
    protocol: str,
    context: Any,
    chat_id: Any,
    max_file_size: int,
    safe_send_message: Callable[..., Awaitable[Any]],
    safe_text: Callable[[Any], str],
    html_parse_mode: Any,
) -> str:
    """Deliver a written file and return the legacy Agent history notice."""
    saved_path = str(written_file["path"])
    saved_size = int(written_file["size"])
    safe_filename = str(written_file["filename"])
    label = "base64" if protocol == "file:base64" else ""

    if saved_size <= max_file_size:
        with open(saved_path, "rb") as saved_file:
            caption = (
                f"📄 已写入服务器并发送 (base64): {safe_filename}"
                if label
                else f"📄 已写入服务器并发送: {safe_filename}"
            )
            await context.bot.send_document(
                chat_id=chat_id,
                document=saved_file,
                filename=safe_filename,
                caption=caption,
            )
    else:
        size_label = "base64 文件" if label else "文件"
        await safe_send_message(
            context,
            chat_id,
            (
                f"✅ {size_label}已写入服务器，但超过发送大小限制。\n"
                f"路径: <code>{safe_text(saved_path)}</code>\n"
                f"大小: {saved_size} bytes"
            ),
            parse_mode=html_parse_mode,
        )

    return (
        f"[{protocol}结果] 已写入服务器文件: {saved_path} "
        f"({saved_size} bytes, {'覆盖' if written_file['existed'] else '新建'})"
    )
