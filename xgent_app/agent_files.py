"""File-operation execution helpers for Agent ``file`` protocols."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import uuid
from typing import Any, Mapping, TypedDict


_BASE64_WHITESPACE_RE = re.compile(r"\s+")


class WrittenFile(TypedDict):
    path: str
    filename: str
    size: int
    existed: bool


async def write_text_protocol_file(
    block: Mapping[str, Any], *, executor: Any
) -> WrittenFile:
    """Execute the existing UTF-8 ``file`` protocol write."""
    result = await executor.write_file(block["path"], block["body"])
    saved_path = result["path"]
    return {
        "path": saved_path,
        "filename": os.path.basename(saved_path)
        or f"bot_file_{uuid.uuid4().hex[:8]}.txt",
        "size": os.path.getsize(saved_path),
        "existed": bool(result["existed"]),
    }


async def write_base64_protocol_file(
    block: Mapping[str, Any], *, executor: Any
) -> WrittenFile:
    """Decode and write ``file:base64`` without blocking the event loop."""
    filename = block["path"]
    b64_content = block["body"]

    def _decode_and_write() -> tuple[str, bool, int]:
        clean_b64 = _BASE64_WHITESPACE_RE.sub("", b64_content)
        if not clean_b64:
            raise ValueError("base64 内容为空")
        raw_bytes = base64.b64decode(clean_b64, validate=False)
        target_path = executor.resolve_write_path(filename)
        existed = os.path.exists(target_path)
        parent = os.path.dirname(target_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target_path, "wb") as file_obj:
            file_obj.write(raw_bytes)
        return target_path, existed, os.path.getsize(target_path)

    target_path, existed, saved_size = await asyncio.to_thread(_decode_and_write)
    return {
        "path": target_path,
        "filename": os.path.basename(target_path)
        or f"bot_file_{uuid.uuid4().hex[:8]}.bin",
        "size": saved_size,
        "existed": existed,
    }
