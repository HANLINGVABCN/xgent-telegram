"""Durable model-facing tool messages, independent of their UI presentation."""
from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import mimetypes
from copy import deepcopy
from dataclasses import dataclass, field

from xgent_app.attachments import AttachmentContextError, resolve_upload_path

TOOL_CONTEXT = "_tool_context_id"
PART = "_tool_context_part"

@dataclass
class ToolResultScope:
    row_ids: list[int] = field(default_factory=list)

active_tool_result = contextvars.ContextVar("active_tool_result", default=None)


def freeze_tool_message(message: dict, save_binary_upload) -> dict:
    """Snapshot native parts, never reread a mutable source path on the next turn."""
    result = deepcopy(message)
    assets = []
    content = result.get("content")
    if isinstance(content, list):
        for index, part in enumerate(content):
            if part.get("type") == "text":
                continue
            template = deepcopy(part)
            if part.get('type') in {'image', 'binary'} and isinstance(part.get('data'), str):
                raw = base64.b64decode(template.pop('data'), validate=True)
                name = part.get('filename') or ('tool-payload' + (
                    mimetypes.guess_extension(part.get('mime_type', '')) or '.bin'))
                encoding = 'native'
            else:
                raw = json.dumps(part, ensure_ascii=False, sort_keys=True).encode('utf-8')
                name, encoding = 'tool-context-part.json', 'json'
            saved = save_binary_upload(name, raw)
            reference = {'path': saved['abs_path'], 'size': len(raw), 'encoding': encoding,
                         'sha256': hashlib.sha256(raw).hexdigest()}
            if encoding == 'native':
                reference['part'] = template
            content[index] = {"type": PART, **reference}
            assets.append(reference)
    result.pop(TOOL_CONTEXT, None)
    return {"version": 1, "messages": [result], "assets": assets}


def messages_from_metadata(metadata: dict, row_id=None) -> list | None:
    payload = metadata.get("model_context")
    if payload is None:
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise AttachmentContextError("Unsupported tool-context version")
    messages = deepcopy(payload.get("messages"))
    if not isinstance(messages, list) or not all(
        isinstance(m, dict) and m.get("role") in {"user", "assistant", "system"}
        and isinstance(m.get("content"), (str, list)) for m in messages
    ):
        raise AttachmentContextError("Invalid persisted tool context")
    if row_id is not None:
        for message in messages:
            message[TOOL_CONTEXT] = row_id
    return messages


def restore_tool_context(history: list, records: list, upload_root) -> list:
    """Also keep native tool payloads outside the ordinary text history window."""
    from xgent_app.compression import metadata_of
    result = deepcopy(history)
    present = {m.get(TOOL_CONTEXT) for m in result if TOOL_CONTEXT in m}
    prefix = []
    for record in records:
        metadata = metadata_of(record)
        payload = metadata.get("model_context") or {}
        if payload.get("assets") and record.get("id") not in present:
            prefix.extend(messages_from_metadata(metadata, record.get("id")) or [])
    result = prefix + result
    for message in result:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for index, part in enumerate(content):
            if part.get("type") != PART:
                continue
            path = resolve_upload_path(part["path"], upload_root)
            try:
                raw = path.read_bytes()
                if len(raw) != part["size"] or hashlib.sha256(raw).hexdigest() != part["sha256"]:
                    raise ValueError("snapshot changed")
                content[index] = ({**part['part'], 'data': base64.b64encode(raw).decode('ascii')}
                                  if part.get('encoding') == 'native' else json.loads(raw))
            except Exception as exc:
                raise AttachmentContextError(f"Tool payload unavailable: {path}: {exc}") from exc
    return result


def deduplicate_attachment_parts(history: list, parts: list) -> list:
    # Keep original conversational placement. The global attachment prefix must
    # not send an identical native payload for a second time.
    def key(part):
        if part.get("type") in {"image", "binary"} and part.get("data"):
            return (part["type"], part.get("mime_type"), part["data"])
        return None
    seen = {key(p) for m in history if isinstance(m.get("content"), list)
            for p in m["content"] if key(p) is not None}
    return [p for p in parts if key(p) is None or key(p) not in seen]
