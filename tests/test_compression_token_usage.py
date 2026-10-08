"""Compression usage must be both persisted and delivered through the active bot."""
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.compression_request_probe import CompressionHarness
from tests.test_thinking_params import run_in_app
from xgent_app.web_bridge import WebMessage, build_web_command_objects


USAGE = {'input_tokens': 1200, 'output_tokens': 90, 'cached_tokens': 300,
         'reasoning_tokens': 10, 'visible_output_tokens': 80, 'total_tokens': 1290}
SUMMARY = 'COMPLETE-COMPRESSION-SUMMARY'


class TelegramProbe:
    def __init__(self, database, *, fail_tokens=False, fail_summary=False):
        self.database = database
        self.fail_tokens = fail_tokens
        self.fail_summary = fail_summary
        self.sent = []
        self.persisted_before_tokens = False
        self.token_attempts = 0
        self.serial = 0

    def message(self, text=''):
        self.serial += 1
        return WebMessage(self, self.serial, 1, text)

    async def send_message(self, chat_id, text, **kwargs):
        if '↑ 1200 tokens' in text:
            self.token_attempts += 1
            records = await self.database.get_global_messages(1000)
            self.persisted_before_tokens = any(row['msg_type'] == 'token_usage' for row in records)
            if self.fail_tokens:
                raise OSError('Telegram token delivery unavailable')
        if self.fail_summary and SUMMARY in text:
            raise OSError('Telegram summary delivery unavailable')
        serializable = {key: value.to_dict() if hasattr(value, 'to_dict') else value
                        for key, value in kwargs.items()}
        self.sent.append({'text': text, 'chat_id': chat_id, **serializable})
        return self.message(text)

    async def edit_message_text(self, text, **kwargs):
        return self.message(text)

    async def edit_message_reply_markup(self, **kwargs):
        return True

    async def delete_message(self, **kwargs):
        return True

    async def send_chat_action(self, **kwargs):
        return True

    async def send_document(self, **kwargs):
        return self.message()

    async def send_photo(self, **kwargs):
        return self.message()


async def compression_usage_probe(bot, directory, surface='telegram', outcome='success', callback=False):
    async with CompressionHarness(bot, directory) as h:
        await bot.GlobalRecorder.record_user_message('原始待压缩内容')
        telegram = TelegramProbe(h.db, fail_tokens=outcome == 'token_failure',
                                fail_summary=outcome == 'summary_failure')
        if surface == 'web':
            update, context, bridge = build_web_command_objects(1, h.outbox, '/compress', telegram)
        else:
            update = SimpleNamespace(message=telegram.message('/compress'), callback_query=None,
                                     effective_chat=SimpleNamespace(id=1))
            if callback:
                update.callback_query = SimpleNamespace(message=update.message)
                update.message = None
            context = SimpleNamespace(bot=telegram)
            bridge = None
        original = bot.current_scope().conversation_id
        target = None

        async def generate(provider, data, model, history, stop_event, usage_sink):
            nonlocal target
            if outcome != 'no_usage':
                usage_sink.append(dict(USAGE))
            if outcome == 'failed':
                raise bot.CompressionError('模型生成失败，已有实际用量')
            if outcome == 'stopped':
                stop_event.set()
                raise bot.CompressionError('用户已停止压缩')
            if outcome == 'cleared':
                await h.db.clear_all_conversation_memory()
            if outcome == 'switched':
                target = (await bot.get_conversations().manage('create', name='B'))['conversation_id']
            return SUMMARY

        try:
            with patch.object(bot, 'is_web_chat_running', return_value=surface != 'telegram'), \
                 patch.object(bot, 'get_web_real_bot', return_value=telegram), \
                 patch.object(bot, 'generate_compression_summary', generate):
                await bot.cmd_compress(update, context)
            if bridge is not None:
                assert await bridge.flush_telegram(timeout=5)
            rows = await h.db.get_global_messages(1000)
            tokens = [row for row in rows if row['msg_type'] == 'token_usage']
            frames = h.drain_frames()
            live = [frame for frame in frames if '↑ 1200 tokens' in str(frame.get('text', ''))]
            sent = [item for item in telegram.sent if '↑ 1200 tokens' in item['text']]
            summaries = [index for index, item in enumerate(telegram.sent) if SUMMARY in item['text']]
            token_positions = [index for index, item in enumerate(telegram.sent) if '↑ 1200 tokens' in item['text']]
            all_history = await h.db.get_display_history(1000)
            model_history = await h.db.get_conversation_messages(1000)
            stats = await h.db.get_token_stats()
            entry = await h.db.get_latest_compression()
            return {
                'sent': sent, 'attempts': telegram.token_attempts,
                'recorded': tokens, 'stat_count': len(stats), 'stats': stats,
                'persisted_before_send': telegram.persisted_before_tokens,
                'live': live,
                'display_count': sum('↑ 1200 tokens' in str(row.get('content', '')) for row in all_history),
                'model_has_tokens': '↑ 1200 tokens' in str(model_history),
                'after_summary': bool(summaries and token_positions and max(summaries) < min(token_positions)),
                'completed': bool(entry and entry['status'] == 'completed'),
                'busy': bot._is_processing or bot._compression_running or bot.get_conversations().lock.busy(),
                'state_events': [frame for frame in frames if frame['type'] == 'compression_state'],
                'original': original, 'selected': (await bot.get_conversations().state())['current_chat_id'],
                'switched_to': target,
            }
        finally:
            if bridge is not None and bridge._local_channel is not None:
                await bridge._local_channel.aclose()


