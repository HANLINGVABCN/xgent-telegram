"""Actual conversation pipeline with an isolated Telegram delivery recorder."""
import asyncio
import html
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.attachment_request_probe import Harness
from xgent_app.conversations import bind_conversation, current_scope
from xgent_app.interaction import interaction
from xgent_app.protocols import ProtocolParser
from xgent_app.web_bridge import WebMessage, deliver_op_to_bot, build_telegram_conversation_objects
from xgent_app.fanout import ChannelWorker
from xgent_app.presentation import presentation_turn


class TelegramCapture:
    def __init__(self):
        self.serial = 100
        self.messages = {}
        self.calls = []

    def message(self, text='', chat_id=31):
        self.serial += 1
        return WebMessage(self,self.serial,chat_id,text)

    def save(self, mid, text, **kwargs):
        # Production Bot applies this exact adapter at its final native boundary.
        text = ProtocolParser.to_telegram_html(text)
        self.messages[mid] = {'text':text, **kwargs}
        self.calls.append((mid,text,kwargs.get('reply_markup')))

    async def send_message(self, chat_id, text, **kwargs):
        message = self.message(text,chat_id)
        self.save(message.message_id,text,chat_id=chat_id,**kwargs)
        return message

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        assert message_id in self.messages, ('unknown target',message_id)
        self.save(message_id,text,chat_id=chat_id,**kwargs)
        return WebMessage(self,message_id,chat_id,text)

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **kwargs):
        assert message_id in self.messages
        self.messages[message_id]['reply_markup'] = reply_markup
        return True

    async def delete_message(self, chat_id, message_id, **kwargs):
        self.messages.pop(message_id,None)
        return True

    async def send_chat_action(self, **kwargs):
        return True


def buttons(message):
    return [button.text for row in getattr(message.get('reply_markup'),'inline_keyboard',()) for button in row]


