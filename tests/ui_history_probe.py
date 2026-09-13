"""Exercise real menu routing, SQLite persistence, and output bridges in isolation."""

import asyncio
import zipfile
from unittest.mock import AsyncMock, patch

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from tests.compression_request_probe import CompressionHarness
from xgent_app.ui_history import UiHistoryError, advance_ui_generation, ui_operation, without_ui_history
from xgent_app.web_bridge import WebBot, MirrorBot, install_tg_to_web_mirror, _markup_to_frame, markup_from_frame


LONG_SKILL = 'private/' + '\u4e2d\u6587\u6280\u80fd\u8def\u5f84' * 12 + '.md'


async def menus(bot):
    return [row for row in await bot._web_read_history(0) if row['msg_type'] == 'ui_message']


def button(menu, prefix):
    return next(item for row in menu['reply_markup'] for item in row
                if item.get('callback_data', '').startswith(prefix))


async def click(bot, h, menu, prefix):
    item = button(menu, prefix)
    await bot._web_handle_ui_callback(menu['ui_message_id'], menu['revision'], item['button_id'], h.outbox)


def prepare(bot, h):
    bot._ensure_web_command_map()
    h.stack.enter_context(patch.object(bot, 'list_skill_files', lambda: [LONG_SKILL]))


async def navigation(bot, root):
    async with CompressionHarness(bot, root) as h:
        prepare(bot, h)
        await bot._web_handle_command('/start', h.outbox)
        first = (await menus(bot))[0]
        await click(bot, h, first, 'menu_more_settings')
        await click(bot, h, (await menus(bot))[0], 'menu_skills')
        current = (await menus(bot))[0]
        assert current['ui_message_id'] == first['ui_message_id']
        assert LONG_SKILL.replace('/', '|') in button(current, 'set_skill_state:')['callback_data']
        bot.CallbackDataStore._store.clear()
        await click(bot, h, current, 'set_skill_state:')
        current = (await menus(bot))[0]
        assert bot.get_skill_state(LONG_SKILL) == 'disabled'
        assert len(await menus(bot)) == 1 and current['revision'] == first['revision'] + 3
        assert current['timestamp'] == first['timestamp']
        visible = await bot._web_read_history(0)
        assert not any(row['msg_type'] == 'button_click' for row in visible)
        assert not any('Skill ' in row['content'] and row['msg_type'] == 'system_op' for row in visible)
        snapshot = await h.db.get_compression_snapshot()
        assert sum(row['msg_type'] == 'button_click' for row in snapshot['records']) == 3
        assert not any(row['msg_type'] == 'ui_message' for row in snapshot['records'])
        await bot._web_handle_command('/start', h.outbox)
        assert len(await menus(bot)) == 2
        return {'final_state_per_menu': True, 'long_callback': True, 'audit_display_only': True}


async def restart(bot, root):
    async with CompressionHarness(bot, root) as h:
        prepare(bot, h)
        saved = await menus(bot)
        assert len(saved) == 2 and bot.get_skill_state(LONG_SKILL) == 'disabled'
        assert not bot.CallbackDataStore._store
        await click(bot, h, saved[0], 'set_skill_state:')
        assert bot.get_skill_state(LONG_SKILL) == 'hidden'
        current = (await menus(bot))[0]
        await click(bot, h, current, 'menu_more_settings')
        await click(bot, h, (await menus(bot))[0], 'act_main_menu')
        assert len(await menus(bot)) == 2
        assert (await menus(bot))[0]['ui_message_id'] == saved[0]['ui_message_id']
        return {'restart_buttons_work': True, 'back_edits_original': True}


