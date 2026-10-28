import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xgent_app.data_restore import main


class DataRestoreTests(unittest.TestCase):
    def test_web_restore_creates_named_pre_import_backup_before_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            code = Path(directory) / "project"
            code.mkdir()
            (code / "install.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            staging = code.parent / ".xgent-restore-fixture"
            staging.mkdir()
            job = code / "job.json"
            job.write_text(json.dumps({"staging": str(staging)}), encoding="utf-8")
            calls = []

            class Bundles:
                def __init__(self, paths):
                    self.paths = paths

                def export(self, **options):
                    calls.append(("export", options))
                    return {"path": str(code.parent / "xgent-backups/pre-import.zip")}

                def activate(self, incoming):
                    calls.append(("activate", str(incoming)))
                    rollback = code / ".install-state/restore-rollback/fixture"
                    rollback.mkdir(parents=True)
                    return {"rollback_dir": str(rollback), "data_dir": str(code)}

                def rollback_activation(self, result):
                    raise AssertionError("successful restore must not roll back")

            def service(arguments, **options):
                action = arguments[-1]
                calls.append(("service", action))
                if action == "start":
                    (code / "runtime-ready.json").write_text(
                        json.dumps(
                            {
                                "at": time.time() + 5,
                                "data_dir": str(code),
                                "web_healthy": True,
                            }
                        ),
                        encoding="utf-8",
                    )
                return SimpleNamespace(returncode=0)

            def save(path, value):
                Path(path).write_text(json.dumps(value), encoding="utf-8")

            with (
                patch("xgent_app.data_restore.DataBundles", Bundles),
                patch("xgent_app.data_restore.subprocess.run", side_effect=service),
                patch("xgent_app.data_restore.write_json", side_effect=save),
                patch("xgent_app.data_restore.time.sleep"),
            ):
                self.assertEqual(0, main(job, code, code))

            saved = json.loads(job.read_text(encoding="utf-8"))
            self.assertEqual("completed", saved["state"])
            self.assertEqual(
                "pre-import",
                calls[1][1]["name_prefix"],
            )
            self.assertTrue(calls[1][1]["_maintenance_locked"])
            self.assertTrue(calls[1][1]["include_prompts"])
            self.assertEqual("activate", calls[2][0])
            self.assertEqual(
                str(code.parent / "xgent-backups/pre-import.zip"),
                saved["result"]["pre_import_backup"]["path"],
            )
            self.assertIsNone(saved["result"]["rollback_dir"])


if __name__ == "__main__":
    unittest.main()
