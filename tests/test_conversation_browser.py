"""Conversation-first sidebar acceptance against an isolated real Web server."""
import pytest
from tests.test_workbench_browser import workspace_url, browser_context, ready


def current(page):
    return page.evaluate('window.XGentConversations.current')


def row(page, conversation_id):
    return page.locator('.conv-row[data-conversation-id="' + conversation_id + '"]')


def select(page, conversation_id):
    row(page, conversation_id).locator('.conv-open').click()
    page.wait_for_function('(id)=>window.XGentConversations.current===id', arg=conversation_id)


def create(context, url, name):
    response = context.request.post(url + '/api/workbench/conversations/create', data={'name': name})
    assert response.ok
    return response.json()['current_chat_id']


@pytest.fixture
def conversation_page(browser_context, workspace_url):
    response = browser_context.request.post(workspace_url['url'] + '/api/workbench/conversations/switch', data={'id': 'global_memory'})
    assert response.ok
    page = browser_context.new_page()
    ready(page, workspace_url['url'])
    page.locator('#conversation-list .conv-row').first.wait_for()
    page.wait_for_function('window.XGentConversations.current === "global_memory"')
    yield page
    page.close()


def test_old_corner_menu_is_removed_and_composer_commands_still_work(conversation_page):
    page = conversation_page
    assert page.locator('#btn-menu,#wb-command,#menu-panel,#menu-search,#cmd-list,#overlay,#wb-conversation-controls').count() == 0
    assert page.locator('#wb-nav').is_visible()
    assert row(page, 'global_memory').locator('.conv-open').get_attribute('aria-current') == 'true'
    page.locator('#input').fill('保留输入草稿')
    page.locator('#btn-composer-menu').click()
    page.locator('#composer-commands .composer-command').first.wait_for()
    assert page.locator('#input').input_value() == '保留输入草稿'
    page.keyboard.press('Escape')
    assert page.locator('#composer-commands').is_hidden()


def test_one_click_switch_and_new_keep_independent_drafts(conversation_page, browser_context, workspace_url):
    page = conversation_page
    original = current(page)
    page.locator('#input').fill('原对话草稿')
    page.get_by_role('button', name='新对话', exact=True).click()
    page.wait_for_function('(id)=>window.XGentConversations.current!==id', arg=original)
    created = current(page)
    page.locator('#input').fill('新对话草稿')
    select(page, original)
    page.wait_for_function('document.querySelector("#input").value==="原对话草稿"')
    select(page, created)
    page.wait_for_function('document.querySelector("#input").value==="新对话草稿"')
    ids = page.locator('.conv-row').evaluate_all('(rows)=>rows.map(row=>row.dataset.conversationId)')
    select(page, original)
    assert page.locator('.conv-row').evaluate_all('(rows)=>rows.map(row=>row.dataset.conversationId)') == ids


def test_rename_inactive_chat_stays_bound_while_other_tab_switches(conversation_page, browser_context, workspace_url):
    page = conversation_page
    a = create(browser_context, workspace_url['url'], '需要重命名的对话')
    b = create(browser_context, workspace_url['url'], '正在查看的对话')
    page.evaluate('window.XGentConversations.refresh()')
    page.wait_for_function('(id)=>window.XGentConversations.current===id', arg=b)
    row(page, a).locator('.conv-more').click()
    page.get_by_role('menuitem', name='重命名', exact=True).click()
    field = page.get_by_role('textbox', name='对话名称', exact=True)
    field.fill('部署任务 · 中文与标点 / <script>')
    page.wait_for_timeout(3300)
    assert field.input_value() == '部署任务 · 中文与标点 / <script>'
    browser_context.request.post(workspace_url['url'] + '/api/workbench/conversations/switch', data={'id': 'global_memory'})
    page.evaluate('window.XGentConversations.refresh()')
    assert field.input_value() == '部署任务 · 中文与标点 / <script>'
    page.get_by_role('button', name='保存', exact=True).click()
    page.locator('#conversation-rename-dialog').wait_for(state='hidden')
    assert row(page, a).locator('.conv-row-title').inner_text() == '部署任务 · 中文与标点 / <script>'
    assert current(page) == 'global_memory'
    assert page.locator('.conv-row-title script').count() == 0
    row(page, a).locator('.conv-more').click()
    page.keyboard.press('Escape')
    assert page.locator('#conversation-item-menu').is_hidden()
    assert row(page, a).locator('.conv-more').evaluate('(element)=>document.activeElement===element')


