"""Non-blocking Web maintenance jobs; never run a model or Telegram sync."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .data_bundles import BundleError, DataBundles, protect, write_json


@dataclass(frozen=True)
class BackupDownload:
    path: Path


class DataOperations:
    def __init__(self, namespace):
        self.ns = namespace
        self.paths = namespace["DATA_PATHS"]
        self.bundles = DataBundles(self.paths)
        self.directory = self.paths.root.parent / (
            ".xgent-jobs-" + self.paths.root.name
        )
        self.directory.mkdir(mode=0o700, exist_ok=True)
        protect(self.directory, True)
        self.jobs = {}
        self.tasks = set()
        self.mutex = threading.Lock()

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

    async def export(self, data):
        if data.get("confirm_secrets") is not True:
            raise BundleError("备份包含 .env 原文及 Bot/API 密钥，请明确确认。")
        saved, created = self.create("export", data.get("request_id"))
        if created:
            self.start(
                self._export(saved["id"], bool(data.get("include_command_outputs")))
            )
        return {"ok": True, **saved}

    async def _export(self, identifier, outputs):
        manager = self.ns["get_conversations"]()
        acquired = False
        try:
            # Lock admissions, not Telegram delivery. Already running commands
            # are never killed for a backup; the caller may retry when idle.
            acquired = manager.lock.acquire()
            if not acquired:
                raise BundleError("有对话/命令正在运行，请结束后再创建备份。")
            db = await self.ns["BotMemoryDB"].get_instance()
            last_write = [0.0]

            def progress(**changes):
                if (
                    time.monotonic() - last_write[0] > 0.3
                    or changes.get("phase") == "completed"
                ):
                    self.update(identifier, **changes)
                    last_write[0] = time.monotonic()

            async with db._write_lock:
                result = await asyncio.to_thread(
                    self.bundles.export, outputs=outputs, progress=progress
                )
            self.update(
                identifier,
                state="completed",
                result=result,
                download_url="/api/workbench/data/download?id=" + identifier,
            )
        except Exception as exc:  # noqa: BLE001 - detached job must persist all failures
            self.update(identifier, state="failed", error=str(exc), phase="failed")
        finally:
            if acquired:
                manager.lock.release()

    async def upload_import(self, data):
        if data.get("confirm_secrets") is not True:
            raise BundleError("请确认备份包含敏感配置并按整套恢复。")
        saved, created = self.create("import", data.get("request_id"))
        if created:
            source = Path(data["_bundle_path"])
            self.start(self._prepare_import(saved["id"], source, data["_temporary"]))
        else:
            data["_temporary"].cleanup()
        return {"ok": True, **saved}

    async def _prepare_import(self, identifier, bundle, temporary):
        try:
            self.update(identifier, phase="verifying")
            staging, manifest = await asyncio.to_thread(
                self.bundles.prepare_import, bundle
            )
            self.update(
                identifier,
                state="ready",
                phase="verified",
                staging=str(staging),
                include_outputs=manifest["include_command_outputs"],
                file_count=len(manifest["files"]),
            )
        except Exception as exc:  # noqa: BLE001 - detached job must persist all failures
            self.update(identifier, state="failed", phase="failed", error=str(exc))
        finally:
            temporary.cleanup()

    def activate(self, data):
        saved = self.status(data.get("id"))
        if (
            data.get("confirm_replace") is not True
            or saved["kind"] != "import"
            or saved["state"] != "ready"
        ):
            raise BundleError("必须先完整校验并确认整套恢复。")
        if self.paths.legacy:
            raise BundleError("此实例仍使用代码内数据；请先执行 xgent data migrate。")
        installer = self.paths.code / "install.sh"
        if os.name != "posix" or not installer.is_file():
            raise BundleError(
                "此启动方式无法安全自动重启；请停止服务后使用 xgent data import 完成恢复。"
            )
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
            path.parent != (self.paths.root.parent / "xgent-backups").resolve()
            or not path.is_file()
            or path.is_symlink()
        ):
            raise BundleError("备份文件已不存在")
        return BackupDownload(path)
