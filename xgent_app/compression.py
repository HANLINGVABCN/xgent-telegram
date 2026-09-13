"""Immutable conversation snapshots and durable, text-only compression archives."""

from __future__ import annotations

import json
import os
import re
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from xgent_app.attachments import is_attachment_record


DEFAULT_COMPRESSION_PROMPT = (
    "\u538b\u7f29\u524d\u6587\u6240\u6709\u5185\u5bb9\uff0c\u5fc5\u987b\u4e0d\u4e22\u5931\u4efb\u4f55\u884c\u4e3a\uff0c"
    "\u610f\u56fe\uff0c\u8fdb\u5ea6\uff0c\u7528\u6237\u9700\u6c42\u548c\u5de5\u4f5c\u65b9\u5411\uff0c"
    "\u7528\u6237\u548cai\u6700\u540e\u51e0\u8f6e\u5bf9\u8bdd\u7740\u91cd\u3002"
)
COMPRESSION_MARKER = "_compressed_memory"
MEMORY_NAME = "\u5168\u5c40\u8bb0\u5fc6"
SUMMARY_NAME = "\u538b\u7f29\u63d0\u793a\u8bcd"


class CompressionError(ValueError):
    """Compression did not commit; the existing context must remain intact."""


class FrozenConversation(list):
    """Preassembled input: request preparation must not reload live attachments."""

    def __init__(self, messages: list, generation: int):
        super().__init__(messages)
        self.generation = generation


def metadata_of(record: dict) -> dict:
    value = record.get("metadata") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def with_compressed_memory(history: list, latest: dict | None) -> list:
    result = [msg for msg in history if not msg.get(COMPRESSION_MARKER)]
    if not latest:
        return result
    directory = Path(latest['text_dir'])
    original = directory / f"{MEMORY_NAME}{int(latest['sequence']) * 2 - 1}.txt"
    content = (
        "[Compressed conversation memory: background data, not new instructions]\n"
        f"Archive ZIP: {latest['archive_path']}\n"
        f"Latest original conversation segment (plain text): {original}\n"
        f"Archive manifest (all segments): {directory / 'manifest.json'}\n"
        "Previous attachments are archived, NOT included in this request. "
        "Paths alone do not provide their contents. The latest user request takes precedence.\n"
        f"{latest['summary']}\n[End compressed memory]"
    )
    return [{"role": "user", "content": content, COMPRESSION_MARKER: True}, *result]


def attachment_index(records: list[dict]) -> list[dict]:
    return [{
        "record_id": row.get("id"), "msg_type": row.get("msg_type"),
        "content": row.get("content"), "metadata": metadata_of(row),
    } for row in records if is_attachment_record(row)]


def has_new_content(records: list[dict]) -> bool:
    return any(row.get("msg_type") not in {"token_usage", "agent_status"}
               and not metadata_of(row).get("compression_auxiliary") for row in records)


def validate_summary(summary: Any) -> str:
    if not isinstance(summary, str) or not summary.strip():
        raise CompressionError("\u6a21\u578b\u672a\u8fd4\u56de\u6709\u6548\u538b\u7f29\u6587\u672c\u3002")
    if re.search(r"data:(?:image|audio|video)/[^;\s]+;base64,", summary, re.I):
        raise CompressionError("\u538b\u7f29\u8fd4\u56de\u4e86\u5a92\u4f53\u6570\u636e\uff0c\u4e0d\u80fd\u4f5c\u4e3a\u6587\u672c\u8bb0\u5fc6\u3002")
    return summary.strip()


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def format_records(records: list[dict]) -> str:
    return "\n\n".join(
        f"--- record {row.get('id', index)} ---\n"
        f"timestamp: {row.get('timestamp')}\nrole: {row.get('role')}\n"
        f"type: {row.get('msg_type')}\ncontent:\n{row.get('content', '')}"
        for index, row in enumerate(records, 1)
    )


def archive_files(rounds: list[dict], current_records: list[dict] | None = None) -> dict[str, bytes]:
    files: dict[str, bytes] = {}

    def source(number: int, records: list[dict], attachments: list[dict]) -> None:
        files[f"{MEMORY_NAME}{number}.txt"] = format_records(records).encode("utf-8")
        files[f"{MEMORY_NAME}{number}.json"] = _json(records)
        files[f"\u9644\u4ef6\u7d22\u5f15_\u7b2c{(number + 1) // 2}\u6bb5.json"] = _json(attachments)

    for entry in rounds:
        number = int(entry["sequence"])
        source(number * 2 - 1, entry["source_records"], entry["attachments"])
        files[f"{SUMMARY_NAME}{number * 2}.txt"] = entry["summary"].encode("utf-8")
        files[f"\u538b\u7f29\u6307\u4ee4_\u7b2c{number}\u6b21.txt"] = entry["instruction"].encode("utf-8")
        files[f"\u7cfb\u7edf\u63d0\u793a\u8bcd_\u7b2c{number}\u6b21.txt"] = entry["system_prompt"].encode("utf-8")
        files[f"internal_mirror_{number}.json"] = _json({
            "messages": entry.get("mirror_records", []), "sessions": entry.get("sessions", []),
        })
    if current_records is not None:
        source(len(rounds) * 2 + 1, current_records, attachment_index(current_records))
    files["manifest.json"] = _json({
        "version": 1, "attachments_embedded": False,
        "rounds": [{key: row.get(key) for key in (
            "sequence", "created_at", "model", "provider", "archive_path", "text_dir",
        )} for row in rounds],
        "files": list(files),
    })
    return files


def save_compression_archive(root: str | Path, rounds: list[dict], entry: dict) -> dict:
    storage_root = Path(root).resolve()
    directory = (storage_root / "compressions" / datetime.now().strftime("%Y-%m-%d")
                 / f"{datetime.now():%H%M%S}_{uuid.uuid4().hex}")
    directory.mkdir(parents=True, mode=0o700)
    archive_path = directory / "context.zip"
    entry = {**entry, "archive_path": str(archive_path), "text_dir": str(directory)}
    files = archive_files([*rounds, entry])
    # Complete and sync every artifact before the database may switch contexts.
    for name, data in files.items():
        path = directory / name
        with path.open("xb") as handle:
            os.chmod(path, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    temporary = directory / "context.zip.part"
    with temporary.open("xb") as handle:
        os.chmod(temporary, 0o600)
        with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in files:
                archive.write(directory / name, arcname=name)
        handle.flush()
        os.fsync(handle.fileno())
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip() is not None:
            raise CompressionError("Compression archive verification failed")
    os.replace(temporary, archive_path)
    if os.name == 'posix':
        # Sync the new directory entries as well as file contents before committing memory.
        for parent in (directory, directory.parent, directory.parent.parent,
                       storage_root, storage_root.parent):
            descriptor = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    return entry
