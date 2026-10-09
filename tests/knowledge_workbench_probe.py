import asyncio
from pathlib import Path
from unittest.mock import patch
from tests.compression_request_probe import CompressionHarness
from xgent_app.workbench import Workbench, WorkbenchError
from xgent_app.conversations import bind_conversation


async def documents(bot,root):
    async with CompressionHarness(bot,root) as h:
        base=Path(root)
        for key,folder in [('SKILL_PUBLIC_DIR','public'),('SKILL_PRIVATE_DIR','private'),('SKILL_LEGACY_DIR','legacy'),('MEMORY_DIR','memory')]:
            (base/folder).mkdir(exist_ok=True);h.stack.enter_context(patch.object(bot,key,str(base/folder)))
        wb=Workbench(vars(bot))
        created=await wb.handle('POST','skills/create',{})
        item=created['item'];assert item['content']=='' and item['state']=='disabled'
        assert (base/'private'/item['filename']).read_bytes()==b''
        item=(await wb.handle('POST','skills/update',{'path':item['path'],'revision':item['revision'],'mode':'sections','summary':'简介专用唯一词','body':''}))['item']
        assert item['summary']=='简介专用唯一词' and item['body']==''
        await wb.handle('POST','skills/state',{'path':item['path'],'state':'hidden'})
        old=item['path']
        item=(await wb.handle('POST','skills/rename',{'path':old,'revision':item['revision'],'name':'已改名.md'}))['item']
        assert item['path']=='private/已改名.md' and item['state']=='hidden'
        assert item['path'] in bot.get_hidden_skills() and old not in bot.get_hidden_skills()
        assert '简介专用唯一词' not in bot.build_skill_prompt_section()
        await wb.handle('POST','skills/state',{'path':item['path'],'state':'enabled'})
        assert '简介专用唯一词' in bot.build_skill_prompt_section()
        raw='# 自由源码\n\n'+chr(96)*3+'!\n新的简介\n'+chr(96)*3+'\n正文尾部\n'
        current=(await wb.handle('POST','skills/update',{'path':item['path'],'revision':item['revision'],'content':raw}))['item']
        assert current['content']==raw
        try:
            await wb.handle('POST','skills/update',{'path':item['path'],'revision':item['revision'],'content':'stale'})
            raise AssertionError('overwrote a newer revision')
        except WorkbenchError as exc:assert exc.status==409
        mem=(await wb.handle('POST','memories/create',{}))['item']
        mem=(await wb.handle('POST','memories/update',{'path':mem['path'],'revision':mem['revision'],'content':'记忆只在此处共享'}))['item']
        assert '记忆只在此处共享' in bot.build_memory_prompt_section()
        assert not await h.db.get_conversation_messages()
        try:
            await wb.handle('POST','memories/delete',{'path':mem['path'],'revision':mem['revision']})
            raise AssertionError('deletion without confirmation')
        except WorkbenchError:pass
        await wb.handle('POST','memories/delete',{'path':mem['path'],'revision':mem['revision'],'confirm':True})
        assert '记忆只在此处共享' not in bot.build_memory_prompt_section()
        await wb.handle('POST','skills/delete',{'path':current['path'],'revision':current['revision'],'confirm':True})
        assert not (base/'private'/current['filename']).exists()
        assert current['path'] not in bot.get_disabled_skills() | bot.get_hidden_skills()
        return {'blank_created':True,'parts_and_source':True,'rename_keeps_state':True,'memory_shared_not_chat':True,'safe_delete':True}


async def source_search(bot,root):
    async with CompressionHarness(bot,root) as h:
        manager=bot.get_conversations();a=(await manager.state())['current_chat_id']
        await bot.GlobalRecorder.record_user_message('这段历史里有雾蓝关键词')
        b=(await manager.manage('create',name='当前 B'))['conversation_id']
        await manager.manage('archive',a)
        wb=Workbench(vars(bot))
        found=await wb.handle('GET','conversations/search',{'q':'雾蓝'})
        assert len(found['items'])==1 and found['items'][0]['id']==a and found['items'][0]['archived']
        assert '雾蓝' in found['items'][0]['snippet']
        assert (await manager.state())['current_chat_id']==b
        return {'history_searched':True,'archived_included':True,'selection_kept':True}
