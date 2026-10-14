"""Run real update-menu routing with all disk/update/restart side effects mocked."""
from tests.test_thinking_params import run_in_app


def test_update_card_source_token_confirmation_failure_and_duplicate_execution():
    result = run_in_app(r'''
import asyncio,tempfile
from unittest.mock import patch,AsyncMock
import xgent_server as bot
from tests.attachment_request_probe import Harness
from tests.ui_history_probe import menus,click
async def main():
    with tempfile.TemporaryDirectory() as folder:
        async with Harness(bot,folder) as h:
            bot._ensure_web_command_map()
            with patch.object(bot.BotConfig,'UPDATE_GITHUB_TOKEN',''), \
                 patch.object(bot,'persist_update_github_token') as persist, \
                 patch.object(bot,'verify_update_github_token',return_value={'ok':True,'message':'verified'}), \
                 patch.object(bot,'download_and_apply_project_update',return_value={'count':3,'overwrite_local_custom_files':False}) as apply, \
                 patch.object(bot,'restart_current_process',AsyncMock()) as restart:
                await bot._web_handle_command('/start',h.outbox)
                card=(await menus(bot))[0]
                uid=card['ui_message_id']
                await click(bot,h,card,'menu_more_settings')
                await click(bot,h,(await menus(bot))[0],'cmd_update')
                assert len(await menus(bot))==1
                await click(bot,h,(await menus(bot))[0],'select_update_source_test')
                prompt=(await menus(bot))[0]
                assert prompt['ui_message_id']==uid and 'GitHub Token' in prompt['content']
                assert bot.UserDataManager.get('state')==bot.BotState.SET_UPDATE_TOKEN
                h.update.message.text='fixture-secret-do-not-log'
                await bot.handle_text_message(h.update,h.context)
                confirmation=(await menus(bot))[0]
                assert confirmation['ui_message_id']==uid and len(await menus(bot))==1
                assert '更新确认' in confirmation['content']
                assert 'fixture-secret-do-not-log' not in str(await bot._web_read_history(0))
                persist.assert_called_once()
                await asyncio.gather(*(click(bot,h,confirmation,'do_update_keep_custom_files') for _ in range(2)))
                apply.assert_called_once_with(False);restart.assert_awaited_once()
                complete=(await menus(bot))[0]
                assert complete['ui_message_id']==uid and '已更新 3' in complete['content'] and not complete['reply_markup']
                # /update intentionally starts another card. Its failure stays on it.
                await bot._web_handle_command('/update',h.outbox)
                assert len(await menus(bot))==2
                second=(await menus(bot))[-1]
                await click(bot,h,second,'select_update_source_normal')
                apply.side_effect=ValueError('fixture update failure')
                await click(bot,h,(await menus(bot))[-1],'do_update_keep_custom_files')
                failed=(await menus(bot))[-1]
                assert failed['ui_message_id']==second['ui_message_id'] and 'fixture update failure' in failed['content']
                assert len(await menus(bot))==2
                # Token cancellation edits the same card too.
                await bot._web_handle_command('/update',h.outbox)
                await click(bot,h,(await menus(bot))[-1],'select_update_source_test')
                cancelled_id=(await menus(bot))[-1]['ui_message_id']
                h.update.message.text='cancel';await bot.handle_text_message(h.update,h.context)
                assert len(await menus(bot))==3
                assert (await menus(bot))[-1]['ui_message_id']==cancelled_id
                assert '已取消' in (await menus(bot))[-1]['content']
                return {'in_place':True,'duplicate_safe':True,'token_secret_not_recorded':True}
print(json.dumps(asyncio.run(main())))
''')
    assert all(result.values())


def test_update_token_expired_card_is_not_written_and_failed_verification_can_retry():
    result = run_in_app(r'''
import asyncio,tempfile
from unittest.mock import patch
import xgent_server as bot
from tests.attachment_request_probe import Harness
from tests.ui_history_probe import menus,click
async def main():
    with tempfile.TemporaryDirectory() as folder:
        async with Harness(bot,folder) as h:
            bot._ensure_web_command_map()
            with patch.object(bot.BotConfig,'UPDATE_GITHUB_TOKEN',''), \
                 patch.object(bot,'persist_update_github_token') as persist, \
                 patch.object(bot,'verify_update_github_token',return_value={'ok':False,'message':'bad fixture token'}) as verify:
                await bot._web_handle_command('/update',h.outbox)
                await click(bot,h,(await menus(bot))[0],'select_update_source_test')
                uid=(await menus(bot))[0]['ui_message_id']
                h.update.message.text='bad-fixture-token';await bot.handle_text_message(h.update,h.context)
                assert len(await menus(bot))==1 and (await menus(bot))[0]['ui_message_id']==uid
                assert bot.UserDataManager.get('state')==bot.BotState.SET_UPDATE_TOKEN
                verify.return_value={'ok':True,'message':'ok'}
                h.update.message.text='correct-fixture-token';await bot.handle_text_message(h.update,h.context)
                assert '更新确认' in (await menus(bot))[0]['content']
                assert persist.call_count==2
                await bot._web_handle_command('/update',h.outbox)
                await click(bot,h,(await menus(bot))[-1],'select_update_source_test')
                stale=(await menus(bot))[-1]
                # Another surface/tab edited the card before text input arrived.
                from xgent_app.ui_history import ui_operation
                from xgent_app.web_bridge import WebBot
                async with ui_operation(binding={'ui_message_id':stale['ui_message_id'],'generation':stale['ui_generation'],'revision':stale['revision']},message_id=123):
                    await WebBot(h.outbox,1).edit_message_text('card replaced elsewhere',message_id=123)
                h.update.message.text='must-not-be-written';await bot.handle_text_message(h.update,h.context)
                assert persist.call_count==2
                assert 'card replaced elsewhere' in (await menus(bot))[-2]['content']
                assert '失效' in str(h.drain_frames())
                return {'retry_original':True,'stale_rejected':True}
print(json.dumps(asyncio.run(main())))
''')
    assert all(result.values())


