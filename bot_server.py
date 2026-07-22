"""Telegram AI Bot entrypoint.

The application is split into ordered domain sections under
``telegram_ai_bot/sections``.  Sections are executed in one shared namespace
for this first refactor phase, preserving the existing runtime contracts while
making each subsystem independently reviewable and testable.
"""

from pathlib import Path


_SECTION_DIR = Path(__file__).resolve().parent / "telegram_ai_bot" / "sections"
_SECTION_FILES = tuple(
    line.strip()
    for line in (_SECTION_DIR / "MANIFEST.txt").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)


def _load_sections(namespace: dict) -> None:
    for filename in _SECTION_FILES:
        section_path = _SECTION_DIR / filename
        source = section_path.read_text(encoding="utf-8")
        code = compile(source, str(section_path), "exec")
        exec(code, namespace, namespace)


_load_sections(globals())
