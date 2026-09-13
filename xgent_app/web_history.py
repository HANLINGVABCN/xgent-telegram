"""Display-only history and durable, authenticated attachment descriptors."""

from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Any

from xgent_app.web_media import generated_media_group_id


_UPLOAD = re.compile(
    r"^(?:\u7b2c\d+\u5f20: )?\[(?:\u6587\u4ef6|\u56fe\u7247)\] (?P<name>.+?)"
    r"\uff0c\u5df2\u4fdd\u5b58\u5230 (?P<path>.+?)(?:\u3002\u8bf4\u660e\uff1a.*)?$"
)
_GENERATED = re.compile(
    r"^\u3010\u7cfb\u7edf\u81ea\u52a8\u751f\u6210\uff1a"
    r"\u672c(?:\u56fe\u7247|\u89c6\u9891|\u97f3\u9891)\u5df2\u81ea\u52a8\u5b58\u5165 "
    r"(?P<path>.+?)\uff0c(?:\u9700\u8981\u65f6\u8bf7read|"
    r"\u539f\u56fe\u81ea\u52a8\u8fdb\u5165).*\u3011$",
    re.MULTILINE,
)
_DELIVERED = re.compile(
    r"^\[(?:sendfile|file(?::base64)?)\u7ed3\u679c\] "
    r"\u5df2(?:\u53d1\u9001\u670d\u52a1\u5668\u6587\u4ef6\u7ed9\u7528\u6237|"
    r"\u5199\u5165\u670d\u52a1\u5668\u6587\u4ef6): "
    r"(?P<path>.+) \(\d+ bytes(?:, [^)\r\n]+)?\)(?: \[\u672c\u5730API\u76f4\u53d1\])?$"
)
_EXPORT = re.compile(
    r"\u670d\u52a1\u5668\u6587\u4ef6\u8def\u5f84\uff1a(?P<path>.+?)"
    r"\uff08\d+ bytes\uff09"
)
_HTML_PRESENTATION = re.compile(r"<(?:b|i|pre|code|blockquote)(?:\s|>)", re.I)
_CONFIG_PREFIXES = (
    "[\u63d0\u4f9b\u5546\u914d\u7f6e\u6587\u4ef6]",
    "[\u9ed1\u540d\u5355\u6587\u4ef6]",
    "[\u8bb0\u5fc6\u6587\u4ef6]",
)
_GENERATED_PREFIX = "\u3010\u7cfb\u7edf\u81ea\u52a8\u751f\u6210\uff1a\u672c"
_FILE_TYPES = {"user_file", "user_photo", "ai_reply", "media_reply", "agent_result", "system_op"}
_MISSING = "\u539f\u4ef6\u4e0d\u5b58\u5728\u6216\u65e0\u6cd5\u8bfb\u53d6"
_INVALID = "\u9644\u4ef6\u5173\u8054\u65e0\u6548"


def media_kind(mime_type: str) -> str:
    # Active formats must be downloaded, not embedded on the app's origin.
    if not isinstance(mime_type, str):
        raise ValueError(_INVALID)
    mime_type = mime_type.lower()
    if mime_type.startswith("image/") and mime_type != "image/svg+xml":
        return "photo"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
        return "audio"
    return "file"


def display_media_reference(
    path: str, name: str | None = None, mime_type: str | None = None,
) -> dict[str, Any]:
    """Authorize exactly the file that a trusted operation saved or delivered."""
    target = Path(path).resolve()
    filename = name or target.name
    return {
        "version": 1, "path": str(target), "name": filename,
        "mime_type": mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream",
    }


def delivered_file_reference(notice: str) -> list[dict[str, Any]]:
    """Only complete operation results authorize files outside app storage."""
    match = _DELIVERED.fullmatch(notice)
    if not match or not Path(match["path"]).is_absolute():
        return []
    return [display_media_reference(match["path"])]


