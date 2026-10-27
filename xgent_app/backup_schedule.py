"""Project-local scheduled exports, configured by the installer menu."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from .backup_storage import default_backup_directory, validate_backup_directory
from .data_bundles import DataBundles, protect, write_json
from .data_lock import DataLease


def parse_interval(value):
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*(m|h|d|分钟|小时|天)\s*",
        str(value),
        re.IGNORECASE,
    )
    if not match:
        raise ValueError('请输入频率，例如 30m、6h、1d（分钟、小时、天）')
    factor = {
        "m": 60,
        "h": 3600,
        "d": 86400,
        "分钟": 60,
        "小时": 3600,
        "天": 86400,
    }[match[2].lower()]
    seconds = float(match[1]) * factor
    if not math.isfinite(seconds) or seconds < 60:
        raise ValueError('备份间隔至少 1 分钟')
    return seconds


def settings_path(code):
    return Path(code).resolve() / 'backup-schedule.json'


def read_settings(code):
    code = Path(code).resolve()
    path = settings_path(code)
    if not path.is_file():
        return {'enabled': False, 'interval_seconds': 86400, 'frequency': '1d',
                'mode': 'accumulate', 'include_command_outputs': False,
                'max_backups': 7,
                'backup_directory': str(default_backup_directory(code))}
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise TypeError("定时备份配置无效，请在安装脚本重新设置")
    seconds = value.get('interval_seconds')
    max_backups = value.get("max_backups", 7)
    if (
        type(value.get("enabled")) is not bool
        or value.get("mode") not in {"overwrite", "accumulate"}
        or type(seconds) not in (int, float)
        or not math.isfinite(seconds)
        or seconds < 60
        or type(value.get("include_command_outputs", False)) is not bool
        or type(max_backups) is not int
        or max_backups < 1
    ):
        raise ValueError("定时备份配置无效，请在安装脚本重新设置")
    value["max_backups"] = max_backups
    value["backup_directory"] = str(
        validate_backup_directory(code, value.get("backup_directory"))
    )
    return value


def configure(
    code,
    *,
    enabled,
    frequency="1d",
    mode="accumulate",
    outputs=False,
    max_backups=7,
    backup_directory=None,
):
    if mode not in {'overwrite', 'accumulate'}:
        raise ValueError('请选择覆盖或累计备份')
    if type(max_backups) is not int or max_backups < 1:
        raise ValueError("累计备份数量必须是正整数")
    code = Path(code).resolve()
    directory = validate_backup_directory(code, backup_directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    protect(directory, True)
    default = default_backup_directory(code).resolve()
    saved = {
        "enabled": bool(enabled),
        "frequency": frequency.strip(),
        "interval_seconds": parse_interval(frequency),
        "mode": mode,
        "include_command_outputs": bool(outputs),
        "max_backups": max_backups,
        # Null follows the installation when the checkout moves. Custom paths
        # stay explicit and are shared by installer, Web and scheduled jobs.
        "backup_directory": None if directory == default else str(directory),
        "revision": uuid.uuid4().hex,
        "configured_at": time.time(),
    }
    write_json(settings_path(code), saved)
    return saved


def menu(code, ask=input, report=print):
    code = Path(code).resolve()
    current = read_settings(code)
    report('定时导出备份：' + ('已开启' if current['enabled'] else '已关闭'))
    report('当前频率：' + current.get('frequency', '1d') + '；模式：'
           + ('覆盖上一份' if current['mode'] == 'overwrite' else
              '累计保留，最多 ' + str(current.get('max_backups', 7)) + ' 份'))
    report("当前保存目录：" + current["backup_directory"])
    answer = ask('是否开启定时导出备份？[y/N，0 取消]：').strip().lower()
    if answer == '0':
        return 0
    if answer not in {'', 'n', 'no', '否', 'y', 'yes', '是', '1'}:
        raise ValueError('请输入 y 开启、n 关闭或 0 取消')
    enabled = answer in {'y', 'yes', '是', '1'}
    if not enabled:
        configure(code, enabled=False, frequency=current.get('frequency', '1d'),
                  mode=current['mode'], outputs=current.get('include_command_outputs', False),
                  max_backups=current.get('max_backups', 7),
                  backup_directory=current["backup_directory"])
        report('定时备份已关闭；已有备份保留。')
        return 0
    report('频率示例：30m = 每 30 分钟，6h = 每 6 小时，1d = 每天。')
    frequency = ask(
        "请输入导出频率 [默认 " + current.get("frequency", "1d") + "]："
    ).strip() or current.get("frequency", "1d")
    parse_interval(frequency)
    directory = ask(
        "备份保存目录 [默认 " + current["backup_directory"] + "]："
    ).strip() or current["backup_directory"]
    directory = str(validate_backup_directory(code, directory))
    report('  1) 覆盖上一份：成功生成新包后替换 scheduled-latest.zip')
    report('  2) 累计保留：每次保存独立备份，使用时间和随机编号命名')
    choice = ask('请选择备份保存方式 [默认 2]：').strip() or '2'
    if choice not in {'1', '2'}:
        raise ValueError('无效备份模式')
    max_backups = current.get("max_backups", 7)
    if choice == "2":
        raw_limit = ask(
            "累计备份最多保留多少份 [默认 " + str(max_backups) + "]："
        ).strip()
        if raw_limit:
            if not raw_limit.isdigit():
                raise ValueError("累计备份数量必须是整数")
            max_backups = int(raw_limit)
        if max_backups < 1:
            raise ValueError("累计备份数量必须是正整数")
    outputs = ask("是否包含完整命令回执？[y/N]：").strip().lower() in {
        "y",
        "yes",
        "是",
        "1",
    }
    configure(
        code,
        enabled=True,
        frequency=frequency,
        mode="overwrite" if choice == "1" else "accumulate",
        outputs=outputs,
        max_backups=max_backups,
        backup_directory=directory,
    )
    report('定时备份已开启。服务运行时自动执行，修改约 15 秒内生效。')
    report('保存目录：' + directory)
    report('备份包含配置及密钥；累计保留会持续占用磁盘，可在此菜单关闭。')
    return 0


class ScheduledBackups:
    def __init__(self, namespace, *, clock=time.time):
        self.ns = namespace
        self.paths = namespace['DATA_PATHS']
        self.bundles = DataBundles(self.paths)
        self.clock = clock
        self.status_file = self.paths.code / '.install-state/backup-status.json'
        self.task = None
        self.status = {}
        if self.status_file.is_file():
            try:
                self.status = json.loads(self.status_file.read_text(encoding='utf-8'))
                if not isinstance(self.status, dict):
                    self.status = {}
            except (OSError, ValueError):
                self.status = {}

    def save_status(self, **changes):
        self.status.update(changes)
        self.status_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_json(self.status_file, self.status)

    def accumulated_target(self):
        stamp = datetime.fromtimestamp(self.clock()).astimezone().strftime(
            "%Y%m%d-%H%M%S"
        )
        return self.paths.backups / (
            "scheduled-" + stamp + "-" + uuid.uuid4().hex[:8] + ".zip"
        )

    def prune_accumulated(self, limit):
        archives = []
        for path in self.paths.backups.glob("scheduled-*.zip"):
            if path.name == "scheduled-latest.zip" or path.is_symlink():
                continue
            try:
                if path.is_file():
                    archives.append((path.stat().st_mtime_ns, path.name, path))
            except FileNotFoundError:
                continue
        deleted, errors = [], []
        for _, _, path in sorted(archives)[:-limit]:
            try:
                path.unlink()
                deleted.append(str(path))
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append(str(path) + "：" + str(exc))
        return deleted, errors

    async def tick(self):
        settings = read_settings(self.paths.code)
        if not settings['enabled']:
            return
        now = self.clock()
        next_at = self.status.get("next_at")
        if (
            self.status.get("revision") != settings.get("revision")
            or type(next_at) not in (int, float)
            or not math.isfinite(next_at)
        ):
            next_at = settings.get("configured_at", now) + settings["interval_seconds"]
            self.save_status(
                revision=settings.get("revision"),
                state="waiting",
                next_at=next_at,
                error=None,
            )
        if now < next_at:
            return
        self.paths.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        lease = DataLease(self.paths.backups / 'scheduled', exclusive=True)
        if not lease.acquire():
            return
        try:
            target = (self.paths.backups / "scheduled-latest.zip"
                      if settings["mode"] == "overwrite"
                      else self.accumulated_target())
            operation = asyncio.create_task(
                asyncio.to_thread(
                    self.bundles.export,
                    target,
                    outputs=settings.get("include_command_outputs", False),
                    overwrite=settings["mode"] == "overwrite",
                )
            )
            try:
                result = await asyncio.shield(operation)
            except asyncio.CancelledError:
                try:
                    await operation
                except Exception as exc:  # noqa: BLE001 - preserve cancellation after writer exits
                    logging.getLogger(__name__).warning(
                        "停机等待定时备份结束时收到失败：%s", exc
                    )
                raise
            deleted, cleanup_errors = ([], [])
            if settings["mode"] == "accumulate":
                deleted, cleanup_errors = self.prune_accumulated(
                    settings["max_backups"]
                )
                result = {
                    **result,
                    "retention_limit": settings["max_backups"],
                    "pruned": deleted,
                }
                if cleanup_errors:
                    logging.getLogger(__name__).warning(
                        "定时备份已完成，但旧备份清理失败：%s",
                        "；".join(cleanup_errors),
                    )
            self.save_status(state='completed', last_completed_at=self.clock(),
                             next_at=self.clock() + settings['interval_seconds'],
                             result=result, error=None,
                             cleanup_warning="；".join(cleanup_errors) or None)
        except Exception as exc:  # noqa: BLE001 - one failed backup must not kill scheduling
            self.save_status(state='failed', error=str(exc), next_at=self.clock() + 60)
            logging.getLogger(__name__).warning('定时备份未完成：%s', exc)
        finally:
            lease.release()

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - invalid state is retried after correction
                logging.getLogger(__name__).warning('定时备份配置无法读取：%s', exc)
            await asyncio.sleep(15)

    def start(self):
        self.task = asyncio.create_task(self.run(), name='scheduled-backups')

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


if __name__ == '__main__':
    import sys
    try:
        raise SystemExit(menu(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]))
    except (TypeError, ValueError, OSError, EOFError, KeyboardInterrupt) as error:
        print('定时备份设置未完成：' + str(error), file=sys.stderr)
        raise SystemExit(1) from error
