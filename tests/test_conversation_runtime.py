"""Full application boundary checks using temporary databases and fake models."""
import unittest
from tests.test_external_sync import SectionsProbeMixin


class ConversationRuntimeTests(SectionsProbeMixin, unittest.TestCase):
    def test_workbench_validates_target_before_mutation(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio
from xgent_app.workbench import Workbench, WorkbenchError
async def main():
    await ns['UserDataManager'].init()
    manager=ns['get_conversations'](); db=await ns['BotMemoryDB'].get_instance(); w=Workbench(ns)
    await ns['GlobalRecorder'].record_user_message('A history')
    created=await w.handle('POST','conversations/create',{'name':'B'})
    b=created['current_chat_id']; rejected=[]
    for body in ({'confirm':'清空当前会话'}, {'conversation_id':'global_memory','confirm':'清空当前会话'}):
        try: await w.handle('POST','memory/clear',body)
        except WorkbenchError as exc: rejected.append(exc.status)
    a=await w.handle('GET','history',{'conversation_id':'global_memory'})
    empty=await w.handle('GET','history',{'conversation_id':b})
    await w.handle('POST','conversations/archive',{'id':b})
    await w.handle('POST','conversations/restore',{'id':b})
    print(json.dumps({'rejected':rejected,'a':len(a['messages']),'b':len(empty['messages']),
                      'restored':not (await db.get_session(b))['archived']}))
    await db.close()
asyncio.run(main())
''')
        self.assertEqual([409, 409], result['rejected'])
        self.assertEqual(1, result['a'])
        self.assertEqual(0, result['b'])
        self.assertTrue(result['restored'])

    def test_menu_callbacks_remain_bound_when_selection_changes(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio
from xgent_app.web_bridge import _markup_to_frame, markup_from_frame
from xgent_app.conversations import bind_callback_markup, bind_conversation, ConversationChanged
async def main():
    await ns['UserDataManager'].init(); manager=ns['get_conversations'](); db=await ns['BotMemoryDB'].get_instance()
    a=await manager.resolve(); long_action='view_prompt:'+('long'*30)
    with bind_conversation(a):
        raw=ns['InlineKeyboardMarkup']([[ns['InlineKeyboardButton']('name', callback_data=ns['CallbackDataStore'].store(long_action))]])
        markup=bind_callback_markup(raw)
        encoded=_markup_to_frame(markup)[0][0]
        assert len(encoded['callback_data'].encode())<=64
        assert encoded['callback_action']==long_action
        assert _markup_to_frame(bind_callback_markup(markup))[0][0]['callback_action']==long_action
    b=(await manager.manage('create',name='B'))['conversation_id']; rejected=False
    with bind_conversation(await manager.resolve(b)):
        try: ns['CallbackDataStore'].get(encoded['callback_data'])
        except ConversationChanged: rejected=True
    print(json.dumps({'rejected':rejected,'action':encoded['callback_action']})); await db.close()
asyncio.run(main())
''')
        self.assertTrue(result['rejected'])
        self.assertTrue(result['action'].startswith('view_prompt:'))


    def test_running_reply_and_archived_trigger_return_to_original_conversation(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio,time
from xgent_app.conversations import bind_conversation,current_scope
async def main():
    await ns['UserDataManager'].init(); manager=ns['get_conversations'](); db=await ns['BotMemoryDB'].get_instance()
    a='global_memory'; outbox=ns['WebOutbox'](); started=asyncio.Event(); release=asyncio.Event()
    async def executing():
        async with manager.operation(a,execution=True,fresh=True):
            await ns['GlobalRecorder'].record_user_message('A request')
            started.set(); await release.wait()
            await ns['GlobalRecorder'].record_ai_reply('A final result')
            outbox.put({'type':'message','message_id':1,'text':'A final result'})
    task=asyncio.create_task(executing()); await started.wait()
    b=(await manager.manage('create',name='B'))['conversation_id']
    await manager.manage('archive',a)
    await ns['_web_scoped_call'](ns['_web_run_conversation']('rejected B',outbox),b,outbox,
                                 execution=True,rejected_text='rejected B')
    release.set(); await task
    now=time.time()
    await db.create_trigger_task({'id':'task-a','chat_id':1,'conversation_id':a,'command':'echo not-executed',
        'schedule_type':'immediate','timezone':'Asia/Shanghai','status':'completed','created_at':now,'updated_at':now})
    run,_=await db.create_trigger_run('task-a',now,'process_exit')
    seen=[]
    async def delivered(cls,run_id):
        seen.append(current_scope().conversation_id)
        await ns['GlobalRecorder'].record_system_op('archived A trigger result')
    ns['SelfTriggerManager']._deliver_run_inner=classmethod(delivered)
    await ns['SelfTriggerManager']._deliver_run(run['run_id'])
    with bind_conversation(await manager.resolve(a,allow_archived=True)):
        rows_a=await db.get_global_messages(100)
    with bind_conversation(await manager.resolve(b)):
        rows_b=await db.get_global_messages(100)
    frames=[event['frame'] for event in outbox.read_events(0,outbox.snapshot()['epoch'])['events']]
    print(json.dumps({'a':str(rows_a),'b':rows_b,'seen':seen,'active':(await manager.state())['current_chat_id']==b,
        'labelled':all(f['conversation_id']==a for f in frames if f.get('text')=='A final result'),
        'rejected':any(f.get('rejected_text')=='rejected B' for f in frames)}))
    await db.close()
asyncio.run(main())
''')
        self.assertIn('A final result', result['a'])
        self.assertIn('archived A trigger result', result['a'])
        self.assertEqual([], result['b'])
        self.assertEqual(['global_memory'], result['seen'])
        self.assertTrue(result['active'] and result['labelled'] and result['rejected'])


    def test_unauthorized_ingress_cannot_take_execution_slot_or_notify_owner(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
async def main():
    await ns['UserDataManager'].init(); manager=ns['get_conversations'](); db=await ns['BotMemoryDB'].get_instance()
    before=await manager.state()
    update=SimpleNamespace(effective_user=SimpleNamespace(id=2),effective_chat=SimpleNamespace(id=2))
    context=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()),error=ns['ConversationBusy']('busy'))
    ns['check_authorized_user_middleware']=AsyncMock(return_value=False)
    await ns['handle_text_message'](update,context)
    await ns['global_error_handler'](update,context)
    context.bot.send_message.assert_not_awaited()
    ns['check_authorized_user_middleware'].assert_awaited_once()
    print(json.dumps({'unchanged':before==await manager.state(),'unlocked':not manager.lock.busy()}));await db.close()
asyncio.run(main())
''')
        self.assertTrue(result['unchanged'] and result['unlocked'])

    def test_telegram_menu_pages_remain_below_button_limit(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio
async def main():
    await ns['UserDataManager'].init(); manager=ns['get_conversations'](); db=await ns['BotMemoryDB'].get_instance()
    for i in range(20): await manager.manage('create',name='会话'+str(i))
    outbox=ns['WebOutbox'](); sub=outbox.subscribe()
    update,context,_=ns['build_web_command_objects'](1,outbox,'/chats',None)
    await ns['show_conversation_menu'](update,context,page=2)
    frame=sub.get(timeout=.1); buttons=[b for row in frame['reply_markup'] for b in row]
    print(json.dumps({'count':len(buttons),'pagination':'conv_page' in str(buttons),'title':frame['text']}))
    sub.close();await db.close()
asyncio.run(main())
''')
        self.assertLessEqual(result['count'], 14)
        self.assertTrue(result['pagination'])
        self.assertIn('2/5', result['title'])
