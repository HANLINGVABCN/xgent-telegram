"""Slow paging, cancellation, visible edits and scroll anchors in real Chromium."""
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_full_output_web import full_output_server  # noqa: F401
from tests.test_web_presentation import browser_session


@pytest.fixture
def history_browser(full_output_server):
    playwright = pytest.importorskip('playwright.sync_api')
    server = full_output_server[0]
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip('Chromium unavailable')
        browser, context, base = browser_session(server, p)
        page = context.new_page()
        page.emulate_media(reduced_motion='reduce')
        try:
            yield server, page, base
        finally:
            browser.close()


def record(i, cid='A'):
    return {'id': i, 'role': 'assistant', 'msg_type': 'ai_reply',
            'content': f'历史 {cid}-{i}\n正文第二行', 'history_cursor': str(i), 'timestamp': i}


def page_data(server, start, stop, *, cid='A', next_cursor=None, baseline=False, menu=False):
    rows = [record(i, cid) for i in range(start, stop)]
    if menu:
        rows[3] = {'ui_message_id': 'current-menu', 'revision': 1, 'ui_generation': 7,
                   'history_cursor': str(start + 3), 'role': 'assistant', 'msg_type': 'ui_message',
                   'content': '原卡片内容', 'reply_markup': [], 'timestamp': start + 3}
    return {'messages': rows, 'next_cursor': next_cursor, 'conversation_id': cid,
            'ui_generation': 7, 'busy': False, **({'live': server.outbox.snapshot()} if baseline else {})}


def ready(page, base):
    page.goto(base)
    page.wait_for_function('!document.getElementById("btn-send").disabled')


def remember_anchor(page):
    return page.evaluate('''()=>{
      const log=document.getElementById('log'),top=log.getBoundingClientRect().top;
      const row=Array.from(log.children).find(r=>r.getBoundingClientRect().bottom>top);
      return {id:row.dataset.recordId,top:row.getBoundingClientRect().top};
    }''')


def assert_anchor(page, anchor):
    top = page.locator('[data-record-id="' + anchor['id'] + '"]').bounding_box()['y']
    assert abs(top - anchor['top']) < 2


def test_prefetch_before_top_single_flight_current_anchor_and_live_updates(history_browser):
    server, page, base = history_browser
    held, requests = [], []
    def route(r):
        q = parse_qs(urlsplit(r.request.url).query)
        if 'before' in q:
            requests.append(q); held.append(r)
        else:
            r.fulfill(json=page_data(server, 301, 351, next_cursor='301', baseline=True))
    page.route('**/api/workbench/history?*', route)
    ready(page, base)
    page.locator('#log').evaluate('(e)=>e.scrollTop=Math.max(800,e.clientHeight*1.5)-20')
    page.wait_for_function('document.querySelector("#wb-earlier").disabled')
    assert len(held) == 1
    assert page.locator('#log').evaluate('(e)=>e.scrollTop') > 0
    # The reader keeps moving while the first page is slow. Do not use the
    # request-start anchor and do not hide live messages just because prefetch ran.
    page.locator('#log').evaluate('(e)=>e.scrollTop=310')
    anchor = remember_anchor(page)
    server.outbox.put({'type': 'message', 'conversation_id': 'A', 'message_id': 9001, 'text': '实时仍更新'})
    page.get_by_text('实时仍更新', exact=True).wait_for()
    assert not page.locator('#wb-chat').evaluate('(e)=>e.classList.contains("history-past")')
    assert_anchor(page, anchor)
    page.wait_for_timeout(120)
    assert len(held) == 1, 'scroll events must not start concurrent pages'
    held.pop().fulfill(json=page_data(server, 251, 301, next_cursor='251'))
    page.locator('[data-record-id="251"]').wait_for()
    assert_anchor(page, anchor)
    assert len(requests) == 1


def test_failed_page_waits_for_explicit_retry_and_stops_at_earliest(history_browser):
    server, page, base = history_browser
    requests = []
    def route(r):
        q = parse_qs(urlsplit(r.request.url).query)
        if 'before' not in q:
            r.fulfill(json=page_data(server, 51, 101, next_cursor='51', baseline=True))
        else:
            requests.append(q)
            if len(requests) == 1:
                r.fulfill(status=503, json={'error': 'fixture slow network'})
            else:
                r.fulfill(json=page_data(server, 1, 51))
    page.route('**/api/workbench/history?*', route)
    ready(page, base)
    page.locator('#log').evaluate('(e)=>e.scrollTop=300')
    page.get_by_role('button', name='重试加载历史', exact=True).wait_for()
    for y in (0, 140, 0, 210):
        page.locator('#log').evaluate('(e,y)=>e.scrollTop=y', y)
        page.wait_for_timeout(40)
    assert len(requests) == 1
    page.get_by_role('button', name='重试加载历史', exact=True).click()
    page.locator('[data-record-id="1"]').wait_for()
    for y in (0, 140, 0):
        page.locator('#log').evaluate('(e,y)=>e.scrollTop=y', y)
        page.wait_for_timeout(40)
    assert len(requests) == 2, 'no cursor means no more automatic or manual requests'


