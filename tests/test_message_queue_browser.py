"""Queue HTTP contract and draft-preserving UI in real Chromium, loopback only."""
import copy
from pathlib import Path

import pytest

from tests.test_full_output_web import full_output_server  # noqa: F401
from tests.test_web_presentation import browser_session


@pytest.fixture
def queue_browser(full_output_server):
    playwright = pytest.importorskip('playwright.sync_api')
    server = full_output_server[0]
    state = {'items': {'A': [], 'B': []}, 'revision': 0, 'requests': {}, 'actions': {}, 'calls': []}
    async def queue_api(action, cid, data):
        state['calls'].append((action, cid, data))
        if action == 'list':
            return {'conversation_id': cid, 'generation': 7, 'revision': state['revision'],
                    'items': copy.deepcopy(state['items'][cid]), 'paused': False, 'reason': ''}
        item = next((i for i in state['items'][cid] if i['id'] == data['id']), None)
        key = data.get('request_id')
        if key in state['actions']:
            return state['actions'][key]
        if not item:
            raise ValueError('消息已接入本轮，无法撤回。')
        state['items'][cid].remove(item)
        state['revision'] += 1
        state['actions'][key] = {'ok': True, **item, 'status': 'edited' if action == 'edit' else 'cancelled'}
        return state['actions'][key]
    async def accept(text, request_id=None, generation=None, **kwargs):
        cid = kwargs.get('conversation_id', 'A')
        if request_id in state['requests']:
            return state['requests'][request_id]
        state['revision'] += 1
        item = {'id': f"{state['revision']:032x}", 'seq': state['revision'], 'conversation_id': cid, 'generation': 7,
                'revision': 1, 'origin': 'web', 'text': text, 'status': 'pending', 'reason': 'waiting'}
        state['items'][cid].append(item)
        state['requests'][request_id] = {'ok': True, 'queued': True, **item}
        return state['requests'][request_id]
    async def conversations():
        return {'current_chat_id': 'A'}
    server.config.queue_api, server.config.accept_text, server.config.read_conversations = queue_api, accept, conversations
    server.config.is_busy = lambda: True
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip('Chromium unavailable')
        browser, context, base = browser_session(server, p)
        page = context.new_page()
        page.emulate_media(reduced_motion='reduce')
        def history(route):
            from urllib.parse import parse_qs, urlsplit
            cid = parse_qs(urlsplit(route.request.url).query).get('conversation_id', ['A'])[0]
            route.fulfill(json={'conversation_id': cid, 'ui_generation': 7, 'busy': True,
                'messages': [{'id': i, 'role': 'assistant', 'msg_type': 'ai_reply', 'content': f'历史 {cid}-{i}', 'timestamp': i} for i in range(1, 51)],
                'live': server.outbox.snapshot(), 'next_cursor': None})
        page.route('**/api/workbench/history?*', history)
        try:
            yield server, page, context, base, state
        finally:
            browser.close()


def ready(page, base):
    page.goto(base)
    page.wait_for_function('!document.querySelector("#btn-send").disabled')


def enqueue(page, text):
    page.locator('#input').fill(text)
    page.locator('#btn-send').click()
    page.wait_for_function('document.querySelector("#input").value===""')


def test_busy_submit_cancel_edit_batch_preview_and_no_log_jump(queue_browser, tmp_path):
    server, page, _, base, state = queue_browser
    ready(page, base)
    assert page.locator('#btn-stop').is_visible()
    page.locator('#log').evaluate('(e)=>e.scrollTop=400')
    top = page.locator('#log').evaluate('(e)=>e.scrollTop')
    for i in range(4):
        enqueue(page, f'排队内容 {i}')
    assert len(state['items']['A']) == 4
    assert page.locator('.queue-item').count() == 3
    assert page.locator('#log>.msg-row').count() == 50
    assert abs(page.locator('#log').evaluate('(e)=>e.scrollTop') - top) < 2
    page.get_by_role('button', name='展开全部', exact=True).click()
    assert page.locator('.queue-item').count() == 4
    page.locator('.queue-item').first.get_by_role('button', name='❌ 取消', exact=True).click()
    page.wait_for_function('document.querySelectorAll(".queue-item").length===3')
    assert page.locator('#input').input_value() == '排队内容 0'
    page.locator('#input').fill('已有草稿')
    page.locator('.queue-item').first.get_by_role('button', name='编辑', exact=True).click()
    page.wait_for_function('document.querySelector("#input").value==="排队内容 1\\n\\n已有草稿"')
    assert len(state['items']['A']) == 2
    for width, theme in ((1440, 'light'), (390, 'dark')):
        page.set_viewport_size({'width': width, 'height': 900})
        page.evaluate('(theme)=>document.body.classList.toggle("dark",theme==="dark")', theme)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path=str(tmp_path / f'queue-{width}-{theme}.png'))


