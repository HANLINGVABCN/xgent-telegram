"""Visible message counts, dynamic history and statistics use the actual turn pipeline."""
import pytest

from tests.test_external_sync import ProbeMixin
from tests.test_full_output_web import full_output_server  # noqa: F401
from xgent_app.presentation import operation_html, usage_line, usage_details
from xgent_app.cli_render import Palette, MessageRenderer
from xgent_app.cli_tui import PtScreen


@pytest.fixture(scope='module')
def turns():
    return ProbeMixin().run_probe(r'''
import asyncio,json,tempfile
from pathlib import Path
from unittest.mock import patch
import xgent_server as bot
from tests.attachment_request_probe import Harness
from xgent_app.web_history import build_history_message
async def main():
    cases=[]
    for mode in ('folded','detailed'):
      for stream,style in ((False,'foreground'),(True,'foreground'),(True,'background')):
        with tempfile.TemporaryDirectory() as folder:
          async with Harness(bot,folder) as h:
            await bot.UserDataManager.save_config('presentation_mode',mode)
            bot.UserDataManager.set('agent_mode',True)
            bot.UserDataManager.set('stream_mode',stream)
            bot.UserDataManager.set('stream_style',style)
            replies=['好的，主人，\n```run-x\n<<BEGIN_PRESENT123456\nuptime\n<<END_PRESENT123456\n```\n主人稍等马上。',
                     '主人，当前负载为 0.15、0.20、0.18。']
            raw=list(replies)
            def response(kwargs):
                kwargs['usage_sink'].append({'input_tokens':2840,'output_tokens':38,'cached_tokens':2130,'total_tokens':2878})
                return replies.pop(0)
            async def complete(*args,**kwargs):return response(kwargs),None
            async def chunks(*args,**kwargs):yield response(kwargs)
            async def operation(*args,**kwargs):return {'kind':'run','success':True,'return_code':0,
                'output':'load average: 0.15, 0.20, 0.18','notice':'[run结果] return_code=0 load average: 0.15, 0.20, 0.18',
                'context_message':{'role':'user','content':'[run结果] load average: 0.15, 0.20, 0.18'}}
            with patch.object(bot.ModelClient,'think_and_reply',complete),patch.object(bot.ModelClient,'think_and_reply_stream',chunks),patch.object(bot,'dispatch_standard_protocol',operation):
                await h.turn('去查下负载')
            frames=h.drain_frames()
            rows=await h.db.get_display_history(0)
            shown=[build_history_message(row,bot.ArtifactManager.ROOT_DIR,folder) for row in rows]
            refreshed=[build_history_message(row,bot.ArtifactManager.ROOT_DIR,folder) for row in rows]
            other='detailed' if mode=='folded' else 'folded'
            bot.UserDataManager.set('presentation_mode',other)
            switched=[build_history_message(row,bot.ArtifactManager.ROOT_DIR,folder) for row in rows]
            from xgent_app.workbench import Workbench
            loaded=[str(row['id']) for row,message in zip(rows,shown) if not message.get('hidden')]
            subset=await Workbench(vars(bot)).history({'presentation_ids':','.join(loaded)})
            bot.UserDataManager.set('presentation_mode',mode)
            restored=[build_history_message(row,bot.ArtifactManager.ROOT_DIR,folder) for row in rows]
            ai=[r for r in rows if r['msg_type']=='ai_reply']
            meta=[json.loads(r['metadata']) if isinstance(r['metadata'],str) else r['metadata'] for r in ai]
            final=[f for f in frames if f['type']=='record_snapshot' and f.get('record_id')==ai[-1]['id']][-1]
            cases.append({'name':f'{mode}/{stream}/{style}','mode':'folded','shown':shown,'refreshed':refreshed,
              'switched':switched,'restored':restored,'raw':[r['content'] for r in ai], 'expected_raw':raw,
              'metadata':meta,'final':final,'frames':frames,'subset':subset['messages'],'stats':len(await h.db.get_token_stats())})
    print(json.dumps(cases,ensure_ascii=False))
asyncio.run(main())
''')


def visible(messages):
    return [message for message in messages if not message.get('hidden') and message['msg_type']!='ui_message']


