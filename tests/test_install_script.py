import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = (ROOT / "install.sh").read_text(encoding="utf-8")


class InstallScriptMigrationTests(unittest.TestCase):
    def test_launchers_force_canonical_entrypoint(self):
        self.assertRegex(
            INSTALL_SCRIPT,
            r'XGENT_APP_ENTRY="\$APP_ENTRY" TELEGRAM_AI_BOT_APP_ENTRY="" \\\s+run_bot_python python',
        )
        self.assertRegex(
            INSTALL_SCRIPT,
            r'XGENT_APP_ENTRY="\$APP_ENTRY" TELEGRAM_AI_BOT_APP_ENTRY="" \\\s+run_bot_python pm2 start',
        )

    def test_pm2_restart_overwrites_legacy_entrypoint_environment(self):
        self.assertIn(
            'TELEGRAM_AI_BOT_APP_ENTRY="" XGENT_APP_ENTRY="$APP_ENTRY"',
            INSTALL_SCRIPT,
        )

    def test_pm2_install_recovers_nonstandard_npm_global_bin(self):
        self.assertIn('npm prefix -g', INSTALL_SCRIPT)
        self.assertIn('npm root -g', INSTALL_SCRIPT)

        ensure_pm2 = INSTALL_SCRIPT[
            INSTALL_SCRIPT.index('ensure_pm2() {'):
            INSTALL_SCRIPT.index('setup_pm2_startup() {')
        ]
        self.assertGreaterEqual(
            ensure_pm2.count('refresh_npm_global_bin_path'),
            2,
            'PATH should be refreshed both before and after npm installs PM2',
        )

    @unittest.skipUnless(shutil.which("bash"), "bash is required")
    def test_npm_prefix_bin_is_added_to_path_and_pm2_is_discovered(self):
        helpers = INSTALL_SCRIPT[
            INSTALL_SCRIPT.index('prepend_path_dir() {'):
            INSTALL_SCRIPT.index('ensure_pm2() {')
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mock_bin = root / "mock-bin"
            npm_prefix = root / "user-packages" / "node"
            pm2_bin = npm_prefix / "bin" / "pm2"
            mock_bin.mkdir()
            pm2_bin.parent.mkdir(parents=True)

            npm = mock_bin / "npm"
            npm.write_text(
                """#!/bin/sh
case "$1 $2" in
  'prefix -g') printf '%s\n' "$MOCK_NPM_PREFIX" ;;
  'root -g') printf '%s\n' "$MOCK_NPM_PREFIX/lib/node_modules" ;;
  *) exit 1 ;;
esac
""",
                encoding="utf-8",
            )
            pm2_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            npm.chmod(0o755)
            pm2_bin.chmod(0o755)

            script = f'''set -eu
command_exists() {{ command -v "$1" >/dev/null 2>&1; }}
run_privileged() {{ "$@"; }}
success() {{ :; }}
warn() {{ :; }}
{helpers}
before="$(command -v pm2 2>/dev/null || true)"
refresh_npm_global_bin_path
after="$(command -v pm2)"
printf 'before=%s\nafter=%s\npath=%s\n' "$before" "$after" "$PATH"
'''
            env = os.environ.copy()
            env["MOCK_NPM_PREFIX"] = str(npm_prefix)
            env["PATH"] = f"{mock_bin}:/usr/bin:/bin"
            result = subprocess.run(
                ["bash", "-c", script],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            values = dict(line.split("=", 1) for line in result.stdout.splitlines())
            self.assertEqual("", values["before"])
            self.assertEqual(str(pm2_bin), values["after"])
            self.assertTrue(values["path"].startswith(f"{npm_prefix / 'bin'}:"))

    @unittest.skipUnless(shutil.which("bash"), "bash is required")
    def test_nonstandard_pm2_gets_persistent_command_link(self):
        helpers = INSTALL_SCRIPT[
            INSTALL_SCRIPT.index('prepend_path_dir() {'):
            INSTALL_SCRIPT.index('ensure_pm2() {')
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pm2_bin = root / "npm-prefix" / "bin" / "pm2"
            link_dir = root / "standard-bin"
            pm2_bin.parent.mkdir(parents=True)
            link_dir.mkdir()
            pm2_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            pm2_bin.chmod(0o755)

            script = f'''set -eu
command_exists() {{ command -v "$1" >/dev/null 2>&1; }}
run_privileged() {{ "$@"; }}
success() {{ :; }}
warn() {{ :; }}
{helpers}
persist_pm2_command
'''
            env = os.environ.copy()
            env["XGENT_PM2_LINK_DIR"] = str(link_dir)
            env["PATH"] = f"{pm2_bin.parent}:{link_dir}:/usr/bin:/bin"
            subprocess.run(
                ["bash", "-c", script],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            command_link = link_dir / "pm2"
            self.assertTrue(command_link.is_symlink())
            self.assertEqual(pm2_bin.resolve(), command_link.resolve())

    def test_child_processes_do_not_keep_legacy_entrypoint_variable(self):
        self.assertGreaterEqual(
            INSTALL_SCRIPT.count('-u TELEGRAM_AI_BOT_APP_ENTRY'),
            6,
        )
        self.assertIn(
            'unset XGENT_APP_ENTRY TELEGRAM_AI_BOT_APP_ENTRY || true',
            INSTALL_SCRIPT,
        )


if __name__ == "__main__":
    unittest.main()
