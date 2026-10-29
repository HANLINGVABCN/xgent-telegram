"""Selective, multi-format backups and non-destructive automatic scanning."""
import asyncio
import hashlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

import pytest

from xgent_app.backup_schedule import ScheduledBackups, configure, read_settings
from xgent_app.data_archives import FORMATS
from xgent_app.data_bundles import BundleError, DataBundles
from xgent_app.data_discovery import DiscoveredBundles
from xgent_app.data_operations import DataOperations
from xgent_app.data_paths import DataPaths


def instance(base):
    code = Path(base) / "code"
    code.mkdir()
    paths = DataPaths(code, code)
    with closing(sqlite3.connect(paths.database)) as connection:
        connection.executescript("CREATE TABLE config(key TEXT,value TEXT);"
                                 "CREATE TABLE global_messages(id INTEGER);CREATE TABLE chat_sessions(id TEXT);")
    for name, content in {".env": "BOT_TOKEN=fixture-private",
                          "memory/note.md": "remember this\n" * 200,
                          "xgent_storage/command_outputs/result.txt": "command output\n" * 100,
                          "xgent_storage/uploads/keep.txt": "unselected upload"}.items():
        target = code / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return paths, DataBundles(paths)


@pytest.fixture
def bundles(tmp_path):
    return instance(tmp_path)


@pytest.mark.parametrize("format", list(FORMATS))
def test_selected_formats_restore_and_rollback_only_included_categories(bundles, format):
    paths, service = bundles
    selected = ["env", "memory", "command_outputs"]
    with patch("xgent_app.data_bundles.sqlite_snapshot", side_effect=AssertionError("unselected database")):
        exported = service.export(categories=selected, archive_format=format)
    assert exported["path"].endswith(FORMATS[format]["extension"])
    manifest = service.verify(exported["path"])
    assert manifest["version"] == 2
    assert manifest["archive_format"] == format
    assert set(manifest["categories"]) == set(selected)
    assert "xgent_memory.db" not in manifest["files"]
    paths.env.write_text("OLD-ENV", encoding="utf-8")
    old_memory = paths.memory / "obsolete.md"
    old_memory.write_text("old memory", encoding="utf-8")
    old_output = paths.storage / "command_outputs/obsolete.txt"
    old_output.write_text("old output", encoding="utf-8")
    before_db = hashlib.sha256(paths.database.read_bytes()).hexdigest()
    staging, manifest = service.prepare_import(exported["path"])
    preview = service.preview(manifest)
    assert preview["restore_mode"] == "categories"
    assert {row["id"] for row in preview["categories"]} == set(selected)
    assert next(row for row in preview["categories"] if row["id"] == "memory")["current_file_count"] == 2
    result = service.activate(staging)
    assert paths.env.read_text() == "BOT_TOKEN=fixture-private"
    assert not old_memory.exists() and not old_output.exists()
    assert (paths.storage / "uploads/keep.txt").read_text() == "unselected upload"
    assert hashlib.sha256(paths.database.read_bytes()).hexdigest() == before_db
    service.rollback_activation(result)
    assert old_memory.read_text() == "old memory"
    assert old_output.read_text() == "old output"
    assert paths.env.read_text() == "OLD-ENV"


@pytest.mark.parametrize("format", list(FORMATS))
def test_full_legacy_backup_remains_readable_in_every_format(bundles, format):
    paths, service = bundles
    exported = service.export(archive_format=format)
    staging, manifest = service.prepare_import(exported["path"])
    assert manifest["version"] == 1
    assert "xgent_memory.db" in manifest["files"]
    assert service.preview(manifest)["restore_mode"] == "full"
    service.discard_staging(staging)


def test_empty_included_category_explicitly_clears_its_old_contents(bundles):
    paths, service = bundles
    (paths.memory / "note.md").unlink()
    exported = service.export(categories=["memory"], archive_format="tar.xz")
    (paths.memory / "old.md").write_text("delete this", encoding="utf-8")
    staging, manifest = service.prepare_import(exported["path"])
    assert not manifest["files"]
    assert service.preview(manifest)["categories"][0]["current_file_count"] == 1
    service.activate(staging)
    assert not (paths.memory / "old.md").exists()
    assert paths.env.is_file() and paths.database.is_file()


