"""Preview bandwidth, mixed file modes and source placement in real Chromium."""
import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image

from tests import test_workbench_browser as browser_fixture
from xgent_app.artifact_covers import video_encoder
from tests.message_fixtures import stamp_fixture_messages

workspace_url = browser_fixture.workspace_url
browser_context = browser_fixture.browser_context
ready = browser_fixture.ready


@pytest.fixture
def sample_media(tmp_path):
    photo = tmp_path / 'cover-image.png'
    Image.new('RGB', (1200, 800), 'navy').save(photo)
    video = tmp_path / 'cover-video.mp4'
    encoder = video_encoder()
    if not encoder:
        pytest.skip('Video encoder unavailable')
    subprocess.run([encoder, '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                    'color=c=blue:s=320x180:r=5:d=1', '-an', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(video)],
                   check=True, capture_output=True, timeout=20)
    return photo, video


@pytest.mark.parametrize('mode', ['attachment', 'server_upload'])
def test_pending_image_and_video_open_clean_preview_and_stop_on_close(browser_context, workspace_url, sample_media, mode, tmp_path):
    page = browser_context.new_page()
    page.set_viewport_size({'width': 390, 'height': 844})
    ready(page, workspace_url['url'])
    page.locator('#input').fill('预览时保留的说明文字')
    photo, video = sample_media
    selector = '#file-input' if mode == 'attachment' else '#server-upload-input'
    page.locator(selector).set_input_files([str(photo), str(video)])
    assert page.locator('.pending-file[data-mode="' + mode + '"]').count() == 2
    page.get_by_role('button', name='预览 cover-image.png', exact=True).click()
    image = page.locator('#media-preview-dialog img')
    image.wait_for()
    page.wait_for_function('document.querySelector("#media-preview-dialog img").naturalWidth===1200')
    page.screenshot(path=str(tmp_path / (mode + '-image.png')))
    page.get_by_role('button', name='关闭媒体预览', exact=True).click()
    page.locator('#media-preview-dialog').wait_for(state='detached')
    page.get_by_role('button', name='预览 cover-video.mp4', exact=True).click()
    page.wait_for_function('document.querySelector("#media-preview-dialog video").readyState>=2')
    assert page.locator('#media-preview-dialog video').evaluate('(video)=>video.controls && video.playsInline')
    page.screenshot(path=str(tmp_path / (mode + '-video.png')))
    page.keyboard.press('Escape')
    page.locator('#media-preview-dialog').wait_for(state='detached')
    assert page.locator('#input').input_value() == '预览时保留的说明文字'
    assert page.locator('.pending-file').count() == 2
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.close()


def test_library_uses_lightweight_covers_filename_first_and_full_media_only_after_click(browser_context, workspace_url, sample_media, tmp_path):
    base = workspace_url['url']
    created = browser_context.request.post(base + '/api/workbench/conversations/create', data={'name': '媒体来源 · 较小链接'})
    assert created.ok
    cid = created.json()['current_chat_id']
    assert browser_context.request.post(base + '/api/workbench/conversations/switch', data={'id': 'global_memory'}).ok
    uploads = Path(workspace_url['database_path']).parent / 'xgent_storage' / 'uploads'
    uploads.mkdir(parents=True, exist_ok=True)
    photo, video = sample_media
    entries = [(photo.name, 'image/png', photo.read_bytes()),
               (video.name, 'video/mp4', video.read_bytes()),
               ('cover-notes.txt', 'text/plain', b'VISIBLE TEXT COVER\n<script>window.previewXss=1</script>')]
    refs = []
    for name, mime, data in entries:
        (uploads / name).write_bytes(data)
        refs.append({'version': 1, 'path': name, 'name': name, 'filename': name,
                     'mime_type': mime, 'storage': 'uploads', 'size': len(data)})
    metadata = {'attachments': [refs[0], refs[2]], 'server_uploads': [refs[1]],
                'file_ingress_mode': 'attachment', 'web_file_message': {'text': '混合批次图片与服务器视频'}}
    with sqlite3.connect(workspace_url['database_path']) as conn:
        row_id = conn.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id,metadata) VALUES(1,1,'user_file','user',?,?,?,?)",
                              ('混合批次图片与服务器视频', time.time(), cid, json.dumps(metadata))).lastrowid
        stamp_fixture_messages(conn, cid)
    page = browser_context.new_page()
    original_requests = []
    page.on('request', lambda request: original_requests.append(request.url) if f'/api/history/media/{row_id}/' in request.url else None)
    def legacy_library_response(route):
        response = route.fetch()
        data = response.json()
        for item in data.get('items', []):
            if item.get('source_id') == row_id:
                # Cached/older list descriptors had unscoped URLs. Opening
                # either preview entry must still use the recorded owner.
                item['download_url'] = item['download_url'].partition('?')[0]
        route.fulfill(response=response, json=data)
    page.route('**/api/workbench/artifacts?*', legacy_library_response)
    ready(page, base, 'files')
    try:
        for name, _, _ in entries:
            row = page.locator('.wb-file-table tbody tr').filter(has_text=name)
            row.locator('.wb-file-cover.is-ready').wait_for()
            assert row.locator('td').nth(0).locator('h3').inner_text() == name
            assert row.locator('td').nth(1).locator('.wb-source-link').inner_text() == '媒体来源 · 较小链接'
            assert row.locator('h3').evaluate('(n)=>parseFloat(getComputedStyle(n).fontSize)') > row.locator('.wb-source-link').evaluate('(n)=>parseFloat(getComputedStyle(n).fontSize)')
        assert len(original_requests) == 0, 'Only bounded covers should be requested before opening a preview'
        assert page.locator('.wb-file-cover pre').filter(has_text='VISIBLE TEXT COVER').count() == 1
        assert page.evaluate('window.previewXss') is None
        assert page.locator('.wb-file-cover img').count() == 2
        page.screenshot(path=str(tmp_path / 'file-library.png'))
        photo_row = page.locator('.wb-file-table tbody tr').filter(has_text=photo.name)
        photo_row.get_by_role('button', name='预览', exact=True).click()
        page.wait_for_function('document.querySelector("#wb-detail img")?.naturalWidth===1200')
        assert '文件可能已移动' not in page.locator('#wb-detail').inner_text()
        page.get_by_role('button', name='关闭详情', exact=True).click()
        photo_row.locator('.wb-file-cover').click()
        page.wait_for_function('document.querySelector("#media-preview-dialog img").naturalWidth===1200')
        assert len(original_requests) > 0
        page.get_by_role('button', name='关闭媒体预览', exact=True).click()
        photo_row.locator('.wb-source-link').click()
        page.get_by_role('button', name='关闭来源定位提示').wait_for()
        banner, close = page.locator('#source-navigation').bounding_box(), page.get_by_role('button', name='关闭来源定位提示').bounding_box()
        assert close['x'] - banner['x'] < 20 and close['y'] - banner['y'] < 15
        assert page.locator('#log .media-ingress-badge[data-mode=attachment]').count() == 2
        assert page.locator('#log .media-ingress-badge[data-mode=server_upload]').count() == 1
        page.reload()
        page.locator('#log .media-ingress-badge[data-mode=server_upload]').wait_for()
        assert page.locator('#wb-earlier,#wb-newer,#wb-history-controls').count() == 0
        page.get_by_role('button', name='关闭来源定位提示').click()
        assert browser_context.request.post(base + '/api/workbench/conversations/archive', data={'id': cid}).ok
        page.goto(base + '/#/files')
        archived_row = page.locator('.wb-file-table tbody tr').filter(has_text=photo.name)
        archived_row.locator('.wb-file-cover.is-ready').wait_for()
        archived_row.get_by_role('button', name='预览', exact=True).click()
        page.wait_for_function('document.querySelector("#wb-detail img")?.naturalWidth===1200')
        page.get_by_role('button', name='关闭详情', exact=True).click()
        archived_row.locator('.wb-file-cover').click()
        page.wait_for_function('document.querySelector("#media-preview-dialog img").naturalWidth===1200')
        page.get_by_role('button', name='关闭媒体预览', exact=True).click()
        archived_row.locator('.wb-source-link').click()
        page.locator('#source-reader .media-ingress-badge[data-mode=server_upload]').wait_for()
        assert page.locator('#source-reader').get_by_role('button', name='加载更早消息').count() == 0
        assert all('conversation_id=' + cid in url for url in original_requests)
    finally:
        page.close()
        browser_context.request.post(base + '/api/workbench/conversations/switch', data={'id': 'global_memory'})
        with sqlite3.connect(workspace_url['database_path']) as conn:
            conn.execute('DELETE FROM global_messages WHERE session_id=?', (cid,))
            conn.execute('DELETE FROM chat_sessions WHERE id=?', (cid,))
