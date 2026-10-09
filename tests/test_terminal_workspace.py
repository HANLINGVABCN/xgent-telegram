"""Independent terminal selections, history routing and non-destructive reconciliation."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_conversations as support
from xgent_app.cli_tui import PtScreen, title_fragments
from xgent_app.cli_render import Palette
from xgent_app.conversations import ConversationManager, ConversationChanged, ConversationScope, bind_conversation
from xgent_app.interaction import interaction
from xgent_app.terminal_workspace import TerminalWorkspace


class TerminalWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ConversationTests.asyncSetUp
    asyncTearDown = support.ConversationTests.asyncTearDown
    write = support.ConversationTests.write

    async def test_two_terminals_do_not_change_web_bot_or_each_other(self):
        first = self.manager
        second = ConversationManager(first.factory, self.path)
        try:
            initial = await self.db.get_conversation_state()
            await first.enable_terminal()
            await second.enable_terminal()
            a = initial['current_chat_id']
            b = (await first.manage('create', name='first terminal B', selector='terminal'))['conversation_id']
            self.assertEqual(a, (await self.db.get_conversation_state())['current_chat_id'])
            self.assertEqual(a, (await self.db.get_conversation_state())['telegram_conversation_id'])
            self.assertEqual(a, (await second.state())['terminal_conversation_id'])
            c = (await second.manage('create', name='second terminal C', selector='terminal'))['conversation_id']
            await self.db.manage_conversation('switch', c)
            await self.db.manage_conversation('switch', c, selector='telegram')
            self.assertEqual(b, (await first.state())['terminal_conversation_id'])
            await self.db.manage_conversation('archive', b)
            self.assertIsNone((await first.state())['terminal_conversation_id'])
            self.assertEqual(c, (await second.state())['terminal_conversation_id'])
            await self.db.manage_conversation('restore', b)
            with self.assertRaises(ConversationChanged):
                await first.resolve(selector='terminal')
            await first.manage('switch', b, selector='terminal')
            with self.assertRaises(ConversationChanged):
                await first.resolve(a, expected=True, selector='terminal')
            conn = await self.db._get_conn()
            row = await (await conn.execute("SELECT COUNT(*) FROM config WHERE key='terminal_conversation_id'")).fetchone()
            self.assertEqual(0, row[0])
        finally:
            await second.close()

    async def test_delayed_archive_observation_cannot_clear_a_new_selection(self):
        await self.manager.enable_terminal()
        a = (await self.manager.state())['terminal_conversation_id']
        b = (await self.db.manage_conversation('create', name='B'))['conversation_id']
        original = self.db.get_session
        waiting, release = asyncio.Event(), asyncio.Event()
        first = True
        async def slow(cid):
            nonlocal first
            if cid == a and first:
                first = False
                waiting.set()
                await release.wait()
                return {'id':a,'archived':True}
            return await original(cid)
        with patch.object(self.db, 'get_session', slow):
            observer = asyncio.create_task(self.manager.state())
            await waiting.wait()
            await self.manager.manage('switch', b, selector='terminal')
            release.set()
            self.assertEqual(b, (await observer)['terminal_conversation_id'])
        self.assertEqual(b, (await self.manager.state())['terminal_conversation_id'])

    async def test_history_switch_and_background_output_are_separate(self):
        await self.manager.enable_terminal()
        a = await self.manager.resolve(selector='terminal')
        with bind_conversation(a):
            await self.write('only A')
        b = (await self.manager.manage('create', name='B', selector='terminal'))['conversation_id']
        with bind_conversation(await self.manager.resolve(b)):
            await self.write('only B')
        screen = PtScreen(Palette(False))
        work = TerminalWorkspace(self.manager, screen, lambda row: [row['content']])
        with patch('xgent_app.conversations._manager', self.manager):
            await work.start()
            try:
                self.assertEqual(['only B'], [line for msg in screen.model.messages for block in msg.blocks for line in block.lines])
                await work.manage('switch', a.conversation_id)
                self.assertEqual(['only A'], [line for msg in screen.model.messages for block in msg.blocks for line in block.lines])
                with bind_conversation(ConversationScope(b, 1, 'old', name='B')):
                    screen.print_block(['background B'], message_id=22)
                self.assertNotIn('background B', str(screen.model.render_rows()))
                await work.manage('switch', b)
                self.assertIn('background B', str(screen.model.render_rows()))
                with bind_conversation(ConversationScope(b, 1, 'new', name='B')):
                    screen.print_block(['new run'], message_id=23)
                work.begin_run(SimpleNamespace(conversation_id=b, run_id='new'))
                await work.end_run(SimpleNamespace(conversation_id=b, run_id='old'))
                self.assertTrue(screen.model.has(23))
                self.assertFalse(screen.model.has(22))
                self.assertEqual('new', work._local_runs[b])
            finally:
                await work.close()

    async def test_reset_preserves_history_and_refresh_does_not_duplicate(self):
        await self.manager.enable_terminal()
        a = await self.manager.resolve(selector='terminal')
        with bind_conversation(a):
            await self.write('kept history')
        screen = PtScreen(Palette(False))
        work = TerminalWorkspace(self.manager, screen, lambda row: [row['content']])
        with patch('xgent_app.conversations._manager', self.manager):
            await work.start()
            try:
                for _ in range(3):
                    await work.refresh_history(force=True)
                self.assertEqual(1, len(screen.model.messages))
                with bind_conversation(a):
                    await self.db.clear_all_conversation_memory()
                await work.refresh_state()
                await work.refresh_history()
                text = str(screen.model.render_rows())
                self.assertIn('kept history', text)
                self.assertIn('上下文已重置', text)
                self.assertNotEqual(a.generation, work.snapshot()['generation'])
            finally:
                await work.close()

    async def test_pending_form_menu_returns_only_to_its_original_generation(self):
        from xgent_app.cli_bridge import set_visible_conversation, get_last_menu_options, reset_menu_state
        screen = PtScreen(Palette(False))
        screen.conversation_routing = True
        a = await self.manager.resolve()
        b = (await self.manager.manage('create', name='B'))['conversation_id']
        set_visible_conversation(a.conversation_id)
        screen.select_conversation(a.conversation_id, a.generation)
        with bind_conversation(a):
            screen.print_block(['待回答表单'], 88)
            screen.tag_menu(88, True, [('提交', 'askd:form')])
        set_visible_conversation(b)
        screen.select_conversation(b, (await self.manager.resolve(b)).generation)
        self.assertFalse(get_last_menu_options())
        set_visible_conversation(a.conversation_id)
        screen.select_conversation(a.conversation_id, a.generation)
        self.assertEqual(['askd:form'], get_last_menu_options())
        self.assertTrue(screen.model.has(88))
        screen.select_conversation(a.conversation_id, a.generation+100)
        self.assertFalse(get_last_menu_options())
        self.assertFalse(screen.model.has(88))
        reset_menu_state()
        set_visible_conversation(None)


class TerminalTitleTests(unittest.TestCase):
    def test_names_use_top_cells_without_overflow(self):
        from wcwidth import wcswidth
        for width in (24, 40, 80, 120):
            line = ''.join(text for _, text in title_fragments('很长的对话名称'*12, 'provider/model-very-long-name'*4, width))
            self.assertEqual(width, wcswidth(line))
            self.assertTrue(line.startswith(' ☰ '))
            self.assertFalse('\n' in line)
        line = ''.join(text for _, text in title_fragments('日常聊天', 'gemini-3.8', 80))
        self.assertTrue(line.rstrip().endswith('gemini-3.8'))
        self.assertIn('日常聊天', line)
