"""Browser selection modes, multipart batching and idempotent retry."""
import io
import json
import re
import time
import urllib.parse
from pathlib import Path

import pytest

from tests.test_workbench_browser import browser_context, workspace_url, ready  # noqa: F401
from xgent_app.file_ingress import read_multipart
from tests.message_fixtures import current_history_message


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


@pytest.mark.parametrize('width,count', [(1440, 1), (390, 2)])
def test_images_and_caption_stay_in_one_bubble_before_and_after_refresh(browser_context, workspace_url, tmp_path, width, count):
    from tests.test_attachments import image_bytes
    expect = pytest.importorskip('playwright.sync_api').expect
    page = browser_context.new_page()
    page.set_viewport_size({'width': width, 'height': 900})
    caption = '这个不是neko，给你看看\n\n完整文字 <tag> & 图片必须在同一个块里'
    image = image_bytes(size=(800, 450))
    stored, held, errors, events, image_requests = [], [], [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda dialog: dialog.accept())
    def poll(route):
        after = int(urllib.parse.parse_qs(urllib.parse.urlsplit(route.request.url).query).get('after', ['0'])[0])
        route.fulfill(json={'epoch': 'file-bubble-test', 'cursor': len(events),
                            'events': [event for event in events if event['id'] > after], 'reset': False})
    page.route(re.compile(r'/api/events(?:\?.*)?$'), poll)
    page.route(re.compile(r'/api/(?:workbench/)?history(?:\?.*)?$'), lambda route: route.fulfill(json={
        'messages': stored, 'busy': False, 'conversation_id': 'global_memory', 'ui_generation': 0,
        'live': {'epoch': 'file-bubble-test', 'cursor': len(events), 'frames': []}}))
    page.route('**/api/attachments', lambda route: held.append(route))
    def serve_image(route):
        image_requests.append(route.request.url)
        route.fulfill(content_type='image/png', body=image)
    page.route('**/api/history/media/900001/*', serve_image)
    ready(page, workspace_url['url'])
    page.evaluate('''() => {
      const NativeXHR=window.XMLHttpRequest;
      window.XMLHttpRequest=function(){const request=new NativeXHR();window.testUploadRequest=request;return request;};
    }''')
    files = [{'name': f'image-{i}.png', 'mimeType': 'image/png', 'buffer': image} for i in range(count)]
    page.locator('#file-input').set_input_files(files)
    page.locator('#input').fill(caption)
    page.locator('#btn-send').click()
    expect(page.locator('#log > .msg-row.user')).to_have_count(1)
    row = page.locator('#log > .msg-row.user')
    expect(row.locator('.message-media img')).to_have_count(count)
    page.wait_for_function('Array.from(document.querySelectorAll("#log .media-photo")).every(node=>node.dataset.imageState==="ready")')
    page.evaluate('''() => {
      window.uploadPreviewNodes=Array.from(document.querySelectorAll('#log .media-img'));
      window.uploadPreviewUrls=uploadPreviewNodes.map(node=>node.src);
      window.previewBubbleWidth=document.querySelector('#log .bubble').getBoundingClientRect().width;
      window.previewDimensions=uploadPreviewNodes.map(node=>({width:node.getBoundingClientRect().width,height:node.getBoundingClientRect().height}));
    }''')
    expect(row.locator('.body')).to_have_text(caption, use_inner_text=True)
    expect(row.locator('.media-path-copy')).to_have_count(0)
    page.evaluate('testUploadRequest.upload.dispatchEvent(new ProgressEvent("progress",{lengthComputable:true,loaded:100,total:100}))')
    expect(row.locator('.upload-progress')).to_have_text('传输完成，正在保存…')
    page.evaluate('window.XGentChat.reload()')
    expect(page.locator('#log > .msg-row.user')).to_have_count(1)
    expect(row.locator('.message-media img')).to_have_count(count)
    expect(row.locator('.upload-progress')).to_be_visible()
    refs = []
    storage = tmp_path / 'storage'
    for i, file in enumerate(files):
        path = storage / 'uploads' / ('day/' + file['name'])
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(image)
        refs.append({'path': 'day/' + file['name'], 'name': file['name'], 'mime_type': 'image/png', 'order': i})
    request = held[0].request
    parsed = read_multipart(io.BytesIO(request.post_data_buffer), len(request.post_data_buffer), request.headers['content-type'], tmp_path)
    try:
        assert parsed['text'] == caption
        request_key = parsed['idempotency_key']
    finally:
        parsed['_temporary'].cleanup()
    message = current_history_message({'id': 900001, 'role': 'user', 'msg_type': 'user_file', 'timestamp': 1792840000,
        'content': caption + '\n\n[文件] image-0.png，已保存到 ' + str(storage/'uploads'/refs[0]['path']),
        'metadata': {'attachments': refs, 'file_ingress_mode': 'attachment', 'upload_request_key': request_key,
                     'web_file_message': {'version': 1, 'text': caption}}}, storage, tmp_path)
    message['conversation_id'] = 'global_memory'
    stored.append(message)
    frame = {**message, 'type': 'record_snapshot', 'record_id': 900001, 'text': message['content'], 'ui_generation': 0}
    frame.pop('content')
    events.append({'epoch': 'file-bubble-test', 'id': 1, 'frame': frame})
    # SSE/poll confirmation may reach the page before the HTTP acknowledgment.
    expect(page.locator('[data-record-id="900001"]')).to_have_count(1)
    assert page.evaluate('uploadPreviewNodes.every((node,index)=>node===document.querySelectorAll("#log .media-img")[index] && node.src===uploadPreviewUrls[index])')
    assert page.evaluate('Math.abs(previewBubbleWidth-document.querySelector("#log .bubble").getBoundingClientRect().width)<1')
    assert page.evaluate('uploadPreviewNodes.every((node,index)=>Math.abs(node.getBoundingClientRect().width-previewDimensions[index].width)<1 && Math.abs(node.getBoundingClientRect().height-previewDimensions[index].height)<1)')
    held[0].fulfill(json={'ok': True, 'record_id': 900001, 'message': message})
    expect(page.locator('[data-record-id="900001"]')).to_have_count(1)
    for refresh in (False, True):
        if refresh:
            page.reload()
            page.wait_for_function('document.getElementById("composer-model")?.disabled===false')
        row = page.locator('[data-record-id="900001"]')
        expect(page.locator('#log > .msg-row')).to_have_count(1)
        expect(row.locator('.message-media img')).to_have_count(count)
        expect(row.locator('.body')).to_have_text(caption, use_inner_text=True)
        expect(row.locator('.media-path-copy')).to_have_count(count)
        assert row.locator('.body').inner_text() == caption
        assert row.locator('.media-path-copy').first.get_attribute('title').startswith(str(storage))
        assert row.locator('.media-path-copy').first.evaluate('(node)=>parseFloat(getComputedStyle(node).fontSize)') <= 11
        assert row.locator('.media-paths-compact').evaluate('(node)=>node.scrollWidth<=node.clientWidth+1')
        assert row.locator('.media-paths-compact').evaluate('(node)=>node.getBoundingClientRect().top>=node.parentElement.querySelector(".body").getBoundingClientRect().bottom')
        if not refresh:
            page.evaluate('window.XGentChat.reload()')
            assert page.evaluate('uploadPreviewNodes.every((node,index)=>node===document.querySelectorAll("#log .media-img")[index] && node.src===uploadPreviewUrls[index])')
            assert not image_requests, 'Acknowledgment/history must not refetch an already decoded local preview'
    screenshots = Path(__file__).resolve().parents[1] / 'workspace' / 'file-message-qa'
    screenshots.mkdir(parents=True, exist_ok=True)
    row.screenshot(path=str(screenshots / f'caption-image-{width}.png'))
    page.evaluate("localStorage.setItem('xgent-theme','dark')")
    page.reload()
    expect(page.locator('body')).to_have_class(re.compile(r'.*\bdark\b.*'))
    row = page.locator('[data-record-id="900001"]')
    expect(row.locator('.message-media img')).to_have_count(count)
    expect(row.locator('.body')).to_have_text(caption, use_inner_text=True)
    row.screenshot(path=str(screenshots / f'caption-image-{width}-dark.png'))
    assert not errors, errors


