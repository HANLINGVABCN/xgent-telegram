from pathlib import Path
import hashlib

ROOT = Path(__file__).resolve().parents[1]
SECTION_DIR = ROOT / "telegram_ai_bot" / "sections"
MANIFEST = [
    line.strip()
    for line in (SECTION_DIR / "MANIFEST.txt").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]

chunks = []
for filename in MANIFEST:
    path = SECTION_DIR / filename
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
    chunks.append(source.split("\n\n", 1)[1])

joined = "".join(chunks)
actual_hash = hashlib.sha256(joined.encode("utf-8")).hexdigest()
expected_hash = (SECTION_DIR / "LEGACY_SOURCE.sha256").read_text(encoding="utf-8").strip()
if actual_hash != expected_hash:
    raise SystemExit(
        "split source integrity mismatch: "
        f"expected {expected_hash}, got {actual_hash}"
    )

print(f"validated {len(MANIFEST)} sections")
print(f"section_source_sha256={actual_hash}")
