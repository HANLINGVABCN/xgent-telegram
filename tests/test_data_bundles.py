import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from xgent_app.data_bundles import BundleError, DataBundles, check_database
from xgent_app.data_lock import DataLease
from xgent_app.data_paths import DataPaths, configure_paths, resolve_stored_path


class DataBundlesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.code = self.base / "code"
        (self.code / "xgent_app").mkdir(parents=True)
        self.root = self.base / "data"
        self.root.mkdir()
        self.paths = DataPaths(self.code, self.root)
        self.service = DataBundles(self.paths)
        with sqlite3.connect(self.paths.database) as conn:
            conn.executescript(
                "CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT);"
                "CREATE TABLE global_messages(id INTEGER PRIMARY KEY,content TEXT);"
                "CREATE TABLE chat_sessions(id TEXT PRIMARY KEY);"
                "INSERT INTO config VALUES('conversation_schema_version','5');"
                "INSERT INTO global_messages VALUES(1,'kept context and terminal summary');"
            )
        conn.close()
        for name, data in {
            ".env": b"BOT_TOKEN=private-token\n",
            "memory/m.txt": b"private memory",
            "skill-private/script/a.py": b"print(1)",
            "xgent_storage/uploads/original.bin": b"\0\xff\x01",
            "xgent_storage/attachment_versions/test/index.json": b"{}",
            "xgent_storage/command_outputs/old.txt": b"full raw output",
            "workspace/result.txt": b"user generated file",
            "server_assets/vps-manager/servers.json": b'{"servers":[]}',
            "memory/debug.log": b"not a memory",
            "skill-private/__pycache__/skip.pyc": b"skip",
        }.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def test_export_whitelist_outputs_opt_in_native_layout_and_restore(self):
        basic = self.service.export(self.base / "basic.zip")
        deep = self.service.export(self.base / "deep.zip", outputs=True)
        with zipfile.ZipFile(basic["path"]) as archive:
            names = archive.namelist()
            self.assertIn(".env", names)
            self.assertIn("workspace/result.txt", names)
            self.assertIn("xgent_storage/attachment_versions/test/index.json", names)
            self.assertNotIn("xgent_storage/command_outputs/old.txt", names)
            self.assertFalse(
                any(".log" in name or "__pycache__" in name for name in names)
            )
            self.assertEqual(b"BOT_TOKEN=private-token\n", archive.read(".env"))
        with zipfile.ZipFile(deep["path"]) as archive:
            self.assertIn("xgent_storage/command_outputs/old.txt", archive.namelist())
        staging, manifest = self.service.prepare_import(basic["path"])
        self.assertFalse(manifest["include_command_outputs"])
        (self.root / "keep-old").write_text("rollback", encoding="utf-8")
        result = self.service.activate(staging)
        self.assertEqual(
            "rollback", (Path(result["rollback_dir"]) / "keep-old").read_text()
        )
        self.assertEqual(
            b"\0\xff\x01",
            (self.root / "xgent_storage/uploads/original.bin").read_bytes(),
        )
        self.assertFalse((self.root / "xgent_storage/command_outputs/old.txt").exists())
        check_database(self.paths.database)
        with sqlite3.connect(self.paths.database) as conn:
            self.assertEqual(
                "kept context and terminal summary",
                conn.execute("SELECT content FROM global_messages").fetchone()[0],
            )
        conn.close()

    def test_corruption_and_traversal_never_replace_active_data(self):
        exported = self.service.export(self.base / "basic.zip")
        for malicious in (
            "../escape.txt",
            "/absolute.txt",
            "C:/escape.txt",
            "memory/../escape.txt",
            "memory/link.txt",
        ):
            target = self.base / "bad.zip"
            with (
                zipfile.ZipFile(exported["path"]) as src,
                zipfile.ZipFile(target, "w") as out,
            ):
                manifest = json.loads(src.read("manifest.json"))
                for name in src.namelist():
                    if name != "manifest.json":
                        out.writestr(name, src.read(name))
                info = zipfile.ZipInfo(malicious)
                if malicious == "memory/link.txt":
                    info.external_attr = (0o120777) << 16
                out.writestr(info, b"bad")
                manifest["files"][malicious] = {"size": 3, "sha256": "0" * 64}
                out.writestr("manifest.json", json.dumps(manifest))
            with self.assertRaises(BundleError):
                self.service.prepare_import(target)
            self.assertEqual(b"BOT_TOKEN=private-token\n", self.paths.env.read_bytes())
        self.assertFalse((self.base / "escape.txt").exists())
        self.assertFalse(list(self.base.glob(".xgent-restore-*")))

    def test_migration_dry_run_preserves_source_and_maps_old_absolute_paths(self):
        target = self.base / "new-data"
        dry = self.service.migrate(target, dry_run=True)
        self.assertTrue(dry["dry_run"])
        self.assertFalse(target.exists())
        with patch.object(Path, "home", return_value=self.base / "empty-home"):
            result = self.service.migrate(target)
        self.assertTrue(result["old_data_preserved"])
        self.assertTrue(self.paths.database.exists())
        discovered = DataPaths.discover(self.code, {})
        self.assertEqual(target, discovered.root)
        configure_paths(discovered)
        try:
            old = self.root / "xgent_storage/uploads/original.bin"
            self.assertEqual(
                target / "xgent_storage/uploads/original.bin",
                resolve_stored_path(str(old)),
            )
            self.assertEqual(
                self.code / "xgent_server.py",
                resolve_stored_path(str(self.code / "xgent_server.py")),
            )
        finally:
            configure_paths(None)
        with self.assertRaises(BundleError):
            self.service.migrate(self.code / "nested")

    def test_lock_shared_runtime_refuses_exclusive_maintenance(self):
        first = DataLease(self.root)
        second = DataLease(self.root)
        maintenance = DataLease(self.root, exclusive=True)
        try:
            self.assertTrue(first.acquire())
            self.assertTrue(second.acquire())
            self.assertFalse(maintenance.acquire())
            first.release()
            self.assertFalse(maintenance.acquire())
            second.release()
            self.assertTrue(maintenance.acquire())
            self.assertFalse(first.acquire())
        finally:
            first.release()
            second.release()
            maintenance.release()

    def test_explicit_external_default_and_legacy_guard(self):
        self.assertEqual(
            self.code.parent / "xgent-data", DataPaths.discover(self.code, {}).root
        )
        self.assertEqual(
            self.root,
            DataPaths.discover(self.code, {"XGENT_DATA_DIR": str(self.root)}).root,
        )
        (self.code / ".env").write_text("BOT_TOKEN=old", encoding="utf-8")
        self.assertTrue(DataPaths.discover(self.code, {}).legacy)
        with self.assertRaises(ValueError):
            DataPaths.discover(self.code, {"XGENT_DATA_DIR": str(self.code / "data")})

    def test_failed_activation_preserves_old_directory(self):
        bundle = self.service.export(self.base / "backup.zip")
        staging, _ = self.service.prepare_import(bundle["path"])
        import os

        replace = os.replace

        def fail_switch(src, dst):
            if Path(src) == staging and Path(dst) == self.root:
                raise OSError("injected switch failure")
            return replace(src, dst)

        with (
            patch("xgent_app.data_bundles.os.replace", side_effect=fail_switch),
            self.assertRaises(OSError),
        ):
            self.service.activate(staging)
        self.assertEqual(b"BOT_TOKEN=private-token\n", self.paths.env.read_bytes())
