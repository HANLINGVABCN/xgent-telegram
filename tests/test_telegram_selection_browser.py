"""Bot badge follows persisted selection without navigating the current Web view."""
import json
import sqlite3
from pathlib import Path

from tests.test_workbench_browser import workspace_url, browser_context, ready


def select_bot(database, conversation_id):
    # An isolated fixture DB; emulate another process, not a new public Web API.
    with sqlite3.connect(database) as conn:
        conn.execute('UPDATE config SET value=? WHERE key=?', (json.dumps(conversation_id), 'telegram_conversation_id'))
        revision = json.loads(conn.execute("SELECT value FROM config WHERE key='conversation_revision'").fetchone()[0])
        conn.execute("UPDATE config SET value=? WHERE key='conversation_revision'", (json.dumps(revision + 1),))


def test_bot_badge_preserves_view_draft_order_and_small_screen(browser_context, workspace_url):
    url = workspace_url['url']
    a = browser_context.request.post(url+'/api/workbench/conversations/create', data={'name':'Bot 的常驻会话'}).json()['current_chat_id']
    b = browser_context.request.post(url+'/api/workbench/conversations/create', data={'name':'网页查看的另一个会话'}).json()['current_chat_id']
    page = browser_context.new_page()
    ready(page, url)
    page.wait_for_function('(id)=>window.XGentConversations.current===id', arg=b)
    page.locator('#input').fill('Bot 变化不能清掉这个网页草稿')
    rows = page.locator('.conv-row').evaluate_all('(rows)=>rows.map(row=>row.dataset.conversationId)')
    history = []
    page.on('request', lambda request: history.append(request.url) if '/api/workbench/history?' in request.url else None)
    select_bot(workspace_url['database_path'], a)
    badge = page.locator('.conv-row[data-conversation-id="'+a+'"] .conv-bot-badge')
    badge.wait_for(state='visible')
    assert page.evaluate('window.XGentConversations.current') == b
    assert page.locator('#input').input_value() == 'Bot 变化不能清掉这个网页草稿'
    assert page.locator('.conv-row').evaluate_all('(rows)=>rows.map(row=>row.dataset.conversationId)') == rows
    assert not history
    output = Path(__file__).resolve().parents[1]/'workspace/bot-independent-qa/screenshots'
    output.mkdir(parents=True, exist_ok=True)
    for width, height in [(1440,900),(390,844),(360,640)]:
        page.set_viewport_size({'width':width,'height':height})
        if width < 768 and not page.locator('#wb-nav').is_visible():
            page.get_by_role('button',name='对话记录',exact=True).click()
        badge.wait_for(state='visible')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        box = badge.bounding_box()
        assert box and box['width']>0 and box['x']+box['width']<=width
        page.screenshot(path=str(output/f'{width}-bot-badge.png'))
    page.evaluate("document.documentElement.style.setProperty('--web-ui-font-size','20px')")
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    select_bot(workspace_url['database_path'], None)
    badge.wait_for(state='hidden')
    assert page.evaluate('window.XGentConversations.current') == b
    assert page.locator('#input').input_value() == 'Bot 变化不能清掉这个网页草稿'
    page.close()