@pytest.mark.parametrize('viewport', [1440, 390])
def test_slow_history_images_keep_placeholder_size_and_reuse_nodes(browser_context, workspace_url, tmp_path, viewport):
    from tests.test_attachments import image_bytes
    expect=pytest.importorskip('playwright.sync_api').expect
    page=browser_context.new_page();page.set_viewport_size({'width':viewport,'height':900})
    storage=tmp_path/'storage';refs=[];images=[]
    for index, size in enumerate(((1200,800),(600,1200))):
        data=image_bytes(size=size);images.append(data)
        path=storage/'uploads'/f'day/photo-{index}.png';path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        refs.append({'path':f'day/photo-{index}.png','name':f'photo-{index}.png','mime_type':'image/png',
                     'width':size[0],'height':size[1],'order':index,'sha256':str(index)*64})
    message=current_history_message({'id':910001,'role':'user','msg_type':'user_file','timestamp':1792840000,'content':'index',
        'metadata':{'file_ingress_mode':'attachment','attachments':refs,'web_file_message':{'version':1,'text':'慢速网络下文字和图片位置都不能跳'}}},storage,tmp_path)
    message['conversation_id']='global_memory';pending=[];received=[];events=[]
    def history(route):
        route.fulfill(json={'messages':[message],'busy':False,'conversation_id':'global_memory','ui_generation':0,
            'live':{'epoch':'slow-images','cursor':len(events),'frames':[]}})
    def poll(route):
        after=int(urllib.parse.parse_qs(urllib.parse.urlsplit(route.request.url).query).get('after',['0'])[0])
        route.fulfill(json={'epoch':'slow-images','cursor':len(events),'events':[e for e in events if e['id']>after],'reset':False})
    page.route(re.compile(r'/api/(?:workbench/)?history(?:\?.*)?$'),history)
    page.route(re.compile(r'/api/events(?:\?.*)?$'),poll)
    def media(route):
        received.append(route.request.url);pending.append(route)
    page.route('**/api/history/media/910001/*',media)
    ready(page,workspace_url['url'])
    row=page.locator('[data-record-id="910001"]')
    expect(row.locator('.media-photo[data-image-state="loading"]')).to_have_count(2)
    page.wait_for_function('Array.from(document.querySelectorAll(".media-image-frame")).every(n=>n.clientHeight>100)')
    measure='''node=>({body:node.querySelector('.body').getBoundingClientRect().top-node.getBoundingClientRect().top,
      frames:Array.from(node.querySelectorAll('.media-image-frame')).map(frame=>({width:frame.getBoundingClientRect().width,height:frame.getBoundingClientRect().height}))})'''
    before=row.evaluate(measure)
    assert row.locator('.message-media').evaluate('(node)=>node.scrollWidth<=node.clientWidth+1')
    assert row.locator('.media-image-frame').evaluate_all('(nodes)=>nodes.every(node=>node.getBoundingClientRect().right<=node.closest(".bubble").getBoundingClientRect().right)')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    for box, size in zip(before['frames'],((1200,800),(600,1200))):
        assert abs(box['width']/box['height']-size[0]/size[1])<.02
    page.evaluate('window.historyImageNodes=Array.from(document.querySelectorAll(".media-img"))')
    page.evaluate('window.XGentChat.reload()')
    assert page.evaluate('historyImageNodes.every((node,index)=>node===document.querySelectorAll(".media-img")[index])')
    assert before==row.evaluate(measure)
    for index in range(2):
        row.locator('.media-image-frame').nth(index).scroll_into_view_if_needed()
    # Any delayed response retains the dimensions before its bytes are available.
    assert len(pending)==2
    screenshots=Path(__file__).resolve().parents[1]/'workspace'/'file-message-qa';screenshots.mkdir(parents=True,exist_ok=True)
    row.screenshot(path=str(screenshots/f'loading-placeholder-{viewport}.png'))
    for route in list(pending):
        index=int(urllib.parse.urlsplit(route.request.url).path.rsplit('/',1)[1])
        route.fulfill(content_type='image/png',body=images[index])
    expect(row.locator('.media-photo[data-image-state="ready"]')).to_have_count(2)
    after=row.evaluate(measure)
    assert abs(before['body']-after['body'])<1
    for first,last in zip(before['frames'],after['frames']):
        assert abs(first['width']-last['width'])<1 and abs(first['height']-last['height'])<1
    received_before=len(received)
    page.evaluate('window.XGentChat.reload()')
    assert page.evaluate('historyImageNodes.every((node,index)=>node===document.querySelectorAll(".media-img")[index])')
    assert len(received)==received_before
    # A changed retained file must replace the old preview rather than keep stale pixels.
    message['media'][0]['sha256']='f'*64
    frame={**message,'type':'record_snapshot','record_id':910001,'text':message['content'],'ui_generation':0};frame.pop('content')
    events.append({'epoch':'slow-images','id':1,'frame':frame})
    page.wait_for_function('historyImageNodes[0]!==document.querySelector(".media-img")')
    row.locator('.media-image-frame').first.scroll_into_view_if_needed()
    expect(row.locator('.media-photo[data-image-state="loading"]')).to_have_count(1)
    assert len(received)==received_before+1
    pending[-1].fulfill(content_type='image/png',body=image_bytes(color='blue',size=(1200,800)))
    expect(row.locator('.media-photo[data-image-state="ready"]')).to_have_count(2)


