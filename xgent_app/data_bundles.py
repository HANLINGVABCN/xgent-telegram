"""Verified streaming backups and in-project state restoration.

This module deliberately has no Telegram/model dependencies. CLI and Web call
the same service. Archives contain state only, and failed validation never
touches the active checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import uuid
import zipfile
from contextlib import closing, nullcontext
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from .data_lock import DataLease
from .data_paths import DataPaths

CHUNK = 1024 * 1024
TREES = (
    "memory",
    "skill-private",
    "server_assets",
    "workspace",
    "xgent_storage/uploads",
    "xgent_storage/generated_media",
    "xgent_storage/exports",
    "xgent_storage/attachment_versions",
)
SKIP_DIRS = {
    "venv",
    ".venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    ".git",
    "build",
    "dist",
    "logs",
}


class BundleError(ValueError):
    pass


class BundleChanged(BundleError):
    """A live file changed; retry a fresh snapshot instead of publishing it."""


def protect(path, directory=False):
    """Unix modes or inheritable owner-only Windows ACL, never chmod-only on NT."""
    path = Path(path)
    if os.name == "nt":
        user = subprocess.check_output(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        import csv

        sid = next(csv.reader([user.strip()]))[-1]
        grant = "(OI)(CI)F" if directory else "F"
        subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:{grant}",
                "*S-1-5-18:" + grant,
            ],
            check=True,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        path.chmod(0o700 if directory else 0o600)


def digest_file(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        protect(temporary)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def check_database(path):
    try:
        with closing(
            sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        ) as conn:
            integrity = [
                str(row[0])
                for row in conn.execute("PRAGMA integrity_check").fetchall()
            ]
            if integrity != ["ok"]:
                details = "；".join(integrity[:5])
                if len(integrity) > 5:
                    details += f"；另有 {len(integrity) - 5} 项"
                raise BundleError("SQLite 完整性检查失败：" + details)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not {"global_messages", "config", "chat_sessions"}.issubset(tables):
                raise BundleError("不是完整的 XGent 数据库")
            version = conn.execute(
                "SELECT value FROM config WHERE key='conversation_schema_version'"
            ).fetchone()
            from .conversations import SCHEMA_VERSION

            if version and int(json.loads(version[0])) > SCHEMA_VERSION:
                raise BundleError("备份数据库版本较新，请先升级 XGent")
    except sqlite3.Error as exc:
        raise BundleError("数据库损坏或无法读取") from exc


def sqlite_snapshot(source, target):
    with (
        closing(
            sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)
        ) as incoming,
        closing(sqlite3.connect(target)) as outgoing,
    ):
        # Incremental SQLite backup yields between page batches. WAL writers
        # continue while the separate reader produces a consistent database.
        incoming.backup(outgoing, pages=256, sleep=0.01)
    protect(target)
    check_database(target)


def safe_name(name):
    posix = PurePosixPath(name)
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or "\x00" in name
        or ":" in name
        or posix.is_absolute()
        or PureWindowsPath(name).drive
        or any(
            part in {"", ".", ".."} or part.endswith((" ", "."))
            for part in name.split("/")
        )
    ):
        raise BundleError("备份包含不安全路径")
    allowed = name in {".env", "xgent_memory.db", "path-map.json", "backup-schedule.json"} or any(
        name.startswith(root + "/")
        for root in (*TREES, "xgent_storage/command_outputs")
    )
    if not allowed:
        raise BundleError("备份包含白名单以外的文件：" + name)
    if any(part in SKIP_DIRS for part in posix.parts) or re.search(
        r"\.log(?:$|\.)", posix.name, re.IGNORECASE
    ):
        raise BundleError("备份包含日志或缓存：" + name)
    return posix


class DataBundles:
    def __init__(self, paths: DataPaths):
        self.paths = paths

    def files(self, outputs=False, *, migration=False):
        root = self.paths.root
        for name in (".env", "xgent_memory.db", "path-map.json", "backup-schedule.json"):
            path = root / name
            if path.is_symlink():
                raise BundleError("数据文件不能是符号链接：" + name)
            if path.is_file():
                yield name, path
        trees = (
            (
                "memory",
                "skill-private",
                "server_assets",
                "workspace",
                "xgent_storage",
                ".install-state",
            )
            if migration
            else (*TREES, *(("xgent_storage/command_outputs",) if outputs else ()))
        )
        for tree in trees:
            folder = root / tree
            if folder.is_symlink():
                raise BundleError("数据目录不能是符号链接：" + tree)
            if not folder.exists():
                continue
            for parent, directories, filenames in os.walk(folder, followlinks=False):
                for directory in directories:
                    if (Path(parent) / directory).is_symlink():
                        raise BundleError(
                            "数据树包含符号链接，未静默漏备份，请先处理："
                            + str(Path(parent) / directory)
                        )
                directories[:] = sorted(
                    d
                    for d in directories
                    if (migration or d not in SKIP_DIRS)
                    and not (Path(parent) / d).is_symlink()
                )
                for filename in sorted(filenames):
                    path = Path(parent) / filename
                    if path.is_symlink():
                        raise BundleError(
                            "数据文件是符号链接，未静默漏备份：" + str(path)
                        )
                    filtered = re.search(
                        r"\.log(?:$|\.)", filename, re.IGNORECASE
                    ) or filename.endswith((".pyc", ".tmp", ".pid", ".lock", ".part"))
                    if (
                        (not migration and filtered)
                        or path.is_symlink()
                        or not path.is_file()
                    ):
                        continue
                    yield path.relative_to(root).as_posix(), path

    def estimate(self, outputs=False):
        items = list(self.files(outputs))
        return {
            "file_count": len(items),
            "size": sum(path.stat().st_size for _, path in items),
            "command_outputs_size": sum(
                path.stat().st_size
                for name, path in self.files(True)
                if name.startswith("xgent_storage/command_outputs/")
            ),
            "includes_secrets": True,
            "include_outputs": bool(outputs),
            "data_dir": str(self.paths.root),
        }

    def export(
        self,
        destination=None,
        *,
        outputs=False,
        progress=lambda **state: None,
        overwrite=False,
        name_prefix="manual",
        _maintenance_locked=False,
    ):
        # Shared with the live application's lease, exclusive only against
        # another export. Never acquire the AI turn lock or database writer.
        with (
            nullcontext()
            if _maintenance_locked
            else DataLease(self.paths.root)
        ), DataLease(self.paths.root / "backup-export", exclusive=True):
            for attempt in range(3):
                try:
                    return self._export_once(
                        destination,
                        outputs=outputs,
                        progress=progress,
                        overwrite=overwrite,
                        name_prefix=name_prefix,
                    )
                except (BundleChanged, FileNotFoundError):
                    if attempt == 2:
                        raise BundleError(
                            "文件持续变化，本次备份未发布；服务继续运行，旧备份保留"
                        ) from None
                    progress(phase="retrying", attempt=attempt + 2)

    def _export_once(
        self,
        destination=None,
        *,
        outputs=False,
        progress=lambda **state: None,
        overwrite=False,
        name_prefix="manual",
    ):
        if not self.paths.database.is_file():
            raise BundleError("核心数据库不存在，不能创建空备份")
        folder = (
            Path(destination).parent
            if destination
            else self.paths.backups
        )
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect(folder, True)
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name_prefix):
            raise BundleError("备份名称前缀无效")
        target = (
            Path(destination)
            if destination
            else folder
            / (
                name_prefix
                + "-"
                + datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
                + "-"
                + uuid.uuid4().hex[:8]
                + ".zip"
            )
        )
        resolved_target = target.resolve()
        if (
            (target.exists() and not overwrite)
            or target.is_symlink()
            or resolved_target.is_relative_to(self.paths.code.resolve())
        ):
            raise BundleError("备份目标已存在，或位于项目目录内部")
        temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.part')
        entries = list(self.files(outputs))
        total = sum(path.stat().st_size for _, path in entries)
        if shutil.disk_usage(folder).free < total + self.paths.database.stat().st_size:
            raise BundleError("磁盘空间不足，无法安全创建备份")
        roots = [str(self.paths.root), str(self.paths.code)]
        mapping = self.paths.root / "path-map.json"
        if mapping.is_file():
            roots += json.loads(mapping.read_text(encoding="utf-8")).get(
                "previous_roots", []
            )
        manifest = {
            "format": "xgent-instance",
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "include_command_outputs": bool(outputs),
            "includes_secrets": True,
            "previous_roots": sorted(set(roots)),
            "files": {},
        }
        try:
            with tempfile.TemporaryDirectory(
                prefix=".xgent-snapshot-", dir=folder
            ) as snapshot:
                database = Path(snapshot) / "xgent_memory.db"
                sqlite_snapshot(self.paths.database, database)
                with zipfile.ZipFile(
                    temporary, "x", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True
                ) as archive:
                    protect(temporary)
                    done = 0
                    for name, source in entries:
                        if name == "xgent_memory.db":
                            source = database
                        before = source.stat()
                        info = zipfile.ZipInfo(name)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        mode = 0o700 if before.st_mode & stat.S_IXUSR else 0o600
                        info.external_attr = (stat.S_IFREG | mode) << 16
                        sha = hashlib.sha256()
                        size = 0
                        with (
                            source.open("rb") as incoming,
                            archive.open(info, "w", force_zip64=True) as outgoing,
                        ):
                            for chunk in iter(lambda: incoming.read(CHUNK), b""):
                                outgoing.write(chunk)
                                sha.update(chunk)
                                size += len(chunk)
                                done += len(chunk)
                                progress(
                                    phase="packing",
                                    completed_bytes=done,
                                    total_bytes=total,
                                    current_file=name,
                                )
                        after = source.stat()
                        if (before.st_size, before.st_mtime_ns) != (
                            after.st_size,
                            after.st_mtime_ns,
                        ) or size != before.st_size:
                            raise BundleChanged("备份期间文件发生变化，请重试：" + name)
                        manifest["files"][name] = {
                            "size": size,
                            "sha256": sha.hexdigest(),
                            "mode": mode,
                        }
                    archive.writestr(
                        "manifest.json", json.dumps(manifest, ensure_ascii=False)
                    )
            self.verify(temporary)
            os.replace(temporary, target)
            protect(target)
            progress(phase="completed", completed_bytes=done, total_bytes=done)
            return {
                "path": str(target),
                "size": target.stat().st_size,
                "file_count": len(entries),
                "include_outputs": bool(outputs),
                "includes_secrets": manifest["includes_secrets"],
            }
        finally:
            temporary.unlink(missing_ok=True)

    def verify(self, bundle, extract_to=None):
        try:
            with zipfile.ZipFile(bundle) as archive:
                names = archive.namelist()
                if len(names) != len(set(names)) or len(names) != len(
                    {name.casefold() for name in names}
                ):
                    raise BundleError("备份含重复文件名")
                if archive.getinfo("manifest.json").file_size > 16 * 1024 * 1024:
                    raise BundleError("备份清单过大，无法安全校验")
                manifest = json.loads(archive.read("manifest.json"))
                if (
                    manifest.get("format") != "xgent-instance"
                    or manifest.get("version") != 1
                ):
                    raise BundleError("不支持的实例备份格式")
                files = manifest.get("files")
                if (
                    not isinstance(files, dict)
                    or "xgent_memory.db" not in files
                    or set(names) != set(files) | {"manifest.json"}
                ):
                    raise BundleError("备份目录清单不完整")
                for name, descriptor in files.items():
                    safe_name(name)
                    if not isinstance(descriptor, dict):
                        raise BundleError("文件清单描述无效")
                    info = archive.getinfo(name)
                    mode = info.external_attr >> 16
                    if (
                        info.is_dir()
                        or stat.S_ISLNK(mode)
                        or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                    ):
                        raise BundleError("备份包含链接或特殊文件")
                    if (
                        type(descriptor.get("size")) is not int
                        or descriptor["size"] < 0
                        or info.file_size != descriptor["size"]
                    ):
                        raise BundleError("备份文件大小不匹配：" + name)
                    if not manifest.get("include_command_outputs") and name.startswith(
                        "xgent_storage/command_outputs/"
                    ):
                        raise BundleError("备份 command_outputs 标记不一致")
                if extract_to and shutil.disk_usage(Path(extract_to).parent).free < sum(
                    v["size"] for v in files.values()
                ):
                    raise BundleError("磁盘空间不足，无法恢复备份")
                for name, descriptor in files.items():
                    sha = hashlib.sha256()
                    size = 0
                    output = None
                    if extract_to:
                        target = Path(extract_to) / name
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        output = target.open("xb")
                    try:
                        with archive.open(name) as source:
                            for chunk in iter(lambda: source.read(CHUNK), b""):
                                size += len(chunk)
                                if size > descriptor["size"]:
                                    raise BundleError("备份展开大小异常")
                                sha.update(chunk)
                                if output:
                                    output.write(chunk)
                        if size != descriptor[
                            "size"
                        ] or sha.hexdigest() != descriptor.get("sha256"):
                            raise BundleError("SHA-256 校验失败：" + name)
                    finally:
                        if output:
                            output.close()
                    if extract_to and os.name == "posix":
                        target.chmod(
                            0o700 if descriptor.get("mode", 0) & 0o100 else 0o600
                        )
                return manifest
        except (
            zipfile.BadZipFile,
            KeyError,
            TypeError,
            json.JSONDecodeError,
            RuntimeError,
        ) as exc:
            raise BundleError("备份损坏或清单无效") from exc

    def prepare_import(self, bundle):
        parent = self.paths.root.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".xgent-restore-", dir=parent))
        protect(staging, True)
        try:
            manifest = self.verify(bundle, staging)
            check_database(staging / "xgent_memory.db")
            old_map = staging / "path-map.json"
            previous = manifest.get("previous_roots", [])
            if old_map.is_file():
                previous += json.loads(old_map.read_text(encoding="utf-8")).get(
                    "previous_roots", []
                )
            write_json(old_map, {"previous_roots": sorted(set(previous))})
            for name in (".env", "xgent_memory.db"):
                if (staging / name).exists():
                    protect(staging / name)
            assets = staging / "server_assets"
            if assets.exists():
                for path in assets.rglob("*"):
                    if path.is_file():
                        protect(path)
            write_json(
                staging / "restore-info.json",
                {
                    "restored_at": datetime.now(timezone.utc).isoformat(),
                    "command_outputs_included": bool(
                        manifest["include_command_outputs"]
                    ),
                },
            )
            return staging, manifest
        except BaseException:
            shutil.rmtree(staging)
            raise

    def activate(self, staging):
        if self.paths.root.resolve() != self.paths.code.resolve():
            raise BundleError('单目录模式只允许恢复当前项目的数据')
        return self._activate_project(Path(staging).resolve())

    PROJECT_STATE = (
        ".env",
        "xgent_memory.db",
        "bot_memory.db",
        "memory",
        "skill-private",
        "workspace",
        "server_assets",
        "xgent_storage",
        "bot_storage",
        "path-map.json",
        "restore-info.json",
        "backup-schedule.json",
        *(
            name + suffix
            for name in ("xgent_memory.db", "bot_memory.db")
            for suffix in ("-wal", "-shm", "-journal")
        ),
    )

    def _activate_project(self, staging):
        """Replace whitelisted state in place; never move code/public skills."""
        root = self.paths.code.resolve()
        if staging.parent != root.parent or not staging.name.startswith(
            ".xgent-restore-"
        ):
            raise BundleError("恢复临时目录无效")
        check_database(staging / "xgent_memory.db")
        for name in self.PROJECT_STATE:
            if (root / name).is_symlink():
                raise BundleError("项目数据不能是符号链接：" + name)
        rollback_root = root / ".install-state" / "restore-rollback"
        rollback_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect(rollback_root, True)
        rollback = rollback_root / ("restore-rollback-" + uuid.uuid4().hex)
        rollback.mkdir(parents=True, mode=0o700)
        protect(rollback, True)
        installed, moved = [], []
        try:
            for name in self.PROJECT_STATE:
                old, new = root / name, staging / name
                if old.exists():
                    os.replace(old, rollback / name)
                    moved.append(name)
                if new.exists():
                    os.replace(new, old)
                    installed.append(name)
            check_database(root / 'xgent_memory.db')
        except BaseException:
            failed_root = root / ".install-state" / "restore-failed"
            failed_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            failed = failed_root / ("restore-failed-" + uuid.uuid4().hex)
            failed.mkdir(mode=0o700)
            for name in reversed(installed):
                if (root / name).exists():
                    os.replace(root / name, failed / name)
            for name in reversed(moved):
                os.replace(rollback / name, root / name)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return {
            "data_dir": str(root),
            "rollback_dir": str(rollback),
            "in_project": True,
            "installed_state": installed,
            "moved_state": moved,
        }

    def rollback_activation(self, result):
        if not result.get('in_project'):
            raise BundleError("此回退只支持项目内数据")
        root = self.paths.code.resolve()
        rollback = Path(result['rollback_dir']).resolve()
        rollback_root = (root / ".install-state" / "restore-rollback").resolve()
        if rollback.parent != rollback_root or not rollback.name.startswith(
            "restore-rollback-"
        ):
            raise BundleError("数据回退目录无效")
        failed_root = root / ".install-state" / "restore-failed"
        failed_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        failed = failed_root / ("restore-failed-" + uuid.uuid4().hex)
        failed.mkdir(mode=0o700)
        for name in result['installed_state']:
            if (root / name).exists():
                os.replace(root / name, failed / name)
        for name in result['moved_state']:
            os.replace(rollback / name, root / name)
        return failed

    def migrate(self, target, *, dry_run=False):
        raise BundleError("已恢复单目录模式，无需迁出；使用导入/导出功能即可")