def test_two_model_replies_one_operation_and_one_round_line(turns):
    for case in turns:
        assert case['raw']==case['expected_raw'],case['name']
        assert case['stats']==2,case['name']
        summary=case['metadata'][-1]['presentation']['summary']
        assert (summary['rounds'],summary['operations'])==(2,1),case['name']
        if case['mode']=='folded':
            messages=visible(case['shown'])
            assert [m['msg_type'] for m in messages]==['user_text','ai_reply','agent_result','agent_status','ai_reply'],case['name']
            assert messages[3]['content']=='Agent 第 1 轮 · 1 个操作',case['name']
            assert '共执行 2 轮，1 个操作' in messages[-1]['content']
            assert '返回码' not in messages[2]['content']
            assert 'data-compact="1"' in messages[1]['content']


def test_retired_mode_writes_preserve_live_history_content_and_ids(turns):
    for case in turns:
        assert case['shown']==case['refreshed']==case['restored'],case['name']
        assert [m['id'] for m in case['shown']]==[m['id'] for m in case['switched']]
        assert case['shown']==case['switched'],case['name']
        final=case['final']
        matching=next(m for m in case['shown'] if m['id']==final['record_id'])
        assert final['text']==matching['content'],case['name']
        assert final['web_display']==matching['web_display'],case['name']
        assert [m['id'] for m in case['subset']]==[m['id'] for m in case['switched']],case['name']


def test_completed_turn_removes_every_stop_button_and_preserves_token_kind(turns):
    for case in turns:
        keyboards={}
        for frame in case['frames']:
            if frame['type']=='delete':
                keyboards.pop(frame.get('message_id'),None)
                continue
            if frame.get('message_id') is not None and 'reply_markup' in frame:
                keyboards[frame['message_id']]=frame['reply_markup'] or []
        assert all('停止回答' not in button['text'] for rows in keyboards.values() for row in rows for button in row),case['name']
        token_frames=[frame for frame in case['frames'] if frame['type']=='message' and frame.get('msg_type')=='token_usage']
        assert not token_frames,case['name']


def test_generation_styles_and_round_line_remain_distinct_in_every_iteration(turns):
    for case in turns:
        _,stream,style=case['name'].split('/')
        label='非流式输出中...' if stream=='False' else '后台流式输出中...' if style=='background' else '前台流式输出中...'
        placeholders=[frame for frame in case['frames'] if frame.get('text')==label and frame['type']=='message']
        assert len(placeholders)==2,case['name']
        assert all(frame.get('msg_type')=='ai_reply' for frame in placeholders),case['name']
        assert not any(frame.get('text')=='思考中…' for frame in case['frames']),case['name']
        rounds=[frame for frame in case['frames'] if frame['type']=='message' and frame.get('msg_type')=='agent_status']
        assert [frame['text'] for frame in rounds]==['Agent 第 1 轮 · 1 个操作'],case['name']


def test_short_tool_blocks_are_really_foldable_in_tui_and_plain_cli_stays_compact():
    text=operation_html({'kind':'run','success':True,'output':'one line'})
    screen=PtScreen(Palette(False),width=80)
    screen.print_block(screen.renderer().render_message(text,parse_mode='HTML'),message_id=1)
    assert len(screen.model.foldable_targets())==1
    assert 'one line' not in '\n'.join(line for line, _ in screen.model.render_rows())
    screen.model.toggle(*screen.model.foldable_targets()[0])
    assert 'one line' in '\n'.join(line for line, _ in screen.model.render_rows())
    assert 'one line' not in '\n'.join(MessageRenderer(Palette(False),width=80).render_text(text,'HTML'))


def test_missing_usage_is_unknown_and_failures_remain_visible():
    line=usage_line({'round':1,'usage':{'input_tokens':100},'elapsed_seconds':1})
    assert '↓ 未提供' in line and 'cache 未提供' in line
    assert '耗时未记录' in usage_line({'usage':{'input_tokens':100}})
    assert '思考 Token：未提供' in usage_details({'round':1,'usage':{}})
    failure=operation_html({'kind':'run','success':False,'return_code':1,'output':'permission denied'})
    assert failure.startswith('⚠️') and '返回码 1' in failure and 'permission denied' in failure


def test_new_replies_keep_response_timing_live_and_after_refresh(turns):
    for case in turns:
        for metadata in case['metadata']:
            elapsed=metadata['presentation']['elapsed_seconds']
            assert isinstance(elapsed,(float,int)) and elapsed>=0,case['name']
        for message in case['shown']:
            if message['msg_type']=='ai_reply':
                footer=next(segment['text'] for segment in message['web_display']['segments'] if segment.get('footer'))
                assert 'cache 2,130' in footer and '约 ' in footer,case['name']
                assert '未采集' not in footer and '耗时未记录' not in footer,case['name']
        final=case['final']
        footer=next(segment['text'] for segment in final['web_display']['segments'] if segment.get('footer'))
        assert 'cache 2,130' in footer and '约 ' in footer,case['name']


