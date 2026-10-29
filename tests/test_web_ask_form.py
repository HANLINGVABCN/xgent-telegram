"""Native ask-x form over the actual turn pipeline, HTTP and Chromium."""
import unittest

import pytest

from tests.test_external_sync import ProbeMixin


class Probe(ProbeMixin, unittest.TestCase):
    pass


def test_unsubmitted_web_form_survives_reload_custom_input_and_checked_choices(tmp_path):
    pytest.importorskip('playwright.async_api')
    result=Probe().run_probe(r'''
import asyncio,json,tempfile,os
from pathlib import Path
from unittest.mock import patch
from playwright.async_api import async_playwright
import xgent_server as bot
from tests.attachment_request_probe import Harness
from xgent_app.web_auth import hash_password
from xgent_app.web_server import WebChatConfig,WebChatServer
from xgent_app.workbench import Workbench
from xgent_app.agent_ask import PENDING_ASKS,SECRET_STORE

FORM='Q1|single|选择环境\n- 测试\n- 生产\n+other\nQ2|multi|选择项目\n- 日志\n- 备份\n+other\nQ3|text|补充说明\nQ4|secret:TEST_FORM_KEY|测试密钥'
async def main():
    cases=[]
    async with async_playwright() as p:
        if not Path(p.chromium.executable_path).exists():print(json.dumps({'skip':True}));return
        browser=await p.chromium.launch(headless=True)
        try:
            for width in (1440,390):
                with tempfile.TemporaryDirectory() as root:
                    async with Harness(bot,root) as h:
                        await bot.UserDataManager.save_config('agent_mode',True)
                        calls=[]
                        response='需要确认。\n```ask-x\n<<BEGIN_NATIVEASK123456\n'+FORM+'\n<<END_NATIVEASK123456\n```'
                        async def complete(*args,**kwargs):
                            calls.append(args[5]);return (response if len(calls)==1 else '收到表单，继续处理。'),None
                        service=Workbench(vars(bot))
                        config=WebChatConfig(host='127.0.0.1',port=0,password_hash=hash_password('temporary-test-only'),
                            bot_token='',authorized_user_id=1,loop=asyncio.get_running_loop(),submit_message=lambda *a:None,
                            read_history=bot._web_read_history,read_settings=bot._web_read_settings,write_setting=bot._web_write_setting,
                            request_stop=bot._web_request_stop,is_busy=bot._web_is_busy,
                            read_conversations=bot.get_conversations().state,workbench=service.handle)
                        server=WebChatServer(config);server.outbox=h.outbox;server.start()
                        base='http://127.0.0.1:'+str(server._httpd.server_address[1])
                        context=await browser.new_context(viewport={'width':width,'height':900},reduced_motion='reduce')
                        await context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(base) else route.abort())
                        assert (await context.request.post(base+'/api/login',data={'password':'temporary-test-only'})).ok
                        page=await context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                        try:
                            with patch.object(bot,'_web_external_outbox',h.outbox),patch.object(bot.ModelClient,'think_and_reply',complete):
                                await page.goto(base)
                                await page.wait_for_function('document.getElementById("composer-model")?.disabled===false')
                                await h.turn('发起表单')
                                form=page.get_by_role('form',name='Agent 提问表单');await form.wait_for()
                                aid=await form.evaluate('(n)=>n.closest(".msg-row").dataset.recordId')
                                await page.reload();await form.wait_for()
                                assert await form.count()==1
                                assert len(calls)==1
                                single=form.get_by_role('radio',name='测试',exact=True)
                                await single.click()
                                await page.wait_for_function('Array.from(document.querySelectorAll(".web-ask-notice")).some(n=>n.textContent==="已保存")')
                                assert await single.get_attribute('aria-checked')=='true'
                                assert '✓' in await single.inner_text()
                                await page.reload();await form.wait_for()
                                assert await single.get_attribute('aria-checked')=='true'
                                multi=form.get_by_role('checkbox',name='日志',exact=True)
                                await multi.click()
                                await page.wait_for_function('document.querySelector(".web-ask-notice").textContent==="已保存"')
                                assert '✓' in await multi.inner_text()
                                await form.get_by_role('checkbox',name='备份',exact=True).click()
                                await page.wait_for_function('document.querySelector(".web-ask-notice").textContent==="已保存"')
                                await form.get_by_role('textbox',name='选择环境 · 自定义答案',exact=True).fill('预发布环境')
                                await form.get_by_role('textbox',name='补充说明',exact=True).fill('原地填写  <tag>  保留两个空格')
                                await page.wait_for_function('document.querySelector(".web-ask-notice").textContent==="已保存"')
                                assert await single.get_attribute('aria-checked')=='false'
                                await page.reload();await form.wait_for()
                                assert await form.get_by_role('textbox',name='选择环境 · 自定义答案',exact=True).input_value()=='预发布环境'
                                assert await form.get_by_role('textbox',name='补充说明',exact=True).input_value()=='原地填写  <tag>  保留两个空格'
                                assert await form.get_by_role('checkbox',name='日志',exact=True).get_attribute('aria-checked')=='true'
                                assert await form.get_by_role('checkbox',name='备份',exact=True).get_attribute('aria-checked')=='true'
                                assert await page.locator('#input').input_value()==''
                                assert '直接发一条消息' not in await page.locator('#log').inner_text()
                                pending=next(value for value in PENDING_ASKS._store.values() if value.form_record_id==int(aid))
                                incomplete=await context.request.post(base+'/api/workbench/ask',data={
                                    'id':pending.ask_id,'conversation_id':pending.conversation_id,'action':'submit',
                                    'revision':pending.revision,'draft':{qid:answer for qid,answer in pending.draft.items() if qid!='3'}})
                                assert not incomplete.ok and PENDING_ASKS.get(pending.ask_id) is pending and len(calls)==1
                                # Cross-conversation and forged password status are rejected.
                                other=(await bot.get_conversations().manage('create',name='other'))['conversation_id']
                                bad=await context.request.get(base+'/api/workbench/ask?id='+pending.ask_id+'&conversation_id='+other)
                                assert not bad.ok
                                await bot.get_conversations().manage('switch',pending.conversation_id,selector='shared')
                                safe=pending.web_snapshot()
                                forged=await context.request.post(base+'/api/workbench/ask',data={'id':pending.ask_id,'conversation_id':pending.conversation_id,
                                    'action':'save','revision':pending.revision,'draft':{'3':{'filled':True}}})
                                assert not forged.ok and pending.web_snapshot()==safe
                                password=page.locator('.web-ask-form input[type=password]')
                                await password.fill('NEVER_PERSIST_FORM_SECRET')
                                await page.screenshot(path=str(Path(os.environ['ASK_TEST_IMAGES'])/f'web-ask-{width}.png'))
                                await form.get_by_role('button',name='提交',exact=True).click()
                                await page.wait_for_function('document.querySelector(".web-ask-notice").textContent==="已提交"')
                                async def resumed():
                                    while len(calls)<2 or bot.get_conversations().busy():await asyncio.sleep(.01)
                                await asyncio.wait_for(resumed(),10)
                                assert len(calls)==2
                                assert PENDING_ASKS.get(pending.ask_id) is None
                                duplicate=await context.request.post(base+'/api/workbench/ask',data={
                                    'id':pending.ask_id,'conversation_id':pending.conversation_id,'action':'submit',
                                    'revision':pending.revision,'draft':{}})
                                assert not duplicate.ok and len(calls)==2
                                assert await password.input_value()==''
                                assert await form.get_by_role('button',name='提交',exact=True).count()==0
                                assert not await form.get_by_role('button',name='提交',exact=True).is_visible()
                                rows=await h.db.get_global_messages(100)
                                raw=json.dumps(rows,ensure_ascii=False)
                                assert 'NEVER_PERSIST_FORM_SECRET' not in raw
                                assert 'NEVER_PERSIST_FORM_SECRET' not in json.dumps(calls)
                                assert SECRET_STORE.has(pending.conversation_id,'TEST_FORM_KEY')
                                assert '预发布环境' in json.dumps(calls,ensure_ascii=False)
                                assert '原地填写  <tag>  保留两个空格' in json.dumps(calls,ensure_ascii=False)
                                assert '$TEST_FORM_KEY' in json.dumps(calls,ensure_ascii=False)
                                await page.reload();await form.wait_for()
                                assert not await form.get_by_role('button',name='提交',exact=True).is_visible()
                                assert '已提交' in await form.inner_text()
                                assert not errors,errors
                                cases.append(width)
                        finally:
                            if service._background:
                                await asyncio.wait_for(asyncio.gather(*service._background,return_exceptions=True),10)
                            await context.close();await asyncio.to_thread(server.stop)
        finally:await browser.close()
    print(json.dumps({'widths':cases}))
asyncio.run(main())
''',extra_env={'ASK_TEST_IMAGES':str(tmp_path)})
    if result.get('skip'):
        pytest.skip('Chromium unavailable')
    assert result['widths']==[1440,390]
