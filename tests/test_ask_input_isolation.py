"""Form input transport is endpoint-local; shared transcript contains only form state."""
import asyncio
import unittest

from tests.test_external_sync import ProbeMixin


class Probe(ProbeMixin, unittest.TestCase):
    pass


def test_legacy_prompt_field_and_ack_are_hidden_across_history_and_source_queries():
    result=Probe().run_probe(r'''
import asyncio,json,tempfile
import xgent_server as bot
from tests.attachment_request_probe import Harness
from xgent_app.workbench import Workbench
from xgent_app.web_history import build_history_message
from xgent_app.conversations import current_scope
async def main():
    with tempfile.TemporaryDirectory() as root:
        async with Harness(bot,root) as h:
            generation=await h.db.get_attachment_generation()
            prompt='请填写：测试备注\n（直接发一条消息即可；发 cancel 取消录入）'
            async def ui(mid,text):
                return await h.db.apply_ui_frame('telegram',1,{'type':'message','message_id':mid,'text':text},[],
                    generation=generation,create=True,guard='fixture')
            await ui(101,prompt)
            field=await bot.GlobalRecorder.record_user_message('测试',chat_id=1)
            await ui(102,'✅ 已填入，回到表单继续。')
            await ui(103,'请填写：测试密钥\n🔒 内容只写进环境变量，绝不会发给 AI。\n（直接发一条消息即可；发 cancel 取消录入）')
            await ui(104,'🔒 已录入变量 $TEST_KEY，内容只进了环境变量，没有发给 AI。回到表单继续。')
            ordinary=await bot.GlobalRecorder.record_user_message('测试',chat_id=1)
            await bot.GlobalRecorder.record_ai_reply('正常回复',chat_id=1,provider='p',model='m')
            # Seed a v7 database, then exercise the startup conversion. Readers
            # no longer scan prompt/ack text on every history request.
            async with h.db._transaction() as conn:
                await conn.execute("UPDATE global_messages SET metadata=json_remove(metadata,'$.message_policy')")
                await conn.execute("UPDATE ui_messages SET payload=json_remove(payload,'$.message_policy')")
                await conn.execute("UPDATE config SET value='7' WHERE key='conversation_schema_version'")
            h.db._config_cache.clear()
            await h.db.backup_legacy_conversations()
            await h.db.migrate_conversations()
            history=await h.db.get_display_history(0)
            messages=[build_history_message(row,bot.ArtifactManager.ROOT_DIR,root) for row in history]
            hidden=[message for message in messages if message.get('hidden')]
            assert len(hidden)==5,hidden
            assert next(message for message in messages if message.get('id')==field)['hidden']
            assert not next(message for message in messages if message.get('id')==ordinary).get('hidden')
            source=build_history_message(await h.db.get_display_message(field),bot.ArtifactManager.ROOT_DIR,root)
            assert source['hidden']
            service=Workbench(vars(bot))
            page=await service.history({'limit':50})
            assert sum(message.get('hidden',False) for message in page['messages'])==5
            focused=await service.history({'presentation_ids':str(field)})
            assert focused['messages'][0]['hidden']
            # Stored user text remains exact; only display copies are marked.
            conn=await h.db._get_conn()
            raw=await (await conn.execute('SELECT id,content FROM global_messages WHERE id IN (?,?)',(field,ordinary))).fetchall()
            assert next(row['content'] for row in raw if row['id']==field)=='测试'
            assert next(row['content'] for row in raw if row['id']==ordinary)=='测试'
            print(json.dumps({'legacy_hidden':True,'field_hidden':True,'ordinary_visible':True,'source_hidden':True,'data_unchanged':True}))
asyncio.run(main())
''')
    assert all(result.values()),result


def test_local_cli_form_notices_stay_visible_locally_but_are_not_relayed():
    import io
    from unittest.mock import patch
    from xgent_app.ask_presentation import send_ask_input_notice
    from xgent_app.cli_bridge import CliBot, _CliRelay, reset_menu_state
    from xgent_app.cli_render import TerminalScreen
    stream=io.StringIO()
    relay=_CliRelay()
    relay._enabled=True
    native=CliBot(1,TerminalScreen(stream=stream,color=False,width=80))
    async def main():
        with patch('xgent_app.cli_bridge._RELAY',relay):
            await send_ask_input_notice(type('Input',(),{'reply_text':lambda _,text:native.send_message(1,text)})(),
                '请填写：测试备注\n（直接发一条消息即可；发 cancel 取消录入）')
            assert relay._queue.empty()
            assert '请填写：' in stream.getvalue()
            await native.send_message(1,'正常回复')
            assert not relay._queue.empty()
    try:asyncio.run(main())
    finally:
        relay._enabled=False
        reset_menu_state()
