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
import tarfile
import uuid
import zipfile
from contextlib import closing, nullcontext
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from .data_archives import ArchiveReader, ArchiveWriter, FORMATS, estimate_formats, normalize_format
from .data_contents import (CATEGORY_MAP, CATEGORIES, category_for, inventory,
                            manifest_categories, normalize_categories, replacement_paths,
                            PART_CATEGORIES, RECOMMENDED_CATEGORIES, validate_independent_selection)
from .data_parts import DataParts, apply_database, part_summary, read_part, replace_env
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
    "xgent_storage/conversation_objects",
    "xgent_storage/conversation_workspaces",
    "prompts",
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
    allowed = name in {".env", "xgent_memory.db", "path-map.json", "backup-schedule.json"} or name in {
        "backup_parts/" + category + ".jsonl" for category in PART_CATEGORIES
    } or any(
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
        self._parts_inventory = None
        self._parts_signature = None

    def files(self, outputs=False, *, migration=False, categories=None):
        root = self.paths.root
        try:
            selected = None if migration else normalize_categories(categories, outputs)
        except ValueError as exc:
            raise BundleError(str(exc)) from exc
        for name in (".env", "xgent_memory.db", "path-map.json", "backup-schedule.json"):
            if selected is not None and category_for(name) not in selected:
                continue
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
            else tuple(tree for tree in (*TREES, "xgent_storage/command_outputs")
                       if category_for(tree) in selected)
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
                    filtered = filename in SKIP_DIRS or re.search(
                        r"\.log(?:$|\.)", filename, re.IGNORECASE
                    ) or filename.endswith((".pyc", ".tmp", ".pid", ".lock", ".part"))
                    if (
                        (not migration and filtered)
                        or path.is_symlink()
                        or not path.is_file()
                    ):
                        continue
                    yield path.relative_to(root).as_posix(), path

    def estimate(self, outputs=False, *, categories=None, compare_formats=False):
        try:
            selected = normalize_categories(categories, outputs)
        except ValueError as exc:
            raise BundleError(str(exc)) from exc
        entries = list(self.files(categories=[item["id"] for item in CATEGORIES]))
        rows = inventory(entries, selected)
        signatures = []
        for source in (self.paths.env, self.paths.database, self.paths.database.with_name(self.paths.database.name + "-wal")):
            name = source.name
            if source.is_symlink():
                raise BundleError("数据文件不能是符号链接：" + name)
            try:
                signature = source.stat()
                signatures.append((name, signature.st_size, signature.st_mtime_ns))
            except FileNotFoundError:
                signatures.append((name, None))
        if signatures != self._parts_signature:
            self._parts_inventory = {key: DataParts(self.paths.root, self.paths.database).measure(key) for key in PART_CATEGORIES}
            self._parts_signature = signatures
        for row in rows:
            if row["id"] in PART_CATEGORIES:
                part = self._parts_inventory[row["id"]]
                row["size"] += part["size"]
                row["file_count"] += part["file_count"]
                row["files"] += part["files"]
                row.update(item_count=part["item_count"], size_is_estimate=True)
        chosen = [entry for entry in entries if category_for(entry[0]) in selected]
        total = sum(row["size"] for row in rows if row["selected"])
        count = sum(row["file_count"] for row in rows if row["selected"])
        return {
            "file_count": count, "size": total,
            "command_outputs_size": next(row["size"] for row in rows if row["id"] == "command_outputs"),
            "includes_secrets": any(CATEGORY_MAP[key].get("sensitive") for key in selected),
            "include_outputs": "command_outputs" in selected,
            "data_dir": str(self.paths.root), "categories": rows,
            "selected_categories": selected, "recommended_categories": list(RECOMMENDED_CATEGORIES),
            "formats": estimate_formats(chosen, total, count,
                samples=[self._parts_inventory[key]["sample"] for key in selected if key in PART_CATEGORIES]) if compare_formats else [],
            "estimate_note": "基于最多 1 MiB 的实际抽样压缩，时间包含打包、快照与校验估算；结果受数据分布、磁盘和 CPU 影响。",
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
        categories=None,
        archive_format="zip",
        include_prompts=False,
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
                        categories=categories,
                        archive_format=archive_format,
                        include_prompts=include_prompts,
                    )
                except (BundleChanged, FileNotFoundError):
                    if attempt == 2:
                        raise BundleError(
                            "文件持续变化，本次备份未发布；服务继续运行，旧备份保留"
                        ) from None
                    progress(phase="retrying", attempt=attempt + 2)

    def _export_once(
        self, destination=None, *, outputs=False, progress=lambda **state: None,
        overwrite=False, name_prefix="manual", categories=None, archive_format="zip", include_prompts=False,
    ):
        try:
            selected = normalize_categories(categories, outputs)
            archive_format = normalize_format(archive_format)
        except ValueError as exc:
            raise BundleError(str(exc)) from exc
        scoped = categories is not None
        if include_prompts and scoped:
            raise BundleError("分类备份请直接选择提示词类型")
        if scoped:
            try:
                validate_independent_selection(selected)
            except ValueError as exc:
                raise BundleError(str(exc)) from exc
        if "database" in selected and not self.paths.database.is_file():
            raise BundleError("核心数据库不存在，请取消数据库类型或先初始化实例")
        folder = Path(destination).parent if destination else self.paths.backups
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect(folder, True)
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name_prefix):
            raise BundleError("备份名称前缀无效")
        target = Path(destination) if destination else folder / (
            name_prefix + "-" + datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
            + "-" + uuid.uuid4().hex[:8] + FORMATS[archive_format]["extension"])
        if not str(target).lower().endswith(FORMATS[archive_format]["extension"]):
            raise BundleError("备份文件扩展名与压缩格式不一致")
        if ((target.exists() and not overwrite) or target.is_symlink()
                or target.resolve().is_relative_to(self.paths.code.resolve())):
            raise BundleError("备份目标已存在，或位于项目目录内部")
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".part")
        entries = list(self.files(outputs, categories=categories))
        if include_prompts:
            entries += list(self.files(categories=["prompts"]))
            selected.append("prompts")
        total = sum(source.stat().st_size for _, source in entries)
        snapshot_required = "database" in selected or any(key in PART_CATEGORIES for key in selected)
        snapshot_size = self.paths.database.stat().st_size if snapshot_required and self.paths.database.is_file() else 0
        if shutil.disk_usage(folder).free < total + snapshot_size:
            raise BundleError("磁盘空间不足，无法安全创建备份")
        roots = [str(self.paths.root), str(self.paths.code)]
        mapping = self.paths.root / "path-map.json"
        if mapping.is_file():
            roots += json.loads(mapping.read_text(encoding="utf-8")).get("previous_roots", [])
        manifest = {
            "format": "xgent-instance", "version": 2 if scoped else 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "include_command_outputs": "command_outputs" in selected,
            "includes_secrets": any(CATEGORY_MAP[key].get("sensitive") for key in selected),
            "archive_format": archive_format, "previous_roots": sorted(set(roots)), "files": {},
        }
        if scoped:
            manifest["categories"] = selected
        if include_prompts:
            manifest["include_prompts"] = True
        done = 0
        try:
            with tempfile.TemporaryDirectory(prefix=".xgent-snapshot-", dir=folder) as snapshot:
                database = Path(snapshot) / "xgent_memory.db"
                if snapshot_required and self.paths.database.is_file():
                    progress(phase="snapshotting", completed_bytes=0, total_bytes=total)
                    sqlite_snapshot(self.paths.database, database)
                if scoped:
                    entries += DataParts(self.paths.root, database).capture(Path(snapshot) / "backup_parts", selected)
                    total = sum(source.stat().st_size for _, source in entries)
                    if shutil.disk_usage(folder).free < total:
                        raise BundleError("磁盘空间不足，无法安全创建分类备份")
                with ArchiveWriter(temporary, archive_format) as archive:
                    protect(temporary)
                    for name, source in entries:
                        if name == "xgent_memory.db":
                            source = database
                        before = source.stat()
                        mode = 0o700 if before.st_mode & stat.S_IXUSR else 0o600
                        def advance(size):
                            nonlocal done
                            done += size
                            progress(phase="packing", completed_bytes=done,
                                     total_bytes=total, current_file=name)
                        size, digest = archive.write_file(name, source, mode, advance)
                        after = source.stat()
                        if ((before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                                or size != before.st_size):
                            raise BundleChanged("备份期间文件发生变化，请重试：" + name)
                        manifest["files"][name] = {"size": size, "sha256": digest, "mode": mode}
                    archive.write_bytes("manifest.json", json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
            progress(phase="verifying", completed_bytes=0, total_bytes=done)
            self.verify(temporary)
            os.replace(temporary, target)
            protect(target)
            progress(phase="completed", completed_bytes=done, total_bytes=done)
            return {"path": str(target), "size": target.stat().st_size,
                    "file_count": len(entries), "include_outputs": manifest["include_command_outputs"],
                    "includes_secrets": manifest["includes_secrets"], "categories": selected,
                    "archive_format": archive_format, "restore_mode": "categories" if scoped else "full"}
        finally:
            temporary.unlink(missing_ok=True)

    def verify(self, bundle, extract_to=None, *, progress=lambda **state: None):
        try:
            with ArchiveReader(bundle) as archive:
                names = [member.name for member in archive.members]
                if len(names) != len(set(names)) or len(names) != len({name.casefold() for name in names}):
                    raise BundleError("备份含重复文件名")
                manifest = json.loads(archive.read_manifest())
                if (not isinstance(manifest, dict) or manifest.get("format") != "xgent-instance"
                        or type(manifest.get("version")) is not int or manifest["version"] not in (1, 2)):
                    raise BundleError("不支持的实例备份格式")
                files = manifest.get("files")
                if (not isinstance(files, dict) or (manifest["version"] == 1 and "xgent_memory.db" not in files)
                        or set(names) != set(files) | {"manifest.json"}):
                    raise BundleError("备份目录清单不完整")
                selected = manifest_categories(manifest)
                if manifest["version"] == 2:
                    for key in selected:
                        if key in PART_CATEGORIES and "backup_parts/" + key + ".jsonl" not in files:
                            raise BundleError("分类备份缺少对应记录文件：" + key)
                if (type(manifest.get("include_command_outputs")) is not bool
                        or manifest["include_command_outputs"] != ("command_outputs" in selected)):
                    raise BundleError("备份 command_outputs 标记不一致")
                previous = manifest.get("previous_roots", [])
                if not isinstance(previous, list) or any(not isinstance(value, str) for value in previous):
                    raise BundleError("备份路径映射无效")
                if manifest.get("archive_format", archive.format) != archive.format:
                    raise BundleError("备份压缩格式与清单不一致")
                for name, descriptor in files.items():
                    safe_name(name)
                    if category_for(name) not in selected:
                        raise BundleError("备份包含未选中类型的文件：" + name)
                    if not isinstance(descriptor, dict):
                        raise BundleError("文件清单描述无效")
                    info = archive.by_name[name]
                    if not info.regular or stat.S_ISLNK(info.mode):
                        raise BundleError("备份包含链接或特殊文件")
                    if (type(descriptor.get("size")) is not int or descriptor["size"] < 0
                            or info.size != descriptor["size"]):
                        raise BundleError("备份文件大小不匹配：" + name)
                    if not re.fullmatch(r"[a-f0-9]{64}", str(descriptor.get("sha256"))):
                        raise BundleError("文件 SHA-256 描述无效：" + name)
                total = sum(value["size"] for value in files.values())
                if extract_to and shutil.disk_usage(Path(extract_to).parent).free < total:
                    raise BundleError("磁盘空间不足，无法恢复备份")
                done = 0
                for name, descriptor in files.items():
                    sha, size, output = hashlib.sha256(), 0, None
                    if extract_to:
                        target = Path(extract_to) / name
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        output = target.open("xb")
                    try:
                        with archive.open(archive.by_name[name]) as source:
                            for chunk in iter(lambda: source.read(CHUNK), b""):
                                size += len(chunk)
                                if size > descriptor["size"]:
                                    raise BundleError("备份展开大小异常")
                                sha.update(chunk)
                                if output:
                                    output.write(chunk)
                                done += len(chunk)
                                progress(phase="verifying", completed_bytes=done, total_bytes=total, current_file=name)
                        if size != descriptor["size"] or sha.hexdigest() != descriptor["sha256"]:
                            raise BundleError("SHA-256 校验失败：" + name)
                    finally:
                        if output:
                            output.close()
                    if extract_to and os.name == "posix":
                        target.chmod(0o700 if descriptor.get("mode", 0) & 0o100 else 0o600)
                manifest["archive_format"] = archive.format
                return manifest
        except BundleError:
            raise
        except (zipfile.BadZipFile, tarfile.TarError, OSError, KeyError, TypeError,
                ValueError, RuntimeError, EOFError) as exc:
            raise BundleError("备份损坏或清单无效：" + str(exc)) from exc

    def prepare_import(self, bundle, *, progress=lambda **state: None):
        parent = self.paths.root.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".xgent-restore-", dir=parent))
        protect(staging, True)
        try:
            manifest = self.verify(bundle, staging, progress=progress)
            if "xgent_memory.db" in manifest["files"]:
                check_database(staging / "xgent_memory.db")
            for key in PART_CATEGORIES:
                part = staging / "backup_parts" / (key + ".jsonl")
                if part.is_file():
                    part_summary(part, key)
            # Old full-file archives can also expose independent import choices.
            derived = []
            if (staging / "xgent_memory.db").is_file():
                derived = list(PART_CATEGORIES)
            elif (staging / ".env").is_file():
                derived = ["secrets", "ai_config", "app_settings"]
            missing = [key for key in derived if not (staging / "backup_parts" / (key + ".jsonl")).exists()]
            if missing:
                DataParts(staging).capture(staging / "backup_parts", missing)
            write_json(staging / "restore-manifest.json", manifest)
            if manifest["version"] == 2:
                write_json(staging / "restore-plan.json", {"categories": manifest_categories(manifest)})
            old_map = staging / "path-map.json"
            previous = list(manifest.get("previous_roots", []))
            current_map = self.paths.root / "path-map.json"
            if manifest["version"] == 2 and "settings" not in manifest_categories(manifest) and current_map.is_file():
                previous += json.loads(current_map.read_text(encoding="utf-8")).get("previous_roots", [])
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
                    "categories": manifest_categories(manifest),
                    "restore_mode": "categories" if manifest["version"] == 2 else "full",
                    "command_outputs_included": bool(
                        manifest["include_command_outputs"]
                    ),
                },
            )
            return staging, manifest
        except BaseException:
            shutil.rmtree(staging)
            raise

    def activate(self, staging, *, categories=None):
        if self.paths.root.resolve() != self.paths.code.resolve():
            raise BundleError('单目录模式只允许恢复当前项目的数据')
        if categories is not None:
            self.select_import(staging, categories)
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

    @staticmethod
    def _state_path(base, name):
        base = Path(base).resolve()
        target = base / name
        if not target.resolve().is_relative_to(base):
            raise BundleError("数据路径超出允许目录：" + name)
        for candidate in (target, *target.parents):
            if candidate == base:
                break
            if candidate.is_symlink():
                raise BundleError("项目数据不能是符号链接：" + name)
        return target

    def preview(self, manifest):
        selected = manifest_categories(manifest)
        scoped = manifest["version"] == 2
        affected = selected if scoped else [item["id"] for item in CATEGORIES]
        rows = []
        for key in affected:
            spec = CATEGORY_MAP[key]
            files = [{"name": name, "size": info["size"]}
                     for name, info in sorted(manifest["files"].items()) if category_for(name) == key]
            current_size, current_count = 0, 0
            for name in spec["paths"]:
                target = self._state_path(self.paths.root, name)
                if target.is_file():
                    current_size += target.stat().st_size
                    current_count += 1
                elif target.is_dir():
                    for parent, folders, names in os.walk(target, followlinks=False):
                        for child in (*folders, *names):
                            if (Path(parent) / child).is_symlink():
                                raise BundleError("当前数据含符号链接，不能安全覆盖：" + str(Path(parent) / child))
                        for child in names:
                            try:
                                current_size += (Path(parent) / child).stat().st_size
                                current_count += 1
                            except FileNotFoundError:
                                continue
            rows.append({**spec, "included": key in selected, "file_count": len(files),
                         "size": sum(file["size"] for file in files),
                         "files": files[:200], "files_truncated": len(files) > 200,
                         "current_file_count": current_count, "current_size": current_size,
                         "replace_paths": replacement_paths([key])})
        return {"restore_mode": "categories" if scoped else "full",
                "archive_format": manifest.get("archive_format", "zip"), "categories": rows,
                "file_count": len(manifest["files"]),
                "size": sum(file["size"] for file in manifest["files"].values()),
                "includes_secrets": any(CATEGORY_MAP[key].get("sensitive") for key in selected),
                "replace_paths": replacement_paths(selected) if scoped else list(self.PROJECT_STATE),
                "metadata_paths": ["path-map.json", "restore-info.json"]}

    def import_preview(self, staging, manifest):
        """Enumerate every available component, including parts derived from old DBs."""
        staging = Path(staging)
        declared = manifest_categories(manifest)
        base = self.preview(manifest)
        original = {row["id"]: row for row in base["categories"]}
        rows = []
        for spec in CATEGORIES:
            key = spec["id"]
            part = staging / "backup_parts" / (key + ".jsonl")
            files = [{"name": name, "size": info["size"]} for name, info in manifest["files"].items()
                     if category_for(name) == key]
            available = (manifest["version"] == 2 and key in declared) or bool(files)
            summary = None
            if key in PART_CATEGORIES and part.is_file():
                summary = part_summary(part, key)
                available = True
                if not any(file["name"].startswith("backup_parts/") for file in files):
                    files.append({"name": "backup_parts/" + key + ".jsonl", "size": part.stat().st_size, "virtual": True})
            if not available:
                continue
            row = {**spec, "available": True, "included": True,
                   "file_count": len(files), "size": sum(file["size"] for file in files),
                   "files": files[:200], "files_truncated": len(files) > 200,
                   "current_file_count": original.get(key, {}).get("current_file_count", 0),
                   "current_size": original.get(key, {}).get("current_size", 0),
                   "physical_paths": replacement_paths([key]) if key in declared else [],
                   "replace_paths": replacement_paths([key]) if key in declared else []}
            if summary:
                row.update(summary)
                current = DataParts(self.paths.root).measure(key)
                row.update(current_item_count=current["item_count"], current_size=current["size"])
                scope = []
                if summary["has_environment_data"]:
                    scope.append(".env 中属于本分类的变量（其他变量保留）")
                if summary["has_database_data"]:
                    scope.append("数据库中属于本分类的配置或记录（其他数据保留）")
                row["replace_paths"] += scope
            rows.append(row)
        available = {row["id"] for row in rows}
        recommended = [key for key in RECOMMENDED_CATEGORIES if key in available]
        if "command_outputs" in available:
            recommended.append("command_outputs")
        return {**base, "categories": rows, "selectable": True,
                "recommended_categories": recommended, "original_restore_mode": base["restore_mode"],
                "restore_mode": "selection"}

    def select_import(self, staging, categories):
        staging = Path(staging)
        if (staging.is_symlink() or staging.resolve().parent != self.paths.code.resolve().parent
                or not staging.name.startswith(".xgent-restore-")):
            raise BundleError("恢复临时目录无效")
        try:
            selected = validate_independent_selection(categories)
            manifest = json.loads((staging / "restore-manifest.json").read_text(encoding="utf-8"))
            preview = self.import_preview(staging, manifest)
        except (ValueError, KeyError, TypeError) as exc:
            raise BundleError(str(exc)) from exc
        available = {item["id"] for item in preview["categories"]}
        if set(selected) - available:
            raise BundleError("所选部分未包含在压缩包中")
        # No active data changes until the stopped-service coordinator activates it.
        physical_paths = [name for item in preview["categories"] if item["id"] in selected
                          for name in item["physical_paths"]]
        write_json(staging / "restore-plan.json", {"categories": selected, "physical_paths": physical_paths})
        return {**preview, "selected_categories": selected}

    def discard_staging(self, staging):
        staging = Path(staging)
        if (staging.is_symlink() or staging.resolve().parent != self.paths.code.resolve().parent
                or not staging.name.startswith(".xgent-restore-")):
            raise BundleError("恢复临时目录无效")
        if staging.exists():
            shutil.rmtree(staging)

    def _activate_project(self, staging):
        """Replace declared categories; old v1 archives retain full restore."""
        root = self.paths.code.resolve()
        if staging.parent != root.parent or not staging.name.startswith(".xgent-restore-"):
            raise BundleError("恢复临时目录无效")
        plan = staging / "restore-plan.json"
        if plan.is_symlink():
            raise BundleError("恢复计划不能是符号链接")
        try:
            saved_plan = json.loads(plan.read_text(encoding="utf-8")) if plan.is_file() else None
            selected = validate_independent_selection(saved_plan["categories"]) if saved_plan else None
        except (ValueError, KeyError, TypeError) as exc:
            raise BundleError("恢复类型清单无效") from exc
        physical_paths = saved_plan.get("physical_paths", replacement_paths(selected)) if selected else []
        if (not isinstance(physical_paths, list) or any(not isinstance(name, str) for name in physical_paths)
                or selected and set(physical_paths) - set(replacement_paths(selected))):
            raise BundleError("恢复物理路径范围无效")
        names = list(dict.fromkeys(physical_paths + ["path-map.json", "restore-info.json"])) if selected else list(self.PROJECT_STATE)
        if selected is None:
            manifest_path = staging / "restore-manifest.json"
            if manifest_path.is_file() and json.loads(manifest_path.read_text(encoding="utf-8")).get("include_prompts") is True:
                names.append("prompts")
        partial_files = {key: staging / "backup_parts" / (key + ".jsonl")
                         for key in (selected or []) if key in PART_CATEGORIES}
        if partial_files:
            try:
                summaries = {key: part_summary(filename, key) for key, filename in partial_files.items()}
                if any(value["has_database_data"] for value in summaries.values()):
                    self._state_path(root, "xgent_memory.db")
                    if not self.paths.database.is_file():
                        raise BundleError("分类数据库导入需要先初始化目标实例，或选择完整数据库恢复")
                    merged = staging / ".selected-database.db"
                    sqlite_snapshot(self.paths.database, merged)
                    with closing(sqlite3.connect(merged)) as connection:
                        try:
                            apply_database(connection, partial_files)
                            connection.commit()
                        except BaseException:
                            connection.rollback()
                            raise
                    check_database(merged)
                    os.replace(merged, staging / "xgent_memory.db")
                    names += ["xgent_memory.db", "xgent_memory.db-wal", "xgent_memory.db-shm", "xgent_memory.db-journal"]
                env_parts = [key for key, value in summaries.items() if value["has_environment_data"]]
                if env_parts:
                    self._state_path(root, ".env")
                    original_env = self.paths.env.read_text(encoding="utf-8-sig") if self.paths.env.is_file() else ""
                    values = {}
                    for key in env_parts:
                        for record in read_part(partial_files[key], key):
                            if record.get("type") == "env":
                                values[record["key"]] = record["value"]
                    merged_env = replace_env(original_env, set(env_parts), values)
                    (staging / ".env").write_text(merged_env, encoding="utf-8")
                    protect(staging / ".env")
                    names.append(".env")
            except (ValueError, sqlite3.Error) as exc:
                raise BundleError("所选部分无法安全导入：" + str(exc)) from exc
        names = list(dict.fromkeys(names))
        if selected:
            write_json(staging / "restore-info.json", {
                "restored_at": datetime.now(timezone.utc).isoformat(), "categories": selected,
                "restore_mode": "selection", "command_outputs_included": "command_outputs" in selected})
        if selected is None or "database" in selected:
            check_database(staging / "xgent_memory.db")
        for name in names:
            self._state_path(root, name)
            self._state_path(staging, name)
        rollback_root = self._state_path(root, ".install-state/restore-rollback")
        rollback_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect(rollback_root, True)
        rollback = rollback_root / ("restore-rollback-" + uuid.uuid4().hex)
        rollback.mkdir(parents=True, mode=0o700)
        protect(rollback, True)
        installed, moved = [], []
        try:
            for name in names:
                old, new = root / name, staging / name
                if old.exists():
                    saved = rollback / name
                    saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.replace(old, saved)
                    moved.append(name)
                if new.exists():
                    old.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.replace(new, old)
                    installed.append(name)
            if selected is None or "database" in selected:
                check_database(root / "xgent_memory.db")
        except BaseException:
            failed_root = self._state_path(root, ".install-state/restore-failed")
            failed = failed_root / ("restore-failed-" + uuid.uuid4().hex)
            failed.mkdir(parents=True, mode=0o700)
            for name in reversed(installed):
                if (root / name).exists():
                    target = failed / name
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.replace(root / name, target)
            for name in reversed(moved):
                (root / name).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(rollback / name, root / name)
            raise
        finally:
            self.discard_staging(staging)
        return {"data_dir": str(root), "rollback_dir": str(rollback), "in_project": True,
                "installed_state": installed, "moved_state": moved,
                "restore_mode": "categories" if selected else "full", "categories": selected}

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
            self._state_path(root, name)
            if (root / name).exists():
                target = failed / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(root / name, target)
        for name in result['moved_state']:
            self._state_path(root, name)
            (root / name).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(rollback / name, root / name)
        return failed

    def migrate(self, target, *, dry_run=False):
        raise BundleError("已恢复单目录模式，无需迁出；使用导入/导出功能即可")
