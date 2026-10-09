import tempfile
import unittest
from tests.test_thinking_params import run_in_app


class ConversationControlsV2Tests(unittest.TestCase):
    def probe(self,name):
        with tempfile.TemporaryDirectory() as root:
            result=run_in_app('import asyncio\nfrom tests import conversation_controls_v2_probe as p\n'
                              f'print(json.dumps(asyncio.run(p.{name}(bot, {root!r}))))')
            self.assertTrue(all(result.values()))

    def test_rename_and_navigation(self):self.probe('rename_and_navigation')
    def test_auto_title_publishes_before_model(self):self.probe('title_event')
    def test_lightweight_telegram_parts(self):self.probe('telegram_parts')

    def test_delete_waits_for_scheduler_owner_and_recovers(self):self.probe('delete_running_and_recover')
    def test_delete_stops_its_real_command(self):self.probe('delete_stops_command')
