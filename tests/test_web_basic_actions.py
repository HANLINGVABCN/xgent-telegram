"""Basic menu/logout survive failed admin imports and real touch interaction."""
from pathlib import Path
import pytest
from tests.test_workbench_browser import workspace_url  # noqa: F401


@pytest.fixture
def touch_context(workspace_url):
    playwright=pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():pytest.skip('Chromium unavailable')
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':390,'height':844},has_touch=True,is_mobile=True)
        context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(workspace_url['url']) else r.abort())
        assert context.request.post(workspace_url['url']+'/api/login',data={'password':workspace_url['password']}).ok
        yield context
        context.close();browser.close()


@pytest.mark.parametrize('failure',['workbench_404','dependency_404','syntax_error','storage_denied'])
def test_touch_menu_and_logout_do_not_depend_on_workbench(touch_context,workspace_url,failure):
    expect=pytest.importorskip('playwright.sync_api').expect
    page=touch_context.new_page();sent=[]
    page.route('**/api/command',lambda r:(sent.append(r.request.post_data_json),r.fulfill(json={'ok':True})))
    if failure=='workbench_404':page.route('**/assets/workbench.js',lambda r:r.fulfill(status=404,body='not found'))
    elif failure=='dependency_404':page.route('**/assets/page-cache.js',lambda r:r.fulfill(status=404,body='not found'))
    elif failure=='syntax_error':page.route('**/assets/workbench.js',lambda r:r.fulfill(content_type='text/javascript',body='export const broken = ;'))
    else:page.add_init_script("Storage.prototype.getItem=function(){throw new DOMException('storage blocked','SecurityError')};Storage.prototype.removeItem=function(){throw new DOMException('storage blocked','SecurityError')};Storage.prototype.setItem=function(){throw new DOMException('storage blocked','SecurityError')};")
    page.goto(workspace_url['url']);page.wait_for_function('!!window.XGentChat && !document.getElementById("btn-send").disabled')
    assert not page.evaluate('!!window.XGentWorkbench')
    draft='基础按钮必须能用';page.locator('#input').fill(draft)
    assert page.locator('#btn-menu,#menu-panel').count() == 0
    for selector in ['#btn-composer-menu']:
        page.locator(selector).tap()
        expect(page.locator('#composer-commands')).to_be_visible()
        assert not page.locator('#wb-dialog').is_visible()
        expect(page.locator('#composer-commands .composer-command strong').first).to_have_text('/start')
        assert page.locator('#input').input_value()==draft
        page.locator('#composer-commands-close').tap()
        expect(page.locator('#composer-commands')).not_to_be_visible()
    page.locator('#btn-composer-menu').tap();page.locator('#composer-commands .composer-command').first.tap()
    assert sent==[{'conversation_id':'global_memory','command':'/start'}]
    page.locator('#conversation-drawer-toggle').tap();page.locator('#workspace-settings').tap();page.locator('#wb-logout').tap()
    expect(page.locator('#login')).to_be_visible()
    assert not touch_context.request.get(workspace_url['url']+'/api/session').json()['authenticated']
    assert page.locator('#log .msg-row').count()==0
    assert page.locator('#input').input_value()==''


@pytest.mark.parametrize('module',['usage','settings'])
def test_optional_page_import_failure_is_local_to_that_page(touch_context,workspace_url,module):
    expect=pytest.importorskip('playwright.sync_api').expect
    page=touch_context.new_page();reads=[]
    page.on('request',lambda r:reads.append(r.url))
    page.route('**/assets/'+module+'.js',lambda r:r.fulfill(status=404,body='not found'))
    page.goto(workspace_url['url']);page.wait_for_function('document.getElementById("composer-model")?.disabled===false')
    assert not any('/assets/'+module+'.js' in url for url in reads)
    page.locator('#btn-composer-menu').tap();expect(page.locator('#composer-commands')).to_be_visible();page.keyboard.press('Escape')
    page.evaluate('(name)=>location.hash="/"+name',module)
    expect(page.locator('#wb-page')).to_contain_text('管理页面脚本加载失败')
    page.keyboard.press('Control+k');expect(page.locator('#composer-commands .composer-command strong').first).to_have_text('/start')
    page.locator('#conversation-drawer-toggle').tap();page.locator('#workspace-settings').tap();page.locator('#wb-logout').tap()
    expect(page.locator('#login')).to_be_visible()


