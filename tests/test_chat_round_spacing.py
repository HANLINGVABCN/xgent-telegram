"""Real multi-round delivery: distinct generation styles and compact Web gaps."""
import json
from pathlib import Path

import pytest

from tests.test_external_sync import ProbeMixin
from tests.test_full_output_web import full_output_server  # noqa: F401


@pytest.fixture(scope='module')
def round_cases():
    return ProbeMixin().run_probe(r'''
import asyncio,json,tempfile
from unittest.mock import patch
import xgent_server as bot
from tests.attachment_request_probe import Harness
from xgent_app.web_history import build_history_message
async def main():
    cases=[]
    for stream,style in ((False,'foreground'),(True,'foreground'),(True,'background')):
        with tempfile.TemporaryDirectory() as folder:
            async with Harness(bot,folder) as h:
                bot.UserDataManager.set('agent_mode',True)
                bot.UserDataManager.set('stream_mode',stream)
                bot.UserDataManager.set('stream_style',style)
                replies=['第一轮，检查服务。\n```run-x\n<<BEGIN_SPACING123456\nprintf first\n<<END_SPACING123456\n```',
                         '第二轮，检查连接。\n```run-x\n<<BEGIN_SPACING234567\nprintf second\n<<END_SPACING234567\n```',
                         '两轮检查已结束，结果保留。']
                original=list(replies)
                def response(kwargs):
                    kwargs['usage_sink'].append({'input_tokens':4110,'output_tokens':86,'cached_tokens':3660})
                    return replies.pop(0)
                async def complete(*args,**kwargs):return response(kwargs),None
                async def chunks(*args,**kwargs):yield response(kwargs)
                async def operation(block,*args,**kwargs):
                    output='first result' if 'first' in block['body'] else 'second result'
                    return {'kind':'run','success':True,'return_code':0,'output':output,'notice':output,
                        'context_message':{'role':'user','content':output}}
                with patch.object(bot.ModelClient,'think_and_reply',complete),patch.object(bot.ModelClient,'think_and_reply_stream',chunks),patch.object(bot,'dispatch_standard_protocol',operation):
                    await h.turn('检查服务和连接')
                frames=h.drain_frames()
                rows=await h.db.get_display_history(0)
                shown=[build_history_message(row,bot.ArtifactManager.ROOT_DIR,folder) for row in rows]
                refreshed=[build_history_message(row,bot.ArtifactManager.ROOT_DIR,folder) for row in rows]
                cases.append({'name':f'{stream}/{style}','frames':frames,'messages':shown,'refreshed':refreshed,
                    'original':original,'raw':[row['content'] for row in rows if row['msg_type']=='ai_reply'],
                    'stats':len(await h.db.get_token_stats()),'ui_generation':next(frame['ui_generation'] for frame in frames if frame['type']=='record_snapshot')})
    print(json.dumps(cases,ensure_ascii=False))
asyncio.run(main())
''')


def test_each_operation_round_has_one_line_after_its_output_live_and_on_refresh(round_cases):
    for case in round_cases:
        assert case['messages']==case['refreshed'],case['name']
        assert case['original']==case['raw'] and case['stats']==3,case['name']
        shown=[message for message in case['messages'] if not message.get('hidden') and message['msg_type']!='ui_message']
        assert [message['msg_type'] for message in shown]==[
            'user_text','ai_reply','agent_result','agent_status','ai_reply','agent_result','agent_status','ai_reply'],case['name']
        assert [message['content'] for message in shown if message['msg_type']=='agent_status']==[
            'Agent 第 1 轮 · 1 个操作','Agent 第 2 轮 · 1 个操作'],case['name']
        live=[frame for frame in case['frames'] if frame['type']=='message' and frame.get('msg_type')=='agent_status']
        assert [frame['text'] for frame in live]==['Agent 第 1 轮 · 1 个操作','Agent 第 2 轮 · 1 个操作'],case['name']


