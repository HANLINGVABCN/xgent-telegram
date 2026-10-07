"""Refresh-safe active messages and immediate cursor updates, using loopback only."""
import json
import threading
import time

import pytest

from xgent_app.web_bridge import WebOutbox
from tests.test_full_output_web import full_output_server  # noqa: F401

STOP = [[{'text':'停止生成','callback_data':'act_stop_generation'}]]


def test_active_snapshot_survives_event_eviction_and_updates_in_place():
    outbox=WebOutbox(maxsize=2)
    outbox.put({'type':'message','message_id':51,'text':'正在生成中','reply_markup':STOP})
    for i in range(10):outbox.put({'type':'chat_action','action':'typing'})
    snap=outbox.snapshot()
    assert snap['frames'][0]['text']=='正在生成中'
    outbox.put({'type':'edit','message_id':51,'text':'最新内容','reply_markup':STOP})
    assert len(outbox.snapshot()['frames'])==1
    assert outbox.snapshot()['frames'][0]['text']=='最新内容'
    snap['frames'][0]['text']='client mutation'
    assert outbox.snapshot()['frames'][0]['text']=='最新内容'
    outbox.put({'type':'edit_markup','message_id':51,'reply_markup':None})
    assert outbox.snapshot()['frames']==[]
    outbox.put({'type':'message','message_id':52,'text':'工作中','reply_markup':STOP})
    outbox.put({'type':'delete','message_id':52})
    assert not outbox.snapshot()['frames']
    outbox.put({'type':'message','message_id':53,'text':'工作中','reply_markup':STOP})
    outbox.put({'type':'history_reset'})
    assert not outbox.snapshot()['frames'] and outbox.snapshot()['epoch']!=snap['epoch']


def test_long_poll_wakes_on_new_event_and_close():
    outbox=WebOutbox();snap=outbox.snapshot();result=[]
    worker=threading.Thread(target=lambda:result.append(outbox.read_events(snap['cursor'],snap['epoch'],wait_seconds=5)))
    worker.start();time.sleep(.05)
    start=time.monotonic();outbox.put({'type':'message','message_id':1,'text':'new'})
    worker.join(1)
    assert not worker.is_alive() and result[0]['events'][0]['frame']['text']=='new'
    assert time.monotonic()-start<.8
    cursor=outbox.snapshot();worker=threading.Thread(target=lambda:outbox.read_events(cursor['cursor'],cursor['epoch'],wait_seconds=5))
    worker.start();time.sleep(.05);outbox.close();worker.join(1)
    assert not worker.is_alive()


def test_browser_refresh_restores_live_placeholder_before_next_chunk(full_output_server):
    server,_,_,_=full_output_server
    playwright=pytest.importorskip('playwright.sync_api')
    from pathlib import Path
    base=f'http://127.0.0.1:{server.config.port}'
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():pytest.skip('Chromium unavailable')
        browser=p.chromium.launch(headless=True)
        try:
            context=browser.new_context()
            context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(base) else r.abort())
            context.request.post(base+'/api/login',data={'password':'temporary-test-only'})
            server.config.is_busy=lambda:True
            server.outbox.put({'type':'message','message_id':7791,'text':'实时生成占位-A','reply_markup':STOP})
            page=context.new_page();errors=[];requests=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('request',lambda r:requests.append(r.url))
            for _ in range(3):
                page.goto(base)
                page.get_by_text('实时生成占位-A',exact=True).wait_for(timeout=5000)
                assert page.get_by_text('实时生成占位-A',exact=True).count()==1
                assert page.locator('#btn-stop').is_visible()
            before=len([url for url in requests if '/api/workbench/history?' in url])
            server.outbox.put({'type':'edit','message_id':7791,'text':'实时生成最新-B','reply_markup':STOP})
            page.get_by_text('实时生成最新-B',exact=True).wait_for(timeout=3000)
            assert page.get_by_text('实时生成占位-A',exact=True).count()==0
            server.outbox.put({'type':'edit','message_id':7791,'text':'最终结果-C','reply_markup':None})
            server.config.is_busy=lambda:False
            server.outbox.put({'type':'generation_end'})
            server.outbox.put({'type':'turn_end'})
            page.get_by_text('最终结果-C',exact=True).wait_for(timeout=3000)
            page.wait_for_timeout(250)
            assert len([url for url in requests if '/api/workbench/history?' in url])==before
            assert page.get_by_text('最终结果-C',exact=True).count()==1
            assert not page.locator('#btn-stop').is_visible()
            assert not errors
        finally:browser.close()


def test_snapshot_race_replays_newer_update_and_long_poll_is_immediate(full_output_server):
    server,_,_,_=full_output_server
    playwright=pytest.importorskip('playwright.sync_api')
    from pathlib import Path
    base=f'http://127.0.0.1:{server.config.port}'
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():pytest.skip('Chromium unavailable')
        browser=p.chromium.launch(headless=True)
        try:
            context=browser.new_context()
            context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(base) else r.abort())
            context.request.post(base+'/api/login',data={'password':'temporary-test-only'})
            page=context.new_page()
            page.add_init_script('window.EventSource=undefined;')
            server.config.is_busy=lambda:True
            server.outbox.put({'type':'message','message_id':444,'text':'snapshot old','reply_markup':STOP})
            requests=[];errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('request',lambda r:requests.append(r.url))
            def history(route):
                response=route.fetch();body=response.json()
                assert body['live']['frames'][0]['text']=='snapshot old'
                server.outbox.put({'type':'edit','message_id':444,'text':'snapshot new','reply_markup':STOP})
                route.fulfill(response=response)
            page.route('**/api/workbench/history?*',history)
            page.goto(base)
            page.get_by_text('snapshot new',exact=True).wait_for(timeout=5000)
            assert page.get_by_text('snapshot old',exact=True).count()==0
            page.wait_for_function("!document.getElementById('status-text').textContent.includes('连接中')")
            start=time.monotonic()
            server.outbox.put({'type':'edit','message_id':444,'text':'instant next','reply_markup':STOP})
            page.get_by_text('instant next',exact=True).wait_for(timeout=1500)
            assert time.monotonic()-start<1.2
            assert any('wait=20' in url for url in requests)
            assert not errors
        finally:browser.close()