def test_env_only_archive_does_not_require_a_database(bundles):
    paths, service = bundles
    paths.database.unlink()
    exported = service.export(categories=["env"])
    staging, manifest = service.prepare_import(exported["path"])
    service.activate(staging)
    assert paths.env.read_text() == "BOT_TOKEN=fixture-private"
    assert not paths.database.exists()


def test_inventory_and_sample_estimates_follow_selection(bundles):
    paths, service = bundles
    estimate = service.estimate(categories=["command_outputs"], compare_formats=True)
    output = paths.storage / "command_outputs/result.txt"
    assert estimate["size"] == output.stat().st_size
    assert estimate["file_count"] == 1
    assert not estimate["includes_secrets"]
    assert set(item["id"] for item in estimate["formats"]) == set(FORMATS)
    for item in estimate["formats"]:
        assert item["is_estimate"] and item["estimated_size"] > 0
        assert item["estimated_seconds"] > 0 and item["sample_bytes"] <= 1024 * 1024
    assert any(row["id"] == "env" and not row["selected"] for row in estimate["categories"])
    for invalid in ([], ["unknown"], ["env", "env"]):
        with pytest.raises(BundleError):
            service.export(categories=invalid)


@pytest.mark.parametrize('bundle_class', [DataBundles, DiscoveredBundles])
@pytest.mark.parametrize('pre_import', [False, True])
def test_server_assets_are_excluded_from_new_backups_and_left_in_place(bundles, bundle_class, pre_import):
    paths, _ = bundles
    asset = paths.root / 'server_assets/vps-manager/servers.json'
    asset.parent.mkdir(parents=True)
    asset.write_text('private-server-config', encoding='utf-8')
    service = bundle_class(paths)
    exported = service.export(outputs=True, include_prompts=pre_import,
                              name_prefix='pre-import' if pre_import else 'manual')
    manifest = service.verify(exported['path'])
    assert not any(name.startswith('server_assets/') for name in manifest['files'])
    assert all(row['id'] != 'server_assets' for row in service.estimate()['categories'])
    with pytest.raises(BundleError):
        service.export(categories=['server_assets'])
    staging, _ = service.prepare_import(exported['path'])
    asset.write_text('keep-current-config', encoding='utf-8')
    service.activate(staging)
    assert asset.read_text() == 'keep-current-config'


def test_scheduled_backup_removes_retired_server_assets_from_saved_choices(bundles):
    paths, service = bundles
    saved = configure(paths.code, enabled=True, frequency='1m', categories=['memory'])
    saved['categories'].append('server_assets')
    (paths.code / 'backup-schedule.json').write_text(json.dumps(saved), encoding='utf-8')
    selected = read_settings(paths.code)['categories']
    assert selected == ['memory']
    exported = service.export(categories=selected, name_prefix='scheduled')
    assert not any(name.startswith('server_assets/') for name in service.verify(exported['path'])['files'])


def test_manifest_cannot_smuggle_unselected_content(bundles, tmp_path):
    _, service = bundles
    original = service.export(categories=["env"])
    forged = tmp_path / "forged.zip"
    with zipfile.ZipFile(original["path"]) as incoming, zipfile.ZipFile(forged, "w") as outgoing:
        manifest = json.loads(incoming.read("manifest.json"))
        manifest["categories"] = ["memory"]
        for name in incoming.namelist():
            outgoing.writestr(name, json.dumps(manifest) if name == "manifest.json" else incoming.read(name))
    with pytest.raises(BundleError):
        service.prepare_import(forged)


