"""Legacy executable entrypoint for pre-rename installations.

The canonical XGent for Telegram entrypoint is :mod:`xgent_server`.  Keeping
this shim allows existing PM2/nohup configurations that still launch
``bot_server.py`` to load the renamed application safely.
"""

from xgent_app.bootstrap import (
    load_sections as _load_sections,
    migrate_legacy_runtime_paths as _migrate_legacy_runtime_paths,
)


_MIGRATED_RUNTIME_PATHS = _migrate_legacy_runtime_paths()
_SECTION_FILES = _load_sections(globals())
