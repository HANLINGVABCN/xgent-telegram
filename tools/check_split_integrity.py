from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xgent_app.bootstrap import read_section_manifest
SECTION_DIR = ROOT / "xgent_app" / "sections"
MANIFEST = read_section_manifest(SECTION_DIR)

chunks = []
for filename in MANIFEST:
    path = SECTION_DIR / filename
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
    chunks.append(source.split("\n\n", 1)[1])

joined = "".join(chunks)
actual_hash = hashlib.sha256(joined.encode("utf-8")).hexdigest()
expected_hash = (SECTION_DIR / "SOURCE_BASELINE.sha256").read_text(encoding="utf-8").strip()
if actual_hash != expected_hash:
    raise SystemExit(
        "split source integrity mismatch: "
        f"expected {expected_hash}, got {actual_hash}"
    )

print(f"validated {len(MANIFEST)} sections")
print(f"section_source_sha256={actual_hash}")
