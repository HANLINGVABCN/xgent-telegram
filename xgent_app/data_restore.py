"""Detached Linux restore coordinator. Invoked only after explicit Web confirm."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .data_bundles import DataBundles, write_json
from .data_lock import DataLease
from .data_paths import DataPaths


def main(job_file, code_dir, data_dir):
    job_file, code, root = (
        Path(job_file).resolve(),
        Path(code_dir).resolve(),
        Path(data_dir).resolve(),
    )
    saved = json.loads(job_file.read_text(encoding="utf-8"))
    paths = DataPaths(code, root)
    bundles = DataBundles(paths)
    rollback = None

    def state(**changes):
        saved.update(changes)
        write_json(job_file, saved)

    def service(action):
        result = subprocess.run(
            ["bash", str(code / "install.sh"), action],
            cwd=code,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("服务" + action + "失败，原数据仍保留")

    try:
        # Never stop the source server until the archive and SQLite have passed
        # every staging check. The HTTP result has already been acknowledged.
        time.sleep(1)
        service("stop")
        with DataLease(root, exclusive=True):
            state(phase="backing_up")
            pre_import = bundles.export(
                outputs=True,
                include_prompts=True,
                name_prefix="pre-import",
                _maintenance_locked=True,
            )
            state(pre_import_backup=pre_import, phase="activating")
            result = bundles.activate(saved["staging"])
            rollback = Path(result["rollback_dir"]) if result["rollback_dir"] else None
            state(rollback_dir=str(rollback) if rollback else None, phase="starting")
        started = time.time()
        service("start")
        deadline = time.monotonic() + 60
        ready = root / "runtime-ready.json"
        while time.monotonic() < deadline:
            if ready.is_file():
                status = json.loads(ready.read_text(encoding="utf-8"))
                if (
                    status.get("at", 0) >= started
                    and status.get("data_dir") == str(root)
                    and status.get("web_healthy", True)
                ):
                    if rollback and rollback.is_dir():
                        shutil.rmtree(rollback)
                    result["rollback_dir"] = None
                    state(
                        state="completed",
                        phase="completed",
                        result={**result, "pre_import_backup": pre_import},
                    )
                    return 0
            time.sleep(0.5)
        raise RuntimeError("恢复后的启动健康检查未通过")
    except Exception as exc:  # noqa: BLE001 - activation boundary always attempts rollback
        if rollback and rollback.exists():
            try:
                service("stop")
                with DataLease(root, exclusive=True):
                    bundles.rollback_activation(result)
                service("start")
                state(
                    state="failed",
                    phase="rolled_back",
                    error=str(exc) + "；已恢复原数据目录。",
                )
            except Exception as rollback_error:  # noqa: BLE001 - preserve rollback location on every failure
                state(
                    state="failed",
                    phase="rollback_required",
                    error=str(exc) + "；自动回退未完成：" + str(rollback_error),
                    rollback_dir=str(rollback),
                )
        else:
            try:
                service("start")
            except Exception as start_error:  # noqa: BLE001 - original directory remains intact
                logging.getLogger(__name__).warning(
                    "恢复未切换，重新启动原服务失败：%s", start_error
                )
            state(state="failed", phase="failed", error=str(exc) + "；数据未被覆盖。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