@pytest.mark.parametrize('action,label', [('edit','编辑'),('cancel','❌ 取消')])
def test_recovery_after_switch_restores_original_draft_not_current_view(queue_browser, action, label):
    _, page, _, base, state = queue_browser
    held = []
    ready(page, base)
    enqueue(page, '待取回的 A 消息')
    page.locator('#input').fill('A 草稿')
    page.route('**/api/queue/'+action, lambda route: held.append(route))
    page.locator('.queue-item').first.get_by_role('button', name=label, exact=True).click()
    page.wait_for_timeout(40)
    assert held
    page.evaluate('window.dispatchEvent(new CustomEvent("xgent-conversation-changed",{detail:{conversation_id:"B"}}))')
    page.locator('#input').fill('B 草稿')
    item = state['items']['A'].pop()
    held[0].fulfill(json={'ok': True, **item, 'status': 'edited'})
    page.wait_for_timeout(100)
    assert page.locator('#input').input_value() == 'B 草稿'
    page.evaluate('window.dispatchEvent(new CustomEvent("xgent-conversation-changed",{detail:{conversation_id:"A"}}))')
    assert page.locator('#input').input_value() == '待取回的 A 消息\n\nA 草稿'


def test_lost_submit_response_uses_same_request_id_and_attachment_not_partially_sent(queue_browser):
    _, page, _, base, state = queue_browser
    requests = []
    def submit(route):
        requests.append(route.request.post_data_json)
        if len(requests) == 1:
            # Commit server-side, then lose just the client response.
            route.fetch()
            route.abort()
        else:
            route.continue_()
    page.route('**/api/chat', submit)
    ready(page, base)
    page.locator('#input').fill('只入队一次')
    page.locator('#btn-send').click()
    page.wait_for_function('!document.querySelector("#btn-send").disabled')
    assert page.locator('#input').input_value() == '只入队一次'
    page.locator('#btn-send').click()
    page.wait_for_function('document.querySelector("#input").value===""')
    assert requests[0]['request_id'] == requests[1]['request_id']
    assert len(state['items']['A']) == 1
    page.locator('#file-input').set_input_files({'name': 'fixture.txt', 'mimeType': 'text/plain', 'buffer': b'fixture'})
    page.locator('#input').fill('带附件的文字不能单独入队')
    assert page.locator('#btn-send').is_disabled()
    assert len(requests) == 2


def test_queue_api_auth_origin_validation_and_refresh(queue_browser):
    _, page, context, base, state = queue_browser
    ready(page, base)
    enqueue(page, '刷新后仍在')
    page.reload()
    page.locator('.queue-item').wait_for()
    assert page.locator('.queue-preview p').inner_text() == '刷新后仍在'
    assert context.request.post(base + '/api/queue/edit', data={'conversation_id': 'A', 'request_id': '/invalid'}).status == 400
    assert context.request.post(base + '/api/queue/cancel', headers={'Origin': 'https://untrusted.invalid'}, data={'conversation_id': 'A'}).status == 403
    context.clear_cookies()
    assert context.request.get(base + '/api/queue?conversation_id=A').status == 401


@pytest.mark.parametrize('action,label', [('edit','编辑'),('cancel','❌ 取消')])
def test_lost_recovery_response_remains_recoverable_after_refresh_without_duplicate_draft(queue_browser, action, label):
    server, page, _, base, state = queue_browser
    requests = []
    def edit(route):
        requests.append(route.request.post_data_json)
        if len(requests) == 1:
            route.fetch()
            route.abort()
        else:
            route.continue_()
    page.route('**/api/queue/'+action, edit)
    ready(page, base)
    enqueue(page, 'must recover exact text')
    page.locator('#input').fill('keep draft')
    page.locator('.queue-item').get_by_role('button', name=label, exact=True).click()
    page.wait_for_timeout(100)
    server.outbox.put({'type': 'queue_state', 'revision': state['revision']})
    page.get_by_role('button', name='重试取回', exact=True).wait_for()
    page.reload()
    page.get_by_role('button', name='重试取回', exact=True).click()
    page.wait_for_function('document.querySelector("#input").value==="must recover exact text\\n\\nkeep draft"')
    assert requests[0]['request_id'] == requests[1]['request_id']
    page.reload()
    page.wait_for_function('!document.querySelector("#btn-send").disabled')
    assert page.locator('#input').input_value() == 'must recover exact text\n\nkeep draft'