async def display_matrix(bot, root):
    cases = []
    for style in ('foreground','background','nonstream'):
      for outcome in ('success','stopped','failure'):
        async with Harness(bot,Path(root)/(style+'-'+outcome)) as h:
            await bot._web_write_setting('response_mode',style)
            bot.UserDataManager.set('agent_mode',True)
            native = TelegramCapture()
            async def deliver(op,mid):
                return await deliver_op_to_bot(native,op,mid,markup_builder=bot._relay_markup_to_telegram)
            channel = ChannelWorker('bot-display-test',deliver,is_configured=lambda:True)
            entered, release = asyncio.Event(), asyncio.Event()
            calls, contexts = [], []
            raw = ['FIRST_REPLY\n```run-x\n<<BEGIN_BOTVIEW1234\nuptime\n<<END_BOTVIEW1234\n```',
                   'SECOND_REPLY\n```read-x\n<<BEGIN_BOTVIEW2345\nworkspace/test.txt\n<<END_BOTVIEW2345\n```',
                   'FINAL_REPLY\n'+('\n'.join('last line '+str(i)+' '+('x'*80) for i in range(80)))]
            def answer(kwargs):
                index = len(calls);calls.append(index)
                if outcome=='failure' and index==1:
                    raise RuntimeError('provider failed after first reply')
                kwargs['usage_sink'].append({'input_tokens':111*(index+1),'output_tokens':11*(index+1)})
                return raw[index]
            async def complete(*args,**kwargs):return answer(kwargs),None
            async def chunks(*args,**kwargs):yield answer(kwargs)
            async def operation(block, *args, **kwargs):
                if block['type']=='run':
                    entered.set();await release.wait()
                stopped = outcome=='stopped'
                return {'kind':block['type'],'success':not stopped,'stopped':stopped,
                    'return_code':0 if not stopped else -1,'output':'OUTPUT-'+block['type']+'\n'+'\n'.join(str(i) for i in range(60)),
                    'notice':'actual '+block['type']+' result',
                    'output_path':str(Path(root)/'output.txt'),
                    'context_message':{'role':'user','content':'actual tool result'}}
            context_class = bot.PresentationContext
            def capture_context(context,turn):
                context = context_class(context,turn)
                contexts.append((context,turn,current_scope()))
                return context
            try:
                with patch.object(bot,'PresentationContext',capture_context), \
                     patch.object(bot.ModelClient,'think_and_reply',complete), \
                     patch.object(bot.ModelClient,'think_and_reply_stream',chunks), \
                     patch.object(bot,'dispatch_standard_protocol',operation):
                    with interaction('telegram'), bind_conversation(await bot.get_conversations().resolve(selector='telegram')):
                        update = SimpleNamespace(message=native.message('test'),callback_query=None,
                            effective_chat=SimpleNamespace(id=31),effective_user=SimpleNamespace(id=1))
                        update, context, _ = build_telegram_conversation_objects(update,SimpleNamespace(bot=native),h.outbox,channel)
                        await bot.GlobalRecorder.record_user_message('test',chat_id=31)
                        task = asyncio.create_task(bot.process_conversation(update,context,'test'))
                        await asyncio.wait_for(entered.wait(),5)
                        assert await channel.wait_idle(5)
                        first = next(message for message in native.messages.values() if 'FIRST_REPLY' in message['text'])
                        running = next(message for message in native.messages.values() if 'run-x · 执行中' in message['text'])
                        assert not buttons(first), buttons(first)
                        assert '回复详情' not in first['text'] and '↑ 111' not in first['text']
                        assert buttons(running)==['❌ 停止回答'],buttons(running)
                        if outcome=='stopped':
                            scope = contexts[-1][2]
                            assert await bot.get_conversations().request_stop(scope.run_id,scope.conversation_id)
                        release.set()
                        try:
                            await asyncio.wait_for(task,10)
                        except RuntimeError:
                            assert outcome=='failure'
                        assert await channel.wait_idle(5)
                        rows = await h.db.get_display_history(0)
                        ai = [row for row in rows if row['msg_type']=='ai_reply']
                        expected = raw if outcome=='success' else raw[:1]
                        assert [row['content'] for row in ai]==expected,(outcome,calls,[row['content'][:150] for row in ai])
                        all_text = '\n'.join(message['text'] for message in native.messages.values())
                        assert all_text.count('<b>回复详情</b>')==1,all_text
                        assert not any(buttons(message) for message in native.messages.values())
                        assert all(len(message['text'].encode('utf-16-le'))//2 <=4096 for message in native.messages.values())
                        assert '↑ '+('333' if outcome=='success' else '111')+' ·' in all_text
                        assert 'cache 0' in all_text
                        assert any(('🟢 run-x' if outcome!='stopped' else '⚪ run-x') in message['text'] for message in native.messages.values())
                        assert '已折叠 11 行 · 存档：' in html.unescape(re.sub(r'<[^>]+>', '', all_text))
                        metadata = json.loads(ai[-1]['metadata']) if isinstance(ai[-1]['metadata'],str) else ai[-1]['metadata']
                        assert metadata['telegram_reply']['record_id']==ai[-1]['id']
                        shared = [frame for frame in h.drain_frames() if '回复详情' in str(frame.get('text',''))]
                        assert not shared
                        context,turn,scope = contexts[-1]
                        ids_before = set(native.messages)
                        with presentation_turn(turn),bind_conversation(scope):
                            await context.bot.finish_telegram_reply(ai[-1],turn.replies[-1],turn.summary())
                            first_message = context.bot.messages.get(turn.replies[0]['message_ids'][0])
                            if first_message:
                                await context.bot.edit_message_reply_markup(chat_id=first_message.chat_id,
                                    message_id=first_message.message_id,reply_markup=bot.build_stop_keyboard())
                        assert await channel.wait_idle(5)
                        assert set(native.messages)==ids_before
                        assert not any(buttons(message) for message in native.messages.values())
                        assert sum(message['text'].count('<b>回复详情</b>') for message in native.messages.values())==1
                        cases.append({'style':style,'outcome':outcome,'reply_count':len(ai),'one_details':True,'buttons_clean':True})
            finally:
                release.set()
                await channel.aclose()
    return {'cases':cases}


async def single_reply_edges(bot, root):
    cases = []
    for style, outcome in [('foreground','single'),('background','single'),('nonstream','single'),
                           ('nonstream','overflow'),('foreground','no_usage'),('nonstream','no_reply')]:
        async with Harness(bot,Path(root)/(style+'-'+outcome)) as h:
            await bot._web_write_setting('response_mode',style)
            native = TelegramCapture()
            async def deliver(op,mid):
                return await deliver_op_to_bot(native,op,mid,markup_builder=bot._relay_markup_to_telegram)
            channel = ChannelWorker('bot-edge-test',deliver,is_configured=lambda:True)
            raw = 'x'*3850 if outcome=='overflow' else 'SINGLE_REPLY'
            def answer(kwargs):
                if outcome=='no_reply':raise RuntimeError('no reply was generated')
                if outcome!='no_usage':kwargs['usage_sink'].append({'input_tokens':100,'output_tokens':10})
                return raw
            async def complete(*args,**kwargs):return answer(kwargs),None
            async def chunks(*args,**kwargs):yield answer(kwargs)
            try:
                with patch.object(bot.ModelClient,'think_and_reply',complete),patch.object(bot.ModelClient,'think_and_reply_stream',chunks):
                    with interaction('telegram'),bind_conversation(await bot.get_conversations().resolve(selector='telegram')):
                        update=SimpleNamespace(message=native.message('single'),callback_query=None,
                            effective_chat=SimpleNamespace(id=31),effective_user=SimpleNamespace(id=1))
                        update,context,_=build_telegram_conversation_objects(update,SimpleNamespace(bot=native),h.outbox,channel)
                        await bot.GlobalRecorder.record_user_message('single',chat_id=31)
                        try:await bot.process_conversation(update,context,'single')
                        except RuntimeError:assert outcome=='no_reply'
                        assert await channel.wait_idle(5)
                        texts=[message['text'] for message in native.messages.values()]
                        assert sum(text.count('<b>回复详情</b>') for text in texts)==(0 if outcome=='no_reply' else 1)
                        assert not any(buttons(message) for message in native.messages.values())
                        if outcome=='overflow':
                            assert raw in texts
                            assert any(text.startswith('<blockquote expandable><b>回复详情</b>') for text in texts)
                        if outcome=='no_usage':assert any('↑ 未提供 · ↓ 未提供 · cache 0' in text for text in texts)
                        assert not any('回复详情' in str(frame.get('text','')) for frame in h.drain_frames())
                        cases.append({'style':style,'outcome':outcome,'details_once':True})
            finally:await channel.aclose()
    return {'cases':cases}