def test_snapshot_finalization_does_not_duplicate_committed_reply(full_output_server):
    server,_,_,_=full_output_server
    playwright=pytest.importorskip('playwright.sync_api')
    from pathlib import Path
    base=f'http://127.0.0.1:{server.config.port}'
    server.outbox.put({'type':'message','message_id':883,'text':'pending-original','reply_markup':STOP})
    epoch=server.outbox.snapshot()['epoch']
    async def history(limit):
        # The HTTP handler captured the pending frame before DB read. Final commit
        # happens while it is assembling history: the saved alias replaces it.
        server.outbox.put({'type':'edit','message_id':883,'text':'saved final reply','reply_markup':None})
        return [{'id':88,'role':'assistant','content':'saved final reply','timestamp':2,
                 'web_live':{'epoch':epoch,'message_ids':[883]}}]
    server.config.read_history=history
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():pytest.skip('Chromium unavailable')
        browser=p.chromium.launch(headless=True)
        try:
            context=browser.new_context();context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(base) else r.abort())
            context.request.post(base+'/api/login',data={'password':'temporary-test-only'})
            page=context.new_page();page.goto(base)
            page.get_by_text('saved final reply',exact=True).wait_for()
            page.wait_for_timeout(250)
            assert page.get_by_text('saved final reply',exact=True).count()==1
            assert page.get_by_text('pending-original',exact=True).count()==0
        finally:browser.close()


def test_unrelated_snapshot_ids_do_not_alias_after_server_restart(full_output_server):
    server,_,_,_=full_output_server
    import urllib.request
    import http.cookiejar
    base=f'http://127.0.0.1:{server.config.port}'
    cookies=http.cookiejar.CookieJar();opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    request=urllib.request.Request(base+'/api/login',data=json.dumps({'password':'temporary-test-only'}).encode(),headers={'Content-Type':'application/json'})
    with opener.open(request):pass
    server.outbox.put({'type':'message','message_id':9,'text':'new epoch active','reply_markup':STOP})
    async def history(limit):return [{'id':99,'role':'assistant','content':'old epoch record','web_live':{'epoch':'old-epoch','message_ids':[9]}}]
    server.config.read_history=history
    with opener.open(base+'/api/workbench/history?limit=50') as response:body=json.load(response)
    assert len(body['live']['frames'])==1
    assert body['live']['frames'][0]['text']=='new epoch active'


def test_real_conversation_publishes_pending_and_final_record_aliases():
    from tests.test_external_sync import ProbeMixin
    result=ProbeMixin().run_probe(r'''
import asyncio,tempfile
from unittest.mock import AsyncMock,patch
import xgent_server as bot
from tests.attachment_request_probe import Harness
async def main():
    result={}
    with tempfile.TemporaryDirectory() as folder:
        async with Harness(bot,folder) as h:
            for stream,style in ((False,'foreground'),(True,'foreground'),(True,'background')):
                await h.db.clear_all_conversation_memory()
                bot.UserDataManager.set('stream_mode',stream);bot.UserDataManager.set('stream_style',style)
                started=asyncio.Event();release=asyncio.Event()
                async def complete(*args,**kwargs):
                    started.set();await release.wait();return 'final body',None
                async def chunks(*args,**kwargs):
                    started.set();await release.wait();yield 'final body'
                with patch.object(bot.ModelClient,'think_and_reply',complete),patch.object(bot.ModelClient,'think_and_reply_stream',chunks):
                    task=asyncio.create_task(h.turn('check refresh'))
                    await asyncio.wait_for(started.wait(),2)
                    snapshot=h.outbox.snapshot()
                    assert len(snapshot['frames'])==1,snapshot
                    assert '输出中' in snapshot['frames'][0]['text']
                    mid=snapshot['frames'][0]['message_id']
                    assert h.outbox.snapshot()['frames'][0]['message_id']==mid
                    release.set();await task
                rows=await h.db.get_global_messages(100)
                saved=next(r for r in reversed(rows) if r['msg_type']=='ai_reply')
                metadata=json.loads(saved['metadata'])
                assert mid in metadata['web_live']['message_ids']
                assert metadata['web_live']['epoch']==snapshot['epoch']
                assert not h.outbox.snapshot()['frames']
                result[str(stream)+'/'+style]=True
    print(json.dumps(result))
asyncio.run(main())
''')
    assert len(result)==3 and all(result.values())


def test_idle_long_poll_does_not_disable_composer_for_twenty_seconds(full_output_server):
    server,_,_,_=full_output_server
    playwright=pytest.importorskip('playwright.sync_api')
    from pathlib import Path
    base=f'http://127.0.0.1:{server.config.port}'
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():pytest.skip('Chromium unavailable')
        browser=p.chromium.launch(headless=True)
        try:
            context=browser.new_context();context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(base) else r.abort())
            context.request.post(base+'/api/login',data={'password':'temporary-test-only'})
            page=context.new_page();page.add_init_script('window.EventSource=undefined;')
            page.goto(base)
            page.wait_for_function('!document.getElementById("btn-send").disabled',timeout=4000)
            assert '连接中' not in page.locator('#status-text').inner_text()
        finally:browser.close()