async def validation(bot, root):
    async with CompressionHarness(bot, root) as h:
        prepare(bot, h)
        await bot._web_handle_command('/skills', h.outbox)
        initial = (await menus(bot))[0]
        h.drain_frames()
        await asyncio.gather(*(click(bot, h, initial, 'set_skill_state:') for _ in range(2)))
        assert bot.get_skill_state(LONG_SKILL) == 'disabled'
        assert (await menus(bot))[0]['revision'] == initial['revision'] + 1
        assert any(frame['type'] == 'callback_answer' and frame.get('show_alert') for frame in h.drain_frames())
        current = (await menus(bot))[0]
        await bot._web_handle_ui_callback(current['ui_message_id'], current['revision'], '99:99', h.outbox)
        assert bot.get_skill_state(LONG_SKILL) == 'disabled'
        assert (await menus(bot))[0]['revision'] == current['revision']
        with patch.object(bot, 'list_skill_files', lambda: []):
            await click(bot, h, current, 'set_skill_state:')
        assert bot.get_skill_state(LONG_SKILL) == 'disabled'
        web = WebBot(h.outbox, 1)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Confirm', callback_data='act_confirm_prompt:assistant_prompt')]])
        bot.UserDataManager.set('state', bot.BotState.SET_PROMPT)
        async with ui_operation(capture_text=True):
            await web.send_message(1, 'UI-CONFIRMATION', reply_markup=keyboard)
        confirmation = (await menus(bot))[-1]
        bot.UserDataManager.set('state', bot.BotState.IDLE)
        with patch.object(bot, 'handle_button_click', AsyncMock()) as execute:
            await click(bot, h, confirmation, 'act_confirm_prompt:')
            execute.assert_not_called()
        async with ui_operation(capture_text=True):
            await web.send_message(1, 'UI-PROVIDER', reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('Select', callback_data='set_mdl|chat|p|vision-model')],
            ]))
        provider_menu = (await menus(bot))[-1]
        await h.db.delete_provider('p')
        await bot.UserDataManager.reload_providers()
        with patch.object(bot, 'handle_button_click', AsyncMock()) as execute:
            await click(bot, h, provider_menu, 'set_mdl|')
            execute.assert_not_called()
        bot.UserDataManager.set('temp_viewing_prov', 'old-provider')
        async with ui_operation(capture_text=True):
            await web.send_message(1, 'UI-WORKFLOW', reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('Select', callback_data='pick_default_model')],
            ]))
        workflow = (await menus(bot))[-1]
        bot.UserDataManager.set('temp_viewing_prov', 'other-window-provider')
        with patch.object(bot, 'handle_button_click', AsyncMock()) as execute:
            await click(bot, h, workflow, 'pick_default_')
            execute.assert_not_called()
        before_clear = current
        await h.db.clear_all_conversation_memory()
        with patch.object(bot, 'handle_button_click', AsyncMock()) as execute:
            await click(bot, h, before_clear, 'set_skill_state:')
            execute.assert_not_called()
        assert not await menus(bot)
        return {'serialized_clicks': True, 'forgery': True, 'missing_skill': True,
                'expired_confirmation': True, 'clear_revokes': True, 'missing_provider': True,
                'other_window_workflow': True}


async def mutations(bot, root):
    async with CompressionHarness(bot, root) as h:
        prepare(bot, h)
        web = WebBot(h.outbox, 1)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Menu', callback_data='act_main_menu')]])
        async with ui_operation(capture_text=True):
            msg = await web.send_message(1, '<b>UI-ONLY-NEVER-EXPORT</b>', parse_mode='HTML', reply_markup=keyboard)
            await msg.edit_text('UI-ONLY-FINAL', reply_markup=keyboard)
            await msg.edit_reply_markup(InlineKeyboardMarkup([[InlineKeyboardButton('Skills', callback_data='menu_skills')]]))
        saved = (await menus(bot))[0]
        assert saved['revision'] == 3 and saved['content'] == 'UI-ONLY-FINAL'
        assert button(saved, 'menu_skills')
        await bot.GlobalRecorder.record_user_message('USER-REMAINS')
        for index in range(40):
            await bot.GlobalRecorder.record_button_click(str(index))
            await bot.GlobalRecorder.record_system_op('internal setting')
        await bot.GlobalRecorder.record_system_message('VISIBLE-ERROR')
        assert [row['content'] for row in await bot._web_read_history(2)] == ['USER-REMAINS', 'VISIBLE-ERROR']
        _, bundle = await bot.create_conversation_export()
        with zipfile.ZipFile(bundle['archive_path']) as archive:
            text = '\n'.join(archive.read(name).decode() for name in archive.namelist())
            assert 'UI-ONLY-' not in text and 'internal setting' in text
        await msg.delete()
        assert not await menus(bot)
        history = await bot._web_read_history(0)
        assert history.tombstones == [{'ui_message_id': saved['ui_message_id'], 'revision': 4, 'ui_generation': 0}]
        try:
            await msg.edit_text('MUST-NOT-REVIVE', reply_markup=keyboard)
            raise AssertionError('deleted menu revived')
        except UiHistoryError:
            pass
        async with ui_operation(capture_text=True):
            await h.db.clear_all_conversation_memory()
            try:
                await web.send_message(1, 'STALE-TASK', reply_markup=keyboard)
                raise AssertionError('stale task revived')
            except UiHistoryError:
                pass
        assert not await menus(bot)
        await without_ui_history(web.send_message)(1, 'ALREADY-RECORDED', reply_markup=keyboard)
        await web.send_message(1, 'Transient', reply_markup=bot.build_stop_keyboard())
        assert not await menus(bot)
        async with ui_operation(capture_text=True):
            gate = asyncio.Event()
            async def delayed_menu():
                await gate.wait()
                await bot.GlobalRecorder.record_system_op('OLD-CHILD-OP')
                await web.send_message(1, 'OLD-CHILD', reply_markup=keyboard)
            child = asyncio.create_task(delayed_menu())
            await h.db.clear_all_conversation_memory()
            await advance_ui_generation()
            gate.set()
            try:
                await child
                raise AssertionError('child inherited the new generation')
            except UiHistoryError:
                pass
            await web.send_message(1, 'FRESH-AFTER-CLEAR', reply_markup=keyboard)
        assert [row['content'] for row in await menus(bot)] == ['FRESH-AFTER-CLEAR']
        assert not (await h.db.get_compression_snapshot())['records']
        return {'edit_markup': True, 'limits_after_filtering': True, 'not_exported': True,
                'tombstone': True, 'generation_race': True, 'no_duplicate_ai': True, 'child_generation': True}


