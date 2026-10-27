"""Interactive installer migration: two actions, no user-written shell steps."""

from __future__ import annotations

import os
import re
import shutil
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

from .data_bundles import BundleError, DataBundles, check_database, protect, write_json
from .data_discovery import (
    DiscoveredBundles,
    DiscoveredPaths,
    configured_directory,
    discover_instance,
    prepare_compatible_import,
    validate_target,
)
from .data_lock import DataLease
from .data_paths import DataPaths
from .data_service import InstallerService


def size_text(size):
    return (
        f"{size / (1024**3):.2f} GiB"
        if size >= 1024**3
        else f"{size / (1024**2):.1f} MiB"
    )


def replace_location(env_file, target):
    """Change only the directory setting; never print or replace other secrets."""
    env_file = Path(env_file)
    if not env_file.is_file():
        return
    original_stat = env_file.stat()
    raw = env_file.read_text(encoding="utf-8-sig")
    if not re.search(r"^\s*(?:export\s+)?XGENT_DATA_DIR\s*=", raw, re.MULTILINE):
        return
    updated = re.sub(
        r"^\s*(?:export\s+)?XGENT_DATA_DIR\s*=.*$",
        lambda _: "XGENT_DATA_DIR=" + str(target),
        raw,
        flags=re.MULTILINE,
    )
    temporary = env_file.with_name(
        env_file.name + ".location-" + uuid.uuid4().hex + ".tmp"
    )
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        protect(temporary)
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    if os.name == "posix" and os.geteuid() == 0:
        os.chown(temporary, original_stat.st_uid, original_stat.st_gid)
    os.replace(temporary, env_file)


