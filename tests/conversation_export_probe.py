import json
import zipfile
from pathlib import Path

from tests.attachment_request_probe import Harness
from xgent_app.conversations import bind_conversation
from xgent_app.workbench import Workbench, WorkbenchError


async def exercise(bot, root):
    async with Harness(bot, root) as h:
        manager = bot.get_conversations()
        a = 'global_memory'
        b = (await manager.manage('create', name='B'))['conversation_id']
        with bind_conversation(await manager.resolve(a)):
            await bot.GlobalRecorder.record_user_message('ONLY_A_OLD_STAGE', chat_id=1)
            await h.db.clear_all_conversation_memory()
            await bot.GlobalRecorder.record_user_message('ONLY_A_CURRENT_STAGE', chat_id=1)
        with bind_conversation(await manager.resolve(b)):
            await bot.GlobalRecorder.record_user_message('ONLY_B_RECORD', chat_id=1)
            service = Workbench(vars(bot))
            conn = await h.db._get_conn()
            before = await (await conn.execute('SELECT COUNT(*) n FROM global_messages')).fetchone()
            selected = await manager.state()
            h.drain_frames()
            exported = await service.handle('POST', 'conversations/export', {'id': a})
            with zipfile.ZipFile(exported.path) as archive:
                content = '\n'.join(archive.read(name).decode('utf-8') for name in archive.namelist())
                assert archive.namelist() == sorted(archive.namelist())
                assert any('全局记忆' in name for name in archive.namelist())
            assert 'ONLY_A_OLD_STAGE' in content and 'ONLY_A_CURRENT_STAGE' in content
            assert 'ONLY_B_RECORD' not in content
            after = await (await conn.execute('SELECT COUNT(*) n FROM global_messages')).fetchone()
            assert before['n'] == after['n']
            assert not h.drain_frames()
            now = await manager.state()
            assert now['current_chat_id'] == selected['current_chat_id']
            assert now['telegram_conversation_id'] == selected['telegram_conversation_id']
            await manager.manage('archive', conversation_id=a)
            archived = await service.handle('POST', 'conversations/export', {'id': a})
            assert Path(archived.path).is_file()
            row = await (await conn.execute('SELECT archived FROM chat_sessions WHERE id=?', (a,))).fetchone()
            assert row['archived'] == 1
            for method, data, status in [('GET', {'id': a}, 405), ('POST', {}, 400), ('POST', {'id': []}, 400)]:
                try:
                    await service.handle(method, 'conversations/export', data)
                except WorkbenchError as exc:
                    assert exc.status == status
                else:
                    raise AssertionError('Invalid export request was accepted')
            invalid = False
            try:
                await service.handle('POST', 'conversations/export', {'id': 'unknown-conversation'})
            except WorkbenchError:
                invalid = True
            assert invalid
        return {'scoped': True, 'full_history': True, 'no_chat_messages': True, 'selectors_unchanged': True, 'archived': True}
