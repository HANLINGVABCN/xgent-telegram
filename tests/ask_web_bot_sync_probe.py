"""A Web draft edits the native Bot form through its existing delivery mapping."""
from dataclasses import replace
from unittest.mock import patch

from tests.attachment_request_probe import Harness
from tests.telegram_rich_probe import RichCapture
from xgent_app.agent_ask import PENDING_ASKS
from xgent_app.conversations import bind_conversation, current_scope
from xgent_app.fanout import get_channel_registry
from xgent_app.interaction import interaction
from xgent_app.web_ask import handle_ask


async def exercise(bot, root):
    async with Harness(bot, root) as h:
        await bot.UserDataManager.save_config('agent_mode', True)
        native = RichCapture()
        response = ('需要确认。\n```ask-x\n<<BEGIN_WEBSYNC123456\n'
                    'Q1|single|环境\n- 测试\n- 生产\n+other\n'
                    'Q2|multi|项目\n- 日志\n- 备份\nQ3|text|说明\n<<END_WEBSYNC123456\n```')
        async def model(*args, **kwargs):
            return response, None
        with patch.object(bot, '_web_real_bot', native), patch.object(bot, 'get_web_outbox', return_value=h.outbox), \
                patch.object(bot.ModelClient, 'think_and_reply', model):
            scope = replace(current_scope(), origin='telegram', purpose='chat', run_id=None, telegram_delivery=True)
            with interaction('telegram', 'chat'), bind_conversation(scope):
                await bot.GlobalRecorder.record_user_message('发起表单', chat_id=1)
                await bot.process_conversation(bot._SelfTriggerUpdate(native, 1), bot._SelfTriggerContext(native), '发起表单')
            pending = next(value for value in PENDING_ASKS._store.values() if value.conversation_id == scope.conversation_id)
            assert await bot.telegram_channel().wait_idle(5)
            form_mid = next(mid for mid, message in native.messages.items() if message.get('reply_markup') and
                            any('✅ 提交' == button.text for row in message['reply_markup'].inline_keyboard for button in row))
            original_count = len(native.messages)
            original_logical = pending.form_message_id
            original_record = pending.form_record_id
            def labels():
                return [button.text for row in native.messages[form_mid]['reply_markup'].inline_keyboard for button in row]
            def no_resume(value):
                raise AssertionError('Saving fields must not resume the Agent')
            web_scope = replace(scope, origin='web', purpose='management', run_id=None, telegram_delivery=False)
            with interaction('web', 'management'), bind_conversation(web_scope):
                await handle_ask(vars(bot), 'POST', {'id': pending.ask_id, 'action': 'save', 'revision': pending.revision,
                    'draft': {'0': {'selected': [0]}, '1': {'selected': [0, 1]}, '2': {'custom': '网页填写的说明'}}}, no_resume)
                assert await bot.telegram_channel().wait_idle(5)
                assert any(label == '✅ 测试' for label in labels()), labels()
                assert any(label == '✅ 日志' for label in labels()) and any(label == '✅ 备份' for label in labels()), labels()
                assert any('✅ 已填：网页填写的说明' == label for label in labels()), labels()
                await handle_ask(vars(bot), 'POST', {'id': pending.ask_id, 'action': 'save', 'revision': pending.revision,
                    'draft': {'0': {'selected': [], 'custom': '自定义环境'}, '1': {'selected': [1]}, '2': {'custom': '修改说明'}}}, no_resume)
                assert await bot.telegram_channel().wait_idle(5)
                assert '✅ 测试' not in labels() and '✅ 日志' not in labels(), labels()
                assert any('✅ 自定义：自定义环境' == label for label in labels()), labels()
                assert any('✅ 已填：修改说明' == label for label in labels()), labels()
            assert pending.form_message_id == original_logical and pending.form_record_id == original_record
            assert len(native.messages) == original_count
            assert pending.status == 'pending'
            await get_channel_registry().aclose_all()
            return {'web_to_bot': True, 'original_form': True, 'pending': True}
