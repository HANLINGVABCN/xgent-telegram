"""History survives context maintenance; permanent deletion is scoped and durable."""
import asyncio
import json
import time
import unittest
from pathlib import Path

from tests import test_conversations as base_tests
from xgent_app.conversations import ConversationError, bind_conversation
from xgent_app.web_appearance import read_appearance, write_appearance


class ContextHistoryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base_tests.ConversationTests.asyncSetUp
    asyncTearDown = base_tests.ConversationTests.asyncTearDown
    write = base_tests.ConversationTests.write

    async def task(self, cid, task_id='task-a'):
        await self.db.create_trigger_task({'id':task_id,'chat_id':1,'conversation_id':cid,'command':'echo test',
            'schedule_type':'once','timezone':'Asia/Shanghai','status':'scheduled','created_at':time.time(),'updated_at':time.time()})

    async def test_reset_keeps_history_attachment_and_ui_but_not_model_context(self):
        from xgent_app.ui_history import ui_record, validated_button, UiHistoryError
        scope=await self.manager.resolve()
        with bind_conversation(scope):
            await self.write('old question')
            await self.write('old attachment','user_file',{'attachments':[{'kind':'file','path':'old.txt'}]})
            ui=await self.db.apply_ui_frame('telegram',1,{'type':'message','message_id':7,'text':'old menu'},
                [[{'text':'button','callback_data':'conv_list','button_id':'0:0'}]],generation=scope.generation,create=True,guard='')
            before=await self.db.get_export_snapshot()
            await self.db.clear_all_conversation_memory()
            self.assertEqual([],await self.db.get_conversation_messages())
            self.assertEqual([],await self.db.get_attachment_records())
            self.assertEqual([],await self.db.get_tool_context_records())
            history=await self.db.get_display_history(0)
            self.assertTrue(any(r.get('content')=='old question' for r in history))
            old_ui=next(r for r in history if r.get('ui_message_id')==ui['ui_message_id'])
            self.assertTrue(old_ui['readonly'])
            self.assertTrue(old_ui['reply_markup'][0][0]['unavailable'])
            with self.assertRaises(UiHistoryError):
                await validated_button(self.db,ui['ui_message_id'],ui['revision'],'0:0')
            await self.write('new question')
            self.assertEqual(['new question'],[m['content'] for m in await self.db.get_conversation_messages()])
            after=await self.db.get_export_snapshot()
            self.assertTrue(set(r['id'] for r in before['records']) <= set(r['id'] for r in after['records']))
            self.assertEqual(1,after['context_epoch'])
            self.assertEqual(['new question'],[r['content'] for r in (await self.db.get_compression_snapshot())['records']])

    async def test_compression_retains_records_and_new_epoch_cannot_seed_old_summary(self):
        with bind_conversation(await self.manager.resolve()):
            await self.write('original')
            async def compress(summary):
                snapshot=await self.db.get_compression_snapshot()
                entry=await self.db.begin_compression(snapshot,{'archive_path':'a.zip','text_dir':'dir','instruction':'compress'},1,'p','m','test',asyncio.Event())
                entry=await self.db.start_compression_attempt(entry['job_id'],'p','m')
                return await self.db.commit_compression(entry,summary,asyncio.Event())
            first=await compress('summary one')
            self.assertIn('original',str(await self.db.get_display_history(0)))
            self.assertNotIn('original',str(await self.db.get_conversation_messages()))
            self.assertIn('summary one',str(await self.db.get_conversation_messages()))
            await self.db.clear_all_conversation_memory()
            self.assertIsNone(await self.db.get_latest_compression())
            self.assertEqual([], (await self.db.get_compression_snapshot())['compressions'])
            await self.write('new epoch question')
            second=await compress('summary two')
            self.assertEqual(first['sequence']+1,second['sequence'])
            self.assertIn('original',str(await self.db.get_export_snapshot()))
            self.assertNotIn('summary one',str(await self.db.get_compression_snapshot()))
            self.assertIn('summary two',str(await self.db.get_conversation_messages()))

    async def test_management_audit_not_a_prompt_or_compression_source(self):
        with bind_conversation(await self.manager.resolve()):
            await self.db.record_global_message(1,1,'system_op','system','rename audit',metadata={'ui_audit':True})
            await self.write('clicked management','button_click')
            await self.write('/chats','command')
            await self.write('洛溪？')
            self.assertEqual(['洛溪？'],[r['content'] for r in await self.db.get_conversation_messages()])
            self.assertEqual(['洛溪？'],[r['content'] for r in (await self.db.get_compression_snapshot())['records']])
            self.assertEqual(4,len((await self.db.get_export_snapshot())['records']))

    async def test_delete_default_last_chat_cancels_tasks_and_never_resurrects(self):
        old=await self.manager.resolve()
        with bind_conversation(old):
            await self.write('old')
        await self.task(old.conversation_id)
        await self.db.create_trigger_run('task-a',time.time(),'test')
        await self.db.begin_conversation_delete(old.conversation_id)
        with self.assertRaises(ConversationError):
            await self.manager.resolve(old.conversation_id,allow_archived=True)
        with self.assertRaises(ConversationError):
            await self.task(old.conversation_id,'late-task')
        with self.assertRaises(ConversationError):
            await self.db.create_trigger_run('task-a',time.time()+1,'test')
        self.assertEqual([old.conversation_id],await self.db.pending_conversation_deletions())
        selected=(await self.manager.state())['current_chat_id']
        self.assertNotEqual(old.conversation_id,selected)
        await self.db.finish_conversation_delete(old.conversation_id)
        self.assertIsNone(await self.db.get_session(old.conversation_id))
        self.assertEqual([],await self.db.list_trigger_tasks(False))
        await self.db.close()
        await self.db._init_db()
        self.assertIsNone(await self.db.get_session('global_memory'))
        self.assertEqual(selected,(await self.manager.state())['current_chat_id'])

    async def test_delete_noncurrent_does_not_stop_another_turn(self):
        a=await self.manager.resolve()
        b=(await self.manager.manage('create',name='B'))['conversation_id']
        async with self.manager.operation(b,execution=True):
            run=(await self.manager.state())['running']
            await self.db.begin_conversation_delete(a.conversation_id)
            await self.db.finish_conversation_delete(a.conversation_id)
            after=await self.manager.state()
            self.assertEqual(b,after['current_chat_id'])
            self.assertEqual(run['run_id'],after['running']['run_id'])
            self.assertFalse(after['running']['stop_requested'])

    async def test_explicit_default_looking_name_is_not_overwritten(self):
        cid=(await self.manager.manage('create',name='新对话'))['conversation_id']
        with bind_conversation(await self.manager.resolve(cid)):
            await self.write('不要覆盖手工名称')
        item=next(c for c in await self.db.get_all_sessions() if c['id']==cid)
        self.assertEqual('新对话',item['name'])
        self.assertEqual('新对话',item['display_title'])

    async def test_appearance_persisted_revision_and_validation(self):
        initial=await read_appearance(self.db)
        saved=await write_appearance(self.db,'web_message_font_size',18)
        self.assertGreater(saved['revision'],initial['revision'])
        await self.db.close();await self.db._init_db()
        self.assertEqual(18,(await read_appearance(self.db))['values']['web_message_font_size'])
        for value in (True,11,25,14.5,float('nan'),'18'):
            with self.assertRaises(ValueError):
                await write_appearance(self.db,'web_message_font_size',value)

    async def test_v2_migration_does_not_reassign_v1_conversations(self):
        # Downgrade the new columns in an isolated DB to exercise a real v1 upgrade.
        a=await self.manager.resolve()
        with bind_conversation(a):await self.write('A')
        b=(await self.manager.manage('create',name='B'))['conversation_id']
        with bind_conversation(await self.manager.resolve(b)):await self.write('B')
        conn=await self.db._get_conn()
        await conn.execute('DROP INDEX idx_ui_conversation_history')
        for table,column in [('ui_messages','conversation_id'),('context_compressions','context_epoch'),('context_compression_jobs','context_epoch'),('chat_sessions','context_start_record_id'),('chat_sessions','context_epoch'),('chat_sessions','deleting'),('chat_sessions','title_auto_pending')]:
            await conn.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
        await self.db.set_config('conversation_schema_version',1)
        await self.db.close();await self.db._init_db()
        self.assertTrue(Path(self.path+'.pre-conversations-v3.sqlite3').exists())
        self.assertEqual(b,(await self.manager.state())['current_chat_id'])
        for cid,text in [(a.conversation_id,'A'),(b,'B')]:
            with bind_conversation(await self.manager.resolve(cid)):
                self.assertEqual([text],[r['content'] for r in await self.db.get_conversation_messages()])
        await self.db.migrate_conversations()
        self.assertEqual(2,len(await self.db.get_all_sessions()))


if __name__=='__main__':unittest.main()
