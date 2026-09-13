import unittest

from tests.test_external_sync import SectionsProbeMixin


class SkillVisibilityTests(SectionsProbeMixin, unittest.TestCase):
    def test_state_change_is_atomic_and_preserves_other_skills(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio
from pathlib import Path
from unittest.mock import patch

async def main():
    await ns['UserDataManager'].init()
    db = await ns['BotMemoryDB'].get_instance()
    root = Path('skill-public')
    root.mkdir(exist_ok=True)
    for name in ('one.md', 'two.md'):
        (root / name).write_text('```!\nSUMMARY\n```', encoding='utf-8')
    await asyncio.gather(ns['save_skill_state']('one.md', 'disabled'), ns['save_skill_state']('two.md', 'hidden'))
    assert ns['get_skill_state']('one.md') == 'disabled'
    assert ns['get_skill_state']('two.md') == 'hidden'
    conn = await db._get_conn()
    async def fail_after_first_write(sql, values):
        await conn.execute(sql, values[0])
        raise OSError('fixture write failure')
    with patch.object(conn, 'executemany', fail_after_first_write):
        try:
            await ns['save_skill_state']('one.md', 'enabled')
        except OSError:
            pass
        else:
            raise AssertionError('write failure swallowed')
    assert ns['get_skill_state']('one.md') == 'disabled'
    assert await db.get_config_fresh('disabled_skills') == ['one.md']
    assert await db.get_config_fresh('hidden_skills') == ['two.md']
    for path, state in (('missing.md', 'enabled'), ('one.md', 'invalid')):
        try:
            await ns['save_skill_state'](path, state)
        except ValueError:
            pass
        else:
            raise AssertionError('invalid state accepted')
    await ns['UserDataManager']._load_from_db()
    assert ns['get_skill_state']('one.md') == 'disabled'
    assert ns['get_skill_state']('two.md') == 'hidden'
    await db.close()
    print(json.dumps({'atomic': True, 'restart': True, 'validation': True}))

asyncio.run(main())
''')
        self.assertTrue(all(result.values()))

    def test_prompt_states_web_settings_callbacks_and_restart(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
from xgent_app.web_bridge import WebOutbox, build_web_callback_objects

async def main():
    await ns['UserDataManager'].init()
    db = await ns['BotMemoryDB'].get_instance()
    public, private = Path('skill-public'), Path('skill-private')
    public.mkdir(exist_ok=True)
    private.mkdir(exist_ok=True)
    long_name = '\u957f\u6587\u4ef6\u540d' * 12 + '.md'
    files = ['first.md', long_name, 'private/local.md']
    for index, relative in enumerate(files):
        path = private / 'local.md' if relative.startswith('private/') else public / relative
        path.write_text('```!\nSUMMARY-' + str(index) + '\n```\nPRIVATE-BODY', encoding='utf-8')
    full = ns['build_skill_prompt_section']()
    assert all(f'SUMMARY-{i}' in full for i in range(3)) and 'PRIVATE-BODY' not in full
    await ns['_web_write_setting']('disabled_skills', files)
    disabled = ns['build_skill_prompt_section']()
    assert all(file in disabled for file in files) and 'SUMMARY-' not in disabled
    assert disabled.count('\u5f53\u524d\u6280\u80fd\u5df2\u5173\u95ed') == 3
    ns['PromptFileManager'].set('agent_disabled_addon', 'AGENT-DISABLED-END')
    assert 'read-x' in ns['get_agent_runtime_prompt'](True)
    assert ns['get_agent_runtime_prompt'](False).endswith('AGENT-DISABLED-END')
    await ns['_web_write_setting']('hidden_skills', files)
    assert ns['build_skill_prompt_section']() == ''
    assert 'read-x' in ns['get_agent_runtime_prompt'](True)
    values = (await ns['_web_read_settings']())['values']
    assert values['hidden_skills'] == sorted(files)
    await ns['UserDataManager']._load_from_db()
    assert ns['get_hidden_skills']() == set(files)
    keyboard = ns['get_skills_menu']().inline_keyboard
    assert all(len(button.callback_data.encode('utf-8')) <= 64 for row in keyboard for button in row)
    assert all(len(row) == 1 for row in keyboard)
    ns['check_authorized_user_middleware'] = AsyncMock(return_value=True)
    for state in ('enabled', 'disabled', 'hidden', 'enabled'):
        keyboard = ns['get_skills_menu']().inline_keyboard
        callback = next(row[0].callback_data for row in keyboard[:-1]
                        if long_name in ns['CallbackDataStore'].get(row[0].callback_data))
        update, context, _ = build_web_callback_objects(1, WebOutbox(), callback, 1)
        await ns['handle_button_click'](update, context)
        assert ns['get_skill_state'](long_name) == state
        await ns['UserDataManager']._load_from_db()
        assert ns['get_skill_state'](long_name) == state
    for state in ('disabled', 'hidden', 'enabled'):
        await ns['_web_write_setting']('skill_state', {'path': files[0], 'state': state})
        assert ns['get_skill_state'](files[0]) == state
    assert long_name not in ns['get_disabled_skills']() and long_name not in ns['get_hidden_skills']()
    await db.close()
    print(json.dumps({'states': True, 'restart': True, 'callbacks': True}))

asyncio.run(main())
''')
        self.assertTrue(all(result.values()))

    def test_editable_compression_prompt_defaults_reload_and_failures(self):
        result = self.run_probe(self.SECTIONS_PREAMBLE + r'''
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch
from xgent_app.compression import DEFAULT_COMPRESSION_PROMPT
from xgent_app.web_bridge import WebOutbox, build_web_callback_objects

async def main():
    await ns['UserDataManager'].init()
    manager = ns['PromptFileManager']
    path = Path(manager.get_abs_path('compression_prompt'))
    assert manager.get_required('compression_prompt').strip() == DEFAULT_COMPRESSION_PROMPT
    keyboard = ns['get_prompts_menu']().inline_keyboard
    assert any(button.callback_data == 'view_prompt:compression_prompt' for row in keyboard for button in row)
    ns['check_authorized_user_middleware'] = AsyncMock(return_value=True)
    update, context, _ = build_web_callback_objects(1, WebOutbox(), 'act_confirm_prompt:compression_prompt', 1)
    ns['UserDataManager'].set('prompt_buffer', 'MENU-VERSION')
    await ns['handle_button_click'](update, context)
    assert manager.get_required('compression_prompt') == 'MENU-VERSION'
    assert path.read_text(encoding='utf-8') == 'MENU-VERSION'
    path.write_text('DISK-VERSION', encoding='utf-8')
    assert manager.get_required('compression_prompt') == 'MENU-VERSION'
    manager.reload_all()
    assert manager.get_required('compression_prompt') == 'DISK-VERSION'
    manager.init()
    assert path.read_text(encoding='utf-8') == 'DISK-VERSION'
    with patch('os.replace', side_effect=OSError('write failed')):
        try:
            manager.set('compression_prompt', 'UNSAVED')
        except OSError:
            pass
        else:
            raise AssertionError('save failure was swallowed')
    assert manager.get('compression_prompt') == 'DISK-VERSION'
    assert path.read_text(encoding='utf-8') == 'DISK-VERSION'
    assert not list(path.parent.glob('.prompt-*.tmp'))
    ns['UserDataManager'].set('state', ns['BotState'].SET_ANY_PROMPT)
    ns['UserDataManager'].set('editing_prompt_key', 'compression_prompt')
    ns['UserDataManager'].set('prompt_buffer', 'RETRY-VERSION')
    with patch('os.replace', side_effect=OSError('menu save failed')):
        await ns['handle_button_click'](update, context)
    assert ns['UserDataManager'].get('state') == ns['BotState'].SET_ANY_PROMPT
    assert ns['UserDataManager'].get('prompt_buffer') == 'RETRY-VERSION'
    assert manager.get_required('compression_prompt') == 'DISK-VERSION'
    await ns['handle_button_click'](update, context)
    assert manager.get_required('compression_prompt') == 'RETRY-VERSION'
    path.write_text('', encoding='utf-8')
    try:
        manager.get_required('compression_prompt')
    except ValueError:
        pass
    else:
        raise AssertionError('empty file silently used stale cache')
    path.unlink()
    try:
        manager.get_required('compression_prompt')
    except OSError:
        pass
    else:
        raise AssertionError('missing file silently used stale cache')
    await (await ns['BotMemoryDB'].get_instance()).close()
    print(json.dumps({'defaults': True, 'menu': True, 'reload': True, 'failure': True}))

asyncio.run(main())
''')
        self.assertTrue(all(result.values()))


if __name__ == '__main__':
    unittest.main()
