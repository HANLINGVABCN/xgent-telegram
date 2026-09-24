import unittest

from tests.test_external_sync import SectionsProbeMixin


class CompressionEventTests(SectionsProbeMixin, unittest.TestCase):
    def test_local_and_cli_relay_preserve_reset_and_busy_sequence(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio
from types import SimpleNamespace
from xgent_app import cli_bridge

async def main():
    await ns['UserDataManager'].init()
    db = await ns['BotMemoryDB'].get_instance()
    local, remote = ns['WebOutbox'](), ns['WebOutbox']()
    local_sub, remote_sub = local.subscribe(), remote.subscribe()
    ns['get_web_outbox'] = lambda: local
    ns['_web_external_outbox'] = remote
    cli_bridge.configure_relay(ns['BotConfig'].DB_FILE, 1, 'compression-events')
    context = SimpleNamespace(bot=cli_bridge.CliBot(1))
    expected = [
        {'type': 'compression_state', 'busy': True}, {'type': 'history_reset'},
        {'type': 'compression_state', 'busy': False, 'committed': True},
    ]
    for frame in expected:
        ns['publish_conversation_event'](context, frame)
    cli_bridge.relay_conversation_event({'type': 'unapproved-frame'})
    cli_bridge.close_relay()
    operations = await db.fetch_relay_ops(0)
    assert len(operations) == 3
    for operation in operations:
        await ns['_replay_relay_op'](None, operation['op'], operation['payload'])
    await ns['_replay_relay_op'](None, 'conversation_event', {'frame': {'type': 'unapproved-frame'}})
    assert [local_sub.get(timeout=0.1) for _ in expected] == expected
    assert [remote_sub.get(timeout=0.1) for _ in expected] == expected
    assert remote_sub.get(timeout=0.01) is None
    local_sub.close()
    remote_sub.close()
    await db.close()
    print(json.dumps({'local': True, 'cli': True, 'order': True, 'filter': True}))

asyncio.run(main())
''')
        self.assertTrue(all(result.values()))


if __name__ == '__main__':
    unittest.main()
