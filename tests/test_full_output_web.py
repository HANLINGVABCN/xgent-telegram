"""Loopback-only HTTP/browser coverage for full folds and bounded output pages."""
import asyncio
import http.client
import json
from pathlib import Path
import random
import threading

import pytest

from xgent_app.web_auth import hash_password
from xgent_app.web_server import WebChatConfig, WebChatServer
from xgent_app.protocols import ProtocolParser
from xgent_app.agent_presenter import build_run_presentation
from xgent_app.output_archive import OUTPUT_PAGE_BYTES


@pytest.fixture
def full_output_server(tmp_path):
    storage = tmp_path / 'xgent_storage'
    output = storage / 'command_outputs'
    output.mkdir(parents=True)
    archive = output / 'run.txt'
    archive.write_text('PAGE_ONE\n' + '中' * 30000 + '\nPAGE_LAST <script>window.hacked=1</script>', encoding='utf-8')
    body = '\n'.join(f'line{i:03d}: <tag> & 中文' for i in range(120)) + '\n<script>window.hacked=1</script>'
    raw = '```run-x\n<<BEGIN_WEB123456\n' + body + '\n<<END_WEB123456\n```'
    canonical = ProtocolParser.render_folded_html(raw, raw_copy=True)
    records = [{'id': 1, 'role': 'assistant', 'content': canonical, 'parse_mode': 'HTML', 'timestamp': 1},
               {'id': 2, 'role': 'assistant', 'content': build_run_presentation({
                   'success': True, 'return_code': 0, 'output': 'bounded summary', 'output_path': str(archive)}),
                'parse_mode': 'HTML', 'timestamp': 2}]
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    async def history(limit): return records
    async def settings(*args): return {'values': {}, 'options': {}}
    config = WebChatConfig(host='127.0.0.1', port=0, password_hash=hash_password('temporary-test-only'),
        bot_token='', authorized_user_id=1, loop=loop, submit_message=lambda *a: None,
        read_history=history, read_settings=settings, write_setting=settings,
        request_stop=lambda: None, is_busy=lambda: False, media_allowed_roots=[str(storage)])
    server = WebChatServer(config)
    try:
        for _ in range(20):
            config.port = random.SystemRandom().randrange(20000, 40000)
            try: server.start(); break
            except OSError: continue
        else: raise AssertionError('No test port available')
        yield server, archive, body, canonical
    finally:
        server.stop()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


def request(server, path, data, cookie='', origin=None):
    headers = {'Content-Type': 'application/json', 'Cookie': cookie}
    if origin: headers['Origin'] = origin
    conn = http.client.HTTPConnection('127.0.0.1', server.config.port, timeout=4)
    try:
        conn.request('POST', path, json.dumps(data), headers)
        resp = conn.getresponse()
        return resp.status, resp.getheader('Set-Cookie'), json.loads(resp.read())
    finally: conn.close()


def test_output_endpoint_requires_auth_origin_and_output_directory(full_output_server):
    server, archive, _, _ = full_output_server
    payload = {'path': str(archive), 'offset': 0}
    assert request(server, '/api/output/page', payload)[0] == 401
    status, cookie, _ = request(server, '/api/login', {'password': 'temporary-test-only'})
    assert status == 200
    cookie = cookie.split(';', 1)[0]
    assert request(server, '/api/output/page', payload, cookie, 'https://evil.invalid')[0] == 403
    status, _, first = request(server, '/api/output/page', payload, cookie)
    assert status == 200 and 'PAGE_ONE' in first['text'] and 'PAGE_LAST' not in first['text']
    assert len(first['text'].encode()) <= OUTPUT_PAGE_BYTES
    status, _, last = request(server, '/api/output/page', {'path': str(archive), 'offset': first['next_offset']}, cookie)
    assert status == 200 and last['eof'] and 'PAGE_LAST' in last['text']
    assert first['text'] + last['text'] == archive.read_bytes().decode('utf-8')
    outside = archive.parent.parent / 'secret.txt'; outside.write_text('PRIVATE', encoding='utf-8')
    for data in ({'path': str(outside)}, {'path': str(archive), 'offset': -1},
                 {'path': str(archive), 'offset': True}, {'path': str(archive), 'offset': '0'}):
        status, _, result = request(server, '/api/output/page', data, cookie)
        assert status == 400 and 'PRIVATE' not in json.dumps(result)
    server.config.is_web_enabled = lambda: False
    assert request(server, '/api/output/page', payload, cookie)[0] == 403


