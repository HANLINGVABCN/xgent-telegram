"""Optional real-browser smoke test (uses only an isolated loopback service)."""
from pathlib import Path
import random
import threading

import pytest

from tests.test_webdav_uploads import app


def test_browser_login_upload_resume_and_safe_breadcrumb(app):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).is_file():
            pytest.skip("Playwright Chromium is not installed")
        # Some Windows ephemeral ports (e.g. 6566) are on Chromium's unsafe-port
        # list. Bind a high test port rather than disabling browser protections.
        server = None
        for _ in range(20):
            try:
                port = random.SystemRandom().randrange(20000, 40000)
                server = app.ThreadedHTTPServer(('127.0.0.1', port), app.FileManagerHandler)
                break
            except OSError:
                continue
        assert server is not None, "No free loopback test port"
        app.AUTH_CRED = 'smoke:temporary-only-password'
        app.FileManagerHandler.log_message = lambda *args: None
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                page = context.new_page()
                page.route('**/*', lambda route: route.continue_()
                           if route.request.url.startswith(base + '/') else route.abort())
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(base)
                page.locator('#loginUser').fill('smoke')
                page.locator('#loginPass').fill('temporary-only-password')
                page.locator('#loginSubmit').click()
                page.wait_for_function("!document.getElementById('loginScreen').classList.contains('show')")
                breadcrumb = page.evaluate('''() => {
                    const old = curPath;
                    curPath = '/audit" onpointerover="window.__auditMarker=1" data-padding="';
                    renderBreadcrumb();
                    const item = document.querySelector('#breadcrumb button:last-child');
                    item.dispatchEvent(new Event('pointerover'));
                    const result = {pathPreserved: item.getAttribute('data-nav-path') === curPath,
                        injectedHandler: item.hasAttribute('onpointerover'), markerRan: window.__auditMarker === 1};
                    curPath = old; renderBreadcrumb(); return result;
                }''')
                assert breadcrumb == {'pathPreserved': True, 'injectedHandler': False, 'markerRan': False}
                result = page.evaluate('''async () => {
                    const task = addUploadTask(new File([], 'empty-browser.bin'), 'browser-empty');
                    await startUploadTask(task); return {status: task.status, error: task.error};
                }''')
                assert result['status'] == 'done', result
                root = Path(app.ROOT_DIR)
                assert (root / 'empty-browser.bin').read_bytes() == b''
                tid = context.request.post(base + '/api/upload-init',
                    data={'path': '/', 'name': 'resumable.bin', 'size': 4}).json()['taskId']
                assert context.request.post(base + '/api/upload-chunk?taskId=' + tid + '&offset=0', data=b'ab').status == 200
                app.UPLOAD_TASKS.clear()
                page.reload()
                page.wait_for_function('(id) => downloadTasks.some(t => t.serverTaskId === id)', arg=tid)
                restored = page.evaluate('''(id) => {
                    const t = downloadTasks.find(t => t.serverTaskId === id);
                    return {status: t.status, needsFile: t.needsFile};
                }''', tid)
                assert restored == {'status': 'paused', 'needsFile': True}
                page.evaluate("() => {addUploadTask(new File(['abcd'], 'after-refresh.bin'), 'browser-after'); processDownloadQueue();}")
                page.wait_for_function("downloadTasks.some(t => t.name === 'after-refresh.bin' && t.status === 'done')")
                assert (root / 'after-refresh.bin').read_bytes() == b'abcd'
                assert not errors, errors
            finally:
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