class MigrationWorkflow:
    def __init__(self, code, *, service=None, report=print, environ=None):
        self.code = Path(code).resolve()
        self.service = service or InstallerService(self.code)
        self.report = report
        self.environ = os.environ if environ is None else environ

    def source(self, required=True):
        return discover_instance(self.code, self.environ, required=required)

    def _backup(self, paths, outputs=True):
        self.report("正在自动查找并备份：" + str(paths.root))
        last = [0.0]

        def progress(**state):
            if time.monotonic() - last[0] >= 2 and state.get("total_bytes"):
                self.report(
                    "打包进度："
                    + str(
                        min(
                            99,
                            int(
                                100
                                * state.get("completed_bytes", 0)
                                / state["total_bytes"]
                            ),
                        )
                    )
                    + "%"
                )
                last[0] = time.monotonic()

        result = DiscoveredBundles(paths).export(outputs=outputs, progress=progress)
        self.report("备份已校验：" + result["path"])
        return result

    def export(self, *, outputs=False):
        paths = self.source()
        state = self.service.inspect(paths)
        self.service.ensure_idle(paths)
        paused = False
        result = None
        try:
            if state.running:
                self.report("正在安全暂停当前实例，导出后自动恢复……")
                self.service.stop(state)
                paused = True
            with DataLease(paths.root, exclusive=True):
                result = self._backup(paths, outputs)
        finally:
            if paused:
                self.report("正在恢复原服务……")
                try:
                    resume = getattr(self.service, "resume", self.service.start)
                    resume(paths, state)
                except Exception as exc:
                    # Backup remains valid even if startup fails. The message
                    # must distinguish a successful export from service health.
                    raise BundleError(
                        (
                            "备份已成功，文件为 " + result["path"] + "；"
                            if result
                            else ""
                        )
                        + "原数据未删除，但服务未能恢复，请查看脚本“运行状态”"
                    ) from exc
        self.report(
            "导出完成。"
            + ("原服务已恢复。" if paused else "服务原本未运行，保持停止状态。")
        )
        return result

    def prepare_import(self, source, target):
        target = validate_target(self.code, target)
        paths = DataPaths(self.code, target)
        validate = getattr(self.service, "validate_import_target", None)
        if validate:
            validate(paths)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.report("正在检查备份、旧版目录结构和数据库；此时不会停止或覆盖当前服务……")
        return prepare_compatible_import(source, paths)

    def _clean_staging(self, staging, target):
        staging = Path(staging).resolve()
        target = Path(target).resolve()
        if (
            staging.parent == target.parent
            and staging.name.startswith(".xgent-restore-")
            and staging.is_dir()
        ):
            shutil.rmtree(staging)

    def _preserve_owner(self, staging, before, destination):
        if os.name != "posix" or os.geteuid() != 0:
            return
        owner = (
            before.database
            if before.database.is_file()
            else destination.database
            if destination.database.is_file()
            else self.code
        ).stat()
        for directory, folders, files in os.walk(staging, followlinks=False):
            folders[:] = [name for name in folders if name != ".local-api-data"]
            os.chown(directory, owner.st_uid, owner.st_gid)
            for name in files:
                file = Path(directory) / name
                if not file.is_symlink():
                    os.chown(file, owner.st_uid, owner.st_gid)

    def activate_import(self, staging, target, *, start_after=True):
        target = validate_target(self.code, target)
        staging = Path(staging).resolve()
        if staging.parent != target.parent or not staging.name.startswith(
            ".xgent-restore-"
        ):
            raise BundleError("恢复临时目录无效")
        check_database(staging / "xgent_memory.db")
        before = self.source(required=False)
        state = self.service.inspect(before)
        self.service.ensure_idle(before)
        local_api = (
            self.service.local_api(before)
            if hasattr(self.service, "local_api")
            else None
        )
        configs = {
            path: path.read_bytes() if path.is_file() else None
            for path in (self.code / "data-location.json", self.code / ".env")
        }
        config_stats = {path: path.stat() for path in configs if path.is_file()}
        rollback = None
        switched = False
        paused = False
        start_attempted = False
        backups = []
        try:
            if state.running:
                self.report("正在安全停止当前实例……")
                self.service.stop(state)
                paused = True
            if local_api:
                self.report("正在保留本地 Bot API 数据与原容器……")
                local_api.pause()
            with ExitStack() as locks:
                for root in sorted({before.root, target}, key=str):
                    locks.enter_context(DataLease(root, exclusive=True))
                # Import always protects the previous instance, even when the
                # user selected a different destination. A third existing target
                # is backed up separately, never silently merged or destroyed.
                if before.database.is_file():
                    backups.append(self._backup(before, True))
                destination = DiscoveredPaths(self.code, target)
                if target != before.root and destination.database.is_file():
                    backups.append(self._backup(destination, True))
                if local_api:
                    local_api.copy_cache(staging)
                self._preserve_owner(staging, before, destination)
                self.report("校验通过，正在切换整套数据（不合并、不改项目名）……")
                result = DataBundles(DataPaths(self.code, target)).activate(staging)
                rollback = (
                    Path(result["rollback_dir"]) if result["rollback_dir"] else None
                )
                switched = True
                replace_location(target / ".env", target)
                replace_location(self.code / ".env", target)
                write_json(
                    self.code / "data-location.json",
                    {"data_dir": str(target), "previous_data_dir": str(before.root)},
                )
                if os.name == "posix" and os.geteuid() == 0:
                    owner = self.code.stat()
                    os.chown(
                        self.code / "data-location.json", owner.st_uid, owner.st_gid
                    )
            if local_api:
                local_api.switch(target)
            if start_after or state.running:
                self.report("正在启动并检查新数据目录……")
                start_attempted = True
                self.service.start(DataPaths(self.code, target), state)
            self.environ["XGENT_DATA_DIR"] = str(target)
            self.report("导入完成。当前数据目录：" + str(target))
            return {
                **result,
                "backups": backups,
                "started": bool(start_after or state.running),
            }
        except BaseException:
            if switched:
                try:
                    if start_attempted:
                        self.service.stop_started(DataPaths(self.code, target))
                    with DataLease(target, exclusive=True):
                        failed = target.with_name(
                            target.name + ".failed-" + uuid.uuid4().hex[:8]
                        )
                        if target.exists():
                            os.replace(target, failed)
                        if rollback and rollback.exists():
                            os.replace(rollback, target)
                        for path, raw in configs.items():
                            if raw is None:
                                path.unlink(missing_ok=True)
                            else:
                                temporary = path.with_name(
                                    path.name + ".rollback-" + uuid.uuid4().hex + ".tmp"
                                )
                                with temporary.open("xb") as handle:
                                    protect(temporary)
                                    handle.write(raw)
                                    handle.flush()
                                    os.fsync(handle.fileno())
                                if os.name == "posix" and os.geteuid() == 0:
                                    original = config_stats[path]
                                    os.chown(
                                        temporary, original.st_uid, original.st_gid
                                    )
                                os.replace(temporary, path)
                    self.report(
                        "导入启动失败，已恢复原目录配置；失败的数据副本保留在 "
                        + str(failed)
                    )
                except Exception as rollback_error:
                    raise BundleError(
                        "导入未完成，自动回退也未完成；原备份及原目录均保留。请勿清理目录。"
                    ) from rollback_error
            if paused:
                if local_api:
                    local_api.rollback()
                try:
                    resume = getattr(self.service, "resume", self.service.start)
                    resume(before, state)
                    self.report("原服务已恢复，未应用本次导入。")
                except Exception as start_error:
                    raise BundleError(
                        "导入失败，原数据/备份仍在，但原服务未能启动，请查看脚本“运行状态”"
                    ) from start_error
            elif local_api:
                local_api.rollback()
            raise
        finally:
            self._clean_staging(staging, target)


