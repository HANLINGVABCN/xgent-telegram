"""Telegram stale-button acknowledgements and named command/menu cards."""
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.attachment_request_probe import Harness
from tests.test_compression_token_usage import TelegramProbe
from tests.test_thinking_params import run_in_app
from xgent_app.conversations import (
    ConversationChanged, ConversationScope, bind_conversation, conversation_labelled_text,
)
from xgent_app.web_bridge import build_web_command_objects


STALE_TEXT = '这个按钮属于另一会话或旧上下文，请切回原会话并重新打开菜单。'


async def stale_callback_probe(bot, directory, mode):
    async with Harness(bot, directory) as h:
        manager = bot.get_conversations()
        original = await manager.resolve()
        if mode == 'clear':
            await h.db.clear_all_conversation_memory()
        elif mode != 'valid':
            await manager.manage('create', name='B')
        selected = await manager.resolve()
        query = SimpleNamespace(data=f'cv:{original.generation}:act_main_menu',
                                answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        update = SimpleNamespace(callback_query=query, message=None,
                                 effective_chat=SimpleNamespace(id=1), effective_user=SimpleNamespace(id=1))
        telegram = TelegramProbe(h.db)
        context = SimpleNamespace(bot=telegram)
        with bind_conversation(selected):
            before = await h.db.get_display_history(1000)
            before_state = await manager.state()
            if mode == 'global_handler':
                context.error = ConversationChanged(STALE_TEXT)
                await bot.global_error_handler(update, context)
            else:
                with patch.object(bot, 'get_web_outbox', return_value=h.outbox), \
                     patch.object(bot, 'get_web_real_bot', return_value=telegram):
                    await bot.mirror_to_web(bot.handle_button_click)(update, context)
            after = await h.db.get_display_history(1000)
            return {'answers': [{'args': list(call.args), 'kwargs': call.kwargs}
                                for call in query.answer.await_args_list],
                    'edits': query.message.edit_text.await_count,
                    'chat_sends': telegram.sent,
                    'history_unchanged': list(before) == list(after),
                    'state_unchanged': before_state == await manager.state(),
                    'frames': h.drain_frames()}


async def unauthorized_menu_probe(bot, directory, unused=None):
    async with Harness(bot, directory) as h:
        telegram = TelegramProbe(h.db)
        update = SimpleNamespace(message=telegram.message('/start'), callback_query=None,
                                 effective_chat=SimpleNamespace(id=2), effective_user=SimpleNamespace(id=2))
        context = SimpleNamespace(bot=telegram)
        action = AsyncMock()
        async def deny(*_):
            await context.bot.send_message(chat_id=2, text='没有访问权限')
            return False
        with patch.object(bot, 'check_authorized_user_middleware', deny), \
             patch.object(bot, 'get_web_outbox', return_value=h.outbox), \
             patch.object(bot, 'get_web_real_bot', return_value=telegram):
            await bot.mirror_to_web(action)(update, context)
        return {'sent': telegram.sent, 'called': action.await_count, 'frames': h.drain_frames()}


async def named_menu_probe(bot, directory, surface):
    async with Harness(bot, directory) as h:
        manager = bot.get_conversations()
        original = (await manager.manage('create', name='洛溪，在不在 <&>'))['conversation_id']
        telegram = TelegramProbe(h.db)
        bridge = None
        if surface == 'web':
            update, context, bridge = build_web_command_objects(1, h.outbox, '/start', telegram)
        else:
            message = telegram.message('/start')
            update = SimpleNamespace(message=message, callback_query=None,
                                     effective_chat=SimpleNamespace(id=1), effective_user=SimpleNamespace(id=1))
            context = SimpleNamespace(bot=telegram)
        try:
            with bind_conversation(await manager.resolve(original)):
                with patch.object(bot, 'get_web_outbox', return_value=h.outbox), \
                     patch.object(bot, 'get_web_real_bot', return_value=telegram):
                    if surface == 'web':
                        async with bot.ui_operation(capture_text=True):
                            await bot.cmd_start(update, context)
                    else:
                        await bot.mirror_to_web(bot.cmd_start)(update, context)
                if bridge is not None:
                    assert await bridge.flush_telegram(timeout=5)
                history = await h.db.get_display_history(1000)
            return {'sent': telegram.sent, 'frames': h.drain_frames(), 'history': list(history),
                    'id': original, 'model_called': bool(h.requests)}
        finally:
            if bridge is not None and bridge._local_channel is not None:
                await bridge._local_channel.aclose()


class TelegramConversationControlTests(unittest.TestCase):
    def probe(self, function, argument):
        with tempfile.TemporaryDirectory() as directory:
            return run_in_app(
                'import asyncio\nfrom tests.test_telegram_conversation_controls import ' + function + '\n'
                f'print(json.dumps(asyncio.run({function}(bot, {directory!r}, {argument!r}))))'
            )

    def test_stale_buttons_answer_with_alert_without_side_effects(self):
        for mode in ('switch', 'clear', 'global_handler'):
            with self.subTest(mode=mode):
                result = self.probe('stale_callback_probe', mode)
                self.assertEqual([{'args': [STALE_TEXT], 'kwargs': {'show_alert': True, 'cache_time': 0}}], result['answers'])
                self.assertEqual(0, result['edits'])
                self.assertEqual([], result['chat_sends'])
                self.assertTrue(result['history_unchanged'] and result['state_unchanged'])
                self.assertFalse(any(frame['type'] in {'message', 'edit', 'turn_error'} for frame in result['frames']))

    def test_valid_button_keeps_its_normal_acknowledgement_and_action(self):
        result = self.probe('stale_callback_probe', 'valid')
        self.assertEqual([{'args': [], 'kwargs': {}}], result['answers'])
        self.assertEqual(1, result['edits'])

    def test_telegram_and_web_start_menu_have_single_native_conversation_label(self):
        for surface in ('telegram', 'web'):
            with self.subTest(surface=surface):
                result = self.probe('named_menu_probe', surface)
                menus = [item for item in result['sent'] if 'XGent for Telegram 已就绪' in item['text']]
                self.assertEqual(1, len(menus))
                prefix = '🗂 洛溪，在不在 &lt;&amp;&gt; · ' + result['id'][:6] + '\n'
                self.assertTrue(menus[0]['text'].startswith(prefix), menus[0]['text'])
                self.assertEqual(1, menus[0]['text'].count(prefix))
                self.assertEqual('HTML', menus[0]['parse_mode'])
                web = [frame for frame in result['frames'] if 'XGent for Telegram 已就绪' in str(frame.get('text', ''))]
                self.assertEqual(1, len(web))
                self.assertEqual(result['id'], web[0]['conversation_id'])
                self.assertFalse(web[0]['text'].startswith(prefix))
                self.assertFalse(result['model_called'])

    def test_label_does_not_require_a_model_run_and_keeps_legacy_short_id(self):
        scope = ConversationScope('8e5bca' + '0' * 26, 2, name='洛溪，在不在')
        with bind_conversation(scope):
            expected = '🗂 洛溪，在不在 · 8e5bca\n菜单正文'
            self.assertEqual(expected, conversation_labelled_text('菜单正文', 'HTML'))
            self.assertEqual(expected, conversation_labelled_text(expected, 'HTML'))
            self.assertEqual('x' * 4096, conversation_labelled_text('x' * 4096, 'HTML'))
        with bind_conversation(ConversationScope('global_memory', 0, name='全局记忆')):
            self.assertEqual('🗂 全局记忆 · global\n菜单正文', conversation_labelled_text('菜单正文'))
        self.assertEqual('菜单正文', conversation_labelled_text('菜单正文'))


    def test_unauthorized_menu_does_not_reveal_current_conversation_label(self):
        result = self.probe('unauthorized_menu_probe', None)
        self.assertEqual(0, result['called'])
        self.assertEqual([{'text': '没有访问权限', 'chat_id': 2}], result['sent'])
        self.assertFalse(any(frame.get('text') == '没有访问权限' for frame in result['frames']))
