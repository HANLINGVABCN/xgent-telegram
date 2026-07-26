from pathlib import Path
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
