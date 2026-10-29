"""Real browser acceptance against synthetic, isolated conversation history."""
import json
import sqlite3
import time
from contextlib import suppress

import pytest

from tests import test_workbench_browser as fixture_helpers
from tests.message_fixtures import typed_record

workspace_url = fixture_helpers.workspace_url
browser_context = fixture_helpers.browser_context
ready = fixture_helpers.ready


@pytest.fixture
def search_seed(browser_context, workspace_url):
    base = workspace_url['url']
    def create(name):
        response = browser_context.request.post(base + '/api/workbench/conversations/create', data={'name': name})
        assert response.ok
        return response.json()['current_chat_id']
    active = create('搜索验收甲：名称里没有正文词')
    archived = create('搜索验收乙：已归档')
    with sqlite3.connect(workspace_url['database_path']) as conn:
        def add(cid, text, kind='user_text', role='user', meta=None):
            cursor = conn.execute('INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id,metadata) VALUES(1,1,?,?,?,?,?,?)',
                (kind, role, text, time.time(), cid, json.dumps(typed_record({'msg_type':kind,'role':role,'metadata':meta})['metadata'])))
            return 'history:' + str(cursor.lastrowid)
        anchor = add(active, '仅在正文命中的独有词 · 用户说明')
        add(archived, '仅在正文命中的独有词 · 归档 AI 回复', 'ai_reply', 'assistant')
        add(active, '<img src=x onerror="window.searchXss=1">原样可检索')
        for i in range(65):
            add(active, '搜索分页验收 ' + str(i), 'agent_result' if i == 0 else 'user_text', 'assistant' if i == 0 else 'user')
    assert browser_context.request.post(base + '/api/workbench/conversations/archive', data={'id': archived}).ok
    assert browser_context.request.post(base + '/api/workbench/conversations/switch', data={'id': 'global_memory'}).ok
    yield {'active': active, 'archived': archived, 'anchor': anchor}
    assert browser_context.request.post(base + '/api/workbench/conversations/switch', data={'id': 'global_memory'}).ok
    with sqlite3.connect(workspace_url['database_path']) as conn:
        conn.execute('DELETE FROM global_messages WHERE session_id IN (?,?)', (active, archived))
        conn.execute('DELETE FROM chat_sessions WHERE id IN (?,?)', (active, archived))


def open_dialog(page):
    page.keyboard.press('Control+f')
    page.locator('#global-search-dialog').wait_for()
    return page.get_by_role('searchbox', name='全局搜索关键词', exact=True)


def test_sidebar_content_search_crosses_conversations_and_does_not_switch_bot(browser_context, workspace_url, search_seed):
    page = browser_context.new_page(); ready(page, workspace_url['url'])
    page.locator('#input').fill('搜索期间不能丢的草稿')
    before = browser_context.request.get(workspace_url['url'] + '/api/workbench/conversations').json()
    search = page.get_by_role('searchbox', name='搜索对话名称和内容', exact=True)
    search.fill('仅在正文命中的独有词')
    page.wait_for_function('document.querySelectorAll("#conversation-list .global-search-hit").length===2')
    assert page.locator('#conversation-list mark').count() == 2
    assert '已归档 · 只读' in page.locator('#conversation-list').inner_text()
    assert page.evaluate('window.XGentConversations.current') == 'global_memory'
    assert page.locator('#input').input_value() == '搜索期间不能丢的草稿'
    after = browser_context.request.get(workspace_url['url'] + '/api/workbench/conversations').json()
    assert after['telegram_conversation_id'] == before['telegram_conversation_id']
    page.locator('#conversation-list .global-search-hit').filter(has_text='用户说明').click()
    page.wait_for_function('(cid)=>window.XGentConversations.current===cid', arg=search_seed['active'])
    page.locator('#log [data-record-id="' + search_seed['anchor'].split(':')[1] + '"]').wait_for()
    assert page.locator('#input').input_value() == ''  # Drafts stay scoped, never copied to the target.
    page.evaluate('window.XGentConversations.select("global_memory")')
    assert page.locator('#input').input_value() == '搜索期间不能丢的草稿'
    page.close()