def test_switch_cancels_old_page_and_rejects_late_results(history_browser):
    server, page, base = history_browser
    held, aborted = [], []
    page.on('requestfailed', lambda request: aborted.append(request.url))
    def route(r):
        q = parse_qs(urlsplit(r.request.url).query)
        cid = q.get('conversation_id', ['A'])[0]
        if 'before' in q:
            held.append(r)
        else:
            r.fulfill(json=page_data(server, 301, 351, cid=cid,
                                    next_cursor='301' if cid == 'A' else None, baseline=True))
    page.route('**/api/workbench/history?*', route)
    ready(page, base)
    page.locator('#log').evaluate('(e)=>e.scrollTop=400')
    page.wait_for_function('document.querySelector("#wb-earlier").disabled')
    assert len(held) == 1
    page.evaluate('window.dispatchEvent(new CustomEvent("xgent-conversation-changed",{detail:{conversation_id:"B"}}))')
    page.wait_for_function('document.querySelector("#log").innerText.includes("历史 B-301")')
    held[0].fulfill(json=page_data(server, 251, 301, next_cursor='251'))
    page.wait_for_timeout(100)
    assert any('before=' in url for url in aborted)
    assert '历史 A-' not in page.locator('#log').inner_text()
    assert page.locator('#log>.msg-row').count() == 50


def test_mismatched_page_is_rejected_without_an_automatic_retry_loop(history_browser):
    server, page, base = history_browser
    requests = []
    def route(r):
        q = parse_qs(urlsplit(r.request.url).query)
        if 'before' in q:
            requests.append(q)
            r.fulfill(json=page_data(server, 1, 51, cid='B' if len(requests) == 1 else 'A'))
        else:
            r.fulfill(json=page_data(server, 51, 101, baseline=True, next_cursor='51'))
    page.route('**/api/workbench/history?*', route)
    ready(page, base)
    page.locator('#log').evaluate('(e)=>e.scrollTop=400')
    page.get_by_role('button', name='重试加载历史', exact=True).wait_for()
    page.locator('#log').evaluate('(e)=>e.scrollTop=0')
    page.wait_for_timeout(150)
    assert len(requests) == 1 and '历史 B-' not in page.locator('#log').inner_text()
    page.get_by_role('button', name='重试加载历史', exact=True).click()
    page.locator('[data-record-id="1"]').wait_for()
    assert len(requests) == 2


def test_reading_history_keeps_visible_card_edits_and_does_not_append_unseen_reply(history_browser):
    server, page, base = history_browser
    def route(r):
        q = parse_qs(urlsplit(r.request.url).query)
        anchored = 'anchor' in q
        r.fulfill(json=page_data(server, 101 if anchored else 301, 151 if anchored else 351,
                                baseline=True, menu=anchored))
    page.route('**/api/workbench/history?*', route)
    ready(page, base)
    page.evaluate('window.XGentChat.locate("history:110")')
    page.get_by_text('原卡片内容', exact=True).wait_for()
    assert page.locator('#wb-chat').evaluate('(e)=>e.classList.contains("history-past")')
    anchor = remember_anchor(page)
    server.outbox.put({'type': 'edit', 'conversation_id': 'A', 'ui_message_id': 'current-menu',
                      'revision': 2, 'ui_generation': 7, 'text': '卡片原地更新', 'reply_markup': []})
    server.outbox.put({'type': 'message', 'conversation_id': 'A', 'message_id': 9002, 'text': '不打断阅读的新回复'})
    server.outbox.put({'type': 'edit_markup', 'conversation_id': 'A', 'ui_message_id': 'outside-window-menu',
                      'revision': 2, 'ui_generation': 7, 'text': '窗口外卡片的更新', 'reply_markup': []})
    server.outbox.put({'type': 'notice', 'conversation_id': 'A', 'message_id': 9003, 'text': '窗口外的系统消息'})
    page.get_by_text('卡片原地更新', exact=True).wait_for()
    page.wait_for_function('document.querySelector("#wb-history-state").textContent.includes("新动态")')
    assert page.get_by_text('不打断阅读的新回复', exact=True).count() == 0
    assert page.get_by_text('窗口外卡片的更新', exact=True).count() == 0
    assert page.get_by_text('窗口外的系统消息', exact=True).count() == 0
    assert page.locator('[data-ui-message-id="current-menu"]').count() == 1
    assert_anchor(page, anchor)


