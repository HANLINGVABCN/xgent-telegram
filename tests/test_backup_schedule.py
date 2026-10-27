import asyncio
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from xgent_app.backup_schedule import (
    ScheduledBackups,
    configure,
    menu,
    parse_interval,
    read_settings,
)
from xgent_app.backup_storage import validate_backup_directory
from xgent_app.data_bundles import BundleError, write_json
from xgent_app.data_paths import DataPaths


def create_instance(root):
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "xgent_memory.db")) as conn:
        conn.executescript(
            "CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT);"
            "CREATE TABLE chat_sessions(id TEXT);"
            "CREATE TABLE global_messages(id INTEGER PRIMARY KEY,content TEXT,metadata TEXT);"
        )
        conn.commit()
    (root / ".env").write_text("BOT_TOKEN=fixture-secret\n", encoding="utf-8")
    upload = root / "xgent_storage/uploads/example.txt"
    upload.parent.mkdir(parents=True)
    upload.write_text("example", encoding="utf-8")


class BackupScheduleSettingsTests(unittest.TestCase):
    def test_backup_directory_must_be_dedicated_and_outside_project(self):
        with tempfile.TemporaryDirectory() as directory:
            code = Path(directory) / "project"
            code.mkdir()
            self.assertEqual(
                code.parent / "xgent-backups", validate_backup_directory(code)
            )
            for invalid in (code, code / "backups", code.parent):
                with self.assertRaises(ValueError):
                    validate_backup_directory(code, invalid)

    def test_parse_interval_supports_all_documented_units(self):
        self.assertEqual(1800, parse_interval("30m"))
        self.assertEqual(21600, parse_interval("6H"))
        self.assertEqual(86400, parse_interval("1d"))
        self.assertEqual(120, parse_interval("2分钟"))
        self.assertEqual(7200, parse_interval("2小时"))
        with self.assertRaises(ValueError):
            parse_interval("30s")
        with self.assertRaises(ValueError):
            parse_interval("0.5m")

    def test_menu_can_enable_overwrite_with_outputs_and_disable_again(self):
        with tempfile.TemporaryDirectory() as directory:
            code = Path(directory)
            messages = []
            answers = iter(["y", "30m", "", "1", "y"])
            self.assertEqual(
                0,
                menu(code, ask=lambda _: next(answers), report=messages.append),
            )
            saved = read_settings(code)
            self.assertTrue(saved["enabled"])
            self.assertEqual("overwrite", saved["mode"])
            self.assertTrue(saved["include_command_outputs"])
            self.assertEqual(1800, saved["interval_seconds"])

            answers = iter(["n"])
            self.assertEqual(
                0,
                menu(code, ask=lambda _: next(answers), report=messages.append),
            )
            self.assertFalse(read_settings(code)["enabled"])

            answers = iter(["y", "6h", "", "2", "12", "n"])
            self.assertEqual(
                0,
                menu(code, ask=lambda _: next(answers), report=messages.append),
            )
            saved = read_settings(code)
            self.assertEqual("accumulate", saved["mode"])
            self.assertEqual(12, saved["max_backups"])

    def test_invalid_config_is_rejected_instead_of_running_wrong_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            code = Path(directory)
            (code / "backup-schedule.json").write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "interval_seconds": 0,
                        "mode": "overwrite",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                read_settings(code)
            (code / "backup-schedule.json").write_text("[]", encoding="utf-8")
            with self.assertRaises(TypeError):
                read_settings(code)


class ScheduledBackupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.code = Path(self.temp.name) / "project"
        create_instance(self.code)
        self.now = [1_800_000_000.0]
        self.paths = DataPaths(self.code, self.code, True)

    async def _cleanup(self):
        self.temp.cleanup()

    def due_settings(self, *, mode="accumulate", outputs=False):
        saved = configure(
            self.code,
            enabled=True,
            frequency="1m",
            mode=mode,
            outputs=outputs,
        )
        saved["configured_at"] = self.now[0] - 61
        write_json(self.code / "backup-schedule.json", saved)
        return saved

    def worker(self):
        return ScheduledBackups(
            {"DATA_PATHS": self.paths}, clock=lambda: self.now[0]
        )

    async def test_overwrite_is_atomic_and_failure_keeps_last_good_zip(self):
        self.due_settings(mode="overwrite")
        worker = self.worker()
        await worker.tick()
        target = self.paths.backups / "scheduled-latest.zip"
        self.assertTrue(target.is_file())
        original = hashlib.sha256(target.read_bytes()).hexdigest()

        self.now[0] += 61
        worker.save_status(next_at=self.now[0])
        with patch.object(worker.bundles, "export", side_effect=BundleError("injected")):
            await worker.tick()
        self.assertEqual(original, hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual("failed", worker.status["state"])

    async def test_accumulate_creates_independent_archives(self):
        saved = self.due_settings(mode="accumulate")
        saved["max_backups"] = 2
        write_json(self.code / "backup-schedule.json", saved)
        worker = self.worker()
        manual = self.paths.backups / "manual-keep.zip"
        manual.parent.mkdir(parents=True, exist_ok=True)
        manual.write_bytes(b"manual")
        for _ in range(3):
            await worker.tick()
            self.now[0] += 61
            worker.save_status(next_at=self.now[0])
        archives = list(self.paths.backups.glob("scheduled-*.zip"))
        self.assertEqual(2, len(archives))
        self.assertNotEqual(archives[0].name, archives[1].name)
        self.assertTrue(manual.is_file())
        self.assertEqual(1, len(worker.status["result"]["pruned"]))

    async def test_revision_and_damaged_status_are_repaired_without_export(self):
        saved = configure(
            self.code,
            enabled=True,
            frequency="1h",
            mode="accumulate",
        )
        saved["configured_at"] = self.now[0]
        write_json(self.code / "backup-schedule.json", saved)
        worker = self.worker()
        worker.status = {"revision": saved["revision"], "next_at": "broken"}
        with patch.object(worker.bundles, "export") as exporting:
            await worker.tick()
        exporting.assert_not_called()
        self.assertEqual("waiting", worker.status["state"])
        self.assertIsInstance(worker.status["next_at"], float)

        changed = configure(
            self.code,
            enabled=True,
            frequency="2h",
            mode="accumulate",
        )
        changed["configured_at"] = self.now[0]
        write_json(self.code / "backup-schedule.json", changed)
        await worker.tick()
        self.assertEqual(changed["revision"], worker.status["revision"])
        self.assertEqual("waiting", worker.status["state"])

    async def test_backup_does_not_consult_ai_or_conversation_locks(self):
        self.due_settings()
        conversation_lock = asyncio.Lock()
        await conversation_lock.acquire()
        worker = ScheduledBackups(
            {
                "DATA_PATHS": self.paths,
                "_conversation_processing_lock": conversation_lock,
                "DB_WRITE_LOCK": object(),
            },
            clock=lambda: self.now[0],
        )
        await worker.tick()
        self.assertEqual("completed", worker.status["state"])
        conversation_lock.release()

    async def test_stop_waits_for_in_progress_writer_before_returning(self):
        self.due_settings()
        worker = self.worker()
        started = threading.Event()
        release = threading.Event()

        def slow_export(*args, **kwargs):
            started.set()
            release.wait(5)
            return {"path": "fixture.zip"}

        with patch.object(worker.bundles, "export", side_effect=slow_export):
            worker.start()
            await asyncio.wait_for(asyncio.to_thread(started.wait, 2), timeout=3)
            stopping = asyncio.create_task(worker.stop())
            await asyncio.sleep(0.05)
            self.assertFalse(stopping.done())
            release.set()
            await asyncio.wait_for(stopping, timeout=3)


if __name__ == "__main__":
    unittest.main()