def test_history_image_failure_keeps_placeholder_and_can_retry(browser_context,workspace_url,tmp_path):
    from tests.test_attachments import image_bytes
    expect=pytest.importorskip('playwright.sync_api').expect
    page=browser_context.new_page();page.set_viewport_size({'width':390,'height':900})
    message={'id':920001,'conversation_id':'global_memory','role':'user','msg_type':'user_file','content':'图片失败也保留位置',
        'media':[{'kind':'photo','filename':'retry.png','path':'/server/retry.png','width':1200,'height':800,
                  'download_url':'/api/history/media/920001/0'}]}
    page.route(re.compile(r'/api/(?:workbench/)?history(?:\?.*)?$'),lambda route:route.fulfill(json={
        'messages':[message],'busy':False,'conversation_id':'global_memory','ui_generation':0,
        'live':{'epoch':'retry-image','cursor':0,'frames':[]}}))
    page.route(re.compile(r'/api/events(?:\?.*)?$'),lambda route:route.fulfill(json={
        'epoch':'retry-image','cursor':0,'events':[],'reset':False}))
    requests=[];held=[]
    def image(route):
        requests.append(route.request.url)
        if len(requests)==1:route.fulfill(status=503,body='temporarily unavailable')
        else:held.append(route)
    page.route('**/api/history/media/920001/*',image)
    ready(page,workspace_url['url'])
    row=page.locator('[data-record-id="920001"]')
    expect(row.locator('.media-photo[data-image-state="error"]')).to_have_count(1)
    frame=row.locator('.media-image-frame');height=frame.bounding_box()['height']
    assert height>150
    row.get_by_role('button',name='重新加载',exact=True).click()
    expect(row.locator('.media-photo[data-image-state="loading"]')).to_have_count(1)
    assert abs(frame.bounding_box()['height']-height)<1
    assert len(held)==1
    held[0].fulfill(content_type='image/png',body=image_bytes(size=(1200,800)))
    expect(row.locator('.media-photo[data-image-state="ready"]')).to_have_count(1)
    assert abs(frame.bounding_box()['height']-height)<1