def test_archive_can_be_undone_then_restored_from_archive_list(conversation_page, browser_context, workspace_url):
    page = conversation_page
    target = create(browser_context, workspace_url['url'], '可以归档的任务')
    page.evaluate('window.XGentConversations.refresh()')
    page.locator('#input').fill('归档后也要保留的草稿')
    row(page, target).locator('.conv-more').click()
    page.get_by_role('menuitem', name='归档对话', exact=True).click()
    row(page, target).wait_for(state='detached')
    page.get_by_role('button', name='撤销', exact=True).click()
    row(page, target).wait_for()
    page.wait_for_function('(id)=>window.XGentConversations.current===id', arg=target)
    assert page.locator('#input').input_value() == '归档后也要保留的草稿'
    row(page, target).locator('.conv-more').click()
    page.get_by_role('menuitem', name='归档对话', exact=True).click()
    row(page, target).wait_for(state='detached')
    after_archive = current(page)
    page.locator('#workspace-settings').click();page.locator('#conversation-archive-view').click()
    row(page, target).locator('.conv-more').click()
    page.get_by_role('menuitem', name='恢复对话', exact=True).click()
    row(page, target).wait_for(state='detached')
    assert current(page) == after_archive  # Restoring a row must not steal the current view.
    page.get_by_role('button', name='返回最近', exact=True).click()
    select(page, target)
    assert page.locator('#input').input_value() == '归档后也要保留的草稿'


def test_search_and_empty_results_do_not_rebuild_input(conversation_page, browser_context, workspace_url):
    page = conversation_page
    target = create(browser_context, workspace_url['url'], 'Python 日志分析与配置排查')
    page.evaluate('window.XGentConversations.refresh()')
    search = page.get_by_role('searchbox', name='搜索对话', exact=True)
    search.fill('python 日志')
    assert page.locator('.conv-row').count() == 1
    assert row(page, target).is_visible()
    page.evaluate('window.XGentConversations.refresh()')
    assert search.input_value() == 'python 日志'
    search.fill('不存在的唯一关键词98765')
    assert page.locator('.conv-row').count() == 0
    assert '没有找到对话' in page.locator('#conversation-list').inner_text()
    page.get_by_role('button', name='清除对话搜索', exact=True).click()
    assert page.locator('.conv-row').count() > 1


def test_rename_failure_preserves_draft_and_retries_same_target(conversation_page, browser_context, workspace_url):
    page = conversation_page
    target = create(browser_context, workspace_url['url'], '修改失败后重试')
    page.evaluate('window.XGentConversations.refresh()')
    row(page, target).locator('.conv-more').click()
    page.get_by_role('menuitem', name='重命名', exact=True).click()
    field = page.get_by_role('textbox', name='对话名称', exact=True)
    field.fill('还没有保存的名称')
    page.route('**/api/workbench/conversations/rename', lambda route: route.fulfill(status=503, json={'error': '保存暂时不可用'}))
    page.get_by_role('button', name='保存', exact=True).click()
    page.get_by_role('alert').filter(has_text='保存暂时不可用').wait_for()
    assert field.input_value() == '还没有保存的名称'
    assert row(page, target).locator('.conv-row-title').inner_text() == '修改失败后重试'
    page.unroute('**/api/workbench/conversations/rename')
    page.get_by_role('button', name='保存', exact=True).click()
    page.locator('#conversation-rename-dialog').wait_for(state='hidden')
    assert row(page, target).locator('.conv-row-title').inner_text() == '还没有保存的名称'


def test_double_new_only_creates_one_chat(conversation_page, browser_context, workspace_url):
    page = conversation_page
    original = current(page)
    before = len(browser_context.request.get(workspace_url['url'] + '/api/workbench/conversations').json()['items'])
    page.evaluate('()=>{const button=document.getElementById("conversation-new");button.click();button.click();}')
    page.wait_for_function('(id)=>window.XGentConversations.current!==id', arg=original)
    after = len(browser_context.request.get(workspace_url['url'] + '/api/workbench/conversations').json()['items'])
    assert after == before + 1


