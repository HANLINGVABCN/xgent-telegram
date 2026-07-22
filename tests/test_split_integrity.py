import hashlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SECTION_DIR = ROOT / "telegram_ai_bot" / "sections"


class SplitIntegrityTests(unittest.TestCase):
    def test_manifest_sections_compile_and_match_baseline(self):
        manifest = [
            line.strip()
            for line in (SECTION_DIR / "MANIFEST.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertGreaterEqual(len(manifest), 10)
        self.assertEqual(len(manifest), len(set(manifest)))

        chunks = []
        for filename in manifest:
            path = SECTION_DIR / filename
            self.assertTrue(path.is_file(), filename)
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
            chunks.append(source.split("\n\n", 1)[1])

        digest = hashlib.sha256("".join(chunks).encode("utf-8")).hexdigest()
        expected = (SECTION_DIR / "LEGACY_SOURCE.sha256").read_text(encoding="utf-8").strip()
        self.assertEqual(expected, digest)

    def test_entrypoint_is_small(self):
        source = (ROOT / "bot_server.py").read_text(encoding="utf-8")
        self.assertLess(len(source.splitlines()), 100)
        self.assertIn("_load_sections(globals())", source)


if __name__ == "__main__":
    unittest.main()
