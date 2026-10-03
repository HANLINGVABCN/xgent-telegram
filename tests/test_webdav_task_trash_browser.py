"""Reproduce the reported delete -> empty trash -> reload sequence in a browser."""
from pathlib import Path
import random
import threading

import pytest

from tests.test_webdav_task_trash import app, seed


@pytest.fixture
def site(app):
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).is_file():
            pytest.skip('Playwright Chromium is not installed')
        task, target = seed(app)
        app.AUTH_CRED = 'test:temporary-password'
        app.FileManagerHandler.log_message = lambda *args: None
        server = None
        for _ in range(20):
            try:
                server = app.ThreadedHTTPServer(('127.0.0.1', random.SystemRandom().randrange(20000, 40000)), app.FileManagerHandler)
                break
            except OSError:
                continue
        assert server is not None
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                page = login(context, base)
                yield app, browser, page, base, task, target
            finally:
                browser.close()
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=3)


def login(context, base):
    context.route('**/*', lambda route: route.continue_()
                  if route.request.url.startswith(base + '/') else route.abort())
    page = context.new_page()
    page.goto(base)
    page.locator('#loginUser').fill('test')
    page.locator('#loginPass').fill('temporary-password')
    page.locator('#loginSubmit').click()
    page.wait_for_function("!document.getElementById('loginScreen').classList.contains('show')")
    page.locator('#appContainer').wait_for(state='visible')
    return page


def open_tasks(page, trash=False):
    page.locator('#appContainer .toolbar .transfer-btn').click()
    page.locator('#transferTabRemote').click()
    if trash:
        page.evaluate("setTransferFilter('trash');")


def delete_row(page, tid):
    open_tasks(page)
    with page.expect_response(lambda r: r.url.endswith('/api/tasks-delete') and r.request.method == 'POST') as response:
        page.locator(f'[data-download-delete="{tid}"]').click()
    assert response.value.status == 200
    page.wait_for_function('(id) => downloadTasks.some(t => t.id === id && t.trashed)', arg=tid)


def clear_rows(page, expected_status=200):
    page.evaluate("setTransferFilter('trash');")
    page.locator('#clearTrashBtn').click()
    with page.expect_response(lambda r: r.url.endswith('/api/tasks-delete') and r.request.method == 'POST') as response:
        page.locator('#confirmOk').click()
    assert response.value.status == expected_status


def test_delete_clear_reload_and_fresh_session_stay_empty(site):
    app, browser, page, base, task, target = site
    tid = task['id']
    delete_row(page, tid)
    assert tid not in app.load_persisted_tasks()
    assert tid in app.load_state()['taskTrash']
    page.reload()
    open_tasks(page, trash=True)
    page.locator(f'[data-trash-purge="{tid}"]').wait_for()
    clear_rows(page)
    page.wait_for_function('(id) => !downloadTasks.some(t => t.id === id)', arg=tid)
    assert tid not in app.load_state()['taskTrash']
    assert tid not in app.load_persisted_tasks()
    app.save_task(task)  # late progress from a worker that started before deletion
    for _ in range(2):
        page.reload()
        page.evaluate('async () => {await loadAllTasks(); await loadTrashedTasks();}')
        assert not page.evaluate('(id) => downloadTasks.some(t => t.id === id)', tid)
    fresh = browser.new_context()
    try:
        other = login(fresh, base)
        other.evaluate('async () => {await loadAllTasks(); await loadTrashedTasks();}')
        assert not other.evaluate('(id) => downloadTasks.some(t => t.id === id)', tid)
    finally:
        fresh.close()
    assert target.read_bytes() == b'keep finished file'


def test_restore_survives_reload(site):
    app, _, page, _, task, _ = site
    tid = task['id']
    delete_row(page, tid)
    page.evaluate("setTransferFilter('trash');")
    with page.expect_response(lambda r: r.url.endswith('/api/tasks-restore')) as response:
        page.locator(f'[data-trash-restore="{tid}"]').click()
    assert response.value.status == 200
    page.wait_for_function('(id) => downloadTasks.some(t => t.id === id && !t.trashed)', arg=tid)
    page.reload()
    page.evaluate('async () => {await loadAllTasks(); await loadTrashedTasks();}')
    assert page.evaluate('(id) => downloadTasks.some(t => t.id === id && !t.trashed)', tid)
    assert tid in app.load_persisted_tasks()


def test_failed_clear_keeps_trash_and_does_not_report_success(site):
    app, _, page, _, task, _ = site
    tid = task['id']
    delete_row(page, tid)
    page.route('**/api/tasks-delete', lambda route: route.fulfill(
        status=503, content_type='application/json', body='{"error":"offline-test"}'))
    clear_rows(page, expected_status=503)
    page.locator('.toast-error').filter(has_text='offline-test').wait_for()
    assert page.evaluate('(id) => downloadTasks.some(t => t.id === id && t.trashed)', tid)
    assert tid in app.load_state()['taskTrash']
    page.reload()
    page.evaluate('async () => {await loadAllTasks(); await loadTrashedTasks();}')
    assert page.evaluate('(id) => downloadTasks.some(t => t.id === id && t.trashed)', tid)