def _within(path: str, root: Path) -> Path:
    root = root.resolve()
    target = Path(path)
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError("\u8def\u5f84\u4e0d\u5728\u5141\u8bb8\u7684\u76ee\u5f55\u5185")
    return target


def _legacy_media(record: dict, storage: Path, workspace: Path) -> list[dict]:
    kind, content = record.get("msg_type"), str(record.get("content") or "")
    refs = []
    if kind in {"user_file", "user_photo"}:
        for line in content.splitlines():
            match = _UPLOAD.fullmatch(line)
            if match:
                refs.append({
                    "path": match["path"], "name": match["name"], "_root": storage / "uploads",
                    "_legacy_pattern": r"\d{6}_[0-9a-f]{8}_.+",
                })
    elif kind in {"ai_reply", "media_reply"}:
        for match in _GENERATED.finditer(content):
            refs.append({
                "path": match["path"], "_root": storage / "generated_media",
                "_legacy_pattern": r"\d{6}_[0-9a-f]{8}_assistant_(?:image|video|audio)\.[a-zA-Z0-9]+",
            })
    elif kind == "agent_result":
        match = _DELIVERED.fullmatch(content)
        if match:
            refs.append({"path": match["path"], "_roots": (storage, workspace)})
    elif kind == "system_op" and "\u5df2\u6210\u529f\u5bfc\u51fa" in content:
        for match in _EXPORT.finditer(content):
            refs.append({
                "path": match["path"], "_root": storage / "exports",
                "_legacy_pattern": r"\d{6}_[0-9a-f]{8}_.+",
                "name": "\u7cfb\u7edf\u8bb0\u5fc6.zip" if match["path"].endswith(".zip") else None,
            })
    return refs


def _describe_media(ref: Any, storage: Path, *, attachment: bool) -> dict[str, Any]:
    item: dict[str, Any] = {"filename": "\u9644\u4ef6", "kind": "file"}
    try:
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
            raise ValueError(_INVALID)
        item["filename"] = str(ref.get("name") or Path(ref["path"]).name)
        if attachment:
            category = ref.get("storage", "uploads")
            if category not in {"uploads", "generated_media"}:
                raise ValueError(_INVALID)
            target = _within(ref["path"], storage / category)
        elif "_root" in ref:
            target = _within(ref["path"], ref["_root"])
        elif "_roots" in ref:
            target = None
            for root in ref["_roots"]:
                try:
                    target = _within(ref["path"], root)
                    break
                except ValueError:
                    pass
            if target is None:
                raise ValueError("\u65e7\u8bb0\u5f55\u8def\u5f84\u4e0d\u5728\u53ef\u6062\u590d\u76ee\u5f55\u5185")
        else:
            target = Path(ref["path"])
            if ref.get("version") != 1 or not target.is_absolute() or target.resolve() != target:
                raise ValueError(_INVALID)
        if ref.get("_legacy_pattern"):
            relative = target.relative_to(ref["_root"].resolve())
            if (len(relative.parts) != 2
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", relative.parts[0])
                    or not re.fullmatch(ref["_legacy_pattern"], target.name)):
                raise ValueError("\u65e7\u9644\u4ef6\u8def\u5f84\u65e0\u6cd5\u9a8c\u8bc1")
        item["path"] = str(target)
        mime = ref.get("mime_type") or mimetypes.guess_type(item["filename"])[0] or "application/octet-stream"
        if not isinstance(mime, str) or not re.fullmatch(r"[a-zA-Z0-9.+_-]+/[a-zA-Z0-9.+_-]+", mime):
            raise ValueError(_INVALID)
        mime = mime.lower()
        item.update(mime_type=mime, kind=media_kind(mime))
        if not target.is_file():
            raise OSError(_MISSING)
        item["size"] = target.stat().st_size
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        item["error"] = str(exc) if isinstance(exc, ValueError) else _MISSING
    return item