def test_reply_output_round_and_next_reply_are_tightly_spaced(full_output_server, round_cases, tmp_path):
    playwright=pytest.importorskip('playwright.sync_api')
    server=full_output_server[0]
    base=f'http://127.0.0.1:{server.config.port}'
    case=round_cases[0]
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip('Chromium unavailable')
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(reduced_motion='reduce',permissions=['clipboard-read','clipboard-write'])
        context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(base) else route.abort())
        assert context.request.post(base+'/api/login',data={'password':'temporary-test-only'}).ok
        context.route('**/api/workbench/history?*',lambda route:route.fulfill(json={
            'messages':case['messages'],'ui_generation':case['ui_generation'],'busy':False,'next_cursor':None,
            'live':{**server.outbox.snapshot(),'frames':[]}}))
        context.route('**/api/config',lambda route:route.fulfill(json={'values':{},'options':{}}))
        page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        for width,height in ((1440,900),(390,844),(320,640)):
            page.set_viewport_size({'width':width,'height':height})
            for size in (16,24):
                if page.url!='about:blank':
                    page.evaluate('sessionStorage.removeItem("xgent-fold-state")')
                page.goto(base)
                page.locator('.agent-round-message').first.wait_for()
                page.evaluate('(size)=>{document.documentElement.style.setProperty("--web-message-font-size",size+"px");document.body.classList.add("dark");}',size)
                assert page.locator('.agent-round-message').count()==2
                assert page.locator('.bubble.ai .bubble-header:visible').count()==1
                gaps=page.locator('.agent-round-message').evaluate_all('''rows=>rows.map(row=>{
                    const before=node=>{node=node.previousElementSibling;while(node&&!node.classList.contains('msg-row'))node=node.previousElementSibling;return node;};
                    const previous=before(row);
                    const output=previous.querySelector('.execution-row').getBoundingClientRect();
                    const outputBlock=previous.querySelector('.execution-block').getBoundingClientRect();
                    const reply=before(previous);
                    const footer=reply.querySelector('.message-footer').getBoundingClientRect();
                    const round=row.querySelector('.body').getBoundingClientRect();
                    const next=row.nextElementSibling.querySelector('.body').getBoundingClientRect();
                    const style=getComputedStyle(row.querySelector('.bubble'));
                    const token=getComputedStyle(reply.querySelector('.token-hud'));
                    return {footerOutput:output.top-footer.bottom,outputRound:round.top-outputBlock.bottom,
                        roundReply:next.top-round.bottom,font:style.fontSize,lineHeight:style.lineHeight,
                        tokenFont:token.fontSize,tokenHeight:token.lineHeight};
                })''')
                for gap in gaps:
                    assert -1<=gap['footerOutput']<=6,gap
                    assert -1<=gap['outputRound']<=6,gap
                    assert -1<=gap['roundReply']<=8,gap
                    assert gap['font']==gap['tokenFont']=='11px',gap
                    assert gap['lineHeight']==gap['tokenHeight']=='17px',gap
                assert page.locator('.bubble.sys .msg-actions,.bubble.sys .bubble-header:visible').count()==0
                assert page.locator('.agent-round-message .message-footer').first.is_hidden()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                page.locator('#log').evaluate('(node)=>node.scrollTop=0')
                page.screenshot(path=str(tmp_path/f'round-spacing-{width}-{size}.png'))
                page.locator('.bubble.ai .copy-message-action').nth(1).click()
                assert page.evaluate('navigator.clipboard.readText()').replace('\r\n','\n')==case['original'][1]
                block=page.locator('.bubble.cmd .execution-block').first
                block.locator('.execution-row').focus();page.keyboard.press('Enter')
                assert block.locator('pre').is_visible()
                block.get_by_role('button',name='复制代码正文').click()
                assert page.evaluate('navigator.clipboard.readText()')=='first result'
                page.reload();page.locator('.agent-round-message').first.wait_for()
                assert page.locator('.bubble.cmd .execution-block').first.locator('pre').is_visible()
        assert not errors,errors
        browser.close()
