"""End-to-end control input and Telegram presentation probes, no real network."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from tests.compression_request_probe import CompressionHarness
from tests.ui_history_probe import menus, click, prepare
from xgent_app.conversations import bind_conversation, current_scope
from xgent_app.telegram_presentation import telegram_label
from xgent_app.web_bridge import MirrorBot, WebOutbox, InteractionOutbox


async def rename_and_navigation(bot, root):
    async with CompressionHarness(bot,root) as h:
        prepare(bot,h)
        await bot._web_handle_command('/chats',h.outbox)
        first=(await menus(bot))[-1]
        assert first['content'].count('🗂')==1
        assert any(b['text']=='🔙 返回' for row in first['reply_markup'] for b in row)
        await click(bot,h,first,'conv_archived')
        await click(bot,h,(await menus(bot))[-1],'conv_list')
        assert len(await menus(bot))==1
        await click(bot,h,(await menus(bot))[-1],'conv_rename')
        assert len(await menus(bot))==1
        await bot._web_run_conversation('测试2下',h.outbox)
        session=await h.db.get_session(current_scope().conversation_id)
        assert session['name']=='测试2下',session
        assert bot.UserDataManager.get('state')==bot.BotState.IDLE
        assert len(await menus(bot))==1
        assert '测试2下' in (await menus(bot))[-1]['content']
        model=await h.db.get_conversation_messages(100)
        assert '测试2下' not in str(model),model
        export=await h.db.get_export_snapshot()
        assert not any(r['msg_type']=='user_text' for r in export['records']),export
        h.replies.append('我在。')
        await bot._web_run_conversation('洛溪？',h.outbox)
        assert h.requests
        assert '测试2下' not in json.dumps(h.requests,ensure_ascii=False)
        assert any(r['content']=='洛溪？' and r['msg_type']=='user_text' for r in (await h.db.get_export_snapshot())['records'])
        return {'single_menu':True,'rename_not_prompt':True,'normal_question_recorded':True}


async def title_event(bot,root):
    async with CompressionHarness(bot,root) as h:
        result=await bot.get_conversations().manage('create')
        cid=result['conversation_id']
        with bind_conversation(await bot.get_conversations().resolve(cid)):
            h.drain_frames()
            before=(await bot.get_conversations().state())['revision']
            await bot.GlobalRecorder.record_user_message('首条提问自动标题')
            after=await bot.get_conversations().state()
            assert after['revision']>before
            frames=h.drain_frames()
            event=next(f for f in frames if f['type']=='conversation_state')
            item=next(c for c in event['items'] if c['id']==cid)
            assert item['name']=='首条提问自动标题'
            assert current_scope().name=='首条提问自动标题'
        return {'title_before_model':True,'revision_advanced':True}


async def telegram_parts(bot,root):
    async with CompressionHarness(bot,root) as h:
        sent=[]
        class Native:
            async def send_message(self,chat_id,text,**kwargs):
                sent.append({'text':text,**kwargs});return SimpleNamespace(message_id=len(sent))
        mirror=MirrorBot(h.outbox,1,real_bot=Native())
        context=SimpleNamespace(bot=mirror)
        await bot.safe_send_message(context,1,'正文。'*3000,parse_mode='HTML')
        await mirror.flush_telegram()
        assert len(sent)>=3
        assert sum(item['text'].count('🗂') for item in sent)==0,sent
        assert not sent[0]['text'].startswith('<b>')
        sent.clear()
        await bot.send_token_usage_message(context,1,{'input_tokens':120,'output_tokens':45,'total_tokens':165},1.2)
        await mirror.flush_telegram()
        assert len(sent)==1 and '🗂' not in sent[0]['text'] and sent[0]['text'].startswith('<i>'),sent
        with telegram_label(False):
            await mirror.send_message(1,'流式输出中...')
        await mirror.flush_telegram()
        assert sent[-1]['text']=='流式输出中...'
        sent.clear()
        await bot.get_conversations().manage('create', name='Bot B', selector='telegram')
        await bot.safe_send_message(context,1,'异会话正文。'*1500,parse_mode='HTML')
        await mirror.flush_telegram()
        assert len(sent) >= 2 and all(item['text'].startswith('🗂 ') for item in sent)
        assert not any('🗂 ' in str(frame.get('text','')) for frame in h.drain_frames())
        await mirror._tg_channel().aclose()
        return {'current_plain':True,'foreign_parts_labelled':True,'usage_plain':True,'placeholder_plain':True}


async def delete_running_and_recover(bot,root):
    from xgent_app.conversations import ExecutionFileLock
    async with CompressionHarness(bot,root) as h:
        manager=bot.get_conversations()
        a=current_scope().conversation_id
        b=(await manager.manage('create',name='保留 B'))['conversation_id']
        # A real OS lock stands in for the other process's scheduler ownership.
        owner=ExecutionFileLock(str(h.db.db_path)+'.scheduler')
        assert owner.acquire()
        result=await bot.delete_conversation(a)
        assert result['deleting'] and (await h.db.get_session(a))['deleting']
        assert (await manager.state())['current_chat_id']==b
        owner.release()
        await bot._conversation_maintenance()
        for _ in range(100):
            if await h.db.get_session(a) is None:break
            await asyncio.sleep(.02)
        assert await h.db.get_session(a) is None
        assert await h.db.get_session(b) is not None
        # The default chat must stay deleted through a database reopen.
        await h.db.close();await h.db._init_db()
        assert await h.db.get_session('global_memory') is None
        return {'deferred_to_owner':True,'recovery_finishes':True,'default_not_resurrected':True}


async def delete_stops_command(bot,root):
    import sys
    async with CompressionHarness(bot,root) as h:
        manager=bot.get_conversations();a=current_scope().conversation_id
        scheduler=bot.SelfTriggerManager
        await scheduler.startup(None)
        try:
            await scheduler.register_from_fields(command=f'"{sys.executable}" -c "import time; time.sleep(30)"',
                task='待删除任务',after='1s',chat_id=1,conversation_id=a)
            task=(await h.db.list_trigger_tasks())[0]
            process=None
            for _ in range(200):
                process=scheduler._processes.get(task['id'])
                if process is not None:break
                await asyncio.sleep(.05)
            assert process is not None
            b=(await manager.manage('create',name='任务 B'))['conversation_id']
            await bot.delete_conversation(a)
            for _ in range(100):
                if await h.db.get_session(a) is None:break
                await asyncio.sleep(.05)
            assert process.returncode is not None
            assert await h.db.get_session(a) is None
            assert not await h.db.list_trigger_tasks(False)
            assert (await manager.state())['current_chat_id']==b
            return {'command_stopped':True,'task_deleted':True,'other_view_kept':True}
        finally:
            await scheduler.shutdown()
