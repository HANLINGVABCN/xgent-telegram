"""Shared conversation exports and one-turn, read-backed context restoration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from xgent_app.attachments import _generated_notice_lines, is_attachment_record
from xgent_app.generated_media import GeneratedMediaReply
from xgent_app.web_history import build_history_message


DEFAULT_COMPRESSION_PROMPT = (
    "\u538b\u7f29\u524d\u6587\u6240\u6709\u5185\u5bb9\uff0c\u5fc5\u987b\u4e0d\u4e22\u5931\u4efb\u4f55\u884c\u4e3a\uff0c"
    "\u610f\u56fe\uff0c\u8fdb\u5ea6\uff0c\u7528\u6237\u9700\u6c42\u548c\u5de5\u4f5c\u65b9\u5411\uff0c"
    "\u7528\u6237\u548cai\u6700\u540e\u51e0\u8f6e\u5bf9\u8bdd\u7740\u91cd\u3002"
)
COMPRESSION_MARKER = "_compressed_memory"
ARCHIVE_MARKER = "_conversation_archive"
MEMORY_NAME = "\u5168\u5c40\u8bb0\u5fc6"
ATTACHMENTS_NAME = "\u9644\u4ef6\u8def\u5f84"
SUMMARY_NAME = "\u538b\u7f29\u7ed3\u679c"
INSTRUCTION_NAME = "\u538b\u7f29\u63d0\u793a\u8bcd.txt"
EXPORT_NAME = "\u7cfb\u7edf\u8bb0\u5fc6.zip"
_INLINE_DATA = re.compile(r"data:(?:image|audio|video)/[^;\s]+;base64,", re.I)


class CompressionError(ValueError):
    """A failed export or restore; the durable archive must remain available."""


class FrozenConversation(list):
    """Complete one-turn input; never reload attachments or archived summaries."""

    def __init__(self, messages: list, generation: int):
        super().__init__(messages)
        self.generation = generation


class CompressionReply(GeneratedMediaReply):
    """Use the normal reply handoff, persisting complete text before delivery."""

    text_only = True

    def __init__(self, persist):
        super().__init__(None, persist)
        self.completed = False
        self.partial = False
        self.stopped = False
        self.error = ""

    async def _prepare(self, raw: str, stopped: bool, partial: bool) -> tuple[str, list[dict]]:
        self.partial, self.stopped = partial or stopped, stopped
        try:
            if self.partial and not raw.strip():
                return "", []
            self.text = validate_summary(raw)
            await self.persist(self.text, [], stopped)
            self.recorded = True
            self.completed = not self.partial
            return self.text, []
        except Exception as exc:
            self.error = str(exc)
            raise


def metadata_of(record: dict) -> dict:
    value = record.get("metadata") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def completed(entry: dict) -> bool:
    return bool(entry.get("summary")) and entry.get("status", "completed") == "completed"


def with_archive_reference(history: list, latest: dict | None) -> list:
    result = [msg for msg in history if not msg.get(COMPRESSION_MARKER) and not msg.get(ARCHIVE_MARKER)]
    if not latest:
        return result
    directory = Path(latest['text_dir'])
    number = int(latest['sequence'])
    original = latest.get('memory_path') or str(directory / f"{MEMORY_NAME}{number * 2 - 1}.txt")
    content = (
        "[Conversation archive reference; paths only, not file contents]\n"
        f"Archive ZIP: {latest['archive_path']}\n"
        f"Text archive directory (segments 1-{number}): {directory}\n"
        f"Latest original segment: {original}\n"
        "Archived text and attachments are NOT included in this request. "
        "Use the existing read-x operation when their contents are needed; "
        "Agent-off mode does not authorize protocol execution."
    )
    return [{"role": "user", "content": content, ARCHIVE_MARKER: True}, *result]


def attachment_index(records: list[dict], storage_root: str | Path | None = None) -> list[dict]:
    """Describe stored references without opening, validating, or embedding originals."""
    result = []
    root = Path(storage_root) if storage_root else None
    fields = ('id', 'name', 'filename', 'kind', 'mime_type', 'size', 'sha256',
              'width', 'height', 'encoding', 'storage', 'source', 'error')
    for row in records:
        metadata = metadata_of(row)
        seen = set()
        for key in ('attachments', 'display_media'):
            refs = metadata.get(key, [])
            if not isinstance(refs, list):
                refs = []
            indexed = [(i, ref) for i, ref in enumerate(refs) if isinstance(ref, dict)]
            indexed.sort(key=lambda pair: (pair[1].get('order')
                                          if type(pair[1].get('order')) is int else pair[0], pair[0]))
            for offset, ref in indexed:
                path = str(ref.get('path') or '')
                if not path:
                    continue
                if (key == 'attachments' and root is not None and not Path(path).is_absolute()
                        and not PureWindowsPath(path).is_absolute()):
                    path = str(root / str(ref.get('storage') or 'uploads') / path)
                identity = (str(PureWindowsPath(path)).casefold() if PureWindowsPath(path).is_absolute()
                            else os.path.normcase(os.path.normpath(path)))
                if identity in seen:
                    continue
                seen.add(identity)
                result.append({
                    'record_id': row.get('id'), 'msg_type': row.get('msg_type'),
                    'order': len(result) + 1, 'attachment_order': ref.get('order', offset),
                    'path': path, **{name: ref[name] for name in fields if name in ref},
                })
        if not seen and is_attachment_record(row):
            # Reuse display-only legacy path checks; never open the attachment body.
            candidate = dict(row)
            if row.get('msg_type') in {'ai_reply', 'media_reply'}:
                candidate['content'] = '\n'.join(_generated_notice_lines(str(row.get('content') or '')))
            display = build_history_message(candidate, root, root) if root is not None else {}
            for offset, item in enumerate(display.get('media', [])):
                result.append({'record_id': row.get('id'), 'msg_type': row.get('msg_type'),
                               'order': len(result) + 1, 'attachment_order': offset,
                               **{key: value for key, value in item.items() if key != 'download_url'}})
            if display.get('media_error') or not display:
                result.append({'record_id': row.get('id'), 'order': len(result) + 1,
                               'notice': row.get('content', ''),
                               'error': display.get('media_error') or 'No structured attachment reference'})
    return result


def has_new_content(records: list[dict]) -> bool:
    return any(row.get('msg_type') not in {'token_usage', 'agent_status'}
               and not metadata_of(row).get('compression_auxiliary')
               and not metadata_of(row).get('compression_job_id') for row in records)


def validate_summary(summary: Any) -> str:
    if not isinstance(summary, str) or not summary.strip():
        raise CompressionError("\u6a21\u578b\u672a\u8fd4\u56de\u6709\u6548\u538b\u7f29\u6587\u672c\u3002")
    if _INLINE_DATA.search(summary):
        raise CompressionError("\u6062\u590d\u8fd4\u56de\u4e86\u5a92\u4f53\u6570\u636e\uff0c\u4e0d\u80fd\u4f5c\u4e3a\u538b\u7f29\u7ed3\u679c\u3002")
    return summary.strip()


def _text(value: Any) -> str:
    text = str(value or '')
    return re.sub(r'(data:(?:image|audio|video)/[^;\s]+;base64,)[A-Za-z0-9+/=]+',
                  '[inline media omitted; original paths are listed separately]', text, flags=re.I)


def format_records(records: list[dict]) -> str:
    return '\n\n'.join(
        f"--- record {row.get('id', index)} ---\n"
        f"timestamp: {row.get('timestamp')}\nrole: {row.get('role')}\n"
        f"type: {row.get('msg_type')}\ncontent:\n{_text(row.get('content'))}"
        for index, row in enumerate(records, 1)
    )


def with_legacy_seed(records: list[dict], previous: dict | None) -> list[dict]:
    if not previous or not completed(previous) or previous.get('version', 1) >= 2:
        return records
    if any(metadata_of(row).get('compression_sequence') == previous['sequence']
           and row.get('role') == 'assistant' for row in records):
        return records
    return [{'role': 'assistant', 'msg_type': 'ai_reply', 'timestamp': previous['created_at'],
             'content': previous['summary'], 'metadata': {'compression_sequence': previous['sequence']}},
            *records]


def archive_files(rounds: list[dict], current_records: list[dict], *, system_prompt: str,
                  compression_prompt: str, access_logs: list[dict], storage_root: str | Path) -> dict[str, bytes]:
    logs = '\n\n'.join(
        '\n'.join(f'{key}: {_text(value)}' for key, value in row.items()) for row in access_logs
    ) or '\u6682\u65e0\u62e6\u622a\u8bb0\u5f55\u3002'
    files = {
        '\u62e6\u622a\u8bb0\u5f55.txt': logs.encode('utf-8'),
        '\u63d0\u793a\u8bcd.txt': system_prompt.encode('utf-8'),
        INSTRUCTION_NAME: compression_prompt.encode('utf-8'),
    }

    def source(number: int, records: list[dict]) -> None:
        files[f'{number}a{MEMORY_NAME}.txt'] = format_records(records).encode('utf-8')
        files[f'{number}b{ATTACHMENTS_NAME}.txt'] = _text(json.dumps(
            attachment_index(records, storage_root), ensure_ascii=False, indent=2,
        )).encode('utf-8')

    previous = None
    for entry in rounds:
        number = int(entry['sequence'])
        source(number, with_legacy_seed(entry['source_records'], previous))
        if completed(entry):
            files[f'{number}c{SUMMARY_NAME}.txt'] = _text(entry['summary']).encode('utf-8')
        previous = entry
    source(len(rounds) + 1, with_legacy_seed(current_records, previous))
    return files


def verify_export(bundle: dict) -> None:
    directory = Path(bundle['text_dir'])
    hashes = bundle['file_hashes']
    with zipfile.ZipFile(bundle['archive_path']) as archive:
        if archive.namelist() != list(hashes) or archive.testzip() is not None:
            raise CompressionError('Conversation export verification failed')
        for name, digest in hashes.items():
            if Path(name).name != name:
                raise CompressionError('Invalid export filename')
            raw = (directory / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest or archive.read(name) != raw:
                raise CompressionError(f'Conversation export changed or is damaged: {name}')


def save_conversation_export(root: str | Path, snapshot: dict, *, system_prompt: str,
                             compression_prompt: str, access_logs: list[dict]) -> dict:
    storage_root = Path(root).resolve()
    now = datetime.now().astimezone()
    directory = (storage_root / 'exports' / now.strftime('%Y-%m-%d')
                 / f'{now:%H%M%S}_{uuid.uuid4().hex}')
    directory.mkdir(parents=True, mode=0o700)
    archive_path = directory / EXPORT_NAME
    files = archive_files(snapshot['compressions'], snapshot['records'], system_prompt=system_prompt,
                          compression_prompt=compression_prompt, access_logs=access_logs,
                          storage_root=storage_root)
    for name, data in files.items():
        with (directory / name).open('xb') as handle:
            os.chmod(handle.name, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    temporary = directory / 'context.zip.part'
    with temporary.open('xb') as handle:
        os.chmod(handle.name, 0o600)
        with zipfile.ZipFile(handle, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in files:
                archive.write(directory / name, arcname=name)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, archive_path)
    number = len(snapshot['compressions']) + 1
    bundle = {
        'version': 2, 'archive_path': str(archive_path), 'text_dir': str(directory),
        'memory_path': str(directory / f'{number}a{MEMORY_NAME}.txt'),
        'attachments_path': str(directory / f'{number}b{ATTACHMENTS_NAME}.txt'),
        'instruction_path': str(directory / INSTRUCTION_NAME),
        'instruction': compression_prompt, 'system_prompt': system_prompt,
        'file_hashes': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        'size': archive_path.stat().st_size,
    }
    verify_export(bundle)
    if os.name == 'posix':
        for parent in (directory, directory.parent, directory.parent.parent, storage_root, storage_root.parent):
            descriptor = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    return bundle