def test_browser_full_expand_copy_live_restore_and_archive_paging(full_output_server):
    playwright = pytest.importorskip('playwright.sync_api')
    server, archive, body, canonical = full_output_server
    base = f'http://127.0.0.1:{server.config.port}'
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).is_file():
            pytest.skip('Playwright Chromium not installed')
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(permissions=['clipboard-read', 'clipboard-write'])
            context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
            assert context.request.post(base + '/api/login', data={'password': 'temporary-test-only'}).ok
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            page.goto(base)
            block = page.locator('blockquote.pb').first
            block.wait_for()
            assert 'line119' not in block.locator('pre').inner_text()
            block.locator('.pb-title').click()
            page.wait_for_function("document.querySelector('blockquote.pb').classList.contains('expanded')")
            assert block.locator('pre').text_content() == body
            block.locator('.btn-copy').click()
            page.wait_for_function('navigator.clipboard.readText().then(t => t.includes("line119"))')
            assert page.evaluate('navigator.clipboard.readText()').replace('\r\n', '\n') == body
            assert page.evaluate('window.hacked') is None
            # Collapse again keeps full source for copy without retaining a giant highlighted DOM.
            block.locator('.pb-title').click()
            page.wait_for_function("!document.querySelector('blockquote.pb').classList.contains('expanded')")
            assert 'line119' not in block.locator('pre').inner_text()
            block.locator('.btn-copy').click()
            assert page.evaluate('navigator.clipboard.readText()').replace('\r\n', '\n') == body
            page.reload()
            block = page.locator('blockquote.pb').first
            block.locator('.pb-title').click()
            page.wait_for_function("document.querySelector('blockquote.pb').classList.contains('expanded')")
            assert block.locator('pre').text_content() == body
            # Live frames must carry the same full body, not the adapted Telegram excerpt.
            server.outbox.put({'type': 'message', 'message_id': 456, 'text': canonical, 'parse_mode': 'HTML'})
            live = page.locator('blockquote.pb').nth(2)
            live.wait_for(timeout=15000)
            live.locator('.pb-title').click()
            page.wait_for_function("document.querySelectorAll('blockquote.pb')[2].classList.contains('expanded')")
            assert live.locator('pre').text_content() == body
            result = page.locator('blockquote.pb[data-output-path]').first
            result.locator('.btn-output').click()
            viewer = result.locator('.output-page-text')
            viewer.wait_for()
            assert 'PAGE_ONE' in viewer.text_content() and 'PAGE_LAST' not in viewer.text_content()
            result.locator('.btn-output-next').click()
            page.wait_for_function("document.querySelector('.output-page-text').textContent.includes('PAGE_LAST')")
            assert 'PAGE_ONE' not in viewer.text_content()
            assert result.locator('.btn-output-next').is_disabled()
            result.locator('.btn-output-copy').click()
            page.wait_for_function('navigator.clipboard.readText().then(t => t.includes("PAGE_LAST"))')
            assert 'PAGE_ONE' not in page.evaluate('navigator.clipboard.readText()')
            assert page.evaluate('window.hacked') is None
            result.locator('.btn-output-prev').click()
            page.wait_for_function("document.querySelector('.output-page-text').textContent.includes('PAGE_ONE')")
            assert len(viewer.text_content().encode()) <= OUTPUT_PAGE_BYTES
            result.locator('.btn-output-close').click()
            assert result.locator('.output-pages').count() == 0
            # Narrow viewport remains horizontally contained while archive controls wrap.
            page.set_viewport_size({'width': 390, 'height': 844})
            result.locator('.btn-output').click()
            result.locator('.output-page-text').wait_for()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
            assert not errors
        finally: browser.close()