def test_independent_cli_namespace_reprojects_history_without_server_module(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from xgent_app import web_history
    monkeypatch.setattr(web_history,'_section_ns',lambda:None)
    namespace={'UserDataManager':SimpleNamespace(get=lambda key,default=None:'folded'),
               'markdown_to_telegram_html':lambda text:text}
    raw='好的。\n```run-x\n<<BEGIN_CLIMODE123456\nuptime\n<<END_CLIMODE123456\n```'
    record={'id':1,'role':'assistant','msg_type':'ai_reply','content':raw,
            'metadata':{'display':{'content':'legacy frozen snapshot','parse_mode':'HTML'}}}
    folded=web_history.build_history_message(record,tmp_path,tmp_path,presentation_namespace=namespace,presentation_mode='folded')
    detailed=web_history.build_history_message(record,tmp_path,tmp_path,presentation_namespace=namespace,presentation_mode='detailed')
    assert 'data-compact="1"' in folded['content']
    assert detailed==folded
    assert 'legacy frozen snapshot' not in folded['content']+detailed['content']
    assert record['content']==raw


def test_browser_retired_mode_events_preserve_expansion_and_reject_stale_snapshot(full_output_server, turns, tmp_path):
    from pathlib import Path
    playwright=pytest.importorskip('playwright.sync_api')
    case=next(item for item in turns if item['name']=='folded/False/foreground')
    server=full_output_server[0]
    base=f'http://127.0.0.1:{server.config.port}'
    mode=['folded']
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():pytest.skip('Chromium unavailable')
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':390,'height':844})
        context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(base) else route.abort())
        context.route('**/api/config',lambda route:route.fulfill(json={'values':{'presentation_mode':mode[0]},'options':{}}))
        def history(route):
            messages=case['shown'] if mode[0]=='folded' else case['switched']
            live=server.outbox.snapshot()
            route.fulfill(json={'messages':messages,'ui_generation':case['final']['ui_generation'],'busy':False,
                'presentation_mode':mode[0],'next_cursor':None,'live':{**live,'frames':[]}})
        context.route('**/api/workbench/history?*',history)
        assert context.request.post(base+'/api/login',data={'password':'temporary-test-only'}).ok
        page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            page.goto(base)
            page.locator('.token-hud').first.wait_for()
            assert page.locator('.msg-row').count()==5
            command=page.locator('blockquote.execution-block').first
            assert not command.locator('pre').is_visible()
            command.locator('.pb-toggle').click()
            page.wait_for_function("document.querySelector('blockquote.execution-block').classList.contains('expanded')")
            assert 'uptime' in command.locator('pre').inner_text()
            page.reload();page.locator('.token-hud').first.wait_for()
            assert page.locator('blockquote.execution-block').first.locator('pre').is_visible()
            mode[0]='detailed'
            server.outbox.put({'type':'presentation_changed','presentation_mode':'detailed'})
            page.wait_for_timeout(300)
            assert page.locator('[data-presentation-mode=detailed]').count()==0
            assert page.locator('.msg-row').count()==5
            assert page.locator('blockquote.execution-block').first.locator('pre').is_visible()
            output=page.locator('.msg-row.cmd .execution-block').first
            output.locator('.execution-row').click()
            output.get_by_role('button',name='查看执行信息').click()
            assert '返回码' in output.locator('.execution-details').inner_text()
            mode[0]='folded'
            server.outbox.put({'type':'presentation_changed','presentation_mode':'folded'})
            page.locator('.token-hud').first.wait_for()
            assert page.locator('.msg-row').count()==5
            assert page.locator('blockquote.execution-block').first.locator('pre').is_visible()
            stale={**case['final'],'presentation_mode':'detailed','text':'stale snapshot'}
            server.outbox.put(stale)
            page.wait_for_timeout(400)
            assert 'stale snapshot' not in page.locator('#log').inner_text()
            assert '共执行 2 轮，1 个操作' in page.locator('#log').inner_text()
            page.screenshot(path=str(tmp_path/'folded-mobile.png'),full_page=True)
            assert not errors
        finally:browser.close()
