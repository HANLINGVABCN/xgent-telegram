"""Discover deployed data by layout, not release number; export a native bundle.

Only known instance locations and state assets are inspected. Never search the
whole machine, import application sections, or merge two different databases.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path

from .data_bundles import (
    CHUNK,
    SKIP_DIRS,
    BundleError,
    DataBundles,
    check_database,
    digest_file,
    protect,
    safe_name,
    sqlite_snapshot,
    write_json,
)
from .data_paths import DataPaths

DATABASE_NAMES = ("xgent_memory.db", "bot_memory.db")
STATE_NAMES = {
    ".env",
    "memory",
    "skill-private",
    "skill",
    "workspace",
    "server_assets",
    "xgent_storage",
    "bot_storage",
    "uploads",
    "generated_media",
    "exports",
}


def configured_directory(code, environ=None):
    env = os.environ if environ is None else environ
    code = Path(code).resolve()
    explicit = str(env.get("XGENT_DATA_DIR") or "").strip()

    def location(value, origin):
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise BundleError("已配置的数据目录不是绝对路径，未猜测位置")
        path = path.resolve()
        if path in {Path(path.anchor), Path.home().resolve()}:
            raise BundleError("数据目录不能是磁盘根目录或主目录")
        return path, origin

    if explicit:
        return location(explicit, "启动配置")
    marker = code / "data-location.json"
    if marker.is_file():
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))["data_dir"]
            return location(value, "已保存的数据目录")
        except (ValueError, KeyError, TypeError) as exc:
            raise BundleError("数据目录配置损坏，未猜测或创建空数据库") from exc
    if (code / ".env").is_file():
        for line in (code / ".env").read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if (
                sep
                and key.strip().removeprefix("export ") == "XGENT_DATA_DIR"
                and value.strip()
            ):
                return location(value.strip().strip("\"'"), "旧 .env 中的数据目录")
    return None, None


class DiscoveredPaths(DataPaths):
    @property
    def database(self):
        return next(
            (
                self.root / name
                for name in DATABASE_NAMES
                if (self.root / name).is_file()
            ),
            self.root / DATABASE_NAMES[0],
        )


def discover_instance(code, environ=None, *, required=True):
    code = Path(code).resolve()
    configured, origin = configured_directory(code, environ)
    if configured is not None:
        if any((configured / name).is_file() for name in DATABASE_NAMES):
            return DiscoveredPaths(code, configured, configured == code)
        if required:
            raise BundleError(
                f"{origin}指向 {configured}，但未找到数据库；未改用可能过期的另一份数据"
            )
        return DiscoveredPaths(code, configured, configured == code)
    # A live in-checkout installation is authoritative over sibling backups.
    if any((code / name).is_file() for name in DATABASE_NAMES):
        return DiscoveredPaths(code, code, True)
    candidates = [
        code / "data",
        code / ".xgent_state",
        code.parent / "xgent-data",
        code.parent / "telegram-ai-bot-data",
    ]
    found = [
        folder
        for folder in candidates
        if any((folder / name).is_file() for name in DATABASE_NAMES)
    ]
    if len(found) > 1:
        raise BundleError(
            "发现多套独立数据库，不能自动混合：" + "、".join(map(str, found))
        )
    if found:
        return DiscoveredPaths(code, found[0].resolve())
    if required:
        raise BundleError(
            "未找到实例数据库；已检查当前项目、已配置目录和相邻数据目录，不会导出空包"
        )
    return DiscoveredPaths(code, code.parent / "xgent-data")


def canonical_asset(name):
    """Map physical legacy names into the portable layout, without version flags."""
    parts = name.split("/")
    first = parts[0]
    if len(parts) == 1 and first in DATABASE_NAMES:
        return "xgent_memory.db"
    if len(parts) == 1 and first in {
        name + suffix for name in DATABASE_NAMES for suffix in ("-wal", "-shm")
    }:
        return "xgent_memory.db" + next(
            suffix for suffix in ("-wal", "-shm") if first.endswith(suffix)
        )
    if first == "bot_storage":
        parts[0] = "xgent_storage"
    elif first in {"uploads", "generated_media", "exports"}:
        parts.insert(0, "xgent_storage")
    elif first == "skill":
        parts[:1] = ["skill-private", "legacy-skill"]
    elif len(parts) == 1 and first in {
        "config.ini",
        "secrets.json",
        "credentials.yaml",
    }:
        parts[:1] = ["server_assets", "deployment-config", first]
    return "/".join(parts)


def portable_name(name, outputs):
    if any(part in SKIP_DIRS for part in name.split("/")):
        return False
    if name.endswith((".pyc", ".tmp", ".pid", ".lock", ".part", "-wal", "-shm")):
        return False
    try:
        safe_name(name)
    except BundleError:
        return False
    return outputs or not name.startswith("xgent_storage/command_outputs/")


class DiscoveredBundles(DataBundles):
    """Old/new instances export through the exact same manifest/hash machinery."""

    def __init__(self, paths, *, include_host_config=True):
        super().__init__(paths)
        self.include_host_config = include_host_config

    def files(self, outputs=False, *, migration=False):
        if migration:
            yield from super().files(outputs, migration=True)
            return
        root = self.paths.root
        chosen = {}

        def add(name, path):
            if path.is_symlink() or not path.is_file():
                raise BundleError("数据包含链接或特殊文件：" + str(path))
            previous = chosen.get(name)
            if previous is not None and previous != path:
                if digest_file(previous) != digest_file(path):
                    raise BundleError("新旧目录内有冲突文件，未覆盖或漏备份：" + name)
                return
            chosen[name] = path

        add("xgent_memory.db", self.paths.database)
        for name in (
            ".env",
            "path-map.json",
            "config.ini",
            "secrets.json",
            "credentials.yaml",
        ):
            source = root / name
            if source.exists() or source.is_symlink():
                add(canonical_asset(name), source)
        # Explicit data roots sometimes retain their legacy .env beside code.
        if (
            self.include_host_config
            and ".env" not in chosen
            and (self.paths.code / ".env").is_file()
        ):
            add(".env", self.paths.code / ".env")
        for tree in sorted(STATE_NAMES - {".env"}):
            folder = root / tree
            if folder.is_symlink():
                raise BundleError("数据目录是符号链接：" + str(folder))
            if not folder.is_dir():
                continue
            for parent, dirs, files in os.walk(folder, followlinks=False):
                dirs[:] = [name for name in sorted(dirs) if name not in SKIP_DIRS]
                for name in dirs:
                    if (Path(parent) / name).is_symlink():
                        raise BundleError(
                            "数据目录是符号链接：" + str(Path(parent) / name)
                        )
                for name in sorted(files):
                    path = Path(parent) / name
                    portable = canonical_asset(path.relative_to(root).as_posix())
                    if portable_name(portable, outputs):
                        add(portable, path)
        # The VPS tool used this user-level location before external data roots.
        legacy_asset = Path.home() / ".config/vps-manager/servers.json"
        if (
            self.include_host_config
            and "server_assets/vps-manager/servers.json" not in chosen
            and legacy_asset.is_file()
        ):
            add("server_assets/vps-manager/servers.json", legacy_asset)
        yield from sorted(chosen.items())


def validate_target(code, target):
    code = Path(code).resolve()
    target = Path(target).expanduser()
    if any(ord(char) < 32 for char in str(target)):
        raise BundleError("数据目录不能包含控制字符")
    if not target.is_absolute():
        raise BundleError("请选择绝对目录，例如 /home/user/xgent-data")
    # Reject a symlink target rather than silently renaming its referent.
    if target.is_symlink():
        raise BundleError("目标目录不能是符号链接")
    target = target.resolve()
    if (
        target == Path(target.anchor)
        or target == Path.home().resolve()
        or target == code
        or target.is_relative_to(code)
        or code.is_relative_to(target)
    ):
        raise BundleError(
            "数据必须放在代码之外的专用目录，不能选择磁盘根目录、主目录或代码的父目录"
        )
    if target.exists() and not target.is_dir():
        raise BundleError("目标不是目录")
    if any(
        (target / name).exists()
        for name in (
            "install.sh",
            "xgent_app",
            "bot_app",
            "bot_server.py",
            "xgent_server.py",
        )
    ):
        raise BundleError("所选目录是程序代码目录，不能整套替换；请选择独立的数据目录")
    if (
        target.exists()
        and any(target.iterdir())
        and not any((target / name).is_file() for name in DATABASE_NAMES)
    ):
        placeholders = STATE_NAMES | {
            "logs",
            ".install-state",
            "path-map.json",
            "runtime-ready.json",
            "restore-info.json",
            "xgent.pid",
            "bot.pid",
        }
        if any(path.name not in placeholders for path in target.iterdir()):
            raise BundleError("目标是已有的非实例目录，拒绝覆盖其中的文件")
    return target


def _legacy_root(archive):
    databases = [
        name for name in archive.namelist() if name.split("/")[-1] in DATABASE_NAMES
    ]
    if not databases:
        raise BundleError(
            "这个包只有对话文本或普通文件，没有实例数据库，不能恢复整套系统"
        )
    roots = {name.rsplit("/", 1)[0] + "/" if "/" in name else "" for name in databases}
    if len(roots) != 1:
        raise BundleError("备份包含多套数据库，请分别导入，不能混合")
    return roots.pop()


def _original_roots(database):
    roots = set()
    with closing(
        sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    ) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(global_messages)")}
        metadata = "metadata" if "metadata" in columns else "NULL"
        if "content" not in columns:
            return roots
        cursor = conn.execute(
            f"SELECT content,{metadata} FROM global_messages WHERE content LIKE '%storage%'"
            + (" OR metadata IS NOT NULL" if "metadata" in columns else "")
        )
        for row in cursor:
            text = "\n".join(str(value or "") for value in row)
            try:
                text += "\n" + json.dumps(
                    json.loads(row[1] or "{}"), ensure_ascii=False
                ).replace("\\\\", "/")
            except (ValueError, TypeError):
                pass
            for match in re.finditer(
                r"(?P<root>(?:[A-Za-z]:)?/[^\s\"<>`]*?)/(?:xgent_storage|bot_storage)/",
                text,
            ):
                if match["root"] not in {"/", ""}:
                    roots.add(match["root"])
    return roots


def _directory_roots(staging, manifest):
    mapping = Path(staging) / "path-map.json"
    old = json.loads(mapping.read_text(encoding="utf-8")).get("previous_roots", [])
    write_json(
        mapping,
        {
            "previous_roots": sorted(
                set(old) | _original_roots(Path(staging) / "xgent_memory.db")
            )
        },
    )
    return staging, manifest


def prepare_compatible_import(source, paths):
    """Accept current verified bundles, old full ZIPs, and extracted instances.

    Legacy packages have no manifest: ZIP CRC plus SQLite integrity and a new
    normalized bundle provide validation; never claim old packages had SHA data.
    """
    source = Path(source).expanduser().resolve()
    paths.root.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        root = source
        if not any((root / name).is_file() for name in DATABASE_NAMES):
            children = [
                p
                for p in root.iterdir()
                if p.is_dir() and any((p / n).is_file() for n in DATABASE_NAMES)
            ]
            if len(children) != 1:
                raise BundleError("所选目录里没有唯一的实例数据库")
            root = children[0]
        # Snapshot through the common exporter, including WAL and legacy assets.
        with tempfile.TemporaryDirectory(
            prefix=".xgent-normalize-", dir=paths.root.parent
        ) as scratch:
            bundle = DiscoveredBundles(
                DiscoveredPaths(paths.code, root, root == paths.code),
                include_host_config=False,
            ).export(Path(scratch) / "instance.zip", outputs=True)
            return _directory_roots(*DataBundles(paths).prepare_import(bundle["path"]))
    if not source.is_file() or not zipfile.is_zipfile(source):
        raise BundleError("请选择实例备份 ZIP 或已解压的数据目录")
    with zipfile.ZipFile(source) as archive:
        if "manifest.json" in archive.namelist():
            return DataBundles(paths).prepare_import(source)
        prefix = _legacy_root(archive)
        selected = {}
        for info in archive.infolist():
            raw = info.filename.rstrip("/")
            if (
                not raw
                or "\\" in raw
                or "\x00" in raw
                or ":" in raw
                or raw.startswith("/")
                or any(part in {"", ".", ".."} for part in raw.split("/"))
            ):
                raise BundleError("旧备份含不安全路径")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in {
                0,
                stat.S_IFREG,
                stat.S_IFDIR,
            }:
                raise BundleError("旧备份包含链接或特殊文件")
            if info.is_dir() or not info.filename.startswith(prefix):
                continue
            relative = info.filename[len(prefix) :]
            canonical = canonical_asset(relative)
            if not (
                portable_name(canonical, True)
                or canonical in {"xgent_memory.db-wal", "xgent_memory.db-shm"}
            ):
                continue
            if canonical.casefold() in selected:
                raise BundleError("旧备份含重复/冲突文件：" + canonical)
            selected[canonical.casefold()] = (canonical, info)
        parent = paths.root.parent
        if (
            shutil.disk_usage(parent).free
            < sum(info.file_size for _, info in selected.values()) * 2
        ):
            raise BundleError("磁盘空间不足，未修改现有实例")
        with tempfile.TemporaryDirectory(
            prefix=".xgent-normalize-", dir=parent
        ) as scratch:
            normalized = Path(scratch) / "data"
            normalized.mkdir(mode=0o700)
            protect(normalized, True)
            for name, info in selected.values():
                target = normalized / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.open(info) as incoming, target.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, CHUNK)
                protect(target)
            snapshot = Path(scratch) / "verified.db"
            sqlite_snapshot(normalized / "xgent_memory.db", snapshot)
            (normalized / "xgent_memory.db-wal").unlink(missing_ok=True)
            (normalized / "xgent_memory.db-shm").unlink(missing_ok=True)
            os.replace(snapshot, normalized / "xgent_memory.db")
            check_database(normalized / "xgent_memory.db")
            write_json(
                normalized / "path-map.json",
                {
                    "previous_roots": sorted(
                        _original_roots(normalized / "xgent_memory.db")
                    )
                },
            )
            bundle = DiscoveredBundles(
                DiscoveredPaths(paths.code, normalized), include_host_config=False
            ).export(Path(scratch) / "verified.zip", outputs=True)
            return DataBundles(paths).prepare_import(bundle["path"])
