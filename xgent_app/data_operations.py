"""Non-blocking Web maintenance jobs; never run a model or Telegram sync."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .backup_schedule import configure as configure_backup_schedule
from .backup_schedule import read_settings as read_backup_settings
from .backup_storage import default_backup_directory
from .data_bundles import BundleError, DataBundles, protect, write_json
from .data_lock import DataLease
from .data_archives import ArchiveReader, archive_format as filename_format, normalize_format
from .data_contents import BACKUP_GROUPS, CATEGORY_MAP, RECOMMENDED_CATEGORIES, manifest_categories, normalize_categories, selected_groups, validate_independent_selection


@dataclass(frozen=True)
class BackupDownload:
    path: Path


class DataOperations:
    def __init__(self, namespace):
        self.ns = namespace
        self.paths = namespace["DATA_PATHS"]
        self.bundles = DataBundles(self.paths)
        self.directory = self.paths.root / '.install-state/data-jobs'
        self.directory.mkdir(mode=0o700, exist_ok=True, parents=True)
        protect(self.directory, True)
        self.jobs = {}
        self.tasks = set()
        self.mutex = threading.Lock()
        self.catalog_cache = {}

    def job_path(self, identifier):
        if not re.fullmatch(r"[a-f0-9]{32}", str(identifier)):
            raise BundleError("无效的数据任务 ID")
        return self.directory / (identifier + ".json")

    def status(self, identifier):
        path = self.job_path(identifier)
        with self.mutex:
            in_memory = dict(self.jobs[identifier]) if identifier in self.jobs else None
        # A detached restore coordinator becomes the writer after activation;
        # do not mask its completed/rolled-back status with the old Web cache.
        if in_memory and in_memory.get("state") != "activating":
            return in_memory
        if path.is_file():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved["state"] == "running" and saved.get("pid") != os.getpid():
                saved.update(
                    state="failed",
                    error="任务所属进程已退出；活动数据未被修改，可重新操作。",
                )
            return saved
        raise BundleError("数据任务不存在")

    def update(self, identifier, **changes):
        with self.mutex:
            self.jobs[identifier].update(changes)
            saved = dict(self.jobs[identifier])
        write_json(self.job_path(identifier), saved)
        return saved

    def create(self, kind, request_id):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", str(request_id)):
            raise BundleError("缺少幂等请求 ID")
        for path in self.directory.glob("*.json"):
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("request_id") == request_id:
                if saved.get("kind") != kind:
                    raise BundleError("请求 ID 已用于另一操作")
                return saved, False
        identifier = uuid.uuid4().hex
        saved = {
            "id": identifier,
            "kind": kind,
            "request_id": request_id,
            "pid": os.getpid(),
            "state": "running",
            "phase": "pending",
            "created_at": time.time(),
        }
        with self.mutex:
            self.jobs[identifier] = saved
        self.update(identifier)
        return saved, True

    def start(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    @staticmethod
    def request_categories(data):
        value = data.get("categories")
        if isinstance(value, str):
            value = value.split(",") if value else []
        if value is not None:
            try:
                return normalize_categories(value)
            except ValueError as exc:
                raise BundleError(str(exc)) from exc
        return None

    def estimate(self, data):
        return self.bundles.estimate(
            data.get("include_command_outputs") in (True, "true", "1"),
            categories=self.request_categories(data), compare_formats=True)

    async def export(self, data):
        categories = self.request_categories(data)
        selected = normalize_categories(categories, bool(data.get("include_command_outputs")))
        format = normalize_format(data.get("archive_format", "zip"))
        if categories is not None:
            validate_independent_selection(selected)
        if any(CATEGORY_MAP[key].get("sensitive") for key in selected) and data.get("confirm_secrets") is not True:
            raise BundleError("备份可能包含 .env 原文及 Bot/API 密钥，请明确确认。")
        saved, created = self.create("export", data.get("request_id"))
        if created:
            self.start(self._export(saved["id"], "command_outputs" in selected, categories, format))
        return {"ok": True, **saved}

    def backup_settings(self):
        settings = read_backup_settings(self.paths.code)
        if not (self.paths.code / "backup-schedule.json").is_file():
            settings["categories"] = list(RECOMMENDED_CATEGORIES)
        return {
            "ok": True,
            **settings,
            "selected_groups": selected_groups(settings["categories"]),
            "default_backup_directory": str(
                default_backup_directory(self.paths.code).resolve()
            ),
        }

    def save_backup_settings(self, data):
        categories = self.request_categories(data)
        if categories is not None:
            validate_independent_selection(categories)
        settings = configure_backup_schedule(
            self.paths.code,
            enabled=data.get("enabled") is True,
            frequency=str(data.get("frequency") or "1d"),
            mode=str(data.get("mode") or "accumulate"),
            outputs=data.get("include_command_outputs") is True,
            max_backups=int(data.get("max_backups") or 7),
            backup_directory=data.get("backup_directory"),
            categories=categories,
            archive_format=data.get("archive_format"),
        )
        return {"ok": True, **self.backup_settings(), "saved": settings}

    def _backup_file(self, name):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}\.(?:zip|tar\.gz|tar\.xz)", str(name), re.IGNORECASE):
            raise BundleError("备份文件名无效")
        directory = self.paths.backups.resolve()
        candidate = directory / str(name)
        path = candidate.resolve()
        if path.parent != directory or candidate.is_symlink() or not path.is_file():
            raise BundleError("备份文件不存在")
        return path

    @staticmethod
    def _backup_kind(name):
        if name.startswith("scheduled-latest."):
            return "scheduled_overwrite", "定时备份（覆盖）"
        if name.startswith("scheduled-"):
            return "scheduled", "定时备份（累计）"
        if name.startswith("pre-import-target-"):
            return "pre_import_target", "导入前目标备份"
        if name.startswith("pre-import-"):
            return "pre_import", "导入前自动备份"
        if name.startswith("manual-"):
            return "manual", "手动备份"
        return "legacy", "兼容备份"

    def list_backups(self):
        directory = self.paths.backups
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect(directory, True)
        items = []
        for path in directory.iterdir():
            if not filename_format(path.name) or path.is_symlink() or not path.is_file():
                continue
            stat_result = path.stat()
            kind, label = self._backup_kind(path.name)
            item = {
                "name": path.name,
                "path": str(path.resolve()),
                "size": stat_result.st_size,
                "created_at": stat_result.st_mtime,
                "kind": kind,
                "label": label,
                "valid_manifest": False,
                "include_command_outputs": None,
                "file_count": None,
                "archive_format": filename_format(path.name),
                "categories": [],
            }
            try:
                cache_key = (str(path), stat_result.st_size, stat_result.st_mtime_ns)
                summary = self.catalog_cache.get(cache_key)
                if summary is None:
                    with ArchiveReader(path) as archive:
                        manifest = json.loads(archive.read_manifest())
                    selected = manifest_categories(manifest)
                    summary = {"valid_manifest": manifest.get("format") == "xgent-instance" and manifest.get("version") in (1, 2),
                               "include_command_outputs": bool(manifest.get("include_command_outputs")),
                               "file_count": len(manifest.get("files") or {}),
                               "manifest_created_at": manifest.get("created_at"),
                               "categories": [{"id": group["id"], "label": group["label"]} for group in BACKUP_GROUPS
                                              if group["id"] in selected_groups(selected)],
                               "restore_mode": "categories" if manifest.get("version") == 2 else "full"}
                    self.catalog_cache[cache_key] = summary
                item.update(summary)
            except (OSError, ValueError, KeyError, TypeError, EOFError):
                pass
            items.append(item)
        items.sort(key=lambda item: (item["created_at"], item["name"]), reverse=True)
        return {
            "ok": True,
            "directory": str(directory.resolve()),
            "items": items,
        }

    def delete_backups(self, data):
        names = data.get("names")
        if data.get("confirm") is not True or not isinstance(names, list) or not names:
            raise BundleError("请选择备份并确认删除")
        if len(names) > 200 or len(set(map(str, names))) != len(names):
            raise BundleError("备份删除列表无效")
        paths = [self._backup_file(name) for name in names]
        deleted = []
        with DataLease(self.paths.root / "backup-export", exclusive=True):
            for path in paths:
                path.unlink()
                deleted.append(path.name)
        return {"ok": True, "deleted": deleted, **self.list_backups()}

    async def _export(self, identifier, outputs, categories=None, archive_format="zip"):
        try:
            last_write = [0.0]

            def progress(**changes):
                if (
                    time.monotonic() - last_write[0] > 0.3
                    or changes.get("phase") == "completed"
                ):
                    self.update(identifier, **changes)
                    last_write[0] = time.monotonic()

            operation = asyncio.create_task(asyncio.to_thread(
                self.bundles.export, outputs=outputs, progress=progress,
                categories=categories, archive_format=archive_format))
            try:
                result = await asyncio.shield(operation)
            except asyncio.CancelledError:
                try:
                    await operation
                except Exception as exc:  # noqa: BLE001 - cancellation still wins
                    logging.getLogger(__name__).warning(
                        "停止 Web 备份任务时，后台写入失败：%s", exc
                    )
                raise
            self.update(
                identifier,
                state="completed",
                result=result,
                download_url="/api/workbench/data/download?id=" + identifier,
            )
        except Exception as exc:  # noqa: BLE001 - detached job must persist all failures
            self.update(identifier, state="failed", error=str(exc), phase="failed")

    async def upload_import(self, data):
        saved, created = self.create("import", data.get("request_id"))
        if created:
            source = Path(data["_bundle_path"])
            self.start(
                self._prepare_import(
                    saved["id"], source, data["_temporary"], source_name=data.get("_source_name") or source.name
                )
            )
        else:
            data["_temporary"].cleanup()
        return {"ok": True, **saved}

    async def import_stored(self, data):
        source = self._backup_file(data.get("name"))
        saved, created = self.create("import", data.get("request_id"))
        if created:
            self.start(
                self._prepare_import(
                    saved["id"], source, None, source_name=source.name
                )
            )
        return {"ok": True, **saved}

    async def _prepare_import(self, identifier, bundle, temporary, source_name=None):
        staging = None
        try:
            self.update(identifier, phase="verifying", source_name=source_name or Path(bundle).name)
            last_write = [0.0]
            def progress(**changes):
                if time.monotonic() - last_write[0] > 0.3:
                    self.update(identifier, **changes)
                    last_write[0] = time.monotonic()
            staging, manifest = await asyncio.to_thread(self.bundles.prepare_import, bundle, progress=progress)
            if self.status(identifier).get("cancelled"):
                await asyncio.to_thread(self.bundles.discard_staging, staging)
                return
            preview = await asyncio.to_thread(self.bundles.import_preview, staging, manifest)
            if self.status(identifier).get("cancelled"):
                await asyncio.to_thread(self.bundles.discard_staging, staging)
                return
            self.update(identifier, state="ready", phase="verified", staging=str(staging),
                        include_outputs=manifest["include_command_outputs"], file_count=len(manifest["files"]),
                        source_name=source_name or Path(bundle).name, preview=preview,
                        completed_bytes=sum(info["size"] for info in manifest["files"].values()),
                        total_bytes=sum(info["size"] for info in manifest["files"].values()), current_file=None)
        except Exception as exc:  # noqa: BLE001 - persist failures without touching active data
            if staging is not None:
                await asyncio.to_thread(self.bundles.discard_staging, staging)
            self.update(identifier, state="failed", phase="failed", error=str(exc))
        finally:
            if temporary is not None:
                temporary.cleanup()

    async def cancel_import(self, data):
        saved = self.status(data.get("id"))
        if saved["kind"] != "import" or saved["state"] not in {"running", "ready", "cancelled", "failed"}:
            raise BundleError("此任务不能取消扫描")
        with self.mutex:
            self.jobs.setdefault(saved["id"], saved)
        self.update(saved["id"], state="cancelled", phase="cancelled", cancelled=True)
        if saved.get("staging"):
            await asyncio.to_thread(self.bundles.discard_staging, saved["staging"])
        return {"ok": True, "id": saved["id"], "state": "cancelled"}

    def activate(self, data):
        saved = self.status(data.get("id"))
        if (
            data.get("confirm_replace") is not True
            or saved["kind"] != "import"
            or saved["state"] != "ready"
        ):
            raise BundleError("必须先完整校验并确认覆盖恢复。")
        installer = self.paths.code / "install.sh"
        if os.name != "posix" or not installer.is_file():
            raise BundleError(
                "此启动方式无法安全自动重启；请停止服务后使用 xgent data import 完成恢复。"
            )
        selected = self.request_categories(data)
        if selected is not None:
            self.bundles.select_import(saved["staging"], selected)
        with self.mutex:
            self.jobs.setdefault(saved["id"], saved)
        self.update(saved["id"], state="activating", phase="restarting")
        options = {
            "start_new_session": True,
            "cwd": str(self.paths.code),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "xgent_app.data_restore",
                str(self.job_path(saved["id"])),
                str(self.paths.code),
                str(self.paths.root),
            ],
            **options,
        )
        return {"ok": True, "id": saved["id"], "state": "activating", "reconnect": True}

    def download(self, identifier):
        saved = self.status(identifier)
        if saved["kind"] != "export" or saved["state"] != "completed":
            raise BundleError("备份尚未完成")
        path = Path(saved["result"]["path"]).resolve()
        if (
            path.parent != self.paths.backups.resolve()
            or not path.is_file()
            or path.is_symlink()
        ):
            raise BundleError("备份文件已不存在")
        return BackupDownload(path)

    def download_backup(self, name):
        return BackupDownload(self._backup_file(name))