def test_failure_fallback_retry_and_no_duplicate_logout(touch_context,workspace_url):
    expect=pytest.importorskip('playwright.sync_api').expect
    page=touch_context.new_page();requests=[]
    page.route('**/assets/workbench.js',lambda r:r.abort())
    page.route('**/api/workbench/bootstrap',lambda r:r.fulfill(status=503,json={'error':'fixture offline'}))
    page.goto(workspace_url['url']);page.wait_for_function('!!window.XGentChat')
    page.locator('#btn-composer-menu').tap()
    expect(page.locator('#composer-commands')).to_contain_text('fixture offline')
    page.unroute('**/api/workbench/bootstrap')
    page.get_by_role('button',name='重新读取',exact=True).tap()
    expect(page.locator('#composer-commands .composer-command strong').first).to_have_text('/start')
    page.keyboard.press('Escape')
    def fail(r):requests.append(r.request);r.fulfill(status=503,json={'error':'logout offline'})
    page.route('**/api/logout',fail)
    page.locator('#conversation-drawer-toggle').tap();page.locator('#workspace-settings').tap();page.locator('#wb-logout').tap()
    expect(page.locator('#wb-dialog')).to_contain_text('退出失败：logout offline')
    assert len(requests)==1
    page.unroute('**/api/logout');page.get_by_role('button',name='重试退出',exact=True).tap()
    expect(page.locator('#login')).to_be_visible()


def test_fallback_request_timeout_is_visible(touch_context,workspace_url):
    expect=pytest.importorskip('playwright.sync_api').expect
    page=touch_context.new_page()
    page.add_init_script("const timer=window.setTimeout;window.setTimeout=(fn,ms,...args)=>timer(fn,ms===12000?100:ms,...args);")
    page.route('**/assets/workbench.js',lambda r:r.abort())
    page.route('**/api/workbench/bootstrap',lambda r:None)
    page.goto(workspace_url['url']);page.wait_for_function('!!window.XGentChat')
    page.locator('#btn-composer-menu').tap()
    expect(page.locator('#composer-commands')).to_contain_text('请求超时')
    assert page.get_by_role('button',name='重新读取',exact=True).is_visible()
    page.keyboard.press('Escape');assert not page.locator('#composer-commands').is_visible()


def test_normal_touch_controls_and_screenshot(touch_context,workspace_url):
    expect=pytest.importorskip('playwright.sync_api').expect
    page=touch_context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(workspace_url['url']);page.wait_for_function('document.getElementById("composer-model")?.disabled===false')
    page.locator('#btn-composer-menu').tap();expect(page.locator('#composer-commands .composer-command strong').first).to_have_text('/start')
    output=Path(__file__).resolve().parents[1]/'workspace'/'web-basic-actions-qa';output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/'390-menu.png'))
    page.keyboard.press('Escape');page.locator('#conversation-drawer-toggle').tap();page.locator('#workspace-settings').tap();page.locator('#wb-logout').tap();expect(page.locator('#login')).to_be_visible()
    page.screenshot(path=str(output/'390-logged-out.png'))
    assert not errors


