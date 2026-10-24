"""Browser selection modes, multipart batching and idempotent retry."""
import io
import json
from pathlib import Path

import pytest

from tests.test_workbench_browser import browser_context, workspace_url, ready  # noqa: F401
from xgent_app.file_ingress import read_multipart


@pytest.mark.parametrize('width', [1440, 390])
def test_attachment_and_upload_selection_share_one_batch(browser_context, workspace_url, tmp_path, width):
    page = browser_context.new_page()
    page.set_viewport_size({'width': width, 'height': 900})
    calls = []
    def capture(route):
        request = route.request
        raw = request.post_data_buffer
        data = read_multipart(io.BytesIO(raw), len(raw), request.headers['content-type'], tmp_path)
        try:
            calls.append({'files': [item['filename'] for item in data['files']],
                          'modes': json.loads(data['modes']), 'key': data['idempotency_key']})
        finally:
            data['_temporary'].cleanup()
        if len(calls) == 1:
            route.fulfill(status=503, json={'error': 'retry fixture'})
        else:
            route.fulfill(json={'ok': True, 'duplicate': True})
    page.route('**/api/attachments', capture)
    ready(page, workspace_url['url'])
    page.locator('#file-input').set_input_files([
        {'name':'one.txt', 'mimeType':'text/plain', 'buffer':b'one'},
        {'name':'two.zip', 'mimeType':'application/zip', 'buffer':b'PK\x03\x04binary'},
    ])
    page.locator('#server-upload-input').set_input_files([
        {'name':'only-path.pdf', 'mimeType':'application/pdf', 'buffer':b'%PDF-test'}])
    assert page.locator('.pending-file').count() == 3
    assert '上传：' in page.locator('.pending-file').last.inner_text()
    page.locator('#input').fill('one batch with mixed modes')
    page.locator('#btn-send').click()
    page.wait_for_function("document.querySelectorAll('.pending-file').length===3 && !document.getElementById('btn-send').disabled")
    page.locator('#btn-send').click()
    page.wait_for_function("document.querySelectorAll('.pending-file').length===0 && !document.getElementById('btn-send').disabled")
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0]['files'] == ['one.txt', 'two.zip', 'only-path.pdf']
    assert calls[0]['modes'] == ['attachment', 'attachment', 'server_upload']
    assert page.locator('#btn-server-upload').is_visible()
    assert page.evaluate("document.getElementById('composer').scrollWidth <= document.getElementById('composer').clientWidth + 1")


def test_server_upload_button_uses_path_only_endpoint(browser_context, workspace_url):
    page = browser_context.new_page()
    calls = []
    page.route('**/api/server-uploads', lambda route: (calls.append(route.request.url), route.fulfill(json={'ok':True,'duplicate':True})))
    ready(page, workspace_url['url'])
    with page.expect_file_chooser() as selected:
        page.get_by_role('button', name='上传到服务器', exact=True).click()
    selected.value.set_files([{'name':'photo.png','mimeType':'image/png','buffer':b'not decoded by upload mode'}])
    page.locator('#btn-send').click()
    page.wait_for_function("document.querySelectorAll('.pending-file').length===0 && !document.getElementById('btn-send').disabled")
    assert len(calls) == 1