def test_reset_received_during_baseline_load_starts_a_fresh_generation(history_browser):
    server, page, base = history_browser
    held, snapshots = [], []
    def route(r):
        data = page_data(server, 301 if len(snapshots) < 2 else 401,
                         351 if len(snapshots) < 2 else 451, baseline=True)
        if len(snapshots) >= 2:
            data['ui_generation'] = 8
        snapshots.append(data)
        if len(snapshots) == 2:
            held.append(r)
        else:
            r.fulfill(json=data)
    page.route('**/api/workbench/history?*', route)
    ready(page, base)
    page.evaluate('void window.XGentChat.reload()')
    page.wait_for_function('document.querySelector("#btn-send").disabled')
    assert len(held) == 1
    server.outbox.put({'type': 'context_reset', 'conversation_id': 'A', 'generation': 8})
    page.wait_for_timeout(150)
    held[0].fulfill(json=snapshots[1])
    page.locator('[data-record-id="401"]').wait_for(timeout=5000)
    assert len(snapshots) == 3
    assert page.locator('[data-record-id="301"]').count() == 0
    page.wait_for_function('!document.querySelector("#btn-send").disabled')


@pytest.mark.parametrize('action', ['locate', 'reset'])
def test_locate_or_reset_cancels_inflight_preload(history_browser, action):
    server, page, base = history_browser
    held, aborted = [], []
    changed = False
    page.on('requestfailed', lambda request: aborted.append(request.url))
    def route(r):
        q = parse_qs(urlsplit(r.request.url).query)
        if 'before' in q:
            held.append(r)
            return
        start = 51 if 'anchor' in q else 401 if changed else 301
        data = page_data(server, start, start + 50, baseline=True,
                         next_cursor=None if changed else '301')
        if changed and action == 'reset':
            data['ui_generation'] = 8
        r.fulfill(json=data)
    page.route('**/api/workbench/history?*', route)
    ready(page, base)
    page.locator('#log').evaluate('(e)=>e.scrollTop=400')
    page.wait_for_function('document.querySelector("#wb-earlier").disabled')
    assert len(held) == 1
    changed = True
    if action == 'locate':
        page.evaluate('window.XGentChat.locate("history:70")')
    else:
        server.outbox.put({'type': 'context_reset', 'conversation_id': 'A', 'generation': 8})
    page.locator('[data-record-id="' + ('51' if action == 'locate' else '401') + '"]').wait_for()
    held[0].fulfill(json=page_data(server, 251, 301, next_cursor='251'))
    page.wait_for_timeout(100)
    assert any('before=' in url for url in aborted)
    assert page.locator('[data-record-id="251"]').count() == 0


def test_bounded_history_forward_page_and_latest_restore_inflight_reply(history_browser):
    server, page, base = history_browser
    def route(r):
        q = parse_qs(urlsplit(r.request.url).query)
        if 'before' in q:
            end = int(q['before'][0])
            r.fulfill(json=page_data(server, end - 50, end, next_cursor=str(end - 50)))
        elif 'after' in q:
            start = int(q['after'][0]) + 1
            r.fulfill(json=page_data(server, start, 451))
        else:
            r.fulfill(json=page_data(server, 401, 451, baseline=True, next_cursor='401'))
    page.route('**/api/workbench/history?*', route)
    ready(page, base)
    for first_id in (351, 301, 251, 201):
        page.locator('#log').evaluate('(e)=>e.scrollTop=400')
        page.locator(f'[data-record-id="{first_id}"]').wait_for()
    assert page.locator('#log>.msg-row').count() <= 200
    assert page.locator('#wb-chat').evaluate('(e)=>e.classList.contains("history-past")')
    assert page.locator('[data-record-id="450"]').count() == 0
    anchor = remember_anchor(page)
    server.outbox.put({'type': 'message', 'conversation_id': 'A', 'message_id': 9010,
                      'text': '正在生成的新回复', 'run_id': 'current-run',
                      'reply_markup': [[{'text': '停止', 'callback_data': 'act_stop_generation:current-run'}]]})
    server.outbox.put({'type': 'edit', 'conversation_id': 'A', 'message_id': 9010,
                      'text': '正在生成的新回复，已追加内容', 'run_id': 'current-run'})
    page.wait_for_function('document.querySelector("#wb-history-state").textContent.includes("新动态")')
    assert_anchor(page, anchor)
    assert page.get_by_text('正在生成的新回复，已追加内容', exact=True).count() == 0
    page.locator('#log').evaluate('(e)=>e.scrollTop=e.scrollHeight')
    page.get_by_role('button', name='加载较新消息', exact=True).click()
    page.locator('[data-record-id="450"]').wait_for()
    assert page.locator('#log>.msg-row').count() <= 200
    assert page.locator('#wb-chat').evaluate('(e)=>e.classList.contains("history-past")')
    page.locator('#wb-latest').click()
    page.get_by_text('正在生成的新回复，已追加内容', exact=True).wait_for()
    assert page.locator('#log>.msg-row').count() == 51
    assert not page.locator('#wb-chat').evaluate('(e)=>e.classList.contains("history-past")')
