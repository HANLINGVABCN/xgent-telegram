"""Compare actual live reply presentation against refresh and paged history."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_external_sync import ProbeMixin
from tests.test_full_output_web import full_output_server  # noqa: F401


@pytest.fixture(scope='module')
def reply_cases():
    return ProbeMixin().run_probe(r'''
import asyncio,tempfile
from unittest.mock import patch
import xgent_server as bot
from tests.attachment_request_probe import Harness
from xgent_app.web_history import build_history_message
async def main():
    result=[]
    with tempfile.TemporaryDirectory() as folder:
        async with Harness(bot,folder) as h:
            for hide in (True,False):
                for stream,style in ((False,'foreground'),(True,'foreground'),(True,'background')):
                    bot.UserDataManager.set('hide_protocol_blocks',hide)
                    bot.UserDataManager.set('stream_mode',stream);bot.UserDataManager.set('stream_style',style)
                    raw='# 标题\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n- [x] 检查完成\n\n'
                    raw+='```python\nprint("<safe> & 中文")\n```\n\n'
                    raw+='```run-x\n<<BEGIN_STABLE123456\n'+''.join(f'line {i}\n' for i in range(90))+'<<END_STABLE123456\n```\n\n'
                    raw+=(('长段落内容 '*30+'\n')*40).rstrip()
                    async def complete(*args,**kwargs):return raw,None
                    async def chunks(*args,**kwargs):yield raw
                    with patch.object(bot.ModelClient,'think_and_reply',complete),patch.object(bot.ModelClient,'think_and_reply_stream',chunks):
                        await h.turn('presentation probe')
                    frames=h.drain_frames()
                    conn=await h.db._get_conn()
                    cur=await conn.execute("SELECT * FROM global_messages WHERE msg_type='ai_reply' ORDER BY id DESC LIMIT 1")
                    saved=dict(await cur.fetchone());await cur.close()
                    metadata=json.loads(saved['metadata'])
                    message=build_history_message(saved,bot.ArtifactManager.ROOT_DIR,folder)
                    bot.UserDataManager.set('hide_protocol_blocks',not hide)
                    changed=build_history_message(saved,bot.ArtifactManager.ROOT_DIR,folder)
                    result.append({'name':f'{hide}/{stream}/{style}','raw_unchanged':raw==saved['content'],
                        'frames':frames,'history':message,'frozen':changed['content']==message['content'],
                        'display':metadata.get('display'),'aliases':metadata.get('web_live',{}).get('message_ids',[])})
    print(json.dumps(result))
asyncio.run(main())
''')


def test_saved_reply_freezes_display_and_tracks_all_final_fragments(reply_cases):
    assert len(reply_cases)==6
    for case in reply_cases:
        assert case['raw_unchanged'] and case['frozen'],case['name']
        assert case['display']['parse_mode']=='HTML'
        finals=[f for f in case['frames'] if f['type']=='record_snapshot']
        assert len(finals)==1,case['name']
        final=finals[0]
        assert final['text']==case['history']['content']
        assert final['parse_mode']==case['history']['parse_mode']
        fragments={f['message_id'] for f in case['frames'] if f['type'] in ('message','edit') and f.get('parse_mode')=='HTML'}
        assert fragments.issubset(final['replace_message_ids']),(case['name'],fragments,final['replace_message_ids'])
        assert fragments.issubset(case['aliases'])


def browser_session(server,p):
    base=f'http://127.0.0.1:{server.config.port}'
    browser=p.chromium.launch(headless=True)
    context=browser.new_context()
    context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(base) else r.abort())
    assert context.request.post(base+'/api/login',data={'password':'temporary-test-only'}).ok
    return browser,context,base


def test_live_refresh_and_pagination_have_identical_dom(full_output_server,reply_cases):
    server,_,_,_=full_output_server
    playwright=pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():pytest.skip('Chromium unavailable')
        browser,context,base=browser_session(server,p)
        try:
            page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            for case in reply_cases:
                records=[]
                async def history(limit):return records
                server.config.read_history=history
                server.outbox.put({'type':'history_reset'})
                page.goto(base);page.wait_for_function('!document.getElementById("btn-send").disabled')
                for frame in case['frames']:server.outbox.put(frame)
                selector=f'.msg-row[data-record-id="{case["history"]["id"]}"]'
                page.locator(selector).wait_for()
                assert page.locator('.msg-row').count()==1,case['name']
                before=page.locator(selector+' .body').inner_html()
                role=page.locator(selector).get_attribute('class')
                timestamp=page.locator(selector+' .ts').text_content()
                # A transport edit arriving after the durable frame must not undo it.
                alias=case['aliases'][0]
                server.outbox.put({'type':'edit','message_id':alias,'text':'stale fragment','parse_mode':'HTML'})
                records.append(case['history'])
                page.reload();page.locator(selector).wait_for()
                assert page.locator(selector+' .body').inner_html()==before,case['name']
                assert page.locator(selector).get_attribute('class')==role
                assert page.locator(selector+' .ts').text_content()==timestamp
                # Load this exact record through the older-page path too.
                def paged(route):
                    older='before=' in route.request.url
                    route.fulfill(json={'messages':[case['history']] if older else [],
                        'next_cursor':None if older else 'older-page','busy':False,'ui_generation':None,
                        **({} if older else {'live':server.outbox.snapshot()})})
                page.route('**/api/workbench/history?*',paged)
                page.reload();page.wait_for_function('!document.getElementById("btn-send").disabled');page.locator('#log').evaluate('(e)=>e.scrollTop=0');page.locator('#wb-earlier').click()
                page.locator(selector).wait_for()
                assert page.locator(selector+' .body').inner_html()==before,case['name']
                assert page.locator(selector).get_attribute('class')==role
                page.unroute('**/api/workbench/history?*',paged)
            assert errors==[]
        finally:browser.close()


def test_identical_edit_preserves_expanded_block_and_error_layout(full_output_server):
    server,_,_,canonical=full_output_server
    playwright=pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():pytest.skip('Chromium unavailable')
        browser,context,base=browser_session(server,p)
        try:
            page=context.new_page();page.goto(base);page.locator('.pb-head').first.wait_for()
            server.outbox.put({'type':'message','message_id':991,'text':canonical,'parse_mode':'HTML'})
            page.wait_for_function('document.querySelectorAll(".pb-head").length>=2')
            block=page.locator('.msg-row').last.locator('.pb-head');block.click()
            page.locator('.msg-row').last.locator('.body').evaluate('(n)=>window.__sameBody=n.firstElementChild')
            server.outbox.put({'type':'edit','message_id':991,'text':canonical,'parse_mode':'HTML'})
            text='⚠️ **upstream** <bad>\nsecond line'
            server.outbox.put({'type':'turn_error','text':text,'record_id':999})
            page.get_by_text(text,exact=True).wait_for()
            assert page.evaluate('window.__sameBody.isConnected')
            live=page.locator('.msg-row').last.locator('.body').inner_html()
            import html
            async def history(limit):return [{'id':999,'role':'assistant','msg_type':'runtime_error',
                'content':'<pre>'+html.escape(text)+'</pre>','parse_mode':'HTML'}]
            server.config.read_history=history
            page.reload();page.locator('[data-record-id="999"]').wait_for()
            assert page.locator('[data-record-id="999"] .body').inner_html()==live
        finally:browser.close()


def test_generated_media_uses_history_prose_renderer(tmp_path):
    import types
    from xgent_app.web_bridge import _present_media_frame
    from xgent_app.web_history import build_history_message
    from xgent_app.web_media import build_media_presentation,media_presentation_scope
    raw='# heading\n\n**bold** | table'
    path=tmp_path/'image.png';path.write_bytes(b'fixture-not-displayed')
    module=types.ModuleType('xgent_server')
    module.markdown_to_telegram_html=lambda text:'<b>'+text+'</b>'
    for hide in (True,False):
        module._should_hide_protocol_blocks=lambda:hide
        with patch.dict('sys.modules',{'xgent_server':module}):
            with media_presentation_scope(build_media_presentation(raw,[{'path':str(path)}])):
                live=_present_media_frame({'type':'photo','message_id':1})
            saved=build_history_message({'id':5,'role':'assistant','msg_type':'ai_reply','content':raw},tmp_path,tmp_path)
        assert (live['text'],live['parse_mode'])==(saved['content'],saved['parse_mode'])


def test_reply_fragment_tracking_is_scoped_and_does_not_leak():
    import asyncio
    from types import SimpleNamespace
    from xgent_app.reply_presentation import track_reply_presentation,bind_reply_message_ids,remember_reply_messages
    async def run():
        ready=asyncio.Event();ids=[];nested=[]
        async def inherited():
            await ready.wait();remember_reply_messages([SimpleNamespace(message_id=999)])
        @track_reply_presentation
        async def inner():
            bind_reply_message_ids(nested);remember_reply_messages([SimpleNamespace(message_id=2)])
        @track_reply_presentation
        async def outer():
            bind_reply_message_ids(ids)
            worker=asyncio.create_task(inherited())
            await inner()
            remember_reply_messages([SimpleNamespace(message_id=1),SimpleNamespace(message_id=1)])
            return worker
        worker=await outer();ready.set();await worker
        assert ids==[1] and nested==[2]
    asyncio.run(run())


def test_late_saved_reply_cannot_return_after_clear(full_output_server):
    server,_,_,_=full_output_server
    from xgent_app.ui_history import UiHistorySnapshot
    async def history(limit):return UiHistorySnapshot([],generation=2)
    server.config.read_history=history
    playwright=pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():pytest.skip('Chromium unavailable')
        browser,context,base=browser_session(server,p)
        try:
            page=context.new_page();page.goto(base)
            page.wait_for_function('!document.getElementById("btn-send").disabled')
            frame={'type':'record_snapshot','record_id':13,'role':'assistant','msg_type':'ai_reply',
                'text':'<b>old-generation</b>','parse_mode':'HTML','ui_generation':1}
            server.outbox.put(frame)
            server.outbox.put({'type':'callback_answer','text':'snapshot processed'})
            page.get_by_text('snapshot processed',exact=True).wait_for()
            assert page.locator('[data-record-id="13"]').count()==0
            server.outbox.put({**frame,'ui_generation':2,'text':'<b>new-generation</b>'})
            page.locator('[data-record-id="13"]').wait_for()
            assert page.locator('[data-record-id="13"] .body').inner_text()=='new-generation'
        finally:browser.close()