def build_history_message(
    record: dict[str, Any], storage_root: str | Path, workspace_root: str | Path,
) -> dict[str, Any]:
    """No Base64 or short-lived download tokens enter the history response."""
    kind = record.get("msg_type")
    role = record.get("role") or "user"
    if kind in {"agent_result", "media_reply", "agent_status"}:
        role = "assistant"
    elif kind == "token_usage":
        role = "system"
    content = str(record.get("content") or "")
    message = {
        "id": record.get("id"), "timestamp": record.get("timestamp"), "msg_type": kind,
        "role": role, "content": content, "media": [],
    }
    raw_metadata = record.get("metadata") or {}
    try:
        metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
        if not isinstance(metadata, dict):
            raise ValueError(_INVALID)
    except (ValueError, TypeError):
        metadata = {}
        message["media_error"] = "\u5386\u53f2\u9644\u4ef6\u5143\u6570\u636e\u635f\u574f"
    display = metadata.get("display")
    if isinstance(display, dict) and isinstance(display.get("content"), str):
        message.update(content=display["content"], parse_mode=display.get("parse_mode"))
    elif kind not in {"ai_reply", "media_reply", "user_text", "user_file", "user_photo"}:
        if _HTML_PRESENTATION.search(content):
            message["parse_mode"] = "HTML"
    if kind not in _FILE_TYPES:
        return message
    if (metadata.get("attachment_purpose") == "configuration"
            or kind in {"user_file", "user_photo"} and content.startswith(_CONFIG_PREFIXES)):
        return message
    storage, workspace = Path(storage_root), Path(workspace_root)
    groups = []
    for key in ("display_media", "attachments"):
        if key in metadata:
            refs = metadata[key]
            if not isinstance(refs, list):
                message["media_error"] = _INVALID
                continue
            if key == "attachments" and not refs:
                message["media_error"] = _INVALID
            if key == "attachments":
                refs = sorted(enumerate(refs), key=lambda pair: (
                    pair[1].get("order", pair[0])
                    if isinstance(pair[1], dict) and type(pair[1].get("order", pair[0])) is int
                    else pair[0], pair[0],
                ))
                refs = [ref for _, ref in refs]
            groups.extend((ref, key == "attachments") for ref in refs)
    if "attachments" not in metadata and "display_media" not in metadata:
        if not (kind in {"ai_reply", "media_reply"} and metadata.get("generated_media_processed") is True):
            legacy = _legacy_media(record, storage, workspace)
            groups.extend((ref, False) for ref in legacy)
            if not legacy and (kind in {"user_file", "user_photo"} or (
                    kind in {"ai_reply", "media_reply"} and _GENERATED_PREFIX in content)):
                message["media_error"] = "\u65e7\u8bb0\u5f55\u672a\u4fdd\u7559\u53ef\u9a8c\u8bc1\u7684\u9644\u4ef6\u5173\u8054\uff0c\u65e0\u6cd5\u6062\u590d\u4e0b\u8f7d"
            album = re.search(r"\[\u76f8\u518c\] \u5171(\d+)\u5f20\u56fe\u7247", content)
            if kind == "user_photo" and album and int(album[1]) != len(legacy):
                message["media_error"] = "\u65e7\u76f8\u518c\u9644\u4ef6\u8bb0\u5f55\u4e0d\u5b8c\u6574"
    seen = set()
    for ref, attachment in groups:
        item = _describe_media(ref, storage, attachment=attachment)
        if item.get("path") and item["path"] in seen:
            continue
        if item.get("path"):
            seen.add(item["path"])
        index = len(message["media"])
        if not item.get("error") and type(record.get("id")) is int:
            item["download_url"] = f"/api/history/media/{record['id']}/{index}"
        message["media"].append(item)
    if kind in {'ai_reply', 'media_reply'} and message['media']:
        message['media_group_id'] = generated_media_group_id([
            item.get('path', '') for item in message['media']
        ])
    return message
