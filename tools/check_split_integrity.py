from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xgent_app.bootstrap import read_section_manifest
SECTION_DIR = ROOT / "xgent_app" / "sections"
BASELINE_PATH = SECTION_DIR / "SOURCE_BASELINE.sha256"


def section_body(source: str) -> str:
    """取文件头注释之后的正文；没有空行分隔时视为整份都是正文。"""
    parts = source.split("\n\n", 1)
    return parts[1] if len(parts) > 1 else ""


def compute_hash(section_dir: Path = SECTION_DIR) -> tuple[str, list[str]]:
    """编译每个 section 并返回 (正文拼接后的 sha256, manifest 列表)。"""
    manifest = read_section_manifest(section_dir)
    chunks = []
    for filename in manifest:
        path = section_dir / filename
        source = path.read_text(encoding="utf-8")
        compile(source, str(path), "exec")
        chunks.append(section_body(source))
    joined = "".join(chunks)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest(), manifest


def read_baseline() -> str:
    return BASELINE_PATH.read_text(encoding="utf-8").strip()


def write_baseline(value: str) -> None:
    BASELINE_PATH.write_text(value + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="校验（或更新）xgent_app/sections 的拆分完整性基线。"
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="改动 section 后用当前哈希覆盖 SOURCE_BASELINE.sha256",
    )
    args = parser.parse_args()

    actual_hash, manifest = compute_hash()

    if args.write:
        previous = read_baseline()
        if previous == actual_hash:
            print(f"baseline already up to date: {actual_hash}")
            return
        write_baseline(actual_hash)
        print(f"baseline updated: {previous} -> {actual_hash}")
        return

    expected_hash = read_baseline()
    if actual_hash != expected_hash:
        raise SystemExit(
            "split source integrity mismatch: "
            f"expected {expected_hash}, got {actual_hash}\n"
            "改动 section 属预期时，运行 python tools/check_split_integrity.py --write 更新基线。"
        )

    print(f"validated {len(manifest)} sections")
    print(f"section_source_sha256={actual_hash}")


if __name__ == "__main__":
    main()
