"""One project directory for code and runtime data, independent of shell cwd."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DataPaths:
    code: Path
    root: Path
    legacy: bool = False

    @classmethod
    def discover(cls, code_root, environ=None):
        code = Path(code_root).resolve()
        return cls(code, code, True)

    @property
    def backups(self):
        from .backup_storage import configured_backup_directory

        return configured_backup_directory(self.code)

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
