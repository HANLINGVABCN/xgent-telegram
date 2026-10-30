"""Standalone form experience with real Chromium and a simulated Telegram host."""
import asyncio
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch

from tests.attachment_request_probe import Harness
from tests.miniapp_probe import init_data
from xgent_app.agent_ask import PENDING_ASKS
from xgent_app.web_auth import hash_password
from xgent_app.web_server import WebChatConfig, WebChatServer
from xgent_app.workbench import Workbench


async def exercise(bot, root, images):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        if not Path(p.chromium.executable_path).exists():return {'skip':True}
        browser=await p.chromium.launch(headless=True)
        results=[]
        try:
            for width in (390,1440):
                async with Harness(bot,Path(root)/str(width)) as h:
                    bot.UserDataManager.set('agent_mode',True)
                    calls=[]
                    form='Q1|single|选择运行环境\n- 测试环境\n- 生产环境\n+other\nQ2|text|补充说明\nQ3|secret:MINI_BROWSER_KEY|私密内容'
                    async def complete(*args,**kwargs):
                        calls.append(args[5]);return ('请填写。\n```ask-x\n<<BEGIN_MINIBROWSER123\n'+form+'\n<<END_MINIBROWSER123\n```' if len(calls)==1 else '收到。'),None
                    service=Workbench(vars(bot))
                    config=WebChatConfig(host='127.0.0.1',port=0,password_hash=hash_password('browser-test-only'),
                        bot_token='123456:browser-test-only',authorized_user_id=1,loop=asyncio.get_running_loop(),
                        submit_message=lambda *a:None,read_history=bot._web_read_history,read_settings=bot._web_read_settings,
                        write_setting=bot._web_write_setting,request_stop=bot._web_request_stop,is_busy=bot._web_is_busy,
                        read_conversations=bot.get_conversations().state,workbench=service.handle)
                    server=WebChatServer(config);server.outbox=h.outbox;server.start()
                    base='http://127.0.0.1:'+str(server._httpd.server_address[1])
                    context=await browser.new_context(viewport={'width':width,'height':850})
                    await context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(base) else route.abort())
                    await context.add_init_script('window.TelegramWebviewProxy={postEvent:(name,data)=>{window.__hostEvents=window.__hostEvents||[];window.__hostEvents.push({name,data});}};')
                    page=await context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    try:
                        with patch.object(bot.ModelClient,'think_and_reply',complete):
                            await h.turn('填写单页表单')
                            pending=next(iter(PENDING_ASKS._store.values()))
                            url=base+'/mini/form?id='+pending.ask_id+'&conversation_id='+pending.conversation_id
                            url+='#tgWebAppData='+quote(init_data(config.bot_token))+'&tgWebAppVersion=10.3&tgWebAppPlatform=web'
                            await page.goto(url)
                            native_form=page.get_by_role('form',name='Agent 提问表单');await native_form.wait_for()
                            assert await page.locator('#log').count()==0
                            await native_form.get_by_role('radio',name='测试环境',exact=True).click()
                            await native_form.get_by_role('textbox',name='补充说明',exact=True).fill('原地填写，不产生聊天消息。')
                            await page.wait_for_function('document.querySelector(".web-ask-notice").textContent==="已保存"')
                            await page.reload();await native_form.wait_for()
                            assert await native_form.get_by_role('radio',name='测试环境',exact=True).get_attribute('aria-checked')=='true'
                            assert await native_form.get_by_role('textbox',name='补充说明',exact=True).input_value()=='原地填写，不产生聊天消息。'
                            assert not await native_form.locator('.web-ask-actions').is_visible()
                            images_dir=Path(images);images_dir.mkdir(parents=True,exist_ok=True)
                            screenshot=str(images_dir/('mini-form-'+str(width)+'.png'))
                            await page.screenshot(path=screenshot,full_page=True)
                            await native_form.get_by_label('私密内容',exact=True).fill('BROWSER_SECRET_NEVER_STORE')
                            await page.evaluate("Telegram.WebView.receiveEvent('main_button_pressed',{})")
                            await page.wait_for_function('window.__hostEvents?.some(e=>e.name==="web_app_close")')
                            assert await native_form.get_by_label('私密内容',exact=True).input_value()==''
                            assert len(calls)<=2
                            assert 'BROWSER_SECRET_NEVER_STORE' not in str(await h.db.get_global_messages(100))
                            assert not errors,errors
                            results.append({'width':width,'image':screenshot,'submitted':True})
                    finally:
                        if service._background:await asyncio.gather(*service._background,return_exceptions=True)
                        await context.close();await asyncio.to_thread(server.stop)
        finally:await browser.close()
        return {'cases':results}
