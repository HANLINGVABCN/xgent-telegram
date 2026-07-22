import hashlib
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SECTION_DIR = ROOT / "bot_app" / "sections"


def read_manifest():
    return [
        line.strip()
        for line in (SECTION_DIR / "MANIFEST.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


class SplitIntegrityTests(unittest.TestCase):
    def test_manifest_sections_compile_and_match_baseline(self):
        manifest = read_manifest()
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
        expected = (SECTION_DIR / "SOURCE_BASELINE.sha256").read_text(encoding="utf-8").strip()
        self.assertEqual(expected, digest)

    def test_entrypoint_is_small(self):
        source = (ROOT / "bot_server.py").read_text(encoding="utf-8")
        self.assertLess(len(source.splitlines()), 30)
        self.assertIn("_SECTION_FILES = _load_sections(globals())", source)

    def test_bootstrap_rejects_unsafe_manifest_entries(self):
        from bot_app.bootstrap import SectionManifestError, read_section_manifest

        with tempfile.TemporaryDirectory() as temp_dir:
            section_dir = Path(temp_dir)
            (section_dir / "MANIFEST.txt").write_text("../outside.py\n", encoding="utf-8")
            with self.assertRaises(SectionManifestError):
                read_section_manifest(section_dir)

    def test_bootstrap_rejects_duplicate_manifest_entries(self):
        from bot_app.bootstrap import SectionManifestError, read_section_manifest

        with tempfile.TemporaryDirectory() as temp_dir:
            section_dir = Path(temp_dir)
            (section_dir / "MANIFEST.txt").write_text("core.py\ncore.py\n", encoding="utf-8")
            (section_dir / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
            with self.assertRaises(SectionManifestError):
                read_section_manifest(section_dir)


if __name__ == "__main__":
    unittest.main()
