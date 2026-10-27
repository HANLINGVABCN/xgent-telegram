import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from xgent_app.conversations import ExecutionFileLock
from xgent_app.data_bundles import BundleError
from xgent_app.data_operations import BackupDownload, DataOperations
from xgent_app.data_paths import DataPaths


class DataOperationsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        code = base / "code"
        code.mkdir()
        root = base / "data"
        root.mkdir()
        self.paths = DataPaths(code, root)
        connection = sqlite3.connect(self.paths.database)
        connection.executescript(
            "CREATE TABLE config(key TEXT,value TEXT);CREATE TABLE chat_sessions(id TEXT);CREATE TABLE global_messages(id INTEGER);"
        )
        connection.close()
        self.paths.env.write_text("BOT_TOKEN=test-secret", encoding="utf-8")
        manager = SimpleNamespace(lock=ExecutionFileLock(self.paths.database))
        self.addCleanup(manager.lock.release)
        db = SimpleNamespace(_write_lock=asyncio.Lock())

        class Database:
            @staticmethod
            async def get_instance():
                return db

        self.ops = DataOperations(
            {
                "DATA_PATHS": self.paths,
                "get_conversations": lambda: manager,
                "BotMemoryDB": Database,
            }
        )
        self.manager = manager

    async def test_export_is_nonblocking_idempotent_private_and_stream_downloadable(
        self,
    ):
        with self.assertRaises(BundleError):
            await self.ops.export({"request_id": "test-export-key-12345"})
        args = {"request_id": "test-export-key-12345", "confirm_secrets": True}
        first = await self.ops.export(args)
        second = await self.ops.export(args)
        self.assertEqual(first["id"], second["id"])
        await asyncio.gather(*self.ops.tasks)
        done = self.ops.status(first["id"])
        self.assertEqual("completed", done["state"], done)
        self.assertTrue(done["result"]["includes_secrets"])
        download = self.ops.download(first["id"])
        self.assertIsInstance(download, BackupDownload)
        self.assertTrue(download.path.is_file())
        self.assertFalse(self.manager.lock.busy())

    async def test_running_command_is_not_killed_or_included_in_export(self):
        self.assertTrue(self.manager.lock.acquire())
        saved = await self.ops.export(
            {"request_id": "blocked-export-key-123", "confirm_secrets": True}
        )
        await asyncio.gather(*self.ops.tasks)
        self.assertEqual("failed", self.ops.status(saved["id"])["state"])
        self.assertIsNotNone(self.manager.lock.fd)

    async def test_import_only_prepares_until_separately_confirmed_and_takes_temp_ownership(
        self,
    ):
        bundle = self.ops.bundles.export()
        temporary = tempfile.TemporaryDirectory(dir=self.paths.root.parent)
        import shutil

        incoming = Path(temporary.name) / "incoming.zip"
        shutil.copyfile(bundle["path"], incoming)
        args = {
            "request_id": "import-key-123456789",
            "confirm_secrets": True,
            "_bundle_path": str(incoming),
            "_temporary": temporary,
        }
        saved = await self.ops.upload_import(args)
        await asyncio.gather(*self.ops.tasks)
        ready = self.ops.status(saved["id"])
        self.assertEqual("ready", ready["state"], ready)
        self.assertFalse(incoming.exists())
        self.assertTrue((Path(ready["staging"]) / ".env").exists())
        self.assertEqual("BOT_TOKEN=test-secret", self.paths.env.read_text())
        with self.assertRaises(BundleError):
            self.ops.activate({"id": saved["id"]})
        self.assertEqual("ready", self.ops.status(saved["id"])["state"])
        self.assertFalse(list(self.paths.root.parent.glob("data.rollback-*")))

    async def test_restarted_process_exposes_interrupted_job_not_false_success(self):
        saved, _ = self.ops.create("export", "interrupted-key-123456")
        path = self.ops.job_path(saved["id"])
        value = json.loads(path.read_text())
        value["pid"] = -1
        path.write_text(json.dumps(value))
        self.ops.jobs.clear()
        self.assertEqual("failed", self.ops.status(saved["id"])["state"])
