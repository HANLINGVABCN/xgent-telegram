import tempfile
import unittest

from tests.test_thinking_params import run_in_app
from xgent_app.ui_history import hide_audit_record


def probe(name, root):
    return run_in_app('import asyncio\nfrom tests import ui_history_probe as probe\n'
                      f'print(json.dumps(asyncio.run(probe.{name}(bot, {root!r}))))')


class UiHistoryTests(unittest.TestCase):
    def test_menu_navigation_and_restart(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertTrue(all(probe('navigation', root).values()))
            self.assertTrue(all(probe('restart', root).values()))

    def test_stale_forged_deleted_objects_and_concurrent_clicks(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertTrue(all(probe('validation', root).values()))

    def test_edits_deletion_clear_export_and_limits(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertTrue(all(probe('mutations', root).values()))

    def test_ui_write_failure_reports_without_reverting_settings(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertTrue(all(probe('persistence_failure', root).values()))

    def test_cli_and_telegram_bridges(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertTrue(all(probe('bridges', root).values()))

    def test_legacy_audits_do_not_hide_user_text_or_real_system_results(self):
        for record in (
            {'msg_type': 'button_click', 'content': 'anything'},
            {'msg_type': 'system_op', 'content': '\u542f\u52a8\u673a\u5668\u4eba', 'metadata': {'command': '/start'}},
            {'msg_type': 'system_op', 'content': 'Skill \u8bbe\u7f6e\u5df2\u66f4\u65b0: fixture',
             'metadata': {'disabled_skills': []}},
        ):
            self.assertTrue(hide_audit_record(record), record)
        for record in (
            {'msg_type': 'user_text', 'content': '\u70b9\u51fb\u6309\u94ae: menu_skills'},
            {'msg_type': 'command', 'content': '/start'},
            {'msg_type': 'system_op', 'content': 'failure'},
            {'msg_type': 'system_op', 'content': 'export', 'metadata': {'display_media': [{}], 'ui_audit': True}},
            {'msg_type': 'system_op', 'content': 'retry', 'metadata': {'compression_task': {'id': 'a'}}},
        ):
            self.assertFalse(hide_audit_record(record), record)
