import hashlib
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SECTION_DIR = ROOT / "xgent_app" / "sections"


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
        source = (ROOT / "xgent_server.py").read_text(encoding="utf-8")
        self.assertLess(len(source.splitlines()), 30)
        self.assertIn("_SECTION_FILES = _load_sections(globals())", source)

    def test_legacy_runtime_paths_migrate_without_overwriting(self):
        from xgent_app.bootstrap import migrate_legacy_runtime_paths

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "bot_memory.db").write_text("legacy-db", encoding="utf-8")
            (root / "bot_storage").mkdir()
            (root / "bot_storage" / "file.txt").write_text("legacy-file", encoding="utf-8")
            migrated = migrate_legacy_runtime_paths(root)

            self.assertIn(("bot_memory.db", "xgent_memory.db"), migrated)
            self.assertIn(("bot_storage", "xgent_storage"), migrated)
            self.assertEqual("legacy-db", (root / "xgent_memory.db").read_text(encoding="utf-8"))
            self.assertEqual("legacy-file", (root / "xgent_storage" / "file.txt").read_text(encoding="utf-8"))

            (root / "bot_output.log").write_text("legacy", encoding="utf-8")
            (root / "xgent_output.log").write_text("canonical", encoding="utf-8")
            migrate_legacy_runtime_paths(root)
            self.assertEqual("canonical", (root / "xgent_output.log").read_text(encoding="utf-8"))
            self.assertTrue((root / "bot_output.log").exists())

    def test_bootstrap_rejects_unsafe_manifest_entries(self):
        from xgent_app.bootstrap import SectionManifestError, read_section_manifest

        with tempfile.TemporaryDirectory() as temp_dir:
            section_dir = Path(temp_dir)
            (section_dir / "MANIFEST.txt").write_text("../outside.py\n", encoding="utf-8")
            with self.assertRaises(SectionManifestError):
                read_section_manifest(section_dir)

    def test_bootstrap_rejects_duplicate_manifest_entries(self):
        from xgent_app.bootstrap import SectionManifestError, read_section_manifest

        with tempfile.TemporaryDirectory() as temp_dir:
            section_dir = Path(temp_dir)
            (section_dir / "MANIFEST.txt").write_text("core.py\ncore.py\n", encoding="utf-8")
            (section_dir / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
            with self.assertRaises(SectionManifestError):
                read_section_manifest(section_dir)


if __name__ == "__main__":
    unittest.main()
