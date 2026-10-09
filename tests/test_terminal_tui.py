"""End-to-end terminal conversation and draft checks, not a mock dispatch-only UI."""
import unittest
from tests.test_cli_input import CliProbeMixin


class TerminalTuiTests(CliProbeMixin, unittest.TestCase):
    def probe(self, scenario):
        result = self.run_probe('import asyncio\nfrom tests.terminal_tui_probe import actual_tui\n'
                               + f'print(json.dumps(asyncio.run(actual_tui(xgent_cli, {scenario!r}))))')
        self.assertTrue(result['passed'])

    def test_web_bot_do_not_steal_tui_and_background_reply_is_isolated(self):
        self.probe('routing')

    def test_stale_context_keeps_draft_and_requires_confirmation(self):
        self.probe('stale')

    def test_real_conversation_panel_navigation_keeps_drafts(self):
        self.probe('panel')

    def test_terminal_visual_layout(self):
        self.probe('visual')

    def test_real_panel_rename_reset_archive_and_delete(self):
        self.probe('management')

    def test_explicit_tui_start_flags(self):
        result = self.run_probe("""
import os
xgent_cli._configure_frontend_args(['--tui'])
a = os.environ.get('XGENT_CLI_TUI') == '1' and not os.environ.get('XGENT_CLI_NO_TUI')
xgent_cli._configure_frontend_args(['--no-tui'])
b = os.environ.get('XGENT_CLI_NO_TUI') == '1'
print(json.dumps({'tui':a,'line':b}))
""")
        self.assertEqual({'tui':True,'line':True}, result)

    def test_failed_persistence_does_not_lose_draft_or_call_model(self):
        self.probe('write_failure')
