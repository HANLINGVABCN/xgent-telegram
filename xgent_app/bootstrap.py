"""Application bootstrap helpers.

The project is being migrated from one large module to regular Python modules.
During that migration, ordered source sections still execute in a shared
namespace so existing cross-section references keep their original behavior.
This module owns all manifest parsing and validation instead of leaving that
bootstrap machinery in the public entrypoint.
"""

from __future__ import annotations

from pathlib import Path
import os
from typing import MutableMapping, Optional, Tuple


PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_SECTION_DIR = PACKAGE_ROOT / "sections"
MANIFEST_FILENAME = "MANIFEST.txt"

LEGACY_RUNTIME_PATHS = (
    ("bot_memory.db", "xgent_memory.db"),
    ("bot_memory.db-wal", "xgent_memory.db-wal"),
    ("bot_memory.db-shm", "xgent_memory.db-shm"),
    ("bot_storage", "xgent_storage"),
    ("bot_server.log", "xgent_server.log"),
    ("bot_output.log", "xgent_output.log"),
    ("bot_full_trace.log", "xgent_full_trace.log"),
    ("bot.pid", "xgent.pid"),
)


def migrate_legacy_runtime_paths(project_root: Optional[Path] = None) -> Tuple[Tuple[str, str], ...]:
    """Move pre-XGent runtime data to canonical names when safe.

    Existing canonical paths always win; legacy paths are only moved when the
    destination does not exist.  This keeps upgrades non-destructive.
    """

    root = (project_root or PACKAGE_ROOT.parent).resolve()
    migrated = []
    for legacy_name, canonical_name in LEGACY_RUNTIME_PATHS:
        legacy_path = root / legacy_name
        canonical_path = root / canonical_name
        if not legacy_path.exists() or canonical_path.exists():
            continue
        try:
            os.replace(legacy_path, canonical_path)
        except OSError:
            continue
        migrated.append((legacy_name, canonical_name))
    return tuple(migrated)


class SectionManifestError(RuntimeError):
    """Raised when the ordered section manifest is missing or invalid."""


def read_section_manifest(section_dir: Optional[Path] = None) -> Tuple[str, ...]:
    """Read and validate the ordered section filenames.

    Entries must be plain Python filenames located directly in ``section_dir``.
    Rejecting absolute paths and nested paths prevents a malformed manifest from
    loading arbitrary files outside the application package.
    """

    directory = (section_dir or DEFAULT_SECTION_DIR).resolve()
    manifest_path = directory / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise SectionManifestError(f"section manifest not found: {manifest_path}")

    filenames = tuple(
        line.strip()
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if not filenames:
        raise SectionManifestError(f"section manifest is empty: {manifest_path}")
    if len(filenames) != len(set(filenames)):
        raise SectionManifestError(f"section manifest contains duplicate entries: {manifest_path}")

    for filename in filenames:
        relative_path = Path(filename)
        if (
            relative_path.is_absolute()
            or len(relative_path.parts) != 1
            or relative_path.name != filename
            or relative_path.suffix != ".py"
        ):
            raise SectionManifestError(f"invalid section filename: {filename!r}")
        section_path = directory / filename
        if not section_path.is_file():
            raise SectionManifestError(f"section file not found: {section_path}")

    return filenames


def load_sections(
    namespace: MutableMapping[str, object],
    section_dir: Optional[Path] = None,
) -> Tuple[str, ...]:
    """Execute ordered application sections in ``namespace``.

    The compiled filename is the real section path, so tracebacks identify the
    subsystem that failed even though all sections share one namespace.
    """

    directory = (section_dir or DEFAULT_SECTION_DIR).resolve()
    filenames = read_section_manifest(directory)
    for filename in filenames:
        section_path = directory / filename
        source = section_path.read_text(encoding="utf-8")
        code = compile(source, str(section_path), "exec")
        exec(code, namespace, namespace)
    return filenames
