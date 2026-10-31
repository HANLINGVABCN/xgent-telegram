import re
import zipfile

from tests.test_conversation_browser import conversation_page, create, current, row  # noqa: F401
from tests.test_workbench_browser import browser_context, workspace_url, ready  # noqa: F401


def organize(context, base, action, **data):
    state = context.request.get(base + '/api/workbench/conversations').json()
    response = context.request.post(base + '/api/workbench/' + action, data={**data, 'revision': state['revision']})
    assert response.ok, response.text()
    return response.json()


def test_default_group_all_pins_hover_move_and_no_reset(conversation_page, browser_context, workspace_url, tmp_path):
    page = conversation_page
    base = workspace_url['url']
    selector = page.locator('#conversation-group-select')
    assert selector.input_value() == 'ungrouped'
    assert selector.locator('option').all_text_contents()[:2] == ['默认分组', '全部对话']
    favorite = organize(browser_context, base, 'conversation-groups/create', name='收藏')['group_id']
    project = organize(browser_context, base, 'conversation-groups/create', name='项目')['group_id']
    a = create(browser_context, base, '默认置顶')
    b = create(browser_context, base, '收藏置顶')
    recent = create(browser_context, base, '普通最近对话')
    organize(browser_context, base, 'conversations/move', id=b, group_id=favorite)
    organize(browser_context, base, 'conversations/pin', id=a, group_id='ungrouped')
    organize(browser_context, base, 'conversations/pin', id=b, group_id=favorite)
    page.evaluate('window.XGentConversations.refresh()')
    row(page, a).wait_for()
    assert not row(page, b).count()
    selector.select_option('all')
    row(page, b).wait_for()
    order = page.locator('.conv-row').evaluate_all('(nodes)=>nodes.map(node=>node.dataset.conversationId)')
    assert set(order[:2]) == {a, b} and order.index(recent) > 1
    assert page.locator('.conv-date-group').first.inner_text() == '置顶'
    row(page, b).locator('.conv-more').click()
    assert page.get_by_role('menuitem', name='导出', exact=True).is_visible()
    assert not page.get_by_role('menuitem', name=re.compile('重置')).count()
    assert '收藏' in page.get_by_role('menuitem', name=re.compile('组内置顶')).get_attribute('title')
    move = page.get_by_role('menuitem', name='移动到', exact=True)
    move.hover()
    submenu = page.get_by_role('menu', name='移动到分组', exact=True)
    submenu.wait_for()
    assert submenu.get_by_role('menuitem', name='项目', exact=True).is_visible()
    assert submenu.get_by_role('menuitem', name='✓ 收藏', exact=True).is_disabled()
    assert not submenu.get_by_role('menuitem', name='全部对话', exact=True).count()
    assert submenu.bounding_box()['x'] >= move.bounding_box()['x'] + move.bounding_box()['width']
    page.screenshot(path=str(tmp_path / 'sidebar-desktop.png'))
    move.focus()
    page.keyboard.press('ArrowRight')
    assert submenu.get_by_role('menuitem', name='默认分组', exact=True).evaluate('(element)=>document.activeElement===element')
    page.keyboard.press('ArrowDown')
    assert submenu.get_by_role('menuitem', name='项目', exact=True).evaluate('(element)=>document.activeElement===element')
    page.keyboard.press('ArrowLeft')
    assert submenu.is_hidden() and move.evaluate('(element)=>document.activeElement===element')
    page.keyboard.press('ArrowRight')
    page.keyboard.press('Escape')
    assert submenu.is_hidden() and page.locator('#conversation-item-menu').is_visible()
    page.keyboard.press('ArrowRight')
    submenu.get_by_role('menuitem', name='项目', exact=True).click()
    page.locator('#conversation-item-menu').wait_for(state='hidden')
    updated = browser_context.request.get(base + '/api/workbench/conversations').json()
    target = next(item for item in updated['items'] if item['id'] == b)
    assert target['group_id'] == project and not target['pinned']
    selector.select_option('ungrouped')
    assert not row(page, b).count()
    page.reload()
    page.wait_for_function('window.XGentConversations.current!=null')
    assert selector.input_value() == 'ungrouped'


def test_sidebar_export_downloads_inactive_conversation_without_chat_messages(conversation_page, browser_context, workspace_url, tmp_path):
    page = conversation_page
    base = workspace_url['url']
    target = 'global_memory'
    active = create(browser_context, base, '当前打开的其他对话')
    page.evaluate('window.XGentConversations.refresh()')
    page.wait_for_function('(id)=>window.XGentConversations.current===id', arg=active)
    page.wait_for_function('document.getElementById("btn-send").disabled===false')
    before = page.locator('#log').inner_text()
    row(page, target).locator('.conv-more').click()
    with page.expect_download() as capture:
        page.get_by_role('menuitem', name='导出', exact=True).click()
    downloaded = capture.value
    assert downloaded.suggested_filename == '系统记忆.zip'
    path = tmp_path / 'export.zip'
    downloaded.save_as(path)
    with zipfile.ZipFile(path) as archive:
        text = '\n'.join(archive.read(name).decode('utf-8') for name in archive.namelist())
        assert '历史记录 100' in text
    assert current(page) == active
    assert page.locator('#log').inner_text() == before
    assert '正在导出' not in page.locator('#log').inner_text()


def test_mobile_move_opens_by_tap_and_stays_inside_viewport(browser_context, workspace_url, tmp_path):
    base = workspace_url['url']
    group = organize(browser_context, base, 'conversation-groups/create', name='手机目标分组')['group_id']
    target = create(browser_context, base, '手机移动测试')
    context = browser_context.browser.new_context(viewport={'width': 390, 'height': 844}, has_touch=True, is_mobile=True)
    context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
    assert context.request.post(base + '/api/login', data={'password': workspace_url['password']}).ok
    try:
        page = context.new_page(); ready(page, base)
        page.locator('#conversation-drawer-toggle').tap()
        row(page, target).locator('.conv-more').tap()
        page.get_by_role('menuitem', name='移动到', exact=True).tap()
        submenu = page.get_by_role('menu', name='移动到分组', exact=True)
        submenu.wait_for()
        assert submenu.bounding_box()['x'] >= 0
        assert submenu.bounding_box()['x'] + submenu.bounding_box()['width'] <= 391
        page.screenshot(path=str(tmp_path / 'sidebar-mobile.png'))
        submenu.get_by_role('menuitem', name='手机目标分组', exact=True).tap()
        page.locator('#conversation-item-menu').wait_for(state='hidden')
        page.locator('#conversation-group-select').select_option(group)
        row(page, target).wait_for()
        assert current(page) == target
    finally:
        context.close()
