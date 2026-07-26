"""XGent for Telegram executable entrypoint.

Application bootstrap lives in :mod:`xgent_app.bootstrap`. Domain code is
kept in ordered sections during the compatibility-first refactor phase.
"""

from xgent_app.bootstrap import (
    load_sections as _load_sections,
    migrate_legacy_runtime_paths as _migrate_legacy_runtime_paths,
)


_MIGRATED_RUNTIME_PATHS = _migrate_legacy_runtime_paths()
_SECTION_FILES = _load_sections(globals())