def test_mobile_drawer_keyboard_backdrop_and_resize(conversation_page, tmp_path):
    page = conversation_page
    page.set_viewport_size({'width': 390, 'height': 844})
    page.evaluate('document.body.classList.add("dark")')
    page.wait_for_timeout(250)
    assert page.locator('#wb-nav').is_hidden()
    toggle = page.get_by_role('button', name='对话记录', exact=True)
    toggle.click()
    drawer = page.get_by_role('dialog', name='对话与工作台', exact=True)
    assert drawer.is_visible()
    assert page.locator('#wb-shell').evaluate('(element)=>element.inert')
    page.locator('#workspace-settings').focus()
    page.keyboard.press('Tab')
    assert page.locator('#wb-nav .wb-brand').evaluate('(element)=>element===document.activeElement')
    row(page, current(page)).locator('.conv-more').click()
    page.keyboard.press('Escape')
    assert drawer.is_visible() and page.locator('#conversation-item-menu').is_hidden()
    page.screenshot(path=str(tmp_path / 'mobile-conversations.png'))
    page.keyboard.press('Escape')
    assert page.locator('#wb-nav').is_hidden()
    assert not page.locator('#wb-shell').evaluate('(element)=>element.inert')
    assert toggle.evaluate('(element)=>element===document.activeElement')
    toggle.click()
    page.locator('#conversation-backdrop').click(position={'x': 383, 'y': 80})
    assert page.locator('#wb-nav').is_hidden()
    toggle.click()
    page.set_viewport_size({'width': 1024, 'height': 800})
    # Viewport acknowledgement can precede the browser's resize event.
    page.wait_for_function('!document.getElementById("wb-shell").inert')
    assert not page.locator('#wb-shell').evaluate('(element)=>element.inert')
    assert page.locator('#wb-nav').is_visible()
    assert page.locator('#conversation-backdrop').is_hidden()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')


def test_cross_tab_switch_updates_sidebar_without_stealing_search(browser_context, workspace_url):
    first = browser_context.new_page(); second = browser_context.new_page()
    ready(first, workspace_url['url']); ready(second, workspace_url['url'])
    first.locator('.conv-row').first.wait_for(); second.locator('.conv-row').first.wait_for()
    original = current(first)
    first.get_by_role('searchbox', name='搜索对话').fill('保留搜索输入')
    second.get_by_role('button', name='新对话', exact=True).click()
    second.wait_for_function('(id)=>window.XGentConversations.current!==id', arg=original)
    selected = current(second)
    first.wait_for_function('(id)=>window.XGentConversations.current===id', arg=selected)
    assert first.get_by_role('searchbox', name='搜索对话').input_value() == '保留搜索输入'
    first.close(); second.close()


def test_sidebar_survives_optional_workbench_script_failure(browser_context, workspace_url):
    page = browser_context.new_page()
    page.route('**/assets/workbench.js', lambda route: route.fulfill(status=404, body='not found'))
    page.route('**/assets/components.js', lambda route: route.fulfill(status=404, body='not found'))
    page.goto(workspace_url['url'])
    page.locator('.conv-row').first.wait_for()
    original = current(page)
    page.get_by_role('button', name='新对话', exact=True).click()
    page.wait_for_function('(id)=>window.XGentConversations.current!==id', arg=original)
    assert not page.evaluate('!!window.XGentWorkbench')
    assert page.locator('#btn-menu').count() == 0
    page.close()



def test_failed_switch_keeps_current_chat_and_draft(conversation_page, browser_context, workspace_url):
    page = conversation_page
    target = create(browser_context, workspace_url['url'], '切换失败不会误投')
    browser_context.request.post(workspace_url['url'] + '/api/workbench/conversations/switch', data={'id': 'global_memory'})
    page.evaluate('window.XGentConversations.refresh()')
    page.locator('#input').fill('绝不能丢失的草稿')
    page.route('**/api/workbench/conversations/switch', lambda route: route.fulfill(status=503, json={'error': '网络暂时不可用'}))
    row(page, target).locator('.conv-open').click()
    page.locator('#conversation-feedback').get_by_text('切换失败：网络暂时不可用', exact=True).wait_for()
    assert current(page) == 'global_memory'
    assert page.locator('#input').input_value() == '绝不能丢失的草稿'
    page.unroute('**/api/workbench/conversations/switch')
    page.locator('#conversation-feedback').get_by_role('button', name='重试', exact=True).click()
    page.wait_for_function('(id)=>window.XGentConversations.current===id', arg=target)
    select(page, 'global_memory')
    assert page.locator('#input').input_value() == '绝不能丢失的草稿'