@pytest.mark.parametrize("format", ["tar.gz", "tar.xz"])
def test_tar_links_and_traversal_never_reach_active_data(bundles, tmp_path, format):
    import io
    paths, service = bundles
    original = service.export(categories=["env"], archive_format=format)
    manifest = service.verify(original["path"])
    for malicious, link in [("../outside", False), ("memory/link", True)]:
        forged = tmp_path / ("bad" + FORMATS[format]["extension"])
        edited = {**manifest, "categories": ["env", "memory"], "files": {**manifest["files"],
                    malicious: {"size": 3, "sha256": hashlib.sha256(b"bad").hexdigest()}}}
        with tarfile.open(forged, "w:gz" if format == "tar.gz" else "w:xz") as archive:
            data = paths.env.read_bytes()
            env = tarfile.TarInfo(".env");env.size = len(data);archive.addfile(env, io.BytesIO(data))
            bad = tarfile.TarInfo(malicious);bad.size = 3
            if link:
                bad.type = tarfile.SYMTYPE;bad.linkname = "../../outside";bad.size = 0
            archive.addfile(bad, None if link else io.BytesIO(b"bad"))
            data = json.dumps(edited).encode();info = tarfile.TarInfo("manifest.json");info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        with pytest.raises(BundleError):
            service.prepare_import(forged)
        assert paths.env.read_text() == "BOT_TOKEN=fixture-private"
        assert not (tmp_path / "outside").exists()


class BackupOptionsOperationsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths, self.bundles = instance(self.temp.name)
        self.operations = DataOperations({"DATA_PATHS": self.paths})

    async def test_scans_are_automatic_non_destructive_and_discardable(self):
        exported = self.bundles.export(categories=["memory", "command_outputs"], archive_format="tar.gz")
        name = Path(exported["path"]).name
        saved = await self.operations.import_stored({"name": name, "request_id": "automatic-scan-test-123"})
        await asyncio.gather(*self.operations.tasks)
        ready = self.operations.status(saved["id"])
        assert ready["state"] == "ready", ready
        assert {row["id"] for row in ready["preview"]["categories"]} == {"memory", "command_outputs"}
        assert self.paths.env.read_text() == "BOT_TOKEN=fixture-private"
        staging = Path(ready["staging"])
        await self.operations.cancel_import({"id": saved["id"]})
        assert not staging.exists()
        assert self.operations.status(saved["id"])["state"] == "cancelled"

    async def test_cancel_during_scan_cleans_staging_instead_of_publishing_ready(self):
        exported = self.bundles.export(categories=["memory"])
        saved = await self.operations.import_stored({"name": Path(exported["path"]).name,
                                                   "request_id": "cancel-scan-running-123"})
        await self.operations.cancel_import({"id": saved["id"]})
        await asyncio.gather(*self.operations.tasks)
        assert self.operations.status(saved["id"])["state"] == "cancelled"
        assert not list(self.paths.code.parent.glob(".xgent-restore-*"))

    async def test_output_only_export_does_not_need_secret_confirmation(self):
        job = await self.operations.export({"request_id": "outputs-only-export-123",
                                           "categories": ["command_outputs"], "archive_format": "tar.xz"})
        await asyncio.gather(*self.operations.tasks)
        done = self.operations.status(job["id"])
        assert done["state"] == "completed", done
        assert not done["result"]["includes_secrets"]
        catalog = self.operations.list_backups()
        assert catalog["items"][0]["archive_format"] == "tar.xz"
        assert catalog["items"][0]["categories"][0]["id"] == "command_outputs"

    async def test_scheduler_uses_selected_format_and_prunes_across_formats(self):
        settings = configure(self.paths.code, enabled=True, frequency="1m", max_backups=1,
                             categories=["memory"], archive_format="tar.xz")
        assert read_settings(self.paths.code)["categories"] == ["memory"]
        old = self.paths.backups / "scheduled-old.zip"
        shutil.copyfile(self.bundles.export(categories=["memory"])["path"], old)
        scheduler = ScheduledBackups({"DATA_PATHS": self.paths}, clock=lambda: settings["configured_at"] + 61)
        await scheduler.tick()
        assert scheduler.status["state"] == "completed", scheduler.status
        remaining = list(self.paths.backups.glob("scheduled-*"))
        assert len(remaining) == 1 and remaining[0].name.endswith(".tar.xz")
        assert self.bundles.verify(remaining[0])["categories"] == ["memory"]
