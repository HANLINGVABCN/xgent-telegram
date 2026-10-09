"""Native Telegram boundary and cross-surface interaction probes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.compression_request_probe import CompressionHarness
from tests.test_compression_token_usage import TelegramProbe
from xgent_app.conversations import current_scope, bind_conversation
from xgent_app.interaction import interaction


def update(native, text='', data=None):
    message = native.message(text)
    query = SimpleNamespace(data=data, answer=AsyncMock(), message=message) if data is not None else None
    return SimpleNamespace(message=message if query is None else None, callback_query=query,
                           effective_chat=SimpleNamespace(id=1), effective_user=SimpleNamespace(id=1))


async def native_entry_and_state(bot, root):
    async with CompressionHarness(bot, root) as h:
        manager = bot.get_conversations()
        a = current_scope().conversation_id
        native = TelegramProbe(h.db)
        context = SimpleNamespace(bot=native)
        calls = []
        async def record(update, context):
            calls.append(current_scope().conversation_id)
            await bot.GlobalRecorder.record_user_message(update.message.text)
        async def no_op(*args):
            calls.append(current_scope().conversation_id)
        with patch.object(bot, '_web_real_bot', native):
            b = (await manager.manage('create', name='网页 B'))['conversation_id']
            c = (await manager.manage('create', name='网页 C'))['conversation_id']
            await manager.manage('switch', b)
            assert native.sent == [], native.sent
            await bot.telegram_entry(record)(update(native, '仍在 Bot A'), context)
            assert calls == [a], calls
            with bind_conversation(await manager.resolve(a)):
                assert '仍在 Bot A' in str(await h.db.get_conversation_messages())
            with bind_conversation(await manager.resolve(b)):
                assert '仍在 Bot A' not in str(await h.db.get_conversation_messages())
            with interaction('telegram'):
                with bind_conversation(await manager.resolve(selector='telegram')):
                    bot.UserDataManager.set('state', bot.BotState.RENAME_CHAT)
                    bot.UserDataManager.set('temp_conversation_rename', {'conversation_id': a, 'generation': current_scope().generation})
            with interaction('web'):
                with bind_conversation(await manager.resolve(b)):
                    assert bot.UserDataManager.get('state') == bot.BotState.IDLE
                    bot.UserDataManager.set('temp_prov_name', 'web only')
                await manager.manage('switch', c)
            with interaction('telegram'):
                with bind_conversation(await manager.resolve(selector='telegram')):
                    assert bot.UserDataManager.get('state') == bot.BotState.RENAME_CHAT
                    assert not bot.UserDataManager.get('temp_prov_name')
            await manager.manage('archive', a)
            calls.clear()
            h.drain_frames()
            await bot.telegram_entry(record)(update(native, '不能改投网页'), context)
            assert not any(frame.get('type') in {'message','edit','user_message'} for frame in h.drain_frames())
            assert not calls
            assert '请选择' in native.sent[-1]['text'] or '选择' in native.sent[-1]['text']
            chooser = next(button['callback_data'] for row in native.sent[-1]['reply_markup']['inline_keyboard']
                           for button in row if bot.CallbackDataStore._store.get(button['callback_data'], button['callback_data']).endswith('conv_switch:' + b))
            choose = update(native, data=chooser)
            await bot.telegram_entry(no_op)(choose, context)
            state = await manager.state()
            assert state['telegram_conversation_id'] == b and state['current_chat_id'] == c
            await bot.telegram_entry(record)(update(native, '现在 Bot B'), context)
            assert calls == [b], calls
            # Explicit Bot /new must not select the new chat in Web.
            await bot.telegram_entry(bot.mirror_to_web(bot.cmd_new_chat))(update(native, '/new 原生新对话'), context)
            state = await manager.state()
            assert state['telegram_conversation_id'] != b and state['current_chat_id'] == c
        return {'native_binding': True, 'states_isolated': True, 'no_silent_retarget': True}


async def mirror_routes(bot, root):
    from xgent_app.fanout import ChannelWorker
    from xgent_app.web_bridge import MirrorBot, deliver_op_to_bot
    from xgent_app.conversations import conversation_snapshot
    async with CompressionHarness(bot, root) as h:
        manager = bot.get_conversations()
        a = current_scope().conversation_id
        b = (await manager.manage('create', name='B'))['conversation_id']
        native = TelegramProbe(h.db)
        edits = []
        async def edit(text, **kwargs):
            edits.append(text)
            return native.message(text)
        native.edit_message_text = edit
        async def deliver(op, native_id):
            return await deliver_op_to_bot(native, op, native_id, markup_builder=bot._relay_markup_to_telegram)
        channel = ChannelWorker('test-telegram', deliver, is_configured=lambda: True)
        try:
            with patch.object(bot, '_web_real_bot', native), patch.object(bot, 'telegram_channel', lambda: channel):
                h.replies.append('B answer')
                await bot._web_scoped_call(bot._web_run_conversation('B question', h.outbox), b, h.outbox, execution=True)
                assert await channel.wait_idle(timeout=5)
                assert not native.sent and not edits, (native.sent, edits)
                await manager.manage('switch', a)
                h.replies.append('A answer')
                await bot._web_scoped_call(bot._web_run_conversation('A question', h.outbox), a, h.outbox, execution=True)
                assert await channel.wait_idle(timeout=5)
                assert any('A answer' in item['text'] for item in native.sent) or any('A answer' in text for text in edits)
                assert not any('🗂' in item['text'] for item in native.sent)
                native.sent.clear(); edits.clear()
                await bot._web_scoped_call(bot._web_handle_command('/start', h.outbox), a, h.outbox, purpose='management')
                assert await channel.wait_idle(timeout=5)
                assert not native.sent and not edits
                mirror = MirrorBot(h.outbox, 1, real_bot=native, channel=channel)
                with interaction('cli'):
                    async with manager.operation(b, fresh=True):
                        rejected = conversation_snapshot()
                await manager.manage('switch', b, selector='telegram')
                await bot._replay_relay_op(mirror, 'send_message', {'conversation_context': rejected,
                    'telegram_presentation': {'mode':'auto'}, 'message_id':101, 'text':'CLI rejected at admission'})
                assert await channel.wait_idle(timeout=5)
                assert not native.sent and not edits
                await manager.manage('switch', a, selector='telegram')
                with interaction('web'):
                    async with manager.operation(a, fresh=True):
                        message = await mirror.send_message(1, 'stream start')
                        assert await channel.wait_idle(timeout=5)
                        assert native.sent[-1]['text'] == 'stream start'
                        await manager.manage('switch', b, selector='telegram')
                        await mirror.edit_message_text('stream end', chat_id=1, message_id=message.message_id)
                        await mirror.send_message(1, 'continuation')
                        assert await channel.wait_idle(timeout=5)
                        assert edits[-1].startswith('🗂 ') and native.sent[-1]['text'].startswith('🗂 ')
                with interaction('task', 'notification'):
                    async with manager.operation(a, fresh=True):
                        await mirror.send_message(1, 'scheduled notification')
                        assert await channel.wait_idle(timeout=5)
                        assert native.sent[-1]['text'].startswith('🗂 ')
        finally:
            await channel.aclose()
        return {'web_filtered':True,'management_muted':True,'relay_frozen':True,'foreign_edits':True,'task_kept':True}


async def pure_telegram_and_chooser(bot, root):
    from xgent_app.web_bridge import WebMessage
    from xgent_app.telegram_presentation import telegram_label
    from xgent_app.conversations import bind_callback_markup
    async with CompressionHarness(bot, root) as h:
        manager = bot.get_conversations()
        a = current_scope().conversation_id
        b = (await manager.manage('create', name='另一对话'))['conversation_id']
        class Native(TelegramProbe):
            def __init__(self, database):
                super().__init__(database)
                self.edits = []
                self.documents = []
            async def edit_message_text(self, text, **kwargs):
                self.edits.append({'text':text, **kwargs})
                return self.message(text)
            async def send_document(self, chat_id=None, document=None, caption=None, **kwargs):
                self.documents.append({'chat_id':chat_id, 'document':document, 'caption':caption, **kwargs})
                return self.message()
        native = Native(h.db)
        context = SimpleNamespace(bot=native)
        async def reply_flow(update, context):
            await context.bot.send_message(1, '同会话简洁正文')
            await manager.manage('switch', b, selector='telegram')
            await bot.safe_send_message(context, 1, '跨会话长回复。'*1200, parse_mode='HTML')
            with telegram_label(False):
                await context.bot.send_message(1, '流式输出中...')
            await bot.send_token_usage_message(context, 1, {'input_tokens':12,'output_tokens':3,'total_tokens':15}, 1)
            await context.bot.send_document(1, 'fixture.txt', '位置参数说明')
            await context.bot.send_document(chat_id=1,document='caption.txt',caption='说明全文。'*204)
        with patch.object(bot, '_web_external_outbox', None), patch.object(bot, 'get_web_outbox', return_value=None), \
             patch.object(bot, '_web_real_bot', native):
            await bot.telegram_entry(reply_flow)(update(native, 'question'), context)
            assert native.sent[0]['text'] == '同会话简洁正文'
            foreign = [item['text'] for item in native.sent if '跨会话长回复' in item['text']]
            assert len(foreign) >= 2 and all(text.startswith('🗂 ') for text in foreign)
            for hint in ('流式输出中...', '↑ 12 tokens'):
                text = next(item['text'] for item in native.sent if hint in item['text'])
                assert text.startswith('🗂 ') and '\n' not in text
            assert any(('说明全文。'*204) in item['text'] for item in native.sent)
            assert native.documents[-1]['caption'].startswith('🗂 ')
            assert native.documents[-2]['caption'].endswith('位置参数说明')
            # No selection: media must not reach even the download handler.
            await manager.manage('archive', b)
            await manager.manage('archive', a)
            blocked = AsyncMock()
            media_update = update(native)
            media_update.message.photo = ['not-downloaded']
            await bot.telegram_entry(blocked)(media_update, context)
            assert blocked.await_count == 0
            def button_data(card, suffix):
                markup = card['reply_markup']
                markup = markup.to_dict() if hasattr(markup, 'to_dict') else markup
                return next(button['callback_data'] for row in markup['inline_keyboard'] for button in row
                            if bot.CallbackDataStore._store.get(button['callback_data'],button['callback_data']).endswith(suffix))
            home_data = button_data(native.sent[-1], 'conv_home')
            await bot.telegram_entry(blocked)(update(native,data=home_data), context)
            assert '尚未选择' in native.edits[-1]['text']
            await bot.telegram_entry(blocked)(update(native,data=button_data(native.edits[-1],'conv_list')), context)
            await bot.telegram_entry(blocked)(update(native,data=button_data(native.edits[-1],'conv_archived')), context)
            # Archived conversation can still be permanently removed, without choosing it.
            await bot.telegram_entry(blocked)(update(native,data=button_data(native.edits[-1],'conv_delete:'+a)), context)
            assert '不可恢复' in native.edits[-1]['text']
            confirm = button_data(native.edits[-1],'conv_confirm_delete:'+a)
            await bot.telegram_entry(blocked)(update(native,data=confirm), context)
            assert (await manager.state())['telegram_conversation_id'] is None
            assert await h.db.get_session(a) is None
            obsolete = update(native,data=confirm)
            await bot.telegram_entry(blocked)(obsolete,context)
            assert obsolete.callback_query.answer.call_args.kwargs['show_alert'] is True
            # Explicit /new recovers without affecting the shared Web view.
            shared = (await manager.state())['current_chat_id']
            await bot.telegram_entry(blocked)(update(native,'/new 再次开始'),context)
            assert (await manager.state())['current_chat_id'] == shared
            assert (await manager.resolve(selector='telegram')).name == '再次开始'
        return {'pure_bot_labels':True,'unselected_media_blocked':True,'chooser_back':True,'archived_delete':True,'stale_chooser_rejected':True}
