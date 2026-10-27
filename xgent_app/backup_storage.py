"""Shared backup-directory policy for installer, Web and background jobs."""

from __future__ import annotations

import json
from pathlib import Path


def default_backup_directory(code):
    return Path(code).resolve().parent / "xgent-backups"


def validate_backup_directory(code, value=None):
    code = Path(code).resolve()
    target = Path(value).expanduser() if value else default_backup_directory(code)
    if not target.is_absolute():
        raise ValueError("备份目录必须是服务器上的绝对路径")
    if target.is_symlink():
        raise ValueError("备份目录不能是符号链接")
    target = target.resolve()
    if (
        target == Path(target.anchor)
        or target == Path.home().resolve()
        or target == code
        or target.is_relative_to(code)
        or code.is_relative_to(target)
    ):
        raise ValueError("备份目录必须位于项目目录之外，且不能是磁盘根目录或用户主目录")
    return target


def configured_backup_directory(code):
    code = Path(code).resolve()
    config = code / "backup-schedule.json"
    raw = None
    if config.is_file():
        try:
            value = json.loads(config.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                raw = value.get("backup_directory")
        except (OSError, ValueError):
            pass
    return validate_backup_directory(code, raw)
