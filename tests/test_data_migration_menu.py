"""Version-independent installer migration and rollback; no real services."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from xgent_app.data_bundles import BundleError, write_json
from xgent_app.data_discovery import (
    DiscoveredBundles,
    DiscoveredPaths,
    discover_instance,
    prepare_compatible_import,
    validate_target,
)
from xgent_app.data_migration import MigrationMenu, MigrationWorkflow
from xgent_app.data_paths import DataPaths, configure_paths, resolve_stored_path
from xgent_app.data_service import (
    InstallerService,
    LocalApiMove,
    ServiceState,
    process_alive,
)


def instance(root, *, old=False, text="original"):
    root.mkdir(parents=True, exist_ok=True)
    database = root / ("bot_memory.db" if old else "xgent_memory.db")
    with closing(sqlite3.connect(database)) as conn:
        conn.executescript(
            "CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT);"
            "CREATE TABLE chat_sessions(id TEXT);"
            "CREATE TABLE global_messages(id INTEGER PRIMARY KEY,content TEXT,metadata TEXT);"
        )
        conn.execute(
            "INSERT INTO global_messages VALUES(1,?,?)",
            (
                text,
                json.dumps({"path": "/old-host/project/bot_storage/uploads/test.bin"}),
            ),
        )
        conn.commit()
    (root / ".env").write_text(
        "BOT_TOKEN=fixture-secret\nAUTHORIZED_USER_ID=1\n", encoding="utf-8"
    )
    storage = root / ("bot_storage" if old else "xgent_storage")
    for relative, body in {
        "uploads/test.bin": b"\0\xfforiginal",
        "command_outputs/run.txt": b"full output",
        "generated_media/picture.bin": b"generated",
        "exports/history.txt": b"export",
    }.items():
        target = storage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    for relative in (
        "memory/a.txt",
        "skill-private/script/task.py",
        "workspace/report.txt",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("keep", encoding="utf-8")
    return DiscoveredPaths(root, root, True)


class FakeService:
    def __init__(self, *, running=True, fail_start=False):
        self.running = running
        self.fail_start = fail_start
        self.events = []
        self.busy = False

    def inspect(self, paths):
        self.events.append(("inspect", str(paths.root)))
        return ServiceState("nohup", self.running, 0)

    def ensure_idle(self, paths):
        if self.busy:
            raise BundleError("busy AI")

    def stop(self, state):
        self.events.append(("stop", state.mode))
        self.running = False

    def start(self, paths, state):
        self.events.append(("start", str(paths.root)))
        if self.fail_start:
            self.fail_start = False
            raise BundleError("injected start failure")
        self.running = True

    def stop_started(self, paths):
        self.events.append(("stop_started", str(paths.root)))
        self.running = False


class DataMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.code = self.base / "project"
        (self.code / "xgent_app").mkdir(parents=True)
        self.messages = []
        self.addCleanup(lambda: configure_paths(None))
        self.home_patch = patch(
            "xgent_app.data_discovery.Path.home", return_value=self.base / "test-home"
        )
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def workflow(self, **kwargs):
        service = FakeService(**kwargs)
        return MigrationWorkflow(
            self.code, service=service, report=self.messages.append, environ={}
        ), service

    def imported_bundle(self):
        root = self.base / "incoming"
        instance(root, text="imported")
        return DiscoveredBundles(DiscoveredPaths(self.code, root)).export(
            self.base / "incoming.zip", outputs=True
        )["path"]

    def test_old_names_auto_export_without_mutating_or_renaming_source(self):
        instance(self.code, old=True)
        flow, service = self.workflow()
        result = flow.export()
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertIn("xgent_memory.db", archive.namelist())
            self.assertIn("xgent_storage/uploads/test.bin", archive.namelist())
            self.assertNotIn(
                "xgent_storage/command_outputs/run.txt", archive.namelist()
            )
            self.assertEqual(
                b"\0\xfforiginal", archive.read("xgent_storage/uploads/test.bin")
            )
        self.assertTrue((self.code / "bot_memory.db").is_file())
        self.assertFalse((self.code / "xgent_memory.db").exists())
        self.assertEqual(
            ["stop", "start"],
            [event[0] for event in service.events if event[0] != "inspect"],
        )
        self.assertTrue(self.code.is_dir())

    def test_configured_external_data_wins_and_missing_config_never_exports_stale_copy(
        self,
    ):
        instance(self.code, old=True)
        root = self.base / "active"
        instance(root)
        write_json(self.code / "data-location.json", {"data_dir": str(root)})
        self.assertEqual(root, discover_instance(self.code, {}).root)
        write_json(
            self.code / "data-location.json", {"data_dir": str(self.base / "missing")}
        )
        with self.assertRaises(BundleError):
            discover_instance(self.code, {})

    def test_export_does_not_start_previously_stopped_service_and_busy_never_stops(
        self,
    ):
        instance(self.code)
        flow, service = self.workflow(running=False)
        flow.export(outputs=True)
        self.assertEqual(["inspect"], [event[0] for event in service.events])
        flow, service = self.workflow()
        service.busy = True
        with self.assertRaises(BundleError):
            flow.export()
        self.assertNotIn("stop", [event[0] for event in service.events])

    def test_export_failure_still_restarts_original(self):
        instance(self.code)
        flow, service = self.workflow()
        with (
            patch.object(DiscoveredBundles, "export", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            flow.export()
        self.assertTrue(service.running)
        self.assertEqual("start", service.events[-1][0])

    def test_import_legacy_wrapped_zip_and_old_absolute_path_mapping(self):
        old = self.base / "old-project"
        instance(old, old=True)
        package = self.base / "old-full.zip"
        with zipfile.ZipFile(package, "w") as archive:
            for file in old.rglob("*"):
                if file.is_file():
                    archive.write(file, "snapshot/" + file.relative_to(old).as_posix())
            archive.writestr("snapshot/venv/ignored.py", b"not data")
            archive.writestr("snapshot/server.log", b"not data")
        target = self.base / "new-data"
        staging, manifest = prepare_compatible_import(
            package, DataPaths(self.code, target)
        )
        self.assertIn("xgent_storage/command_outputs/run.txt", manifest["files"])
        self.assertFalse((staging / "venv").exists())
        mapping = json.loads((staging / "path-map.json").read_text())
        self.assertIn("/old-host/project", mapping["previous_roots"])
        configure_paths(DataPaths(self.code, staging))
        self.assertEqual(
            staging / "xgent_storage/uploads/test.bin",
            resolve_stored_path("/old-host/project/bot_storage/uploads/test.bin"),
        )
        shutil.rmtree(staging)

    def test_import_legacy_directory_including_custom_skill_and_separate_assets(self):
        root = self.base / "old"
        instance(root, old=True)
        (root / "skill").mkdir()
        (root / "skill/custom.md").write_text("private", encoding="utf-8")
        target = self.base / "new-data"
        staging, _ = prepare_compatible_import(root, DataPaths(self.code, target))
        self.assertEqual(
            "private", (staging / "skill-private/legacy-skill/custom.md").read_text()
        )
        self.assertTrue((root / "bot_memory.db").exists())
        shutil.rmtree(staging)

    def test_invalid_and_multidatabase_old_archives_never_touch_current_instance(self):
        instance(self.code)
        for name in ("../escape.txt", "root/../../escape", "/absolute", "root/linked"):
            package = self.base / "malicious.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.write(self.code / "xgent_memory.db", "root/bot_memory.db")
                info = zipfile.ZipInfo(name)
                if name == "root/linked":
                    info.external_attr = 0o120777 << 16
                archive.writestr(info, b"bad")
            flow, service = self.workflow()
            with self.assertRaises(BundleError):
                flow.prepare_import(package, self.base / "new-data")
            self.assertFalse(service.events)
            self.assertFalse((self.base / "escape.txt").exists())
        with zipfile.ZipFile(package, "w") as archive:
            archive.write(self.code / "xgent_memory.db", "A/bot_memory.db")
            archive.write(self.code / "xgent_memory.db", "B/xgent_memory.db")
        with self.assertRaises(BundleError):
            prepare_compatible_import(
                package, DataPaths(self.code, self.base / "new-data")
            )

    def test_import_switches_config_restarts_and_preserves_code_and_original_data(self):
        instance(self.code, old=True)
        before = (self.code / "bot_memory.db").read_bytes()
        flow, service = self.workflow()
        target = self.base / "custom-data"
        staging, _ = flow.prepare_import(self.imported_bundle(), target)
        result = flow.activate_import(staging, target)
        self.assertEqual(target, discover_instance(self.code, {}).root)
        self.assertEqual(before, (self.code / "bot_memory.db").read_bytes())
        self.assertEqual(str(target), service.events[-1][1])
        self.assertTrue(result["backups"])
        self.assertTrue(self.code.is_dir())

    def test_failed_start_rolls_back_existing_target_and_previous_configuration(self):
        root = self.base / "active"
        instance(root)
        write_json(self.code / "data-location.json", {"data_dir": str(root)})
        (self.code / ".env").write_text(
            "XGENT_DATA_DIR=" + str(root) + "\nOTHER=keep\n", encoding="utf-8"
        )
        marker = (self.code / "data-location.json").read_bytes()
        env = (self.code / ".env").read_bytes()
        flow, service = self.workflow(fail_start=True)
        staging, _ = flow.prepare_import(self.imported_bundle(), root)
        original = (root / "xgent_memory.db").read_bytes()
        with self.assertRaises(BundleError):
            flow.activate_import(staging, root)
        self.assertEqual(original, (root / "xgent_memory.db").read_bytes())
        self.assertEqual(marker, (self.code / "data-location.json").read_bytes())
        self.assertEqual(env, (self.code / ".env").read_bytes())
        self.assertEqual(("start", str(root)), service.events[-1])
        self.assertTrue(service.running)
        self.assertTrue(list(self.base.glob("active.failed-*")))

    def test_failed_first_import_retains_failed_copy_but_does_not_activate_it(self):
        flow, _ = self.workflow(running=False, fail_start=True)
        target = self.base / "xgent-data"
        staging, _ = flow.prepare_import(self.imported_bundle(), target)
        with self.assertRaises(BundleError):
            flow.activate_import(staging, target)
        self.assertFalse(target.exists())
        self.assertFalse((self.code / "data-location.json").exists())
        self.assertTrue(list(self.base.glob("xgent-data.failed-*")))

    def test_configured_directory_is_shown_and_asked_before_choosing_new_one(self):
        root = self.base / "configured"
        instance(root)
        write_json(self.code / "data-location.json", {"data_dir": str(root)})
        flow, _ = self.workflow()
        answers = iter(["n"])
        questions = []
        menu = MigrationMenu(
            self.code,
            workflow=flow,
            report=self.messages.append,
            ask=lambda q: questions.append(q) or next(answers),
        )
        self.assertEqual(root, menu.choose_directory())
        self.assertEqual(1, len(questions))
        self.assertIn("是否修改", questions[0])
        self.assertTrue(any(str(root) in text for text in self.messages))
        answers = iter(["y", "2", str(self.base / "chosen")])
        self.assertEqual(self.base / "chosen", menu.choose_directory())
        self.assertEqual(
            str(root),
            json.loads((self.code / "data-location.json").read_text())["data_dir"],
        )

    def test_cancel_menu_never_calls_services_or_changes_configuration(self):
        instance(self.code, old=True)
        flow, service = self.workflow()
        answers = iter(["2", "n", "n"])
        menu = MigrationMenu(
            self.code,
            workflow=flow,
            report=self.messages.append,
            ask=lambda _: next(answers),
        )
        self.assertEqual(0, menu.run())
        self.assertFalse(service.events)
        self.assertFalse((self.code / "data-location.json").exists())
        self.assertIn("1) 导入", "\n".join(self.messages))
        self.assertIn("2) 导出", "\n".join(self.messages))

    def test_unrelated_directories_and_root_targets_are_refused(self):
        root = self.base / "unrelated"
        root.mkdir()
        (root / "keep").write_text("keep")
        for target in (
            root,
            self.code,
            self.code / "nested",
            self.base,
            Path(self.base.anchor),
        ):
            with self.assertRaises(BundleError):
                validate_target(self.code, target)

    def test_import_does_not_inject_receiver_secrets_or_assets_into_legacy_package(
        self,
    ):
        instance(self.code)
        asset = Path.home() / ".config/vps-manager/servers.json"
        asset.parent.mkdir(parents=True)
        asset.write_text('{"receiver":"private"}', encoding="utf-8")
        source = self.base / "no-env"
        instance(source, old=True)
        (source / ".env").unlink()
        staging, _ = prepare_compatible_import(
            source, DataPaths(self.code, self.base / "imported")
        )
        self.assertFalse((staging / ".env").exists())
        self.assertFalse((staging / "server_assets/vps-manager/servers.json").exists())
        shutil.rmtree(staging)

    def test_source_and_configured_paths_do_not_require_version_flags(self):
        root = self.base / "active"
        instance(root, old=True)
        (self.code / ".env").write_text(
            'export XGENT_DATA_DIR="' + str(root) + '"\n', encoding="utf-8"
        )
        self.assertEqual(root, discover_instance(self.code, {}).root)
        self.assertEqual(
            "bot_memory.db", discover_instance(self.code, {}).database.name
        )
        (self.code / ".env").write_text(
            "XGENT_DATA_DIR=relative-dir\n", encoding="utf-8"
        )
        with self.assertRaises(BundleError):
            discover_instance(self.code, {})

    def test_bare_data_command_opens_menu_and_cancellation_has_no_side_effects(self):
        environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(
            [sys.executable, "-m", "xgent_app.data_cli"],
            cwd=Path(__file__).resolve().parents[1],
            input="0\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            timeout=15,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("1) 导入", result.stdout)
        self.assertIn("2) 导出", result.stdout)
        self.assertNotIn("请选择版本", result.stdout)

    @unittest.skipUnless(os.name == "posix", "installer bootstrap runs through Bash")
    def test_new_installer_can_export_old_checkout_without_installed_helpers(self):
        workspace = Path(__file__).resolve().parents[1]
        old = self.base / "old-checkout"
        instance(old, old=True)
        shutil.copy2(workspace / "install.sh", old / "install.sh")
        archive = self.base / "helpers.zip"
        names = (
            "__init__.py",
            "data_migration.py",
            "data_service.py",
            "data_discovery.py",
            "data_bundles.py",
            "data_paths.py",
            "data_lock.py",
            "conversations.py",
        )
        with zipfile.ZipFile(archive, "w") as package:
            for name in names:
                package.write(
                    workspace / "xgent_app" / name, "release/xgent_app/" + name
                )
        interpreter = self.base / "helper-python"
        interpreter.write_text(
            "#!"
            + sys.executable
            + "\n"
            + textwrap.dedent("""\
            import io,sys,os,urllib.request
            from pathlib import Path
            urllib.request.urlopen=lambda *a,**k:io.BytesIO(Path(os.environ['FIXTURE_HELPER_ZIP']).read_bytes())
            arguments=sys.argv[1:]
            if arguments[0]=='-c':
                source=arguments[1];sys.argv=['-c',*arguments[2:]]
            elif arguments[0]=='-':
                source=sys.stdin.read();sys.argv=['-',*arguments[1:]]
            else:raise SystemExit('unexpected interpreter invocation')
            exec(compile(source,'<fixture>','exec'),{'__name__':'__main__'})
        """),
            encoding="utf-8",
        )
        interpreter.chmod(0o700)
        environment = {
            **os.environ,
            "PYTHONPATH": "",
            "XGENT_PYTHON": str(interpreter),
            "FIXTURE_HELPER_ZIP": str(archive),
            "XGENT_INSTALL_SH_LIB": "1",
        }
        environment.pop("XGENT_DATA_DIR", None)
        result = subprocess.run(
            ["bash", "-c", "source ./install.sh; manage_data_migration"],
            cwd=old,
            env=environment,
            input="2\nn\ny\n",
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("正在自动准备兼容工具", result.stdout)
        self.assertIn("导出完成", result.stdout)
        self.assertTrue((old / "bot_memory.db").is_file())
        self.assertFalse((old / "xgent_app").exists())
        backups = list((self.base / "xgent-backups").glob("*.zip"))
        self.assertEqual(1, len(backups))
        with zipfile.ZipFile(backups[0]) as saved:
            self.assertIn("xgent_memory.db", saved.namelist())
            self.assertIn("xgent_storage/uploads/test.bin", saved.namelist())

    def test_process_probe_never_sends_a_windows_termination_signal(self):
        self.assertTrue(process_alive(os.getpid()))
        self.assertFalse(process_alive(-1))

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0,
        "ownership preservation under sudo",
    )
    def test_import_preserves_instance_and_config_owner_under_root_maintenance(self):
        instance(self.code, old=True)
        os.chown(self.code, 10021, 10021)
        os.chown(self.code / "bot_memory.db", 10021, 10021)
        (self.code / ".env").write_text(
            "XGENT_DATA_DIR=" + str(self.code) + "\nBOT_TOKEN=keep\n", encoding="utf-8"
        )
        os.chown(self.code / ".env", 10021, 10021)
        flow, _ = self.workflow()
        target = self.base / "owned-data"
        staging, _ = flow.prepare_import(self.imported_bundle(), target)
        flow.activate_import(staging, target)
        self.assertEqual(10021, target.stat().st_uid)
        self.assertEqual(10021, (target / ".env").stat().st_uid)
        self.assertEqual(10021, (self.code / ".env").stat().st_uid)
        self.assertEqual(10021, (self.code / "data-location.json").stat().st_uid)

    def test_import_menu_runs_full_workflow_without_manual_commands(self):
        root = self.base / "configured"
        instance(root)
        write_json(self.code / "data-location.json", {"data_dir": str(root)})
        bundle = self.imported_bundle()
        flow, service = self.workflow()
        responses = iter(["1", str(bundle), "n", "y"])
        menu = MigrationMenu(
            self.code,
            workflow=flow,
            report=self.messages.append,
            ask=lambda _: next(responses),
        )
        self.assertEqual(0, menu.run())
        self.assertTrue(service.running)
        self.assertEqual(
            str(root),
            json.loads((self.code / "data-location.json").read_text())["data_dir"],
        )
        self.assertIn("导入完成", "\n".join(self.messages))
        self.assertNotIn("fixture-secret", "\n".join(self.messages))

    @unittest.skipUnless(
        os.name == "posix" and Path("/proc").is_dir(), "Linux process lifecycle test"
    )
    def test_real_local_process_export_restore_and_directory_change(self):
        instance(self.code, old=True)
        (self.code / "xgent_app/data_paths.py").touch()
        script = self.code / "xgent_server.py"
        script.write_text(
            textwrap.dedent("""\
            import os,json,time,signal
            from pathlib import Path
            root=Path(os.environ.get('XGENT_DATA_DIR') or Path(__file__).parent)
            root.mkdir(exist_ok=True)
            (root/'xgent.pid').write_text(str(os.getpid()))
            (root/'runtime-ready.json').write_text(json.dumps({'pid':os.getpid(),'at':time.time(),'data_dir':str(root),'web_healthy':True}))
            stop=False
            def finish(*args):
                global stop
                stop=True
            signal.signal(signal.SIGINT,finish)
            signal.signal(signal.SIGTERM,finish)
            while not stop:time.sleep(.05)
        """),
            encoding="utf-8",
        )
        installer = self.code / "install.sh"
        installer.write_text(
            '#!/bin/bash\nset -eu\ntest "$1" = data-start-internal\nexport XGENT_DATA_DIR="$XGENT_MAINTENANCE_DATA_DIR"\nnohup '
            + sys.executable
            + ' "'
            + str(script)
            + '" > "$XGENT_DATA_DIR/test-runtime.txt" 2>&1 &\necho $! > "$XGENT_DATA_DIR/xgent.pid"\n',
            encoding="utf-8",
        )
        target = self.base / "chosen-data"
        service = InstallerService(self.code)

        def cleanup():
            for root in (self.code, target):
                marker = root / "xgent.pid"
                if marker.is_file():
                    pid = int(marker.read_text())
                    if process_alive(pid) and service._same_checkout(pid):
                        import signal

                        os.kill(pid, signal.SIGTERM)
            time.sleep(0.15)

        self.addCleanup(cleanup)
        initial = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=self.code,
            env={**os.environ, "XGENT_DATA_DIR": str(self.code)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: initial.wait(timeout=5))
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while not (self.code / "runtime-ready.json").is_file():
            if time.monotonic() > deadline:
                self.fail("test process did not start")
            time.sleep(0.05)
        flow = MigrationWorkflow(
            self.code, service=service, report=self.messages.append, environ={}
        )
        bundle = flow.export(outputs=True)
        self.assertTrue(Path(bundle["path"]).is_file())
        self.assertTrue(
            service.inspect(DiscoveredPaths(self.code, self.code, True)).running
        )
        staging, _ = flow.prepare_import(self.imported_bundle(), target)
        flow.activate_import(staging, target)
        self.assertTrue(service.inspect(DataPaths(self.code, target)).running)
        self.assertEqual(target, discover_instance(self.code, {}).root)
        self.assertEqual(
            str(target),
            json.loads((target / "runtime-ready.json").read_text())["data_dir"],
        )

    def test_local_api_preserves_original_and_does_not_pull_or_delete_containers(self):
        service = InstallerService(self.code)
        calls = []
        service.command = lambda args, **_: calls.append(args) or ""
        config = {
            "State": {"Running": True},
            "Image": "sha256:existing-image",
            "HostConfig": {
                "RestartPolicy": {"Name": "unless-stopped"},
                "PortBindings": {
                    "8081/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8081"}]
                },
            },
            "Config": {"Env": ["TELEGRAM_API_HASH=fixture-private"], "Cmd": ["server"]},
        }
        api = LocalApiMove(service, DiscoveredPaths(self.code, self.code), config)
        api.pause()
        api.switch(self.base / "data")
        api.rollback()
        self.assertFalse(any("rm" in args or "pull" in args for args in calls))
        self.assertTrue(
            any(
                args[:2] == ["docker", "rename"] and api.saved_name in args
                for args in calls
            )
        )
        self.assertTrue(
            any(
                str(self.base / "data/.local-api-data") in " ".join(args)
                for args in calls
            )
        )
