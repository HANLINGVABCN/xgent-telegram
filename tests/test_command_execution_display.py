"""Real process + HTTP + Chromium: command progress appears before output exists."""
import unittest

import pytest

from tests.test_external_sync import ProbeMixin


class Probe(ProbeMixin, unittest.TestCase):
    pass


def test_real_long_command_has_inline_interrupt_and_updates_the_same_output_row(tmp_path):
    pytest.importorskip('playwright.async_api')
    result=Probe().run_probe(r'''
import asyncio,json,os,subprocess,sys,tempfile
from pathlib import Path
from unittest.mock import patch
from playwright.async_api import async_playwright
import xgent_server as bot
from tests.attachment_request_probe import Harness
from xgent_app.web_auth import hash_password
from xgent_app.web_server import WebChatConfig,WebChatServer
from xgent_app.workbench import Workbench

async def main():
    cases=[]
    async with async_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            print(json.dumps({'skip':'Chromium unavailable'}));return
        browser=await p.chromium.launch(headless=True)
        try:
            for style,width,interrupt_command in (('foreground',1440,True),('background',390,True),
                                                   ('nonstream',320,True),('nonstream',1440,False)):
                with tempfile.TemporaryDirectory() as root:
                    async with Harness(bot,root) as h:
                        await bot._web_write_setting('response_mode',style)
                        await bot.UserDataManager.save_config('agent_mode',True)
                        script=Path(root)/'long_command.py'
                        started=Path(root)/'command.started'
                        done=Path(root)/'command.finished'
                        release=Path(root)/'command.release'
                        wait=('time.sleep(60)\n' if interrupt_command else
                              f'while not Path({str(release)!r}).exists():time.sleep(.01)\n')
                        script.write_text('import os,time\nfrom pathlib import Path\n'
                            +f'Path({str(started)!r}).write_text(str(os.getpid()))\n'
                            +'print("COMMAND_STARTED",flush=True)\n'+wait
                            +f'Path({str(done)!r}).write_text("finished")\n',encoding='utf-8')
                        command=subprocess.list2cmdline([sys.executable,'-u',str(script)]) if os.name=='nt' else __import__('shlex').join([sys.executable,'-u',str(script)])
                        response='执行 60 秒测试命令。\n```run-x\n<<BEGIN_CMDSTOP123456\n'+command+'\n<<END_CMDSTOP123456\n```'
                        calls=[]
                        def answer(kwargs):
                            calls.append(True)
                            kwargs['usage_sink'].append({'input_tokens':100,'output_tokens':20})
                            return response if len(calls)==1 else '命令已结束，结果保留。'
                        generation_entered=asyncio.Event();release_generation=asyncio.Event()
                        async def wait_generation():
                            if not calls:
                                generation_entered.set()
                                await release_generation.wait()
                        async def complete(*args,**kwargs):
                            await wait_generation();return answer(kwargs),None
                        async def chunks(*args,**kwargs):
                            await wait_generation();yield answer(kwargs)
                        spawned=[]
                        original_spawn=asyncio.create_subprocess_shell
                        async def spawn(*args,**kwargs):
                            process=await original_spawn(*args,**kwargs);spawned.append(process);return process
                        workbench=Workbench(vars(bot))
                        config=WebChatConfig(host='127.0.0.1',port=0,password_hash=hash_password('temporary-test-only'),
                            bot_token='',authorized_user_id=1,loop=asyncio.get_running_loop(),submit_message=lambda *a:None,
                            read_history=bot._web_read_history,read_settings=bot._web_read_settings,write_setting=bot._web_write_setting,
                            request_stop=bot._web_request_stop,is_busy=bot._web_is_busy,
                            read_conversations=bot.get_conversations().state,workbench=workbench.handle,
                            media_allowed_roots=[str(Path(root)/'storage')])
                        server=WebChatServer(config);server.outbox=h.outbox;server.start()
                        base='http://127.0.0.1:'+str(server._httpd.server_address[1])
                        context=await browser.new_context(viewport={'width':width,'height':900},reduced_motion='reduce')
                        await context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(base) else route.abort())
                        assert (await context.request.post(base+'/api/login',data={'password':'temporary-test-only'})).ok
                        page=await context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                        task=None
                        try:
                            await page.goto(base)
                            await page.wait_for_function('document.getElementById("composer-model")?.disabled===false')
                            with patch.object(bot.ModelClient,'think_and_reply',complete),patch.object(bot.ModelClient,'think_and_reply_stream',chunks),patch('asyncio.create_subprocess_shell',spawn):
                                task=asyncio.create_task(h.turn('运行长命令'))
                                await asyncio.wait_for(generation_entered.wait(),5)
                                ai_stop=page.locator('.bubble.ai').get_by_role('button',name='❌ 停止回答',exact=True)
                                await ai_stop.wait_for()
                                assert await ai_stop.is_visible()
                                assert await page.locator('.bubble.ai').get_by_role('button',name='打断',exact=True).count()==0
                                release_generation.set()
                                async def process_started():
                                    while not started.is_file():await asyncio.sleep(.01)
                                await asyncio.wait_for(process_started(),5)
                                command_output=page.locator('.bubble.cmd')
                                await command_output.get_by_text('执行中',exact=True).wait_for()
                                interrupt=command_output.get_by_role('button',name='❌ 停止回答',exact=True)
                                assert await interrupt.is_visible()
                                assert 'cache 0' in await page.locator('.bubble.ai').inner_text()
                                assert not task.done() and not done.exists()
                                assert len(spawned)==1 and spawned[0].returncode is None
                                snapshot=h.outbox.snapshot()
                                active=next(frame for frame in snapshot['frames'] if frame.get('msg_type')=='agent_result')
                                assert active['reply_markup'] and 'data-state="running"' in active['web_display']['segments'][0]['text']
                                await page.reload()
                                await page.locator('.bubble.cmd').get_by_text('执行中',exact=True).wait_for()
                                assert await page.locator('.bubble.cmd').count()==1
                                assert await page.locator('.bubble.cmd').get_by_role('button',name='❌ 停止回答',exact=True).is_visible()
                                await page.locator('.bubble.cmd').evaluate('(node)=>window.commandRow=node.closest(".msg-row")')
                                await page.screenshot(path=str(Path(os.environ['COMMAND_TEST_SCREENSHOTS'])/f'command-running-{width}-{interrupt_command}.png'))
                                if interrupt_command:
                                    await page.locator('.bubble.cmd').get_by_role('button',name='❌ 停止回答',exact=True).click()
                                else:
                                    release.write_text('finish')
                                await asyncio.wait_for(task,8)
                                assert spawned[0].returncode is not None and done.exists()==(not interrupt_command)
                                state='stopped' if interrupt_command else 'success'
                                await page.locator('.bubble.cmd .execution-dot.state-'+state).wait_for()
                                assert await page.locator('.bubble.cmd .execution-label').inner_text()==('输出 · 已停止' if interrupt_command else '输出')
                                assert await page.locator('.bubble.cmd').count()==1
                                assert await page.evaluate('window.commandRow.isConnected && window.commandRow===document.querySelector(".msg-row.cmd")')
                                assert await page.locator('.bubble.cmd').get_by_role('button',name='❌ 停止回答',exact=True).count()==0
                                rows=await h.db.get_display_history(0)
                                output=next(row for row in rows if row['msg_type']=='agent_result')
                                metadata=json.loads(output['metadata']) if isinstance(output['metadata'],str) else output['metadata']
                                assert metadata['presentation']['operation']['state']==state
                                assert metadata['web_live']['message_ids']==[active['message_id']]
                                assert len(calls)==(1 if interrupt_command else 2)
                                if interrupt_command:
                                    assert not any('已完成' in row['content'] for row in rows if row['msg_type']=='agent_status')
                                    terminal=page.locator('.msg-row.sys').filter(has_text='已有结果保留')
                                    await page.wait_for_function('Array.from(document.querySelectorAll(".msg-row.sys")).some(n=>n.textContent.includes("总耗时"))')
                                    assert await terminal.count()==1,await page.locator('#log').inner_text()
                                    assert (await terminal.locator('.body').inner_text()).count('已有结果保留')==1
                                    assert (await terminal.inner_text()).count('总耗时')==1
                                    terminal_text=await terminal.inner_text()
                                await page.reload()
                                await page.locator('.bubble.cmd .execution-dot.state-'+state).wait_for()
                                assert await page.locator('.bubble.cmd').count()==1
                                assert await page.locator('.bubble.cmd').get_by_role('button',name='❌ 停止回答',exact=True).count()==0
                                if interrupt_command:
                                    terminal=page.locator('.msg-row.sys').filter(has_text='已有结果保留')
                                    assert await terminal.count()==1
                                    assert await terminal.inner_text()==terminal_text
                                assert not errors,errors
                                cases.append({'style':style,'width':width,'outcome':state,'process_finished':True,'one_output':True})
                        finally:
                            if task and not task.done():
                                runs=(await bot.get_conversations().state()).get('runs',{})
                                for run in runs.values():await bot.get_conversations().request_stop(run['run_id'],run['conversation_id'])
                                await asyncio.wait_for(task,10)
                            await context.close()
                            await asyncio.to_thread(server.stop)
        finally:await browser.close()
    print(json.dumps({'cases':cases}))
asyncio.run(main())
''',extra_env={'COMMAND_TEST_SCREENSHOTS':str(tmp_path)})
    if 'skip' in result:
        pytest.skip(result['skip'])
    assert len(result['cases'])==4
    assert [case['outcome'] for case in result['cases']]==['stopped','stopped','stopped','success']
    assert all(case['process_finished'] and case['one_output'] for case in result['cases'])
