"""One source of truth for state paths, independent of the working directory.

Legacy installations remain on their existing data until the explicit migration
command succeeds. New installations default to a sibling ``xgent-data`` folder.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DataPaths:
    code: Path
    root: Path
    legacy: bool = False

    @classmethod
    def discover(cls, code_root, environ=None):
        env = os.environ if environ is None else environ
        code = Path(code_root).resolve()
        value = env.get("XGENT_DATA_DIR", "").strip()
        marker = code / "data-location.json"
        if not value and marker.is_file():
            value = json.loads(marker.read_text(encoding="utf-8"))["data_dir"]
        if not value and (code / ".env").is_file():
            # Bootstrap/install tools must resolve this one path without
            # importing dotenv or executing any shell content from .env.
            for line in (code / ".env").read_text(encoding="utf-8-sig").splitlines():
                key, sep, raw = line.partition("=")
                if sep and key.strip().removeprefix('export ') == "XGENT_DATA_DIR":
                    value = raw.strip().strip("\"'")
        if value:
            root = Path(value).expanduser()
            if not root.is_absolute():
                raise ValueError("XGENT_DATA_DIR 必须是绝对路径")
            root = root.resolve()
            if root == Path(root.anchor) or root == Path.home().resolve():
                raise ValueError("数据目录不能是磁盘根目录或用户主目录，请使用专用目录")
            if root == code or root.is_relative_to(code):
                raise ValueError(
                    "数据目录必须位于代码目录之外；旧部署请执行 data migrate"
                )
            return cls(code, root)
        # Section probes and other isolated test runners execute a temporary
        # ``xgent_server.py`` without a checkout beside it. Keep their state
        # under that temporary root instead of sharing the host's sibling data
        # directory between independent processes.
        if not (code / "xgent_app").is_dir():
            return cls(code, code, True)
        # Do not silently start an empty database after a normal code update.
        legacy = any(
            (code / name).exists()
            for name in (
                ".env",
                "xgent_memory.db",
                "bot_memory.db",
                "xgent_storage",
                "memory",
                "skill-private",
                "workspace",
            )
        )
        return cls(code, code if legacy else code.parent / "xgent-data", legacy)

    @property
    def env(self):
        return self.root / ".env"

    @property
    def database(self):
        return self.root / "xgent_memory.db"

    @property
    def storage(self):
        return self.root / "xgent_storage"

    @property
    def uploads(self):
        return self.storage / "uploads"

    @property
    def memory(self):
        return self.root / "memory"

    @property
    def private_skills(self):
        return self.root / "skill-private"

    @property
    def workspace(self):
        return self.root / "workspace"

    @property
    def logs(self):
        return self.root if self.legacy else self.root / "logs"

    @property
    def assets(self):
        return self.root / "server_assets"

    def ensure(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.legacy:
            from .data_bundles import protect

            protect(self.root, True)
        self.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)


_active: DataPaths | None = None


def configure_paths(paths):
    global _active
    _active = paths


def resolve_stored_path(value):
    """Rebase known legacy roots only; callers still enforce their allowlists.

    Historical prose is never rewritten. A restored bundle carries its original
    root mappings, so the same references work on a different host as well.
    """
    path = Path(value)
    if _active is None:
        return path
    roots = [str(_active.code)] if not _active.legacy else []
    marker = _active.root / "path-map.json"
    if marker.is_file():
        data = json.loads(marker.read_text(encoding="utf-8"))
        roots = data.get("previous_roots", []) + roots
    text = str(value).replace("\\", "/")
    for old in sorted(set(roots), key=len, reverse=True):
        normalized = str(old).replace("\\", "/").rstrip("/")
        windows = len(normalized) > 1 and normalized[1] == ":"
        candidate, prefix = (
            (text.casefold(), normalized.casefold()) if windows else (text, normalized)
        )
        if not candidate.startswith(prefix + "/"):
            continue
        relative = Path(text[len(normalized) + 1 :])
        # Only state trees are aliases, never arbitrary files under the checkout.
        if (
            ".." not in relative.parts
            and relative.parts
            and relative.parts[0]
            in {
                "xgent_storage",
                "bot_storage",
                "memory",
                "skill-private",
                "workspace",
                "server_assets",
            }
        ):
            if relative.parts[0] == 'bot_storage':
                relative = Path('xgent_storage',*relative.parts[1:])
            return _active.root / relative
    return path
