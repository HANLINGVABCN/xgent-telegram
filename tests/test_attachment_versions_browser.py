import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def version_server():
    process = subprocess.Popen([sys.executable, '-u', str(ROOT / 'tools/workbench_fixture.py'), '--messages', '160',
                                '--conversations', '3', '--version-demo'], cwd=ROOT, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding='utf-8', env={**os.environ,'PYTHONIOENCODING':'utf-8'})
    lines = queue.Queue()
    def consume():
        for line in process.stdout:
            lines.put(line)
    threading.Thread(target=consume, daemon=True).start()
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try: line = lines.get(timeout=1)
            except queue.Empty: continue
            try: result = json.loads(line)
            except ValueError: continue
            if isinstance(result,dict) and result.get('url'):
                yield result
                return
        raise AssertionError('Fixture did not start')
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def browser(version_server):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip('Chromium unavailable')
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        base = version_server['url']
        context.route('**/*',lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
        assert context.request.post(base + '/api/login',data={'password': 'preview-only'}).ok
        page = context.new_page()
        errors=[]
        page.on('pageerror',lambda error: errors.append(str(error)))
        page.emulate_media(reduced_motion='reduce')
        try:
            yield page, context, base, version_server['demo'], errors
            assert not errors
        finally:
            browser.close()


def test_version_detail_text_paging_download_and_restore_without_new_messages(browser, tmp_path):
    page, context, base, demo, _ = browser
    page.set_viewport_size({'width':1440,'height':1000})
    page.goto(base)
    page.locator('.media-versions').last.wait_for()
    before=page.locator('#log>.msg-row').count()
    page.locator('.media-versions').last.click()
    panel=page.locator('#wb-detail')
    original=panel.locator('[data-version-id="'+demo['original_version']+'"]')
    original.get_by_role('button',name='预览版本',exact=True).click()
    page.locator('.version-preview pre').wait_for()
    assert '上传原件\n<safe> $value' == page.locator('.version-preview pre').inner_text()
    link=original.get_by_role('link',name='下载版本',exact=True).get_attribute('href')
    response=context.request.get(base+link)
    assert response.ok and response.body() == '上传原件\n<safe> $value'.encode()
    assert response.headers['content-disposition'].startswith('attachment')
    assert response.headers['x-content-type-options']=='nosniff'
    latest=panel.locator('[data-version-id="'+demo['current_version']+'"]')
    latest.get_by_role('button',name='预览版本',exact=True).click()
    page.wait_for_function('document.querySelector(".version-preview").innerText.includes("utf-16")')
    page.locator('.version-preview').get_by_role('button',name='下一页',exact=True).click()
    page.wait_for_function('document.querySelector(".version-preview").innerText.includes("65536")')
    original.get_by_role('button',name='恢复此版本',exact=True).click()
    page.locator('#wb-dialog').get_by_role('button',name='确认操作',exact=True).click()
    page.locator('.attachment-versions .wb-success').wait_for()
    assert page.locator('#log>.msg-row').count()==before
    assert original.get_by_role('button',name='恢复此版本',exact=True).is_disabled()
    for width, theme in ((1440,'light'),(390,'dark')):
        page.set_viewport_size({'width':width,'height':900})
        page.evaluate('(theme)=>document.body.classList.toggle("dark",theme==="dark")',theme)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path=str(tmp_path/f'attachment-versions-{width}-{theme}.png'))


def test_versions_from_files_and_identity_validation(browser):
    page, context, base, demo, _ = browser
    page.goto(base+'/#/files')
    page.get_by_role('heading',name='文件与输出',exact=True).wait_for()
    row=page.locator('.wb-file-table tbody tr').filter(has_text='版本说明.txt')
    row.get_by_role('button',name='版本',exact=True).click()
    page.locator('.attachment-versions .version-card').first.wait_for()
    for value in ('../secret',demo['versions_id'].split(':')[0]+':'+'0'*64):
        response=context.request.get(base+'/api/workbench/artifacts/versions',params={'id':value,'conversation_id':'global_memory'})
        assert response.status==409
    response=context.request.get(base+'/api/workbench/artifacts/version_download',params={'id':demo['versions_id'],'conversation_id':'global_memory','version_id':'../index.json'})
    assert response.status==409
    context.clear_cookies()
    assert context.request.get(base+'/api/workbench/artifacts/versions',params={'id':demo['versions_id'],'conversation_id':'global_memory'}).status==401


def test_set_bot_menu_updates_only_bot_selection_and_keeps_draft(browser, tmp_path):
    page,context,base,_,_=browser
    page.goto(base)
    page.wait_for_function('!!window.XGentConversations?.current')
    current=page.evaluate('window.XGentConversations.current')
    items=context.request.get(base+'/api/workbench/conversations').json()['items']
    target=next(item for item in items if item['id']!=current and not item['archived'])
    page.locator('#input').fill('保留网页草稿')
    order=page.locator('.conv-row').evaluate_all('(rows)=>rows.map(r=>r.dataset.conversationId)')
    row=page.locator('[data-conversation-id="'+target['id']+'"]')
    row.locator('.conv-more').click()
    page.get_by_role('menuitem',name='🤖 设为 Bot 当前对话',exact=True).click()
    row.locator('.conv-bot-badge').wait_for(state='visible')
    assert page.evaluate('window.XGentConversations.current')==current
    assert page.locator('#input').input_value()=='保留网页草稿'
    assert page.locator('.conv-row').evaluate_all('(rows)=>rows.map(r=>r.dataset.conversationId)')==order
    row.locator('.conv-more').click()
    assert page.get_by_role('menuitem',name='🤖 Bot 当前对话',exact=True).is_disabled()
    page.screenshot(path=str(tmp_path/'set-bot-menu.png'))
