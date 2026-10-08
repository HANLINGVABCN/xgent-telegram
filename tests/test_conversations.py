"""Conversation isolation, stale writes, archive semantics and OS execution lock."""
import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_database_write_lock import _load_db_class
from xgent_app.conversations import (
    ConversationError, ConversationBusy, ConversationChanged, ConversationManager,
    ConversationScope, ExecutionFileLock, bind_conversation, current_scope,
)


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'memory.db')
        cls = _load_db_class()
        core = (Path(__file__).resolve().parents[1] / 'xgent_app/sections/core.py').read_text(encoding='utf-8')
        constants = core[core.index('class MessageType:'):core.index('def _read_int_env(')]
        exec(constants, cls._init_db.__globals__)
        from xgent_app.web_history import display_media_reference
        cls._init_db.__globals__['display_media_reference'] = display_media_reference
        self.db = cls(self.path)
        await self.db._init_db()
        async def factory():
            return self.db
        self.manager = ConversationManager(factory, self.path)

    async def asyncTearDown(self):
        await self.manager.close()
        self.manager.lock.release()
        await self.db.close()
        self.temp.cleanup()

    async def write(self, text, kind='user_text', metadata=None):
        return await self.db.record_global_message(1, 1, kind, 'user', text, metadata=metadata)

    async def test_missing_scope_fails_closed(self):
        with self.assertRaises(ConversationError):
            await self.db.get_conversation_messages()
        with self.assertRaises(ConversationError):
            await self.write('must not land anywhere')

    async def test_switch_does_not_rebind_running_scope(self):
        a = await self.manager.resolve()
        with bind_conversation(a):
            await self.write('A first')
            b = (await self.manager.manage('create', name='B'))['conversation_id']
            await self.write('A result after switch')
            self.assertEqual(a.conversation_id, current_scope().conversation_id)
            self.assertEqual(2, len(await self.db.get_conversation_messages()))
        with bind_conversation(await self.manager.resolve(b)):
            self.assertEqual([], await self.db.get_conversation_messages())
            await self.write('B first')
            self.assertNotEqual(a.generation, await self.db.get_attachment_generation())
            self.assertEqual(['B first'], [r['content'] for r in await self.db.get_conversation_messages()])
        with bind_conversation(a):
            self.assertNotIn('B first', str(await self.db.get_conversation_messages()))

    async def test_clear_keeps_identity_and_only_invalidates_target(self):
        a = await self.manager.resolve()
        with bind_conversation(a):
            await self.write('A')
        b = (await self.manager.manage('create', name='B'))['conversation_id']
        bscope = await self.manager.resolve(b)
        with bind_conversation(bscope):
            await self.write('B')
        with bind_conversation(a):
            await self.db.clear_all_conversation_memory()
            self.assertGreater(current_scope().generation, bscope.generation)
            self.assertIsNotNone(await self.db.get_session(a.conversation_id))
        with bind_conversation(a):
            with self.assertRaises(ConversationError):
                await self.write('late A result')
        with bind_conversation(bscope):
            self.assertEqual('B', (await self.db.get_conversation_messages())[0]['content'])

    async def test_archive_keeps_history_and_allocates_fallback(self):
        a = await self.manager.resolve()
        with bind_conversation(a):
            await self.write('retained')
        state = await self.manager.manage('archive', a.conversation_id)
        self.assertNotEqual(a.conversation_id, state['current_chat_id'])
        with self.assertRaises(ConversationChanged):
            await self.manager.resolve(a.conversation_id)
        with bind_conversation(await self.manager.resolve(a.conversation_id, allow_archived=True)):
            self.assertIn('retained', str(await self.db.get_conversation_messages()))
        await self.manager.manage('restore', a.conversation_id)
        await self.manager.manage('switch', a.conversation_id)
        self.assertEqual(a.generation, (await self.manager.resolve()).generation)

    async def test_execution_slot_and_stale_stop(self):
        async with self.manager.operation(execution=True) as a:
            other = ConversationManager(self.manager.factory, self.path)
            with self.assertRaises(ConversationBusy):
                async with other.operation(execution=True):
                    self.fail('second execution admitted')
            b = (await self.manager.manage('create', name='B'))['conversation_id']
            self.assertEqual(a.conversation_id, current_scope().conversation_id)
            self.assertFalse(await self.manager.request_stop('old-run', a.conversation_id))
            self.assertFalse(await self.manager.request_stop(a.run_id, b))
            self.assertTrue(await self.manager.request_stop(a.run_id, a.conversation_id))
            self.assertTrue(self.manager.stop_event().is_set())
        self.assertFalse(self.manager.lock.busy())
        self.assertIsNone((await self.manager.state())['running'])

    async def test_restart_does_not_collapse_conversations(self):
        b = (await self.manager.manage('create', name='B'))['conversation_id']
        with bind_conversation(await self.manager.resolve(b)):
            await self.write('persistent B')
        await self.db.close()
        self.db._initialized = False
        await self.db._init_db()
        self.assertEqual(b, (await self.manager.state())['current_chat_id'])
        with bind_conversation(await self.manager.resolve(b)):
            self.assertIn('persistent B', str(await self.db.get_conversation_messages()))

    async def test_cross_process_lock_released_after_crash(self):
        source = ('from xgent_app.conversations import ExecutionFileLock; import sys,time; '
                  'lock=ExecutionFileLock(sys.argv[1]); print(lock.acquire(),flush=True); time.sleep(60)')
        child = subprocess.Popen([sys.executable, '-c', source, self.path], stdout=subprocess.PIPE,
                                 text=True, cwd=Path(__file__).resolve().parents[1])
        try:
            self.assertEqual('True', child.stdout.readline().strip())
            self.assertTrue(self.manager.lock.busy())
        finally:
            child.kill()
            child.wait(timeout=10)
            child.stdout.close()
        self.assertFalse(self.manager.lock.busy())


    async def test_compression_sequences_and_attachments_are_local(self):
        from xgent_app.compression import save_conversation_export
        first = await self.manager.resolve()
        second = (await self.manager.manage('create', name='B'))['conversation_id']
        sequences = []
        for scope in (first, await self.manager.resolve(second)):
            with bind_conversation(scope):
                await self.write('only-' + scope.conversation_id)
                snapshot = await self.db.get_compression_snapshot()
                self.assertTrue(all(r['session_id'] == scope.conversation_id for r in snapshot['records']))
                bundle = save_conversation_export(Path(self.temp.name) / scope.conversation_id, snapshot,
                                                  system_prompt='', compression_prompt='summarize', access_logs=[])
                event = asyncio.Event()
                entry = await self.db.begin_compression(snapshot, bundle, 1, 'p', 'm', 'test', event)
                entry = await self.db.start_compression_attempt(entry['job_id'], 'p', 'm')
                entry = await self.db.commit_compression(entry, 'summary-' + scope.conversation_id, event)
                sequences.append(entry['sequence'])
                self.assertEqual(entry['generation'], current_scope().generation)
        self.assertEqual([1, 1], sequences)
        for cid in (first.conversation_id, second):
            with bind_conversation(await self.manager.resolve(cid)):
                self.assertEqual('summary-' + cid, (await self.db.get_latest_compression())['summary'])
        with bind_conversation(await self.manager.resolve(first.conversation_id)):
            await self.db.clear_all_conversation_memory()
        with bind_conversation(await self.manager.resolve(second)):
            self.assertIsNotNone(await self.db.get_latest_compression())

    async def test_stale_selected_id_rejected_before_recording(self):
        original = (await self.manager.resolve()).conversation_id
        await self.manager.manage('create', name='B')
        with self.assertRaises(ConversationChanged):
            async with self.manager.operation(original, expected=True, execution=True, fresh=True):
                await self.write('must not be recorded')
        with bind_conversation(await self.manager.resolve(original)):
            self.assertEqual([], await self.db.get_conversation_messages())


    async def test_legacy_migration_keeps_history_and_backs_up_wal_database(self):
        import sqlite3
        from contextlib import closing
        legacy = Path(self.temp.name) / 'legacy.db'
        with closing(sqlite3.connect(legacy)) as conn:
            conn.executescript('''
                CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO config VALUES('attachment_generation','3');
                CREATE TABLE global_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER,user_id INTEGER,
                  msg_type TEXT,role TEXT,content TEXT,timestamp REAL,session_id TEXT,metadata TEXT);
                INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id)
                  VALUES(1,1,'user_text','user','old history',1,NULL);
                CREATE TABLE ui_messages(ui_message_id TEXT PRIMARY KEY,source TEXT,chat_id INTEGER,message_id INTEGER,
                  generation INTEGER,revision INTEGER,timestamp REAL,payload TEXT,UNIQUE(source,chat_id,message_id,generation));
                INSERT INTO ui_messages VALUES('old-ui','telegram',1,1,9,1,1,'{}');
                CREATE TABLE cli_relay_ops(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT,chat_id INTEGER,
                  op TEXT,payload TEXT,created_at REAL);
                INSERT INTO cli_relay_ops(session_id,chat_id,op,payload,created_at)
                  VALUES('old-cli-process',1,'user_echo','{"text":"pending"}',1);
            ''')
        cls = type(self.db)
        db = cls(str(legacy))
        await db._init_db()
        try:
            session = await db.get_session('global_memory')
            self.assertEqual(3, session['generation'])
            with bind_conversation(ConversationScope('global_memory', 3)):
                self.assertEqual('old history', (await db.get_conversation_messages())[0]['content'])
            created = await db.manage_conversation('create', name='new')
            self.assertGreater((await db.get_session(created['conversation_id']))['generation'], 9)
            conn = await db._get_conn()
            row = await (await conn.execute('SELECT payload FROM cli_relay_ops')).fetchone()
            payload = json.loads(row['payload'])
            self.assertEqual('global_memory', payload['conversation_context']['conversation_id'])
            self.assertEqual(3, payload['conversation_context']['generation'])
            backup = Path(str(legacy) + '.pre-conversations-v1.sqlite3')
            self.assertTrue(backup.is_file())
            with closing(sqlite3.connect(backup)) as old:
                self.assertIsNone(old.execute('SELECT session_id FROM global_messages').fetchone()[0])
        finally:
            await db.close()


    async def test_completion_is_published_after_execution_slot_releases(self):
        from unittest.mock import patch
        from xgent_app.web_bridge import WebOutbox
        outbox = WebOutbox()
        with outbox.subscribe() as sub, patch('xgent_app.conversations._manager', self.manager):
            async with self.manager.operation(execution=True):
                outbox.put({'type': 'turn_end'})
                self.assertTrue(self.manager.lock.busy())
                self.assertIsNone(sub.get(timeout=.01))
            self.assertFalse(self.manager.lock.busy())
            frame = sub.get(timeout=.1)
            self.assertEqual('turn_end', frame['type'])
            self.assertEqual('global_memory', frame['conversation_id'])


    async def test_default_title_uses_first_prompt_and_survives_clear(self):
        cid = (await self.manager.manage('create'))['conversation_id']
        with bind_conversation(await self.manager.resolve(cid)):
            await self.write('[后台任务结果] 不应该成为标题')
            self.assertEqual('新对话', (await self.db.get_session(cid))['name'])
            await self.write('  排查 Python 日志\n并修复上传问题  ')
            expected = '排查 Python 日志 并修复上传问题'
            self.assertEqual(expected, (await self.db.get_session(cid))['name'])
            await self.write('这是另一个问题')
            self.assertEqual(expected, (await self.db.get_session(cid))['name'])
            await self.db.clear_all_conversation_memory()
            self.assertEqual(expected, (await self.db.get_session(cid))['name'])
        await self.manager.manage('rename', cid, '自己指定的名称')
        with bind_conversation(await self.manager.resolve(cid)):
            await self.write('新消息不能覆盖手动名称')
        listing = {item['id']: item for item in await self.db.get_all_sessions()}
        self.assertEqual('自己指定的名称', listing[cid]['display_title'])
        self.assertNotIn('first_user_text', listing[cid])

    async def test_sidebar_activity_order_does_not_change_when_viewing(self):
        a = (await self.manager.manage('create', name='older'))['conversation_id']
        b = (await self.manager.manage('create', name='newer'))['conversation_id']
        before = [row['id'] for row in await self.db.get_all_sessions()]
        self.assertLess(before.index(b), before.index(a))
        await self.manager.manage('switch', a)
        after = [row['id'] for row in await self.db.get_all_sessions()]
        self.assertEqual(before, after)


