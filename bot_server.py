"""Telegram AI Bot executable entrypoint.

Application bootstrap lives in :mod:`bot_app.bootstrap`. Domain code is
kept in ordered sections during the compatibility-first refactor phase.
"""

from bot_app.bootstrap import load_sections as _load_sections


_SECTION_FILES = _load_sections(globals())