class MigrationMenu:
    def __init__(self, code, *, ask=input, report=print, workflow=None, environ=None):
        self.code = Path(code).resolve()
        self.ask = ask
        self.report = report
        self.workflow = workflow or MigrationWorkflow(
            self.code, report=report, environ=environ
        )
        self.environ = self.workflow.environ

    def confirm(self, question, default=False):
        answer = (
            self.ask(question + (" [Y/n]：" if default else " [y/N]：")).strip().lower()
        )
        return default if not answer else answer in {"y", "yes", "是", "确定"}

    def choose_directory(self):
        configured, origin = configured_directory(self.code, self.environ)
        if configured is not None:
            self.report(f"已配置的数据目录：{configured}（{origin}）")
            if not self.confirm("是否修改这个数据目录？"):
                return validate_target(self.code, configured)
        default = self.code.parent / "xgent-data"
        self.report("选择导入后的数据保存目录；项目名称和代码位置保持不变。")
        self.report("  1) 默认目录：" + str(default))
        self.report("  2) 自定义目录")
        self.report("  0) 取消")
        choice = self.ask("请选择 [默认 1]：").strip()
        if choice in {"0", "q"}:
            return None
        if choice in {"", "1"}:
            return validate_target(self.code, default)
        if choice == "2":
            value = (
                self.ask("请输入数据目录的绝对路径（无需填写变量名）：")
                .strip()
                .strip("\"'")
            )
            if not value:
                return None
            return validate_target(self.code, value)
        raise BundleError("无效的目录选项")

    def choose_source(self):
        folders = {self.code.parent / "xgent-backups"}
        configured, _ = configured_directory(self.code, self.environ)
        if configured:
            folders.add(configured.parent / "xgent-backups")
        files = sorted(
            {
                p
                for folder in folders
                if folder.is_dir()
                for p in folder.glob("*.zip")
                if p.is_file()
            },
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:8]
        self.report("请选择备份，或直接输入旧数据目录/ZIP 的完整路径：")
        for index, path in enumerate(files, 1):
            self.report(f"  {index}) {path}")
        value = self.ask("备份编号或路径（0 取消）：").strip().strip("\"'")
        if value in {"", "0", "q"}:
            return None
        return (
            files[int(value) - 1]
            if value.isdigit() and 0 < int(value) <= len(files)
            else Path(value).expanduser()
        )

    def run(self):
        self.report("\n数据迁移\n  1) 导入\n  2) 导出\n  0) 退出数据迁移")
        try:
            choice = self.ask("请选择：").strip()
            if choice in {"", "0", "q"}:
                return 0
            if choice == "2":
                paths = self.workflow.source()
                service = DiscoveredBundles(paths)
                estimate = service.estimate()
                self.report("已自动找到数据：" + str(paths.root))
                self.report("已识别数据库：" + paths.database.name + "；无需选择版本。")
                self.report(
                    "核心数据约 "
                    + size_text(estimate["size"])
                    + "；完整命令回执约 "
                    + size_text(estimate["command_outputs_size"])
                )
                self.report(
                    "不含完整回执不影响历史聊天/摘要，但备份无法恢复原始终端输出；包含会增大体积和耗时。"
                )
                outputs = self.confirm("是否额外包含完整命令回执？")
                if not self.confirm(
                    "备份包含密钥；脚本会自动停服、备份并恢复原服务。确认导出？"
                ):
                    return 0
                result = self.workflow.export(outputs=outputs)
                self.report("备份文件：" + result["path"])
                return 0
            if choice == "1":
                source = self.choose_source()
                if source is None:
                    return 0
                target = self.choose_directory()
                if target is None:
                    return 0
                self.report("备份来源：" + str(source) + "\n导入目录：" + str(target))
                self.report(
                    "将校验备份、自动暂停服务、备份现有数据、整套切换并重启验证；失败自动回退。"
                )
                if not self.confirm("现有数据不合并，旧目录/备份会保留。确认导入？"):
                    return 0
                staging, _ = self.workflow.prepare_import(source, target)
                try:
                    self.workflow.activate_import(staging, target)
                finally:
                    self.workflow._clean_staging(staging, target)
                return 0
            raise BundleError("无效的数据迁移选项")
        except (BundleError, ValueError, OSError, EOFError, KeyboardInterrupt) as exc:
            self.report(
                "数据操作未完成："
                + (str(exc) or "已取消")
                + "。没有要求你手动删除或搬动任何文件。"
            )
            return 1


def main():
    return MigrationMenu(
        os.environ.get("XGENT_MIGRATION_CODE_ROOT")
        or Path(__file__).resolve().parents[1]
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