@pytest.mark.parametrize('width,kind', [(1440, 'user_photo'), (390, 'user_file')])
def test_telegram_image_live_and_refresh_match_web_image_style(browser_context,workspace_url,tmp_path,width,kind):
    from tests.test_attachments import image_bytes
    expect=pytest.importorskip('playwright.sync_api').expect
    page=browser_context.new_page();page.set_viewport_size({'width':width,'height':900})
    page.add_init_script("localStorage.setItem('xgent-theme','dark')")
    caption='刚刚更新了一下，再给你发个图片测试\n\n原始附言要完整显示，不能出现文件名和路径。'
    data=image_bytes(size=(800,600))
    storage=tmp_path/'storage';path=storage/'uploads'/'day'/'photo_telegram.png'
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    source=f'[文件] photo_telegram.png，已保存到 {path.as_posix()}。说明：{caption}'
    message=current_history_message({'id':930001,'timestamp':1792840000,'role':'user','msg_type':kind,'content':source,
        'metadata':{'attachments':[{'path':'day/photo_telegram.png','name':'photo_telegram.png','mime_type':'image/png',
            'width':800,'height':600,'caption':caption,'source_message_id':333}]}},storage,tmp_path)
    message['conversation_id']='global_memory';stored=[];events=[];errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    def poll(route):
        after=int(urllib.parse.parse_qs(urllib.parse.urlsplit(route.request.url).query).get('after',['0'])[0])
        route.fulfill(json={'epoch':'telegram-file','cursor':len(events),'events':[e for e in events if e['id']>after],'reset':False})
    page.route(re.compile(r'/api/events(?:\?.*)?$'),poll)
    page.route(re.compile(r'/api/(?:workbench/)?history(?:\?.*)?$'),lambda route:route.fulfill(json={
        'messages':stored,'busy':False,'conversation_id':'global_memory','ui_generation':0,
        'live':{'epoch':'telegram-file','cursor':len(events),'frames':[]}}))
    page.route('**/api/history/media/930001/*',lambda route:route.fulfill(content_type='image/png',body=data))
    ready(page,workspace_url['url'])
    stored.append(message)
    frame={**message,'type':'record_snapshot','record_id':930001,'text':message['content'],'ui_generation':0};frame.pop('content')
    events.append({'epoch':'telegram-file','id':1,'frame':frame})
    row=page.locator('[data-record-id="930001"]')
    expect(row).to_have_count(1)
    for refresh in (False,True):
        if refresh:
            page.reload();page.wait_for_function('document.getElementById("composer-model")?.disabled===false')
        expect(page.locator('#log > .msg-row')).to_have_count(1)
        expect(row.locator('.body')).to_have_text(caption,use_inner_text=True)
        assert row.locator('.body').inner_text()==caption
        assert '已保存到' not in row.locator('.body').inner_text()
        assert 'photo_telegram.png' not in row.locator('.body').inner_text()
        expect(row.locator('.media-photo[data-image-state="ready"]')).to_have_count(1)
        expect(row.locator('.media-path-copy')).to_have_count(1)
        assert row.locator('.media-path-copy').get_attribute('title').startswith(str(path))
        assert row.locator('.media-path-copy').evaluate('(node)=>parseFloat(getComputedStyle(node).fontSize)')<=11
        assert row.locator('.message-media').evaluate('(node)=>node.scrollWidth<=node.clientWidth+1')
    screenshots=Path(__file__).resolve().parents[1]/'workspace'/'file-message-qa';screenshots.mkdir(parents=True,exist_ok=True)
    row.screenshot(path=str(screenshots/f'telegram-photo-{width}.png'))
    assert not errors,errors


