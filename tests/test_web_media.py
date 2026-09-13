"""Generated-media presentation stays complete and grouped across transports."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from telegram.ext import ExtBot

from tests.test_external_sync import SectionsProbeMixin
from xgent_app.cli_bridge import _CliRelay
from xgent_app.web_bridge import (
    MEDIA_TOKEN_REGISTRY, MirrorBot, WebBot, WebOutbox, install_tg_to_web_mirror,
)
from xgent_app.web_history import build_history_message, display_media_reference
from xgent_app.web_media import (
    build_media_presentation, current_media_presentation, generated_media_group_id,
    media_presentation_scope,
)


class WebMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.artifacts = []
        for number, color in enumerate(('red', 'green')):
            path = self.root / f'image {number}.png'
            Image.new('RGB', (24, 16), color).save(path)
            self.artifacts.append({'path': str(path), 'mime_type': 'image/png'})
        self.text = '# Complete reply\n\n' + ('All details are retained. ' * 200) + 'TEXT END\n\n'
        self.text += '\n'.join(item['path'] for item in self.artifacts)
        self.presentation = build_media_presentation(self.text, self.artifacts, replace_message_ids=[42, 43])

    def history(self, kind='ai_reply'):
        return build_history_message({
            'id': 8, 'timestamp': 123, 'role': 'assistant', 'msg_type': kind,
            'content': self.text,
            'metadata': {'display_media': [display_media_reference(item['path']) for item in self.artifacts]},
        }, self.root, self.root)

    def assert_frame(self, frame):
        self.assertEqual('message', frame['type'])
        self.assertEqual(self.text, frame['text'])
        self.assertEqual([42, 43], frame['replace_message_ids'])
        self.assertEqual(self.history()['media_group_id'], frame['media_group_id'])
        self.assertEqual([item['path'] for item in self.artifacts], [item['path'] for item in frame['media']])
        self.assertNotIn('base64', json.dumps(frame))
        for item in frame['media']:
            original = MEDIA_TOKEN_REGISTRY.resolve(item['download_url'].rsplit('/', 1)[1])
            self.assertEqual(Path(item['path']).read_bytes(), Path(original[0]).read_bytes())

    async def test_web_and_mirror_group_all_originals_with_full_text(self):
        for bot_class in (WebBot, MirrorBot):
            with self.subTest(bot=bot_class.__name__):
                outbox = WebOutbox()
                bot = bot_class(outbox, 1)
                with outbox.subscribe() as stream:
                    with media_presentation_scope(self.presentation):
                        with open(self.artifacts[0]['path'], 'rb') as photo:
                            await bot.send_photo(1, photo, caption='Truncated Telegram caption')
                        with open(self.artifacts[1]['path'], 'rb') as document:
                            await bot.send_document(1, document, caption='Only second image path')
                    self.assert_frame(stream.get(1))
                    self.assert_frame(stream.get(1))
                    await bot.send_message(1, 'Unrelated reply')
                    self.assertNotIn('media_group_id', stream.get(1))

    async def test_telegram_mirror_keeps_native_caption_and_groups_web(self):
        calls = []

        async def send_photo(_bot, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message_id=71)

        outbox = WebOutbox()
        with patch.object(ExtBot, 'send_photo', send_photo):
            bot = ExtBot('1:TEST')
            with outbox.subscribe() as stream:
                restore = install_tg_to_web_mirror(bot, outbox)
                try:
                    with media_presentation_scope(self.presentation):
                        with open(self.artifacts[0]['path'], 'rb') as photo:
                            await bot.send_photo(chat_id=1, photo=photo, caption='Native caption')
                    self.assert_frame(stream.get(1))
                finally:
                    restore()
        self.assertEqual('Native caption', calls[0]['caption'])
        self.assertNotIn('media_presentation', calls[0])

    async def test_same_caption_or_path_without_scope_does_not_merge(self):
        outbox = WebOutbox()
        with outbox.subscribe() as stream:
            with open(self.artifacts[0]['path'], 'rb') as photo:
                await WebBot(outbox, 1).send_photo(1, photo, caption=self.text)
            frame = stream.get(1)
            self.assertEqual('photo', frame['type'])
            self.assertNotIn('media_group_id', frame)

    async def test_missing_original_is_reported_inside_group(self):
        Path(self.artifacts[1]['path']).unlink()
        outbox = WebOutbox()
        with outbox.subscribe() as stream:
            with media_presentation_scope(self.presentation):
                with open(self.artifacts[0]['path'], 'rb') as photo:
                    await WebBot(outbox, 1).send_photo(1, photo)
            frame = stream.get(1)
        self.assertEqual(2, len(frame['media']))
        self.assertIn('error', frame['media'][1])
        self.assertNotIn('download_url', frame['media'][1])
        self.assertIn('error', self.history()['media'][1])
        self.assertEqual(self.history()['media_group_id'], frame['media_group_id'])

    async def test_cli_relay_preserves_group_without_process_local_tokens(self):
        relay = _CliRelay()
        relay._enabled = True
        with media_presentation_scope(self.presentation):
            relay.emit('send_photo', message_id=91, path=self.artifacts[0]['path'], caption='native')
        operation, payload = relay._queue.get_nowait()
        self.assertEqual('send_photo', operation)
        self.assertEqual(self.presentation, payload['media_presentation'])
        self.assertNotIn('download_url', json.dumps(payload))
        self.assertNotIn('base64', json.dumps(payload))
        relay.emit('send_photo', path=self.artifacts[0]['path'])
        self.assertNotIn('media_presentation', relay._queue.get_nowait()[1])

    async def test_scopes_do_not_leak_to_later_worker_deliveries(self):
        ready = asyncio.Event()

        async def inherited_worker():
            await ready.wait()
            return current_media_presentation()

        with media_presentation_scope(self.presentation):
            self.assertEqual(self.presentation, current_media_presentation())
            worker = asyncio.create_task(inherited_worker())
            with media_presentation_scope(None):
                self.assertIsNone(current_media_presentation())
            self.assertEqual(self.presentation, current_media_presentation())
        ready.set()
        self.assertIsNone(await worker)
        self.assertIsNone(current_media_presentation())
        with self.assertRaises(asyncio.CancelledError):
            with media_presentation_scope(self.presentation):
                raise asyncio.CancelledError
        self.assertIsNone(current_media_presentation())

    async def test_history_ids_preserve_order_and_external_media_groups(self):
        self.assertEqual(self.history()['media_group_id'], self.history('media_reply')['media_group_id'])
        reverse = generated_media_group_id([item['path'] for item in reversed(self.artifacts)])
        self.assertNotEqual(self.presentation['media_group_id'], reverse)
        unique = build_media_presentation(self.text, [*self.artifacts, self.artifacts[0]])
        self.assertEqual(self.presentation['media_group_id'], unique['media_group_id'])
        self.assertEqual(2, len(unique['media']))


class WebMediaServiceTests(SectionsProbeMixin, unittest.TestCase):
    def test_generated_delivery_and_cli_replay_share_full_presentation(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio
from pathlib import Path
from types import SimpleNamespace
from PIL import Image
from xgent_app.web_bridge import WebBot, WebOutbox, MirrorBot
from xgent_app.web_media import build_media_presentation

async def main():
    await ns['UserDataManager'].init()
    artifacts = []
    for index in range(2):
        path = Path(f'generated {index}.png').resolve()
        Image.new('RGB', (8, 8), 'red').save(path)
        artifacts.append({'path': str(path), 'mime_type': 'image/png'})
    text = 'DETAIL ' * 1000 + 'FINAL DETAIL'
    notice = ns['build_generated_media_reply_text'](text, artifacts)
    outbox = WebOutbox()
    with outbox.subscribe() as stream:
        await ns['send_generated_media_artifacts'](
            SimpleNamespace(bot=WebBot(outbox, 1)), 1, artifacts, caption=notice,
        )
        frames = [stream.get(1), stream.get(1)]
        assert all(frame['text'] == notice and len(frame['media']) == 2 for frame in frames)
        assert frames[0]['media_group_id'] == frames[1]['media_group_id']
        assert all(len(frame['caption']) <= 1024 for frame in frames)
        await ns['_replay_relay_op'](MirrorBot(outbox, 1), 'send_photo', {
            'message_id': 105, 'path': artifacts[0]['path'], 'caption': 'Native caption',
            'media_presentation': build_media_presentation(notice, artifacts),
        })
        replay = stream.get(1)
        assert replay['media_group_id'] == frames[0]['media_group_id']
        assert replay['text'] == notice and len(replay['media']) == 2
    await (await ns['BotMemoryDB'].get_instance()).close()
    print(json.dumps({'delivery': True, 'long_text': True, 'cli_replay': True}))

asyncio.run(main())
''')
        self.assertTrue(all(result.values()))


if __name__ == '__main__':
    unittest.main()