def test_initial_list_error_has_retry_without_breaking_chat(browser_context, workspace_url):
    page = browser_context.new_page()
    page.route('**/api/workbench/conversations', lambda route: route.fulfill(status=503, json={'error': '列表读取失败'}))
    page.route('**/api/workbench/bootstrap', lambda route: route.fulfill(status=503, json={'error': '配置暂时不可用'}))
    page.goto(workspace_url['url'])
    page.locator('#conversation-list').get_by_text('对话列表暂时不可用', exact=True).wait_for()
    assert page.get_by_role('button', name='新对话', exact=True).is_disabled()
    assert page.evaluate('!!window.XGentChat')
    page.unroute('**/api/workbench/conversations')
    page.locator('#conversation-list').get_by_role('button', name='重新加载', exact=True).click()
    page.locator('.conv-row').first.wait_for()
    assert page.get_by_role('button', name='新对话', exact=True).is_enabled()
    page.close()


def test_background_generation_is_marked_and_stop_uses_original_run(conversation_page, browser_context, workspace_url):
    page = conversation_page
    a = create(browser_context, workspace_url['url'], '后台继续执行的任务')
    b = create(browser_context, workspace_url['url'], '当前查看的对话')
    payload = browser_context.request.get(workspace_url['url'] + '/api/workbench/conversations').json()
    payload['running'] = {'run_id': 'original-run', 'conversation_id': a, 'name': '后台继续执行的任务'}
    page.evaluate('(state)=>window.XGentConversations.accept(state)', payload)
    assert current(page) == b
    assert row(page, a).locator('.conv-running-dot').is_visible()
    assert '后台继续执行的任务' in page.locator('#conversation-running').inner_text()
    assert page.get_by_role('button', name='新对话', exact=True).is_enabled()
    stopped = []
    def stop(route):
        stopped.append(route.request.post_data_json)
        route.fulfill(json={'ok': True})
    page.route('**/api/stop', stop)
    page.locator('#conversation-running').get_by_role('button', name='停止', exact=True).click()
    page.wait_for_function('document.getElementById("conversation-running").hidden')
    assert stopped == [{'run_id': 'original-run', 'conversation_id': a}]


@pytest.mark.parametrize('width,height', [(320, 640), (390, 844), (768, 1024), (1024, 768), (1440, 900)])
def test_long_title_and_dialog_fit_both_themes(browser_context, workspace_url, width, height, tmp_path):
    target = create(browser_context, workspace_url['url'], '长标题中文名称与英文long-title' * 3)
    page = browser_context.new_page(); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': height})
    ready(page, workspace_url['url'])
    row(page, target).wait_for(state='attached')
    for dark in (False, True):
        page.evaluate('(dark)=>document.body.classList.toggle("dark",dark)', dark)
        page.wait_for_timeout(220)
        if width < 768: page.get_by_role('button', name='对话记录', exact=True).click()
        row(page, target).locator('.conv-more').click()
        box = page.locator('#conversation-item-menu').bounding_box()
        assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1
        assert box['y'] >= 0 and box['y'] + box['height'] <= height + 1
        page.get_by_role('menuitem', name='重命名', exact=True).click()
        dialog = page.locator('#conversation-rename-dialog').bounding_box()
        assert dialog['x'] >= 0 and dialog['x'] + dialog['width'] <= width + 1
        page.get_by_role('textbox', name='对话名称', exact=True).fill('   ')
        page.get_by_role('button', name='保存', exact=True).click()
        assert page.locator('#conversation-rename-error').inner_text() == '请输入对话名称。'
        page.keyboard.press('Escape')
        assert page.locator('#conversation-rename-dialog').is_hidden()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path=str(tmp_path / (str(width) + ('-dark' if dark else '-light') + '.png')))
        if width < 768: page.keyboard.press('Escape')
    assert not errors
    page.close()


def test_list_and_row_menu_support_keyboard_navigation(conversation_page):
    page = conversation_page
    search = page.get_by_role('searchbox', name='搜索对话', exact=True)
    search.focus(); page.keyboard.press('ArrowDown')
    assert page.locator('.conv-open').first.evaluate('(element)=>document.activeElement===element')
    page.keyboard.press('End')
    assert page.locator('.conv-open').last.evaluate('(element)=>document.activeElement===element')
    page.keyboard.press('Home'); page.keyboard.press('ArrowUp')
    assert search.evaluate('(element)=>document.activeElement===element')
    row(page, current(page)).locator('.conv-more').focus()
    page.keyboard.press('ArrowDown')
    assert page.get_by_role('menuitem', name='重命名', exact=True).evaluate('(element)=>document.activeElement===element')
    page.keyboard.press('ArrowDown')
    assert page.get_by_role('menuitem', name='归档对话', exact=True).evaluate('(element)=>document.activeElement===element')
    page.keyboard.press('Escape')
    assert page.locator('#conversation-item-menu').is_hidden()