def test_large_file_uses_short_chunks_and_recovers_proxy_524_without_reupload(browser_context,workspace_url):
    expect=pytest.importorskip('playwright.sync_api').expect
    page=browser_context.new_page();held=[];chunks=[];control=[]
    state={'offset':0,'complete':False,'polls':0,'id':'a'*64}
    size=51*1024*1024
    def api_route(route):
        request=route.request;url=urllib.parse.urlsplit(request.url);control.append(url.path)
        if url.path.endswith('/init'):
            body=request.post_data_json
            assert body['files'][0]['size']==size
            assert body['text']=='大文件和完整说明'
            route.fulfill(json={'ok':True,'transfer_id':state['id'],'state':'receiving','offsets':[state['offset']],'chunk_bytes':4*1024*1024})
        elif '/chunk/' in url.path:
            offset=int(urllib.parse.parse_qs(url.query)['offset'][0]);content=request.post_data_buffer
            assert len(content)<=4*1024*1024 and offset==state['offset']
            chunks.append((offset,len(content)));state['offset']+=len(content)
            if len(chunks)==1:
                # The proxy lost a confirmation after the origin stored the chunk.
                route.fulfill(status=524,content_type='text/html',body='<html>Proxy timeout</html>')
            else:
                route.fulfill(json={'ok':True,'offset':state['offset']})
        elif '/complete/' in url.path:
            assert state['offset']==size;state['complete']=True
            route.fulfill(json={'ok':True,'transfer_id':state['id'],'state':'processing','offsets':[size]})
        else:
            if state['complete']:
                state['polls']+=1
                if state['polls']>1:
                    held.append(route)
                    return
            route.fulfill(json={'ok':True,'transfer_id':state['id'],'state':'processing' if state['complete'] else 'receiving',
                                'offsets':[state['offset']]})
    page.route('**/api/file-transfers/**',api_route)
    page.route('**/api/attachments',lambda route:route.fulfill(status=500,json={'error':'large files must use chunks'}))
    ready(page,workspace_url['url'])
    page.evaluate('''() => {
      const input=document.getElementById('file-input'),transfer=new DataTransfer();
      const part=new Uint8Array(1024*1024);
      transfer.items.add(new File(Array(51).fill(part),'large.zip',{type:'application/zip'}));
      input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}));
    }''')
    page.locator('#input').fill('大文件和完整说明');page.locator('#btn-send').click()
    page.wait_for_function('document.querySelector(".upload-progress")?.textContent==="传输完成，正在保存校验…"',timeout=60000)
    expect(page.locator('.upload-progress')).to_have_text('传输完成，正在保存校验…')
    assert len(chunks)==13 and sum(length for _,length in chunks)==size
    assert control.count('/api/file-transfers/init')==1
    assert not page.locator('.wb-upload-status').count()
    page.wait_for_function('document.querySelector(".upload-progress")?.textContent.includes("保存")')
    deadline=time.monotonic()+5
    while not held and time.monotonic()<deadline:
        page.wait_for_timeout(50)
    assert held
    held[0].fulfill(json={'ok':True,'transfer_id':state['id'],'state':'accepted','offsets':[size],
                         'record_id':940001,'duplicate':True})
    expect(page.locator('.pending-file')).to_have_count(0)
    page.wait_for_function('!document.getElementById("btn-send").disabled')
