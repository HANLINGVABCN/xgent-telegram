"""Real browser workspace acceptance; all data comes from an isolated loopback fixture."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(scope='module')
def workspace_url():
    process=subprocess.Popen([sys.executable,'-u',str(ROOT/'tools/workbench_fixture.py'),'--messages','10000'],
        cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',
        env={**os.environ,'PYTHONIOENCODING':'utf-8'})
    lines=queue.Queue()
    def read():
        for line in process.stdout:lines.put(line)
    threading.Thread(target=read,daemon=True).start()
    try:
        end=time.time()+45
        while time.time()<end:
            try:line=lines.get(timeout=1)
            except queue.Empty:continue
            try:data=json.loads(line)
            except ValueError:continue
            if isinstance(data,dict) and 'url' in data:
                yield data
                return
        raise AssertionError('Fixture did not start')
    finally:
        process.terminate();process.wait(10)

@pytest.fixture
def browser_context(workspace_url):
    playwright=pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():pytest.skip('Chromium not installed')
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1440,'height':900})
        context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(workspace_url['url']) else route.abort())
        assert context.request.post(workspace_url['url']+'/api/login',data={'password':workspace_url['password']}).ok
        yield context
        context.close();browser.close()


def ready(page,url,route='chat'):
    page.goto(url+'/#/'+route)
    if route=='chat':page.wait_for_function('document.getElementById("composer-model")?.disabled===false')
    else:page.locator('#wb-page .wb-page-head').wait_for()


def test_asset_etag_and_authenticated_management_routes(workspace_url):
    import urllib.request
    import urllib.error
    base=workspace_url['url']
    with urllib.request.urlopen(base+'/assets/workbench.js') as response:
        assert 'javascript' in response.headers['Content-Type']
        assert response.headers['Cache-Control']=='no-cache'
        etag=response.headers['ETag']
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(urllib.request.Request(base+'/assets/workbench.js',headers={'If-None-Match':etag}))
    assert caught.value.code==304
    for path in ['/api/workbench/providers','/api/workbench/tasks','/api/workbench/history','/api/term/sessions']:
        with pytest.raises(urllib.error.HTTPError) as caught:urllib.request.urlopen(base+path)
        assert caught.value.code==401
    with pytest.raises(urllib.error.HTTPError) as caught:urllib.request.urlopen(base+'/assets/../workbench.py')
    assert caught.value.code==404


def test_responsive_routes_themes_and_no_layout_overflow(browser_context,workspace_url,tmp_path):
    page=browser_context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    for size in [(1440,900),(768,1024),(390,844)]:
        page.set_viewport_size({'width':size[0],'height':size[1]})
        for theme in ['light','dark']:
            ready(page,workspace_url['url'])
            page.evaluate('(theme)=>{localStorage.setItem("xgent-theme",theme);document.body.classList.toggle("dark",theme==="dark");}',theme)
            for route in ['chat','tasks','files','models','usage','settings']:
                page.evaluate('(route)=>location.hash="/"+route',route)
                if route!='chat':page.locator('#wb-page .wb-page-head').wait_for()
                page.wait_for_timeout(80)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
                if size[0]==390:
                    assert page.locator('#wb-mobile-nav').count()==0
                    assert page.get_by_role('button',name='对话记录',exact=True).is_visible()
                page.screenshot(path=str(tmp_path/f'{size[0]}-{theme}-{route}.png'))
    assert not errors


def test_history_is_paged_search_locates_unloaded_messages_and_draft_survives(browser_context,workspace_url):
    page=browser_context.new_page();responses=[]
    page.on('response',lambda r:responses.append(r) if '/api/workbench/history?' in r.url else None)
    ready(page,workspace_url['url'])
    page.wait_for_function('document.querySelectorAll("#log .msg-row").length>0')
    assert not any('/api/history?limit=0' in r.url for r in responses)
    assert len(responses[0].json()['messages'])==50
    assert page.locator('#log>.msg-row').count()<=200
    page.locator('#input').fill('跨页保留的输入草稿')
    page.evaluate('location.hash="/tasks"');page.locator('#wb-page h1').wait_for()
    page.evaluate('location.hash="/chat"')
    assert page.locator('#input').input_value()=='跨页保留的输入草稿'
    for _ in range(5):
        with page.expect_response('**/api/workbench/history?limit=50&before=*'):
            page.locator('#log').evaluate('(e)=>e.scrollTop=0');page.locator('#wb-earlier').click()
        page.wait_for_timeout(80)
    assert page.locator('#log>.msg-row').count()<=200
    before=page.locator('#log').inner_text()
    with page.expect_response('**/api/workbench/history?limit=50&after=*'):
        page.locator('#wb-newer').click()
    page.wait_for_timeout(100)
    assert page.locator('#log').inner_text()!=before
    page.locator('#workspace-settings').click();page.locator('#btn-search').click()
    page.get_by_placeholder('搜索当前会话历史…').fill('历史记录 1234：')
    page.locator('#wb-dialog .wb-command-result').first.wait_for()
    page.locator('#wb-dialog .wb-command-result').first.click()
    page.wait_for_function('document.querySelector("#log").textContent.includes("历史记录 1234：")')
    assert page.locator('#input').input_value()=='跨页保留的输入草稿'
    t=time.perf_counter();page.locator('#input').fill('快速输入');assert time.perf_counter()-t<1
    assert page.locator('#input').input_value()=='快速输入'


def test_provider_and_task_forms_use_real_service_without_secret_readback(browser_context,workspace_url):
    page=browser_context.new_page();ready(page,workspace_url['url'],'models')
    page.get_by_role('button',name='添加提供商',exact=True).first.click()
    dialog=page.locator('#wb-dialog')
    dialog.locator('[name=name]').fill('Browser fixture')
    dialog.locator('[name=base_url]').fill('https://browser.invalid/v1')
    dialog.locator('[name=api_key]').fill('SECRET-WRITE-ONLY')
    dialog.locator('[name=models]').fill('fixture-model')
    dialog.get_by_role('button',name='保存配置').click()
    page.wait_for_function('!document.getElementById("wb-dialog").open')
    page.get_by_role('heading',name='Browser fixture',exact=True).wait_for()
    response=browser_context.request.get(workspace_url['url']+'/api/workbench/providers')
    assert 'SECRET-WRITE-ONLY' not in response.text()
    assert any(item['name']=='Browser fixture' and item['has_key'] for item in response.json()['items'])
    page.evaluate('location.hash="/tasks"');page.get_by_role('button',name='创建任务',exact=True).first.click()
    dialog.locator('[name=task]').fill('浏览器登记测试')
    dialog.locator('[name=command]').fill('echo registered-not-executed-in-fixture')
    dialog.get_by_role('button',name='确认创建任务').click()
    page.wait_for_function('!document.getElementById("wb-dialog").open')
    page.get_by_role('button',name='浏览器登记测试',exact=True).wait_for()
    page.get_by_role('button',name='浏览器登记测试',exact=True).click()
    page.locator('#wb-detail').get_by_role('button',name='取消任务',exact=True).click()
    dialog.get_by_role('button',name='确认操作').click()
    page.wait_for_function('!document.getElementById("wb-dialog").open')
    status=browser_context.request.get(workspace_url['url']+'/api/workbench/tasks').json()
    assert next(t for t in status['items'] if t['summary']=='浏览器登记测试')['status']=='cancelled'


def test_command_palette_and_output_detail(browser_context,workspace_url):
    page=browser_context.new_page();ready(page,workspace_url['url'])
    page.keyboard.press('Control+k');page.locator('#composer-commands .composer-command').first.wait_for()
    assert page.locator('#composer-commands .composer-command strong').first.text_content()=='/start'
    page.keyboard.press('Escape')
    page.evaluate('location.hash="/files"');page.locator('#wb-page h1').wait_for()
    page.get_by_role('tab',name='命令输出',exact=True).click()
    page.get_by_role('button',name='查看完整输出',exact=True).first.wait_for();page.get_by_role('button',name='查看完整输出',exact=True).first.click()
    page.locator('#wb-detail .wb-pre').wait_for()
    assert 'Review completed' in page.locator('#wb-detail .wb-pre').text_content()
    assert len(page.locator('#wb-detail .wb-pre').text_content().encode())<=65536
    page.keyboard.press('Escape');assert not page.locator('#wb-detail').is_visible()


def test_settings_pages_and_text_attachment_preview(browser_context,workspace_url):
    page=browser_context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    ready(page,workspace_url['url'],'settings')
    page.get_by_role('tab',name='对话与记忆',exact=True).click()
    page.locator('[name=global_depth]').fill('47')
    page.get_by_role('button',name='保存设置',exact=True).click()
    page.get_by_text('设置已保存',exact=True).wait_for()
    page.reload();page.locator('#wb-page .wb-tabs').wait_for()
    page.get_by_role('tab',name='对话与记忆',exact=True).click()
    assert page.locator('[name=global_depth]').input_value()=='47'
    page.get_by_role('tab',name='Web 与安全',exact=True).click()
    assert page.locator('[name=web_port]').input_value()
    assert page.get_by_role('button',name='修改访问密码').is_visible()
    page.get_by_role('tab',name='系统状态',exact=True).click()
    page.get_by_role('heading',name='通道状态').wait_for()
    page.evaluate('location.hash="/files"')
    page.get_by_role('button',name='部署报告.md',exact=True).wait_for()
    page.get_by_role('button',name='部署报告.md',exact=True).click()
    page.locator('#wb-detail .wb-document-preview').wait_for()
    first=page.locator('#wb-detail .wb-document-preview').text_content()
    assert '# 检查报告' in first
    assert len(first.encode('utf-8'))<=65536
    page.locator('#wb-detail').get_by_role('button',name='下一页',exact=True).click()
    page.wait_for_function('document.querySelector(".wb-document-preview") && !document.querySelector(".wb-document-preview").textContent.startsWith("# 检查报告")')
    page.locator('#wb-detail').get_by_role('button',name='上一页',exact=True).click()
    page.wait_for_function('document.querySelector(".wb-document-preview")?.textContent.startsWith("# 检查报告")')
    assert not errors


def test_task_modes_alt_enter_and_current_model(browser_context,workspace_url):
    page=browser_context.new_page();ready(page,workspace_url['url'])
    page.locator('#input').fill('一行');page.locator('#input').press('Alt+Enter')
    assert page.locator('#input').input_value()=='一行\n'
    page.evaluate('location.hash="/tasks"');page.get_by_role('button',name='创建任务',exact=True).click()
    dialog=page.locator('#wb-dialog')
    for value in ('cron','at','when','after'):
        dialog.locator('[name=mode]').select_option(value)
        if value=='at':assert dialog.locator('[name=expression]').get_attribute('type')=='datetime-local'
        if value=='when':assert dialog.locator('[name=repeat]').is_visible()
        else:assert dialog.locator('[name=repeat]').count()==0
    page.on('dialog',lambda d:d.accept());page.keyboard.press('Escape')
    page.evaluate('location.hash="/models"')
    page.get_by_role('heading',name='OpenAI',exact=True).wait_for()
    card=page.locator('.wb-card').filter(has=page.get_by_role('heading',name='OpenAI',exact=True))
    card.locator('select').select_option('gpt-4.1-mini')
    card.get_by_role('button',name='用于对话',exact=True).click()
    page.wait_for_function('document.querySelector("#composer-model")?.dataset.value==="OpenAI|gpt-4.1-mini"')
    page.reload();page.locator('#wb-page .wb-card').first.wait_for()
    card=page.locator('.wb-card').filter(has=page.get_by_role('heading',name='OpenAI',exact=True))
    assert card.locator('select').input_value()=='gpt-4.1-mini'
    assert card.get_by_text('当前对话',exact=True).is_visible()


def test_terminal_session_ui_handles_switch_keys_close_and_stale_session(browser_context,workspace_url):
    import base64
    page=browser_context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    sessions=[{'id':'session_one','pid':111,'closed':False},{'id':'session_two','pid':222,'closed':False}]
    writes=[];opened=[];closed=[]
    def route(r):
        endpoint=r.request.url.split('/api/term/')[1].split('?')[0]
        if endpoint=='sessions': data={'sessions':sessions,'supported':True,'max_sessions':3,'idle_timeout':1800}
        elif endpoint=='open':
            opened.append(1);data={'session_id':'session_three','pid':333};sessions.append({'id':'session_three','pid':333,'closed':False})
        elif endpoint=='input': writes.append(r.request.post_data_json);data={'ok':True}
        elif endpoint=='close':
            sid=r.request.post_data_json['session_id'];closed.append(sid);sessions[:]=[s for s in sessions if s['id']!=sid];data={'ok':True}
        else:data={'ok':True}
        r.fulfill(json=data)
    page.route('**/api/term/**',route)
    page.add_init_script("""window.__streams=[];window.EventSource=class{constructor(url){this.url=url;this.handlers={};window.__streams.push(this);setTimeout(()=>this.onopen?.({}),0);}addEventListener(n,f){this.handlers[n]=f;}close(){this.closed=true;}};""")
    page.goto(workspace_url['url']+'/terminal')
    page.locator('#sessionTabs button').first.wait_for()
    assert not opened
    page.locator('#sessionSelect').select_option('session_two')
    page.locator('#keybar').get_by_role('button',name='TAB',exact=True).click()
    page.wait_for_timeout(100)
    assert writes[-1]['session_id']=='session_two'
    assert base64.b64decode(writes[-1]['data'])==b'\t'
    page.locator('#newSessionBtn').click()
    page.wait_for_function('document.querySelectorAll("#sessionTabs button").length===3')
    assert len(opened)==1
    assert page.locator('#newSessionBtn').is_disabled()
    page.on('dialog',lambda d:d.accept())
    page.locator('#closeBtn').click()
    page.wait_for_function('document.querySelectorAll("#sessionTabs button").length===2')
    assert closed==['session_three']
    page.evaluate("sessionStorage.setItem('xgent-terminal-session','lost_session')")
    page.reload();page.locator('#endedOverlay').wait_for()
    assert '上次会话已失效' in page.locator('#endReason').text_content()
    assert len(opened)==1
    assert page.locator('#existingSessions button').count()==2
    assert not errors
