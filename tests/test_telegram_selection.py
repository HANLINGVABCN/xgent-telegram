"""Independent Bot selection, frozen routing and migration without network."""
import unittest
from unittest.mock import patch
from pathlib import Path

from tests import test_conversations as support
from xgent_app.conversations import ConversationChanged, bind_conversation, current_scope, conversation_snapshot, replay_conversation
from xgent_app.interaction import interaction
from xgent_app.telegram_presentation import render_telegram_text, telegram_label
from xgent_app.fanout import Op, OP_SEND


class TelegramSelectionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ConversationTests.asyncSetUp
    asyncTearDown = support.ConversationTests.asyncTearDown

    async def test_selections_are_independent_and_null_survives_restore_and_restart(self):
        a = (await self.manager.state())['current_chat_id']
        b = (await self.manager.manage('create', name='B'))['conversation_id']
        self.assertEqual(a, (await self.manager.state())['telegram_conversation_id'])
        c = (await self.manager.manage('create', name='Bot C', selector='telegram'))['conversation_id']
        self.assertEqual(b, (await self.manager.state())['current_chat_id'])
        self.assertEqual(c, (await self.manager.resolve(selector='telegram')).conversation_id)
        await self.manager.manage('archive', c)
        self.assertIsNone((await self.manager.state())['telegram_conversation_id'])
        await self.manager.manage('restore', c)
        await self.db.close()
        await self.db._init_db()
        self.assertIsNone((await self.manager.state())['telegram_conversation_id'])
        with self.assertRaises(ConversationChanged):
            await self.manager.resolve(selector='telegram')
        self.assertEqual(b, (await self.manager.state())['current_chat_id'])

    async def test_v2_to_v3_does_not_repeat_alter_or_reassign_history(self):
        a = await self.manager.resolve()
        b = (await self.manager.manage('create', name='B'))['conversation_id']
        with bind_conversation(a):
            await self.db.record_global_message(1, 1, 'user_text', 'user', 'kept A')
        await self.db.set_config('conversation_schema_version', 2)
        conn = await self.db._get_conn()
        await conn.execute("DELETE FROM config WHERE key='telegram_conversation_id'")
        await conn.commit()
        await self.db.close()
        await self.db._init_db()
        self.assertTrue(Path(self.path + '.pre-conversations-v3.sqlite3').exists())
        self.assertEqual(b, (await self.manager.state())['telegram_conversation_id'])
        with bind_conversation(a):
            self.assertIn('kept A', str(await self.db.get_conversation_messages()))
        await self.db.set_config('telegram_conversation_id', None)
        await self.db.migrate_conversations()
        self.assertIsNone((await self.manager.state())['telegram_conversation_id'])

    async def test_delete_detaches_bot_without_changing_other_selection(self):
        a = (await self.manager.state())['telegram_conversation_id']
        b = (await self.manager.manage('create', name='B'))['conversation_id']
        await self.db.begin_conversation_delete(a)
        self.assertIsNone((await self.manager.state())['telegram_conversation_id'])
        self.assertEqual(b, (await self.manager.state())['current_chat_id'])
        await self.db.finish_conversation_delete(a)
        await self.db.close()
        await self.db._init_db()
        self.assertIsNone(await self.db.get_session(a))
        self.assertIsNone((await self.manager.state())['telegram_conversation_id'])

    async def test_routing_frozen_and_foreign_parts_label_at_actual_send(self):
        a = (await self.manager.state())['telegram_conversation_id']
        b = (await self.manager.manage('create', name='B'))['conversation_id']
        with patch('xgent_app.conversations._manager', self.manager):
            with interaction('web'):
                async with self.manager.operation(a, fresh=True):
                    self.assertTrue(current_scope().telegram_delivery)
                    self.assertEqual('hello', await render_telegram_text('hello'))
                    snapshot = conversation_snapshot()
                    await self.manager.manage('switch', b, selector='telegram')
                    self.assertTrue(current_scope().telegram_delivery)
                    self.assertTrue((await render_telegram_text('part 2')).startswith('🗂 '))
                    with telegram_label('inline'):
                        text = await render_telegram_text('<i>↑ 12 tokens</i>', 'HTML')
                        self.assertIn(' · <i>↑ 12 tokens</i>', text)
                        self.assertNotIn('\n', text)
                    self.assertTrue((await render_telegram_text(None, limit=1024)).startswith('🗂 '))
            with replay_conversation(snapshot):
                self.assertTrue((await render_telegram_text('replayed')).startswith('🗂 '))
            with interaction('cli'):
                async with self.manager.operation(a, fresh=True):
                    op = Op(OP_SEND, payload={'text': 'never queued'})
                    self.assertTrue(op.muted)
                    await self.manager.manage('switch', a, selector='telegram')
                    self.assertTrue(Op.from_row(op.to_row()).muted)
            with interaction('web', 'management'):
                async with self.manager.operation(a, fresh=True):
                    self.assertFalse(current_scope().telegram_delivery)
            with interaction('task', 'notification'):
                async with self.manager.operation(b, fresh=True):
                    self.assertTrue(current_scope().telegram_delivery)
                    self.assertTrue((await render_telegram_text('task done')).startswith('🗂 B'))

    async def test_only_exact_legacy_switch_notice_is_suppressed(self):
        payload = {'text': '🗂 A · abcdef\n已切换会话，三端同步；未提交的设置输入已取消。',
                   '_conversation_context': {'conversation_id': 'a', 'generation': 1, 'run_id': None},
                   '_telegram_presentation': {'label': False}}
        self.assertTrue(Op(OP_SEND, payload=payload).muted)
        payload['_conversation_context']['run_id'] = 'real-reply'
        self.assertFalse(Op(OP_SEND, payload=payload).muted)

    async def test_foreign_html_measures_native_display_not_raw_protocol_data(self):
        a = await self.manager.resolve()
        await self.manager.manage('create', name='Bot B', selector='telegram')
        with patch('xgent_app.conversations._manager', self.manager), bind_conversation(a):
            raw = '<blockquote expandable data-raw="' + ('large raw payload '*400) + '">small preview</blockquote>'
            from xgent_app.protocols import ProtocolParser
            # A protocol block keeps its full source for Web, but Telegram adapts it.
            adapted = ProtocolParser.to_telegram_html(raw)
            self.assertLess(len(adapted), 4096)
            self.assertTrue((await render_telegram_text(raw, 'HTML')).startswith('🗂 '))
            html = '<b>' + '&amp;'*1000 + '</b>'
            self.assertTrue((await render_telegram_text(html, 'HTML')).startswith('🗂 '))

    async def test_form_resume_keeps_original_mirror_choice_and_new_run(self):
        from xgent_app.conversations import restore_reply_route
        a = (await self.manager.state())['telegram_conversation_id']
        b = (await self.manager.manage('create', name='B'))['conversation_id']
        with interaction('web'):
            async with self.manager.operation(b, fresh=True):
                original = conversation_snapshot()
                self.assertFalse(current_scope().telegram_delivery)
        await self.manager.manage('switch', b, selector='telegram')
        with interaction('telegram'):
            async with self.manager.operation(execution=True, fresh=True):
                run_id = current_scope().run_id
                restore_reply_route(original)
                self.assertEqual(run_id, current_scope().run_id)
                self.assertFalse(current_scope().telegram_delivery)
                self.assertEqual('web', current_scope().origin)
        await self.manager.manage('switch', a, selector='telegram')
