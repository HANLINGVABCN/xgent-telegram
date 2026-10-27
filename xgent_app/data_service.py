"""Verified service lifecycle for the installer's import/export wizard.

Never operate on an identically named service belonging to another checkout.
The wizard does not need Telegram/network access or a model invocation.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .data_bundles import BundleError


def process_alive(pid):
    try:
        if int(pid) <= 0:
            return False
        if os.name == "nt":
            # os.kill(pid, 0) is not a harmless existence probe on Windows.
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return ctypes.get_last_error() == 5
            try:
                code = wintypes.DWORD()
                return (
                    bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                    and code.value == 259
                )
            finally:
                kernel.CloseHandle(handle)
        status = Path("/proc") / str(pid) / "stat"
        if status.is_file() and status.read_text().split(") ", 1)[1].startswith("Z "):
            return False
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


@dataclass
class ServiceState:
    mode: str = "nohup"
    running: bool = False
    pid: int = 0
    name: str = ""
    command: tuple[str, ...] = ()


class InstallerService:
    def __init__(self, code):
        self.code = Path(code).resolve()
        self._resumed = []

    def command(self, arguments, *, env=None, timeout=30):
        try:
            result = subprocess.run(
                arguments,
                cwd=self.code,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BundleError(
                "服务操作未完成，请查看安装器/进程管理器状态；没有删除实例数据"
            ) from exc
        if result.returncode:
            # Tool output may contain credentials. Do not echo command lines,
            # full PM2 environments, or unfiltered subprocess output.
            raise BundleError(
                "服务操作失败（退出码 "
                + str(result.returncode)
                + "），没有删除数据；请检查当前账户的服务管理权限"
            )
        return result.stdout

    def _same_checkout(self, pid):
        try:
            cmd = (
                (Path("/proc") / str(pid) / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="replace")
            )
            cwd = (Path("/proc") / str(pid) / "cwd").resolve()
            return cwd == self.code and any(
                name in cmd for name in ("xgent_server", "bot_server", "xgent_cli")
            )
        except (OSError, ValueError):
            return False

    def inspect(self, paths):
        mode = "nohup"
        for root in (paths.root, self.code):
            for folder in (".install-state", ".xgent_state"):
                marker = root / folder / "runtime-mode"
                if marker.is_file():
                    value = marker.read_text().strip()
                    if value in {"pm2", "systemd", "nohup"}:
                        mode = value
        matches = []
        collision = False
        if shutil.which("pm2"):
            for row in json.loads(self.command(["pm2", "jlist"])):
                entry = row.get("pm2_env") or {}
                if row.get("name") not in {"xgent-telegram", "telegram-ai-bot"}:
                    continue
                cwd = Path(entry.get("pm_cwd") or "/nonexistent").resolve()
                if cwd != self.code:
                    collision = True
                    continue
                pid = int(row.get("pid") or 0)
                matches.append(
                    ServiceState(
                        "pm2",
                        entry.get("status") == "online" and process_alive(pid),
                        pid,
                        row["name"],
                    )
                )
        if shutil.which("systemctl") and Path("/run/systemd/system").is_dir():
            result = subprocess.run(
                [
                    "systemctl",
                    "show",
                    "xgent",
                    "--property=WorkingDirectory",
                    "--property=ActiveState",
                    "--property=MainPID",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            values = dict(
                line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
            )
            if (
                values.get("WorkingDirectory")
                and Path(values["WorkingDirectory"]).resolve() == self.code
            ):
                pid = int(values.get("MainPID") or 0)
                matches.append(
                    ServiceState(
                        "systemd",
                        values.get("ActiveState") == "active" and process_alive(pid),
                        pid,
                        "xgent",
                    )
                )
        for root in {paths.root, self.code}:
            for name in ("xgent.pid", "bot.pid"):
                marker = root / name
                if marker.is_file():
                    try:
                        pid = int(marker.read_text().strip())
                    except ValueError:
                        continue
                    if (
                        process_alive(pid)
                        and self._same_checkout(pid)
                        and not any(s.pid == pid for s in matches)
                    ):
                        matches.append(ServiceState("nohup", True, pid, name))
        live = [item for item in matches if item.running]
        if len(live) > 1:
            raise BundleError(
                "发现本项目同时运行多个服务，请先在脚本中检查保活方式，未停止任何进程"
            )
        state = (
            live[0]
            if live
            else next(
                (item for item in matches if item.mode == mode), ServiceState(mode)
            )
        )
        if collision and state.mode == "pm2" and not matches:
            raise BundleError(
                "同名 PM2 服务属于另一个项目，未操作它；请从当前实例的安装目录打开脚本"
            )
        ready = paths.root / "runtime-ready.json"
        if ready.is_file():
            stamp = json.loads(ready.read_text(encoding="utf-8"))
            if process_alive(stamp.get("pid")) and not self._same_checkout(
                stamp.get("pid")
            ):
                raise BundleError(
                    "这份数据正被另一个代码目录的实例使用，未操作该服务；请从正在使用的实例脚本进入"
                )
        # Old CLI versions do not hold the new shared data lease. Detect their
        # actual process/cwd instead of assuming the missing lease means idle.
        if Path("/proc").is_dir():
            for folder in Path("/proc").iterdir():
                if (
                    folder.name.isdigit()
                    and int(folder.name) not in {os.getpid(), state.pid}
                    and self._same_checkout(int(folder.name))
                ):
                    raise BundleError(
                        "本项目还有独立 CLI/前台服务在运行，请先退出该会话；没有停止或修改任何数据"
                    )
        if state.running and state.mode == "nohup":
            state.command = tuple(
                (Path("/proc") / str(state.pid) / "cmdline")
                .read_bytes()
                .decode(errors="replace")
                .rstrip("\0")
                .split("\0")
            )
        return state

    def ensure_idle(self, paths):
        if not paths.database.is_file():
            return
        if not self.inspect(paths).running:
            return  # Stale task rows from an exited old process are data, not a live job.
        with closing(
            sqlite3.connect(paths.database.resolve().as_uri() + "?mode=ro", uri=True)
        ) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "config" in tables:
                row = conn.execute(
                    "SELECT value FROM config WHERE key='conversation_running'"
                ).fetchone()
                if row:
                    running = json.loads(row[0])
                    if running and process_alive(running.get("pid")):
                        raise BundleError(
                            "AI/命令仍在执行，请等本轮结束再选择导入或导出；不会为迁移强杀任务"
                        )
            if (
                "trigger_tasks" in tables
                and conn.execute(
                    "SELECT 1 FROM trigger_tasks WHERE status='running' LIMIT 1"
                ).fetchone()
            ):
                raise BundleError("后台命令仍在运行，请结束后再试；未停止服务")

    def stop(self, state):
        if not state.running:
            return
        if state.mode == "pm2":
            self.command(["pm2", "stop", state.name])
        elif state.mode == "systemd":
            self.command(["systemctl", "stop", "xgent"])
        else:
            if not self._same_checkout(state.pid):
                raise BundleError("进程归属已改变，未发送停止信号")
            os.kill(state.pid, signal.SIGINT)
        deadline = time.monotonic() + 30
        while process_alive(state.pid):
            if time.monotonic() >= deadline:
                raise BundleError("服务尚未安全退出，已停止迁移；未强制结束进程")
            time.sleep(0.2)
        for process in self._resumed:
            process.poll()

    def validate_import_target(self, paths):
        if (
            paths.root != self.code
            and not (self.code / "xgent_app/data_paths.py").is_file()
        ):
            raise BundleError(
                "目标程序尚不支持独立数据目录；导入会回退，不会用旧程序打开空数据库。请用脚本的安装/完整更新选项准备目标程序后再导入"
            )

    def start(self, paths, state):
        self.validate_import_target(paths)
        env = dict(os.environ)
        env["XGENT_MAINTENANCE_DATA_DIR"] = str(paths.root)
        env["XGENT_MAINTENANCE_CODE_DIR"] = str(self.code)
        env.pop("XGENT_INSTALL_SH_LIB", None)
        if paths.root == self.code:
            env.pop("XGENT_DATA_DIR", None)
        else:
            env["XGENT_DATA_DIR"] = str(paths.root)
        mode = state.mode if state.mode in {"pm2", "systemd", "nohup"} else "nohup"
        started = time.time()
        self.command(
            ["bash", str(self.code / "install.sh"), "data-start-internal", mode],
            env=env,
            timeout=600,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = paths.root / "runtime-ready.json"
            if ready.is_file():
                try:
                    status = json.loads(ready.read_text(encoding="utf-8"))
                    if (
                        status.get("at", 0) >= started
                        and Path(status.get("data_dir", "")).resolve() == paths.root
                        and process_alive(status.get("pid"))
                        and status.get("web_healthy", True)
                    ):
                        return status
                except (ValueError, OSError):
                    pass  # A concurrent atomic write will be checked next poll.
            time.sleep(0.25)
        raise BundleError("新进程未通过启动与数据目录检查，将尝试恢复原配置和服务")

    def stop_started(self, paths):
        self.stop(self.inspect(paths))

    def resume(self, paths, state):
        """Export resumes the existing service definition, not a code upgrade."""
        if state.mode == "pm2":
            self.command(["pm2", "restart", state.name], timeout=60)
        elif state.mode == "systemd":
            self.command(["systemctl", "start", "xgent"], timeout=60)
        elif state.command:
            env = dict(os.environ)
            if paths.root != self.code:
                env["XGENT_DATA_DIR"] = str(paths.root)
            else:
                env.pop("XGENT_DATA_DIR", None)
            log_dir = paths.logs
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "xgent_output.log").open("ab") as output:
                process = subprocess.Popen(
                    list(state.command),
                    cwd=self.code,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            self._resumed.append(process)
            (paths.root / "xgent.pid").write_text(str(process.pid), encoding="ascii")
        else:
            return self.start(paths, state)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            active = self.inspect(paths)
            if active.running:
                return {"pid": active.pid, "data_dir": str(paths.root)}
            time.sleep(0.25)
        raise BundleError("备份已保存，但原服务未恢复到运行状态")

    def local_api(self, paths):
        if not shutil.which("docker"):
            return None
        result = subprocess.run(
            ["docker", "inspect", "telegram-local-bot-api"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode:
            return None
        values = json.loads(result.stdout)[0]
        mounts = values.get("Mounts") or []
        own = next(
            (
                mount
                for mount in mounts
                if mount.get("Destination") == "/var/lib/telegram-bot-api"
                and Path(mount.get("Source", "/")).resolve()
                == (paths.root / ".local-api-data").resolve()
            ),
            None,
        )
        if own is None:
            return None  # Another instance's container is outside this workflow.
        host = values.get("HostConfig") or {}
        if (
            len(mounts) != 1
            or own.get("Type") != "bind"
            or host.get("Privileged")
            or host.get("NetworkMode") not in {"bridge", "default"}
            or len((values.get("Config") or {}).get("Entrypoint") or []) > 1
        ):
            raise BundleError(
                "当前本地 Bot API 使用自定义容器配置，无法安全自动换挂载；未停服或改动数据"
            )
        return LocalApiMove(self, paths, values)


class LocalApiMove:
    """Rebind the installer's own Bot API container; preserve the original."""

    def __init__(self, service, paths, config):
        self.service = service
        self.paths = paths
        self.config = config
        self.running = bool(config.get("State", {}).get("Running"))
        self.name = "telegram-local-bot-api"
        self.saved_name = self.name + "-before-data-" + uuid.uuid4().hex[:8]
        self.paused = False
        self.renamed = False
        self.created = False

    def pause(self):
        if self.running:
            self.service.command(
                ["docker", "stop", "--time", "30", self.name], timeout=45
            )
            self.paused = True

    def copy_cache(self, staging):
        source = self.paths.root / ".local-api-data"
        if source.is_symlink():
            raise BundleError("本地 API 数据目录是链接，拒绝误搬其他目录")
        target = Path(staging) / ".local-api-data"
        if source.is_dir():
            required = sum(
                path.stat().st_size
                for path in source.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
            if shutil.disk_usage(Path(staging)).free < required:
                raise BundleError("本地 Bot API 数据迁移空间不足，未切换目录")
            shutil.copytree(source, target, symlinks=True)
            if os.name == "posix" and os.geteuid() == 0:
                for directory, _, files in os.walk(source, followlinks=False):
                    original = Path(directory).stat()
                    copy = target / Path(directory).relative_to(source)
                    os.chown(copy, original.st_uid, original.st_gid)
                    for name in files:
                        original = (Path(directory) / name).lstat()
                        os.chown(
                            copy / name,
                            original.st_uid,
                            original.st_gid,
                            follow_symlinks=False,
                        )
        else:
            target.mkdir(mode=0o700)

    def switch(self, target):
        self.service.command(["docker", "rename", self.name, self.saved_name])
        self.renamed = True
        host = self.config["HostConfig"]
        config = self.config["Config"]
        args = [
            "docker",
            "create",
            "--name",
            self.name,
            "--restart",
            host.get("RestartPolicy", {}).get("Name") or "no",
            "--mount",
            "type=bind,src="
            + str(Path(target) / ".local-api-data")
            + ",dst=/var/lib/telegram-bot-api",
        ]
        for key, value in (config.get("Labels") or {}).items():
            args.extend(["--label", key + "=" + value])
        for value in config.get("Env") or []:
            args.extend(["--env", value])
        for port, bindings in (host.get("PortBindings") or {}).items():
            for binding in bindings or []:
                ip = binding.get("HostIp") or "0.0.0.0"
                if ":" in ip:
                    ip = "[" + ip + "]"
                args.extend(["--publish", ip + ":" + binding["HostPort"] + ":" + port])
        if config.get("User"):
            args.extend(["--user", config["User"]])
        if config.get("WorkingDir"):
            args.extend(["--workdir", config["WorkingDir"]])
        if config.get("Entrypoint"):
            entry = config["Entrypoint"]
            if len(entry) != 1:
                raise BundleError("本地 API 入口包含自定义参数；原容器仍保留，可回退")
            args.extend(["--entrypoint", entry[0]])
        args.append(self.config["Image"])
        args.extend(config.get("Cmd") or [])
        self.service.command(args, timeout=60)
        self.created = True
        if self.running:
            self.service.command(["docker", "start", self.name])

    def rollback(self):
        if self.created:
            self.service.command(
                ["docker", "stop", "--time", "30", self.name], timeout=45
            )
            self.service.command(
                [
                    "docker",
                    "rename",
                    self.name,
                    self.name + "-failed-data-" + uuid.uuid4().hex[:8],
                ]
            )
        if self.renamed:
            self.service.command(["docker", "rename", self.saved_name, self.name])
        if self.running and (self.paused or self.renamed):
            self.service.command(["docker", "start", self.name])