class ScopeTransportTests(unittest.TestCase):
    def test_replay_does_not_inherit_worker_conversation(self):
        from xgent_app.conversations import replay_conversation
        from xgent_app.fanout import Op
        with bind_conversation(ConversationScope('A', 10, 'run-a')):
            row = Op('send', payload={'text': 'A result'}).to_row()
        with bind_conversation(ConversationScope('B', 11, 'run-b')):
            op = Op.from_row(row)
            with replay_conversation(op.conversation_context):
                self.assertEqual('A', current_scope().conversation_id)
            with replay_conversation(None):
                self.assertIsNone(current_scope(required=False))
            self.assertEqual('B', current_scope().conversation_id)

    def test_reset_and_old_end_cannot_remove_other_live_turns(self):
        from xgent_app.web_bridge import WebOutbox
        outbox = WebOutbox()
        def message(cid, generation, run, mid):
            with bind_conversation(ConversationScope(cid, generation, run)):
                outbox.put({'type': 'message', 'message_id': mid, 'text': cid,
                            'reply_markup': [[{'callback_data': 'act_stop_generation:' + run}]]})
        message('A', 10, 'old-a', 1)
        message('A', 10, 'new-a', 2)
        message('B', 11, 'b', 3)
        with bind_conversation(ConversationScope('A', 10, 'old-a')):
            outbox.put({'type': 'generation_end'})
        self.assertEqual([2], [f['message_id'] for f in outbox.snapshot('A')['frames']])
        with bind_conversation(ConversationScope('A', 10)):
            outbox.put({'type': 'turn_end'})  # A menu ending must not end an active model turn.
        self.assertEqual([2], [f['message_id'] for f in outbox.snapshot('A')['frames']])
        with bind_conversation(ConversationScope('B', 12)):
            outbox.put({'type': 'history_reset'})
        self.assertEqual([2], [f['message_id'] for f in outbox.snapshot()['frames']])

    def test_secret_names_and_redaction_registration_do_not_collide(self):
        from xgent_app.agent_ask import SecretStore
        store = SecretStore()
        removed = []
        store.unregister_hook = removed.append
        store.set('A', 'PASSWORD', 'shared-value')
        store.set('B', 'PASSWORD', 'shared-value')
        store.purge('A')
        self.assertEqual([], removed)
        self.assertEqual({'PASSWORD': 'shared-value'}, store.environment('B'))
        store.set('B', 'PASSWORD', 'new-value')
        self.assertEqual(['shared-value'], removed)

    def test_callback_version_rejects_other_conversation(self):
        from xgent_app.conversations import decode_conversation_callback
        with bind_conversation(ConversationScope('A', 5)):
            self.assertEqual('conv_archive', decode_conversation_callback('cv:5:conv_archive'))
        with bind_conversation(ConversationScope('B', 6)):
            with self.assertRaises(ConversationChanged):
                decode_conversation_callback('cv:5:conv_archive')


    def test_native_label_does_not_change_unscoped_text_or_duplicate_prefix(self):
        from xgent_app.conversations import conversation_labelled_text
        self.assertEqual('body', conversation_labelled_text('body'))
        with bind_conversation(ConversationScope('abc123', 2, 'run', '开发')):
            labelled = conversation_labelled_text('body')
            self.assertEqual('body', labelled.splitlines()[-1])
            self.assertIn('开发', labelled)
            self.assertEqual(labelled, conversation_labelled_text(labelled))
