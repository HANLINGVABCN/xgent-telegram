"""Real SQLite transactions and OS locks; no Telegram or model network calls."""
import asyncio
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests import test_conversations as support
from xgent_app.conversations import ConversationChanged, bind_conversation, current_scope
from xgent_app.interaction import interaction
from xgent_app.message_queue import MessageQueue, batch_messages, mark_outcome


class MessageQueueTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = support.ConversationTests.asyncTearDown
    write = support.ConversationTests.write

    async def asyncSetUp(self):
        await support.ConversationTests.asyncSetUp(self)
        self.patch = patch('xgent_app.conversations._manager', self.manager)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.delivered = []
        async def dispatch(batch):
            self.delivered.append(batch)
        self.queue = self.manager.text_queue = MessageQueue(self.manager, dispatch)
        await self.queue.start()
        with interaction('web', 'chat'):
            self.scope = await self.manager.resolve()

    async def submit(self, text='queued', scope=None, request_key=None):
        with bind_conversation(scope or self.scope):
            return await self.queue.submit(text, chat_id=1, request_key=request_key)

    async def test_text_hidden_until_atomic_claim_and_batch_not_depth_limited(self):
        first = await self.submit('ONE')
        await self.submit('TWO')
        with bind_conversation(self.scope):
            self.assertEqual([], await self.db.get_conversation_messages(1))
            self.assertEqual([], (await self.db.get_export_snapshot())['records'])
        async with self.manager.operation(self.scope, execution=True):
            batch = await self.queue.claim_continuation()
            self.assertEqual(['ONE', 'TWO'], [m['content'] for m in batch_messages(batch)])
            self.assertEqual([], await self.queue.claim_continuation())
            self.assertEqual([], await self.db.get_conversation_messages(1, before_record_id=batch[0]['record_id']))
            with self.assertRaisesRegex(ConversationChanged, '已接入'):
                await self.db.queue_change(self.scope.conversation_id, self.scope.generation, first['id'], first['revision'], 'cancel')
        self.assertEqual('completed', (await self.db.queue_item(first['id']))['status'])

    async def test_idempotent_request_not_content_deduplication(self):
        a = await self.submit('SAME', request_key='web:fixture-request')
        b = await self.submit('SAME', request_key='web:fixture-request')
        c = await self.submit('SAME', request_key='web:another-request')
        self.assertEqual(a['id'], b['id'])
        self.assertNotEqual(a['id'], c['id'])
        with self.assertRaises(ConversationChanged):
            await self.submit('DIFFERENT', request_key='web:fixture-request')

    async def test_cancel_and_edit_are_atomic_idempotent_and_not_model_history(self):
        a, b = await self.submit('cancel me'), await self.submit('recover  text\nexact')
        cid, gen = self.scope.conversation_id, self.scope.generation
        await self.db.queue_change(cid, gen, a['id'], a['revision'], 'cancel')
        await self.db.queue_change(cid, gen, a['id'], a['revision'], 'cancel')
        edited = await self.db.queue_change(cid, gen, b['id'], b['revision'], 'edit', 'retrieve-one')
        again = await self.db.queue_change(cid, gen, b['id'], b['revision'], 'edit', 'retrieve-one')
        self.assertEqual(edited, again)
        self.assertEqual('recover  text\nexact', again['text'])
        self.assertEqual([], (await self.queue.snapshot(cid))['items'])
        with bind_conversation(self.scope):
            self.assertEqual([], await self.db.get_conversation_messages())

    async def test_cancel_claim_race_has_only_one_winner(self):
        a = await self.submit()
        async with self.manager.operation(self.scope, execution=True):
            results = await asyncio.gather(self.queue.claim_continuation(), self.db.queue_change(
                self.scope.conversation_id, self.scope.generation, a['id'], a['revision'], 'cancel'), return_exceptions=True)
            saved = await self.db.queue_item(a['id'])
            if saved['status'] == 'claimed':
                self.assertEqual(1, len(results[0]))
                self.assertIsInstance(results[1], ConversationChanged)
            else:
                self.assertEqual('cancelled', saved['status'])
                self.assertEqual([], results[0])

    async def test_other_conversation_waits_and_original_selection_is_unchanged(self):
        b = (await self.manager.manage('create', name='B'))['conversation_id']
        with interaction('web', 'chat'):
            other = await self.manager.resolve(b)
        await self.submit('B', other)
        async with self.manager.operation(self.scope, execution=True):
            await self.submit('A')
            batch = await self.queue.claim_continuation()
            self.assertEqual(['A'], [i['text'] for i in batch])
            self.assertEqual(b, (await self.manager.state())['current_chat_id'])
        await self.queue.poll()
        if self.queue._task:
            await self.queue._task
        self.assertEqual(['B'], [i['text'] for group in self.delivered for i in group])

    async def test_stop_pauses_all_and_resume_only_one(self):
        b = (await self.manager.manage('create', name='B'))['conversation_id']
        with interaction('web', 'chat'):
            other = await self.manager.resolve(b)
        async with self.manager.operation(self.scope, execution=True):
            await self.submit('A')
            await self.submit('B', other)
            await self.manager.request_stop(current_scope().run_id)
            self.assertEqual([], await self.queue.claim_continuation())
        for cid in (b, self.scope.conversation_id):
            self.assertTrue((await self.queue.snapshot(cid))['paused'])
        await self.submit('A new')
        self.assertIsNone(await self.db.queue_head())
        with bind_conversation(other):
            await self.db.queue_resume(other, self.queue.instance)
        self.assertEqual('B', (await self.db.queue_head())['text'])

    async def test_failure_pauses_but_tool_nonzero_is_not_run_failure(self):
        async with self.manager.operation(self.scope, execution=True):
            await self.submit()
            mark_outcome('failed')
        self.assertTrue((await self.queue.snapshot(self.scope.conversation_id))['paused'])

    async def test_failure_before_dispatch_pauses_instead_of_retrying_forever(self):
        item = await self.submit('must remain recoverable')
        self.manager.code_root = Path(self.temp.name)
        self.manager.object_root = Path(self.temp.name) / 'objects'
        with patch('xgent_app.conversation_branching.begin_turn',
                   side_effect=OSError('fixture admission failure')):
            await self.queue.poll()
            self.assertIsNotNone(self.queue._task)
            await self.queue._task
        saved = await self.db.queue_item(item['id'])
        self.assertEqual('paused', saved['status'])
        self.assertEqual('failed', saved['reason'])
        self.assertIsNone(await self.db.queue_head())
        self.assertFalse(self.manager.lock.busy())

    async def test_ask_wait_submit_cancel_and_stop_precedence(self):
        with bind_conversation(self.scope):
            await self.db.queue_wait_ask(self.scope, self.queue.instance)
            a = await self.submit()
            self.assertEqual('ask', a['reason'])
            with self.assertRaisesRegex(ConversationChanged, '表单'):
                await self.db.queue_resume(self.scope, self.queue.instance)
            await self.db.queue_resume(self.scope, self.queue.instance, ask=True)
            self.assertEqual('pending', (await self.db.queue_item(a['id']))['status'])
            await self.db.queue_wait_ask(self.scope, self.queue.instance)
            await self.db.queue_resume(self.scope, self.queue.instance, ask=True, cancel_ask=True)
            self.assertEqual('ask_cancelled', (await self.db.queue_item(a['id']))['reason'])
            await self.db.queue_wait_ask(self.scope, self.queue.instance)
            await self.db.queue_pause_all('stopped')
            await self.db.queue_resume(self.scope, self.queue.instance, ask=True)
            self.assertEqual('stopped', (await self.db.queue_item(a['id']))['reason'])

    async def test_generation_change_keeps_editable_but_not_deliverable_text(self):
        a = await self.submit('old context')
        with bind_conversation(self.scope):
            await self.db.clear_all_conversation_memory()
        snapshot = await self.queue.snapshot(self.scope.conversation_id)
        self.assertEqual('invalid', snapshot['items'][0]['status'])
        self.assertIsNone(await self.db.queue_head())
        recovered = await self.db.queue_change(self.scope.conversation_id, self.scope.generation,
            a['id'], snapshot['items'][0]['revision'], 'edit', 'recover-invalid')
        self.assertEqual(a['text'], recovered['text'])

    async def test_source_liveness_uses_os_lock_not_pid_and_new_client_does_not_pause(self):
        a = await self.submit()
        from xgent_app.conversations import ConversationManager
        peer = ConversationManager(self.manager.factory, self.path)
        peer.text_queue = MessageQueue(peer, lambda batch: None)
        await peer.text_queue.start()
        try:
            await peer.text_queue.poll()
            self.assertEqual('pending', (await self.db.queue_item(a['id']))['status'])
            self.queue._alive.release()  # Simulate source crash, not a graceful pause.
            await peer.text_queue.poll()
            self.assertEqual('restart', (await self.db.queue_item(a['id']))['reason'])
        finally:
            await peer.close()

    async def test_claim_rollback_never_partially_records_batch(self):
        await self.submit('one')
        await self.submit('two')
        original = self.db._insert_global_record
        calls = 0
        async def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('fixture disk error')
            return await original(*args)
        async with self.manager.operation(self.scope, execution=True):
            with patch.object(self.db, '_insert_global_record', fail_second):
                with self.assertRaises(OSError):
                    await self.queue.claim_continuation()
            self.assertEqual([], await self.db.get_conversation_messages())
            self.assertEqual(2, len((await self.queue.snapshot(self.scope.conversation_id))['items']))
            mark_outcome('failed')

    async def test_telegram_receipt_updates_one_native_message_even_after_delivery_restart(self):
        from types import SimpleNamespace
        from xgent_app.fanout import Op, OP_SEND
        from xgent_app.web_bridge import deliver_op_to_bot
        native_messages, events = {}, []
        class Native:
            async def send_message(self, **kwargs):
                mid = len(native_messages) + 1
                native_messages[mid] = kwargs
                events.append(('send', mid))
                return SimpleNamespace(message_id=mid)
            async def edit_message_text(self, **kwargs):
                assert kwargs['message_id'] in native_messages
                native_messages[kwargs['message_id']].update(kwargs)
                events.append(('edit', kwargs['message_id']))
        native = Native()
        async with self.manager.operation(self.scope, execution=True):
            item = await self.submit('native queued', replace(self.scope, origin='telegram'))
            self.assertTrue(item['receipt_requested'])
            op = lambda: Op(OP_SEND, chat_id=1, logical_id=-100, payload={'queue_receipt': item['id']})
            await deliver_op_to_bot(native, op(), None)
            await self.db.queue_change(self.scope.conversation_id, self.scope.generation, item['id'], item['revision'], 'cancel')
            await deliver_op_to_bot(native, op(), None)
            await deliver_op_to_bot(native, op(), None)
        self.assertEqual([('send', 1), ('edit', 1)], events)
        self.assertIn('已取消', native_messages[1]['text'])
        self.assertIsNone(native_messages[1]['reply_markup'])

    async def test_archive_restore_and_delete_do_not_auto_send_or_leak_pending_text(self):
        item = await self.submit('private pending')
        await self.manager.manage('archive', self.scope.conversation_id)
        self.assertEqual('archived', (await self.queue.snapshot(self.scope.conversation_id))['reason'])
        await self.manager.manage('restore', self.scope.conversation_id)
        self.assertIsNone(await self.db.queue_head())
        await self.db.begin_conversation_delete(self.scope.conversation_id)
        await self.db.finish_conversation_delete(self.scope.conversation_id)
        self.assertIsNone(await self.db.queue_item(item['id']))

    async def test_dead_run_is_interrupted_not_requeued(self):
        item = await self.submit()
        async with self.manager.operation(self.scope, execution=True):
            await self.queue.claim_continuation()
            mark_outcome('interrupted')
        self.assertEqual('interrupted', (await self.db.queue_item(item['id']))['status'])
        self.assertIsNone(await self.db.queue_head())

    async def test_v3_upgrade_backs_up_preserves_history_and_is_idempotent(self):
        from pathlib import Path
        from xgent_app.conversations import SCHEMA_VERSION
        with bind_conversation(self.scope):
            await self.write('historical text')
        await self.db.set_config('conversation_schema_version', 3)
        await self.db.close()
        await self.db._init_db()
        self.assertTrue(Path(self.path + f'.pre-conversations-v{SCHEMA_VERSION}.sqlite3').exists())
        await self.db.migrate_conversations()
        self.assertEqual(SCHEMA_VERSION, await self.db.get_config_fresh('conversation_schema_version'))
        with bind_conversation(self.scope):
            self.assertEqual('historical text', (await self.db.get_conversation_messages())[0]['content'])


    async def test_resume_cas_and_retry_do_not_override_a_new_stop(self):
        await self.submit()
        await self.db.queue_pause_all('stopped')
        saved = await self.queue.snapshot(self.scope.conversation_id)
        with bind_conversation(self.scope):
            await self.submit('later input')
            with self.assertRaisesRegex(ConversationChanged, '队列已更新'):
                await self.db.queue_resume(self.scope, self.queue.instance, revision=saved['revision'], request_key='resume-once')
            saved = await self.queue.snapshot(self.scope.conversation_id)
            await self.db.queue_resume(self.scope, self.queue.instance, revision=saved['revision'], request_key='resume-once')
            await self.db.queue_pause_all('stopped')
            await self.db.queue_resume(self.scope, self.queue.instance, revision=saved['revision'], request_key='resume-once')
        self.assertIsNone(await self.db.queue_head())

    async def test_management_receipt_survives_restart_without_secret_data(self):
        with bind_conversation(self.scope):
            receipt = await self.db.queue_accept_management(self.scope, 'web:opaque-request')
            self.assertTrue(receipt['new'])
        await self.db.close()
        await self.db._init_db()
        self.assertEqual('management', (await self.db.queue_request('web:opaque-request'))['kind'])
        with self.assertRaisesRegex(ConversationChanged, '管理输入'):
            await self.submit('a secret must not become a prompt', request_key='web:opaque-request')
        self.assertEqual([], (await self.queue.snapshot(self.scope.conversation_id))['items'])

    async def test_separate_sqlite_connections_cancel_and_claim_only_once(self):
        peer = type(self.db)(self.path)
        await peer._get_conn()
        try:
            item = await self.submit()
            async with self.manager.operation(self.scope, execution=True):
                result = await asyncio.gather(self.queue.claim_continuation(), peer.queue_change(self.scope.conversation_id,
                    self.scope.generation, item['id'], item['revision'], 'cancel'), return_exceptions=True)
                final = await self.db.queue_item(item['id'])
                self.assertIn(final['status'], {'claimed','cancelled'})
                self.assertEqual(bool(final['record_id']), final['status'] == 'claimed')
                self.assertEqual(bool(result[0]), final['status'] == 'claimed')
        finally:
            await peer.close()