def test_archive_search_is_read_only_and_can_browse_without_restoring(browser_context, workspace_url, search_seed):
    page = browser_context.new_page(); ready(page, workspace_url['url'])
    search = open_dialog(page); search.fill('仅在正文命中的独有词')
    page.get_by_role('combobox', name='搜索对话范围').select_option('archived')
    page.locator('#global-search-dialog .global-search-hit').first.wait_for()
    assert page.locator('#global-search-dialog .global-search-hit').count() == 1
    page.locator('#global-search-dialog .global-search-hit').click()
    page.locator('#source-reader').wait_for()
    assert '归档 AI 回复' in page.locator('#source-reader').inner_text()
    assert page.locator('#input').is_hidden()
    sessions = browser_context.request.get(workspace_url['url'] + '/api/workbench/conversations').json()
    assert next(s for s in sessions['items'] if s['id'] == search_seed['archived'])['archived']
    assert page.evaluate('window.XGentConversations.current') == 'global_memory'
    page.close()


def test_dialog_paging_filters_and_no_html_injection(browser_context, workspace_url, search_seed):
    page = browser_context.new_page(); ready(page, workspace_url['url'])
    search = open_dialog(page); search.fill('搜索分页验收')
    page.wait_for_function('document.querySelectorAll("#global-search-dialog .global-search-hit").length===30')
    page.get_by_role('button', name='加载更多结果', exact=True).click()
    page.wait_for_function('document.querySelectorAll("#global-search-dialog .global-search-hit").length===60')
    page.get_by_role('button', name='加载更多结果', exact=True).click()
    page.wait_for_function('document.querySelectorAll("#global-search-dialog .global-search-hit").length===65')
    keys = page.locator('#global-search-dialog .global-search-hit').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.searchKey)')
    assert len(set(keys)) == 65
    page.get_by_role('combobox', name='搜索内容类型').select_option('tool')
    page.wait_for_function('document.querySelectorAll("#global-search-dialog .global-search-hit").length===1')
    assert '工具结果' in page.locator('#global-search-dialog').inner_text()
    page.get_by_role('combobox', name='搜索内容类型').select_option('all')
    search.fill('<img src=x')
    page.locator('#global-search-dialog .global-search-hit').first.wait_for()
    assert page.locator('#global-search-dialog img').count() == 0
    assert page.evaluate('window.searchXss') is None
    assert '<img src=x' in page.locator('#global-search-dialog .global-search-hit').inner_text()
    page.keyboard.press('Escape'); page.locator('#global-search-dialog').wait_for(state='detached')
    page.close()


def test_search_error_retry_and_fast_input_do_not_show_old_response(browser_context, workspace_url):
    page = browser_context.new_page(); ready(page, workspace_url['url'])
    search = open_dialog(page)
    page.route('**/api/workbench/global-search?*', lambda route: route.fulfill(status=503, json={'error':'搜索暂时不可用'}))
    search.fill('历史记录 1234：')
    page.get_by_role('alert').filter(has_text='搜索暂时不可用').wait_for()
    assert search.input_value() == '历史记录 1234：'
    page.unroute('**/api/workbench/global-search?*')
    page.get_by_role('button', name='重试搜索', exact=True).click()
    page.locator('#global-search-dialog .global-search-hit').first.wait_for()
    held = []
    page.route('**/api/workbench/global-search?*', lambda route: held.append(route) if 'old-query' in route.request.url else route.continue_())
    search.fill('old-query')
    page.wait_for_function('document.querySelector("#global-search-dialog [aria-busy=true]")!==null')
    page.wait_for_timeout(350)
    search.fill('历史记录 4321：')
    page.locator('#global-search-dialog .global-search-hit').filter(has_text='历史记录 4321：').wait_for()
    for route in held:
        # The browser may already have discarded the aborted request.
        with suppress(pytest.importorskip('playwright.sync_api').Error):
            route.fulfill(json={'items':[], 'next_cursor':None})
    assert '历史记录 4321：' in page.locator('#global-search-dialog .global-search-hit').inner_text()
    page.close()


def test_mobile_keyboard_search_and_dismiss_keep_draft(browser_context, workspace_url, tmp_path):
    page = browser_context.new_page(); page.set_viewport_size({'width':390,'height':844})
    ready(page, workspace_url['url']); page.locator('#input').fill('保留手机输入')
    search = open_dialog(page); search.fill('历史记录 1234：')
    page.locator('#global-search-dialog .global-search-hit').first.wait_for()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    assert page.locator('#global-search-dialog').evaluate('(node)=>node.scrollWidth<=node.clientWidth+1')
    page.screenshot(path=str(tmp_path / 'global-search-mobile.png'))
    search.press('ArrowDown')
    assert page.locator('#global-search-dialog .global-search-hit').first.evaluate('(node)=>document.activeElement===node')
    page.keyboard.press('Enter')
    page.wait_for_function('document.querySelector("#log").textContent.includes("历史记录 1234：")')
    assert page.locator('#input').input_value() == '保留手机输入'
    page.close()
