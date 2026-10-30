"""The production conversation path with a simulated native Bot API 10.3."""
from telegram import InlineKeyboardMarkup

from tests.telegram_chat_probe import TelegramCapture


class RichCapture(TelegramCapture):
    id = 7001

    def __init__(self):
        super().__init__()
        self.drafts = []
        self.api_calls = []

    async def _post(self, method, data=None, **kwargs):
        self.api_calls.append((method,data))
        if method == 'sendRichMessageDraft':
            self.drafts.append(data)
            assert data['can_stop'] is False and data['keep_on_stop'] is False
            assert len(data['rich_message']['html']) <= 32768
            return True
        if method == 'deleteMessage':
            self.messages.pop(data['message_id'],None)
            return True
        if method == 'editMessageReplyMarkup':
            mid=data['message_id']
            if mid in self.messages:self.messages[mid]['reply_markup']=InlineKeyboardMarkup.de_json(data['reply_markup'],None) if data.get('reply_markup') else None
            return True
        if method in {'sendMessage','sendRichMessage','editMessageText'}:
            mid = data.get('message_id')
            if mid is None:mid=self.message().message_id
            else:assert mid in self.messages,('unknown target',mid)
            text=data.get('rich_message',{}).get('html',data.get('text',''))
            markup=InlineKeyboardMarkup.de_json(data['reply_markup'],None) if data.get('reply_markup') else None
            self.messages[mid]={'text':text,'reply_markup':markup,'rich':bool(data.get('rich_message'))}
            self.calls.append((mid,text,markup))
            return {'message_id':mid,'chat':{'id':31,'type':'private'},'date':0,'text':text}
        raise AssertionError(method)


async def outer_stop(bot, root):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from tests.attachment_request_probe import Harness
    from xgent_app.conversations import current_scope
    from xgent_app.interaction import interaction
    from xgent_app.stop_targets import begin_phase
    async with Harness(bot,root) as h:
        manager=bot.get_conversations()
        with interaction('telegram','chat'):
            async with manager.operation(execution=True,fresh=True,selector='telegram'):
                scope=current_scope()
                old=begin_phase(scope.run_id,'model');new=begin_phase(scope.run_id,'operation')
                native=RichCapture();context=SimpleNamespace(bot=native)
                replies=[]
                async def answered(text,**kwargs):replies.append(text)
                async def unused(*args):raise AssertionError('stop reached the ordinary handler')
                async def click(ref):
                    query=SimpleNamespace(data=bot.CallbackDataStore.store('act_stop_generation:'+scope.run_id+':'+ref),answer=answered)
                    update=SimpleNamespace(callback_query=query,message=None,effective_chat=SimpleNamespace(id=31),effective_user=SimpleNamespace(id=1))
                    with patch.object(bot,'authorize_before_conversation',AsyncMock(return_value=True)):
                        await bot.telegram_entry(unused)(update,context)
                await click(old)
                assert not manager.stop_event().is_set()
                assert '已结束' in replies[-1]
                await click(new)
                assert manager.stop_event().is_set() and '已收到' in replies[-1]
                return {'active_stopped':True,'old_phase_rejected':True}
