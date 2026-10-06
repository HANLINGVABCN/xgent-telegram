"""Bounded, display-only reads of command output archives. Never executes content."""
from __future__ import annotations

import codecs
import html
import re
import os
from pathlib import Path
import stat
from typing import Iterable

OUTPUT_PAGE_BYTES = 64 * 1024
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parent.parent / 'xgent_storage' / 'command_outputs'


class OutputArchiveError(ValueError):
    pass


def read_output_page(path: str, offset: int = 0, *, roots: Iterable[str | Path] | None = None) -> dict:
    """UTF-8 pages with byte cursors; only regular .txt files under allowed roots.

    Pages replace rather than append in the UI. The cursor is independent of
    Unicode character widths and never splits a valid UTF-8 sequence.
    """
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise OutputArchiveError('无效的输出分页位置')
    try:
        target = Path(path).resolve(strict=True)
        allowed = [Path(root).resolve() for root in (roots if roots is not None else [DEFAULT_OUTPUT_ROOT])]
        if target.suffix.lower() != '.txt' or not any(target.is_relative_to(root) for root in allowed):
            raise OutputArchiveError('只能查看命令输出目录中的文本存档')
        if not stat.S_ISREG(target.stat().st_mode):
            raise OutputArchiveError('输出存档不是普通文件')
        # O_NONBLOCK avoids blocking on a file replaced by a FIFO between stat/open.
        fd = os.open(target, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or offset > info.st_size:
                raise OutputArchiveError('存档已变化，请从第一页重新查看')
            handle.seek(offset)
            raw = handle.read(OUTPUT_PAGE_BYTES)
            eof = offset + len(raw) >= info.st_size
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            text = decoder.decode(raw, final=eof)
            pending = len(decoder.getstate()[0])
            next_offset = offset + len(raw) - pending
            return {'text': text, 'offset': offset, 'next_offset': next_offset,
                    'size': info.st_size, 'eof': eof, 'filename': target.name}
    except OutputArchiveError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise OutputArchiveError('输出存档不存在或不可读取') from exc


def attach_archive_path(presentation: str) -> str:
    """Old result cards have a visible path but no attribute; add a display-only link."""
    if 'data-output-path=' in presentation or '<blockquote expandable' not in presentation:
        return presentation
    match = re.search(r'(?:完整输出|输出存档（已截断）):\s*<code>([^<]+)</code>', presentation)
    if not match:
        return presentation
    path = html.unescape(match[1])
    if not path.endswith('.txt'):
        return presentation
    attr = ' data-output-path="' + html.escape(path, quote=True) + '"'
    return presentation.replace('<blockquote expandable', '<blockquote expandable' + attr, 1)