def test_native_telegram_token_keeps_its_card_when_web_opens_another_update_card():
    result = run_in_app(r'''
import asyncio,tempfile
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
from telegram.error import BadRequest
from tests.attachment_request_probe import Harness
from tests.ui_history_probe import menus,click
from xgent_app.interaction import interaction
from xgent_app.web_bridge import WebMessage

class Native:
    def __init__(self):
        self.messages={};self.events=[]
    async def send_message(self,chat_id,text,**kwargs):
        mid=100+len(self.messages)
        self.messages[mid]={'text':text,**kwargs}
        self.events.append(('send',mid))
        return WebMessage(self,mid,chat_id,text)
    async def edit_message_text(self,text,chat_id,message_id,**kwargs):
        assert message_id in self.messages,'must edit a previously created native card'
        if self.messages[message_id]=={'text':text,**kwargs}:
            raise BadRequest('Message is not modified')
        self.messages[message_id]={'text':text,**kwargs}
        self.events.append(('edit',message_id))
        return WebMessage(self,message_id,chat_id,text)

async def main():
    with tempfile.TemporaryDirectory() as folder:
        async with Harness(bot,folder) as h:
            bot._ensure_web_command_map()
            native=Native();context=SimpleNamespace(bot=native,args=[],application=None)
            command=bot.telegram_entry(bot.mirror_to_web(bot.cmd_update_system))
            callback=bot.telegram_entry(bot.mirror_to_web(bot.handle_button_click))
            incoming=bot.telegram_entry(bot.handle_text_message)
            def update(text='',action=None):
                message=WebMessage(native,99 if action is None else 100,1,text)
                query=SimpleNamespace(data=action,message=message,answer=AsyncMock()) if action else None
                return SimpleNamespace(message=None if query else message,callback_query=query,
                    effective_chat=SimpleNamespace(id=1),effective_user=SimpleNamespace(id=1))
            with patch.object(bot.BotConfig,'UPDATE_GITHUB_TOKEN',''), \
                 patch.object(bot,'get_web_outbox',return_value=h.outbox), \
                 patch.object(bot,'check_authorized_user_middleware',AsyncMock(return_value=True)), \
                 patch.object(bot,'persist_update_github_token') as persist, \
                 patch.object(bot,'verify_update_github_token',return_value={'ok':True,'message':'verified'}), \
                 patch.object(bot,'download_and_apply_project_update',return_value={'count':2,'overwrite_local_custom_files':False}) as apply, \
                 patch.object(bot,'restart_current_process',AsyncMock()) as restart:
                await command(update('/update'),context)
                await asyncio.gather(*(callback(update(action='select_update_source_test'),context) for _ in range(2)))
                assert len(native.messages)==1 and '需要 GitHub Token' in native.messages[100]['text']
                with interaction('web','management'):
                    await bot._web_handle_command('/update',h.outbox)
                    await click(bot,h,(await menus(bot))[-1],'select_update_source_normal')
                    web_card=(await menus(bot))[-1]
                    assert '更新确认' in web_card['content']
                await incoming(update('native-fixture-secret'),context)
                assert len(native.messages)==1 and '更新确认' in native.messages[100]['text']
                assert (await menus(bot))[-1]['ui_message_id']==web_card['ui_message_id']
                persist.assert_called_once()
                await asyncio.gather(*(callback(update(action='do_update_keep_custom_files'),context) for _ in range(2)))
                apply.assert_called_once_with(False);restart.assert_awaited_once()
                assert len(native.messages)==1 and '已更新 2' in native.messages[100]['text']
                assert all(mid==100 for kind,mid in native.events)
                assert 'native-fixture-secret' not in str(await bot._web_read_history(0))
                return {'native_card_stable':True,'surface_binding_independent':True,'native_duplicate_safe':True}
print(json.dumps(asyncio.run(main())))
''')
    assert all(result.values())