class CompressionTokenUsageTests(unittest.TestCase):
    def probe(self, surface='telegram', outcome='success', callback=False):
        with tempfile.TemporaryDirectory() as directory:
            return run_in_app(
                'import asyncio\nfrom tests.test_compression_token_usage import compression_usage_probe\n'
                f'print(json.dumps(asyncio.run(compression_usage_probe(bot, {directory!r}, '
                f'{surface!r}, {outcome!r}, {callback!r}))))'
            )

    def assert_usage(self, result):
        self.assertEqual(1, len(result['sent']))
        self.assertEqual(1, len(result['recorded']))
        self.assertEqual(1, result['stat_count'])
        self.assertEqual(1, result['display_count'])
        self.assertFalse(result['model_has_tokens'])
        self.assertFalse(result['busy'])
        sent = result['sent'][0]
        self.assertTrue(sent['disable_notification'])
        self.assertEqual('HTML', sent['parse_mode'])
        self.assertIn('↑ 1200 tokens (300 cached)', sent['text'])
        self.assertIn('↓ 90 tokens (80 text + 10 thoughts)', sent['text'])
        self.assertIn('tokens/s', sent['text'])
        self.assertIn(result['recorded'][0]['content'], sent['text'])
        self.assertTrue(result['persisted_before_send'])

    def test_telegram_command_sends_tokens_after_summary(self):
        result = self.probe()
        self.assert_usage(result)
        self.assertTrue(result['completed'] and result['after_summary'])

    def test_telegram_button_mirrors_tokens_to_web_once(self):
        result = self.probe(surface='telegram_web', callback=True)
        self.assert_usage(result)
        self.assertTrue(result['completed'] and result['after_summary'])
        self.assertEqual(1, len(result['live']))
        self.assertEqual(result['original'], result['live'][0]['conversation_id'])

    def test_web_compression_mirrors_tokens_to_telegram_once(self):
        result = self.probe(surface='web')
        self.assert_usage(result)
        self.assertEqual(1, len(result['live']))
        self.assertTrue(result['completed'] and result['after_summary'])

    def test_token_delivery_failure_does_not_rollback_summary_or_stats(self):
        result = self.probe(outcome='token_failure')
        self.assertEqual(1, result['attempts'])
        self.assertEqual([], result['sent'])
        self.assertEqual(1, len(result['recorded']))
        self.assertEqual(1, result['stat_count'])
        self.assertTrue(result['completed'])
        self.assertFalse(result['busy'])
        self.assertFalse(result['state_events'][-1]['busy'])

    def test_summary_delivery_failure_does_not_skip_token_notice(self):
        result = self.probe(outcome='summary_failure')
        self.assert_usage(result)
        self.assertTrue(result['completed'])

    def test_failure_and_stop_still_report_actual_returned_usage(self):
        for outcome in ('failed', 'stopped'):
            with self.subTest(outcome=outcome):
                result = self.probe(outcome=outcome)
                self.assert_usage(result)
                self.assertFalse(result['completed'])

    def test_missing_usage_and_cleared_generation_do_not_emit_stale_notice(self):
        for outcome in ('no_usage', 'cleared'):
            with self.subTest(outcome=outcome):
                result = self.probe(outcome=outcome)
                self.assertEqual([], result['sent'])
                self.assertEqual([], result['recorded'])
                self.assertEqual(0, result['stat_count'])
                self.assertFalse(result['busy'])

    def test_switch_keeps_usage_bound_to_original_conversation(self):
        result = self.probe(surface='telegram_web', outcome='switched')
        self.assert_usage(result)
        self.assertNotEqual(result['original'], result['selected'])
        self.assertEqual(result['switched_to'], result['selected'])
        self.assertEqual(result['original'], result['live'][0]['conversation_id'])
        self.assertEqual(result['original'], result['recorded'][0]['session_id'])