async def persistence_failure(bot, root):
    async with CompressionHarness(bot, root) as h:
        prepare(bot, h)
        await bot._web_handle_command('/skills', h.outbox)
        saved = (await menus(bot))[0]
        h.drain_frames()
        with patch.object(h.db, 'apply_ui_frame', AsyncMock(side_effect=OSError('disk full'))):
            await click(bot, h, saved, 'set_skill_state:')
        assert bot.get_skill_state(LONG_SKILL) == 'disabled'
        assert (await menus(bot))[0]['revision'] == saved['revision']
        assert any(frame['type'] == 'callback_answer' and '\u4fdd\u5b58\u5931\u8d25' in frame.get('text', '')
                   for frame in h.drain_frames())
        return {'saved_configuration_not_rolled_back': True, 'failure_is_visible': True}


async def bridges(bot, root):
    async with CompressionHarness(bot, root) as h:
        prepare(bot, h)
        web_app = InlineKeyboardMarkup([[InlineKeyboardButton('Web', web_app=WebAppInfo(url='https://example.com'))]])
        restored_app = markup_from_frame(_markup_to_frame(web_app))
        assert restored_app.inline_keyboard[0][0].web_app.url == 'https://example.com'
        action = 'set_skill_state:disabled:' + LONG_SKILL.replace('/', '|')
        short = bot.CallbackDataStore.store(action)
        rows = [[{'text': 'Skill', 'callback_data': short, 'callback_action': action}]]
        bot.CallbackDataStore._store.clear()
        mirror = MirrorBot(h.outbox, 1, real_bot=None, ui_source='cli:test')
        payload = {'message_id': 9, 'text': 'CLI-MENU', 'reply_markup': rows,
                   'ui_context': {'generation': 0, 'capture_text': True, 'guard': ''}}
        await bot._replay_relay_op(mirror, 'send_message', payload)
        assert button((await menus(bot))[0], 'set_skill_state:')['callback_data'] == action
        await h.db.clear_all_conversation_memory()
        try:
            await bot._replay_relay_op(mirror, 'send_message', payload)
            raise AssertionError('old CLI task revived')
        except UiHistoryError:
            pass
        assert not await menus(bot)

        class TelegramStub:
            async def send_message(self, *args, **kwargs):
                return type('Reply', (), {'message_id': 42})()
            async def edit_message_text(self, *args, **kwargs):
                return True
            async def edit_message_reply_markup(self, *args, **kwargs):
                return True
            async def delete_message(self, *args, **kwargs):
                return True
            send_chat_action = send_document = send_photo = send_message

        stub = TelegramStub()
        restore = install_tg_to_web_mirror(stub, h.outbox)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Menu', callback_data='act_main_menu')]])
        try:
            async with ui_operation(capture_text=True):
                await stub.send_message(17, 'TG-MENU', reply_markup=keyboard)
                await stub.edit_message_text('TG-UPDATED', 17, 42, reply_markup=keyboard)
                await stub.edit_message_reply_markup(17, 42, reply_markup=keyboard)
                saved = (await menus(bot))[0]
                assert saved['revision'] == 3 and saved['content'] == 'TG-UPDATED'
                await stub.delete_message(17, 42)
                assert not await menus(bot)
        finally:
            restore()
        return {'canonical_cli_actions': True, 'cli_clear_race': True, 'telegram_positional_calls': True}