def test_composer_menu_is_anchored_nonmodal_toggle_and_outside_dismiss(touch_context,workspace_url):
    expect=pytest.importorskip('playwright.sync_api').expect
    page=touch_context.new_page();page.goto(workspace_url['url']);page.wait_for_function('document.getElementById("composer-model")?.disabled===false')
    page.locator('#input').fill('草稿不被菜单覆盖')
    panel=page.locator('#composer-commands');trigger=page.locator('#btn-composer-menu')
    for height in [844,560]:
        page.set_viewport_size({'width':390,'height':height})
        trigger.tap();expect(panel).to_be_visible()
        expect(trigger).to_have_attribute('aria-expanded','true')
        bounds=panel.bounding_box();composer=page.locator('#composer').bounding_box()
        assert bounds['y']>=page.locator('#header').bounding_box()['height']
        assert bounds['y']+bounds['height']<=composer['y']
        assert bounds['height']<=320 and bounds['height']<height*.7
        assert abs(bounds['x']-composer['x'])<=1 and abs(bounds['width']-composer['width'])<=1
        assert not page.locator('dialog[open]').count()
        assert not page.locator('#overlay').is_visible()
        assert not page.locator('#wb-shell').evaluate('(n)=>n.inert')
        assert not page.locator('#input').evaluate('(n)=>n===document.activeElement')
        panel.locator('.composer-command-list').evaluate('(n)=>n.scrollTop=n.scrollHeight')
        assert panel.locator('.composer-command').last.is_visible()
        trigger.tap();expect(panel).not_to_be_visible();expect(trigger).to_have_attribute('aria-expanded','false')
        trigger.tap();expect(panel).to_be_visible()
        page.locator('#input').tap();expect(panel).not_to_be_visible()
        assert page.locator('#input').input_value()=='草稿不被菜单覆盖'
        trigger.tap();expect(panel).to_be_visible();page.locator('#wb-conversation-title').tap();expect(panel).not_to_be_visible()


def test_menu_dismissed_request_cannot_reopen_and_dangerous_command_confirms(touch_context,workspace_url):
    expect=pytest.importorskip('playwright.sync_api').expect
    page=touch_context.new_page();page.route('**/assets/workbench.js',lambda r:r.abort())
    reads=[];page.route('**/api/workbench/bootstrap',lambda r:reads.append(r))
    page.goto(workspace_url['url']);page.wait_for_function('!!window.XGentChat && !document.getElementById("btn-send").disabled')
    page.locator('#btn-composer-menu').tap();expect(page.locator('#composer-commands')).to_contain_text('正在读取')
    page.locator('#btn-composer-menu').tap();expect(page.locator('#composer-commands')).not_to_be_visible()
    for route in reads:route.fulfill(json={'commands':[{'cmd':'/start','desc':'打开主菜单'}]})
    expect(page.locator('#composer-commands')).not_to_be_visible()
    page.unroute('**/api/workbench/bootstrap')
    sent=[];page.route('**/api/command',lambda r:(sent.append(r.request.post_data_json),r.fulfill(json={'ok':True})))
    page.on('dialog',lambda d:d.dismiss())
    page.locator('#btn-composer-menu').tap()
    page.locator('#composer-commands .composer-command').filter(has_text='/clear').tap()
    assert not sent
    assert page.locator('#composer-commands').is_hidden()


def test_only_primary_reset_command_is_listed_and_legacy_alias_still_executes(touch_context,workspace_url):
    url=workspace_url['url']
    data=touch_context.request.get(url+'/api/workbench/bootstrap').json()
    resets=[item['cmd'] for item in data['commands'] if item['cmd'] in ('/clear','/clear_memory')]
    assert resets==['/clear']
    page=touch_context.new_page();page.goto(url)
    page.wait_for_function('!!window.XGentWorkbench')
    page.locator('#btn-composer-menu').tap()
    assert page.locator('#composer-commands .composer-command strong').filter(has_text='/clear_memory').count()==0
    assert page.locator('#composer-commands .composer-command strong').get_by_text('/clear',exact=True).count()==1
    page.keyboard.press('Escape')
    current=touch_context.request.get(url+'/api/workbench/conversations').json()['current_chat_id']
    before=touch_context.request.get(url+'/api/workbench/history',params={'conversation_id':current,'limit':1}).json()['ui_generation']
    response=touch_context.request.post(url+'/api/command',data={'command':'/clear_memory','conversation_id':current})
    assert response.ok
    page.wait_for_function('async ([cid,before])=>{const r=await fetch("/api/workbench/history?limit=1&conversation_id="+encodeURIComponent(cid));return (await r.json()).ui_generation>before;}',arg=[current,before])
    page.close()
