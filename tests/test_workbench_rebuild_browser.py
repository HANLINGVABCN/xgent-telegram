"""End-to-end workbench controls with real persisted records and isolated credentials."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest
from tests.test_workbench_browser import browser_context,ready  # noqa: F401
from tests.test_full_output_web import full_output_server  # noqa: F401

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(scope='module')
def workspace_url():
    process=subprocess.Popen([sys.executable,'-u',str(ROOT/'tools/workbench_fixture.py'),'--messages','120','--conversations','8','--workbench-demo'],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHON_DOTENV_DISABLED':'1'})
    output=queue.Queue()
    threading.Thread(target=lambda:[output.put(line) for line in process.stdout],daemon=True).start()
    try:
        deadline=time.time()+45
        while time.time()<deadline:
            try:line=output.get(timeout=1)
            except queue.Empty:continue
            try:data=json.loads(line)
            except ValueError:continue
            if 'url' in data:
                yield data
                return
        raise AssertionError('Isolated fixture failed to start')
    finally:process.terminate();process.wait(10)


def open_launcher(page):
    if page.locator('#workspace-settings').is_hidden():page.locator('#conversation-drawer-toggle').click()
    page.locator('#workspace-settings').click()
    page.locator('#workspace-menu').wait_for()


@pytest.mark.parametrize('width',[360,390,768,1440])
def test_requested_layout_and_launcher(browser_context,workspace_url,width):
    page=browser_context.new_page();page.set_viewport_size({'width':width,'height':844});ready(page,workspace_url['url'])
    assert page.locator('#chat-options-toggle,#chat-options,#wb-mobile-nav,.wb-nav-bottom').count()==0
    assert page.get_by_role('button',name='切换主题',exact=True).is_visible()
    assert page.get_by_role('button',name='终端',exact=True).is_visible()
    title=page.locator('#wb-conversation-title');header=page.locator('#header')
    assert title.bounding_box()['x']-header.bounding_box()['x']<90
    controls=page.locator('#wb-chat-controls').bounding_box();text=page.locator('#input').bounding_box()
    assert controls['y']>=text['y']+text['height']
    assert page.locator('#workspace-menu').is_hidden()
    open_launcher(page)
    menu=page.locator('#workspace-menu').bounding_box();trigger=page.locator('#workspace-settings').bounding_box()
    assert menu['width']<=260 and menu['y']+menu['height']<=trigger['y']+1
    page.keyboard.press('Escape');assert page.locator('#workspace-menu').is_hidden()
    page.locator('#workspace-settings').click()
    page.locator('#wb-nav-links [data-route=tasks]').click();page.get_by_role('heading',name='任务中心',exact=True).wait_for()
    assert page.locator('#workspace-menu').is_hidden()
    assert page.locator('#wb-page-title').inner_text()=='任务'
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    if width<768:assert page.get_by_label('任务来源对话',exact=True).bounding_box()['width']>220
    page.close()


def test_file_filters_precise_source_location_and_return(browser_context,workspace_url):
    page=browser_context.new_page();ready(page,workspace_url['url'],'files')
    source=workspace_url['demo']['source_conversation'];anchor=workspace_url['demo']['file_message']
    page.get_by_label('文件来源对话',exact=True).select_option(source)
    page.get_by_role('searchbox',name='按文件名搜索',exact=True).fill('来源验证')
    page.locator('.wb-file-table .wb-source-link').first.wait_for()
    page.wait_for_function('document.querySelectorAll(".wb-file-table tbody tr").length===1')
    link=page.locator('.wb-file-table .wb-source-link').first
    assert 'message=history%3A'+str(anchor) in link.get_attribute('href')
    page.get_by_role('button',name='预览',exact=True).click()
    page.locator('.wb-document-preview').wait_for()
    assert '来自来源验证对话' in page.locator('.wb-document-preview').inner_text()
    page.locator('#wb-detail-close').click()
    page.evaluate('window.savedFilePage=document.querySelector("#wb-page").firstElementChild')
    link.click()
    page.locator('.msg-row[data-record-id="'+str(anchor)+'"]').wait_for()
    assert page.evaluate('window.XGentConversations.current')==source
    target=page.locator('.msg-row[data-record-id="'+str(anchor)+'"]').bounding_box();log=page.locator('#log').bounding_box()
    assert target['y']<log['y']+log['height'] and target['y']+target['height']>log['y']
    page.get_by_role('link',name='返回文件与输出',exact=True).click()
    page.get_by_role('heading',name='文件与输出',exact=True).wait_for()
    assert page.get_by_label('文件来源对话',exact=True).input_value()==source
    assert page.get_by_role('searchbox',name='按文件名搜索',exact=True).input_value()=='来源验证'
    page.close()


def test_task_source_tokens_rounds_and_dark_code_unchanged(browser_context,workspace_url):
    page=browser_context.new_page();ready(page,workspace_url['url'],'tasks')
    target=page.locator('.wb-task-table tr').filter(has=page.get_by_role('button',name='每日服务健康检查',exact=True))
    target.locator('.wb-source-link').click()
    page.locator('.msg-row[data-record-id="'+str(workspace_url['demo']['task_message'])+'"]').wait_for()
    page.locator('#wb-latest').click()
    token=page.locator('.msg-row[data-record-id="'+str(workspace_url['demo']['token_message'])+'"]');token.wait_for()
    before=token.inner_text()
    assert '2400' in before and '800' in before and 'cached' in before
    assert page.locator('.msg-row[data-record-id="'+str(workspace_url['demo']['round_message'])+'"]').get_by_text('Agent 第 2 轮',exact=False).count()>0
    assert page.locator('.bubble-header').count()>0
    for dark in [False,True]:
        page.evaluate('(dark)=>document.body.classList.toggle("dark",dark)',dark)
        color=page.locator('.bubble pre').first.evaluate('(e)=>getComputedStyle(e).backgroundColor')
        assert color in ('rgb(24, 35, 52)','rgb(30, 41, 59)')
    page.reload();ready(page,workspace_url['url']);token.wait_for()
    assert token.inner_text()==before
    page.close()


def test_archived_source_is_read_only_without_restore(browser_context,workspace_url):
    source=workspace_url['demo']['source_conversation'];anchor=workspace_url['demo']['file_message']
    assert browser_context.request.post(workspace_url['url']+'/api/workbench/conversations/archive',data={'id':source}).ok
    current=browser_context.request.get(workspace_url['url']+'/api/workbench/conversations').json()['current_chat_id']
    page=browser_context.new_page();page.goto(workspace_url['url']+'/#/chat?conversation='+source+'&message=history:'+str(anchor)+'&archived=1')
    page.locator('#source-reader .source-target').wait_for()
    assert page.locator('#wb-shell > footer').is_hidden()
    state=browser_context.request.get(workspace_url['url']+'/api/workbench/conversations').json()
    assert state['current_chat_id']==current
    assert next(item for item in state['items'] if item['id']==source)['archived']
    page.locator('#conversation-new').click()
    page.wait_for_function('!document.body.classList.contains("source-readonly")')
    assert page.locator('#wb-shell > footer').is_visible()
    assert page.locator('#source-reader').count()==0
    page.close()


def test_complete_output_can_close_during_loading(full_output_server):
    playwright=pytest.importorskip('playwright.sync_api');server,_,_,_=full_output_server;base=f'http://127.0.0.1:{server.config.port}'
    with playwright.sync_playwright() as p:
        browser=p.chromium.launch(headless=True);context=browser.new_context()
        context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(base) else r.abort())
        assert context.request.post(base+'/api/login',data={'password':'temporary-test-only'}).ok
        page=context.new_page();page.goto(base)
        block=page.locator('blockquote.pb[data-output-path]').first;block.wait_for()
        pending=[];page.route('**/api/output/page',lambda r:pending.append(r))
        button=block.locator('.btn-output');button.click();assert button.inner_text()=='收起完整输出'
        button.click();assert block.locator('.output-pages').count()==0
        for request in pending:
            try:request.fulfill(json={'text':'late result','offset':0,'next_offset':11,'size':11,'filename':'test.txt','eof':True})
            except Exception:pass
        page.wait_for_timeout(150);assert block.locator('.output-pages').count()==0
        page.unroute('**/api/output/page');button.click();block.locator('.output-page-text').wait_for()
        button.click();assert block.locator('.output-pages').count()==0
        context.close();browser.close()


def test_settings_blacklist_and_price_forms(browser_context,workspace_url):
    page=browser_context.new_page();ready(page,workspace_url['url'],'settings')
    page.get_by_role('tab',name='Agent',exact=True).click()
    page.get_by_role('button',name='管理命令黑名单',exact=True).click()
    field=page.get_by_role('textbox',name='禁止片段（每行一条）',exact=True);field.wait_for();field.fill('fixture-only-blocked-command')
    page.get_by_role('button',name='保存黑名单',exact=True).click();page.get_by_text('黑名单已保存',exact=True).wait_for()
    assert browser_context.request.get(workspace_url['url']+'/api/workbench/settings/blacklist').json()['patterns']==['fixture-only-blocked-command']
    page.locator('#wb-dialog-close').click()
    page.evaluate('location.hash="/usage"');page.get_by_role('button',name='配置价格与合并规则',exact=True).click()
    page.get_by_role('button',name='添加模型价格',exact=True).click()
    price=page.locator('.wb-price-row').last;price.locator('[name=model]').fill('fixture-price-model');price.locator('[name=input]').fill('1.2');price.locator('[name=output]').fill('3.4')
    page.get_by_role('button',name='添加合并规则',exact=True).click();row=page.locator('.wb-merge-row').last;row.locator('[name=model]').fill('fixture-price-model');row.locator('[name=aliases]').fill('fixture-alias')
    page.get_by_role('button',name='保存规则',exact=True).click();page.locator('#wb-dialog').wait_for(state='hidden')
    boot=browser_context.request.get(workspace_url['url']+'/api/workbench/bootstrap').json()
    assert boot['settings']['values']['model_price_table']['fixture-price-model']['input']==1.2
    assert boot['settings']['values']['model_merge_map']['fixture-price-model']==['fixture-alias']
    page.close()
