"""Display-only grouping of a generated reply across live and restored views."""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import mimetypes
import os
from pathlib import Path


_current = contextvars.ContextVar('web_media_presentation', default=None)


def generated_media_group_id(paths: list[str]) -> str | None:
    if not paths or not all(paths):
        return None
    normalized = [os.path.normcase(str(Path(path).resolve())) for path in paths]
    return 'generated:' + hashlib.sha256(json.dumps(normalized).encode('utf-8')).hexdigest()


def build_media_presentation(text: str, artifacts: list[dict], *,
                             replace_message_ids: list | None = None) -> dict | None:
    media = []
    seen = set()
    for artifact in artifacts:
        if not artifact.get('path'):
            continue
        path = str(Path(artifact['path']).resolve())
        if path in seen:
            continue
        seen.add(path)
        mime = artifact.get('mime_type') or mimetypes.guess_type(path)[0] or 'application/octet-stream'
        kind = 'photo' if mime.startswith('image/') and mime != 'image/svg+xml' else (
            'video' if mime.startswith('video/') else 'audio' if mime.startswith('audio/') else 'file'
        )
        media.append({'path': path, 'filename': Path(path).name, 'mime_type': mime, 'kind': kind})
    if not media:
        return None
    return {'media_group_id': generated_media_group_id([item['path'] for item in media]),
            'text': text, 'media': media,
            'replace_message_ids': [value for value in (replace_message_ids or []) if value is not None]}


@contextlib.contextmanager
def media_presentation_scope(presentation: dict | None):
    state = {'presentation': presentation, 'active': True}
    token = _current.set(state)
    try:
        yield
    finally:
        # Long-lived delivery workers may inherit the context when first started.
        state['active'] = False
        _current.reset(token)


def current_media_presentation() -> dict | None:
    state = _current.get()
    return state['presentation'] if state and state['active'] else None
