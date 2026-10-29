"""Approved single-line records, full code and inline controls on the real Web shell."""
import html
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_full_output_web import full_output_server  # noqa: F401
from tests.test_presentation_modes import turns  # noqa: F401
from tests.test_external_sync import ProbeMixin
from xgent_app.web_display import build_web_display, build_operation_display, operation_state


def test_protocol_names_and_recorded_states_preserve_original_code():
    commands = [('run-x', 'run', '', 'printf "A  B"\n  echo done'),
                ('file-x:workspace/report.md', 'file', 'workspace/report.md', '# Report\n\n  indented')]
    for tag, kind, path, body in commands:
        raw = f'```{tag}\n<<BEGIN_ROWTEST123456\n{body}\n<<END_ROWTEST123456\n```'
        for mode in ('folded', 'detailed'):
            operation = {'kind': kind, 'path': path, 'command': body, 'state': 'success'}
            display = build_web_display(raw, mode=mode, presentation={'operations': [operation]})
            block = display['segments'][0]['text']
            assert f'data-protocol="{kind}-x"' in block
            assert 'data-state="success"' in block
            assert '<pre>' + html.escape(body, quote=False) + '</pre>' in block
            assert html.escape(raw, quote=True).replace('\n', '&#10;') in block
        pending = build_web_display(raw, mode='folded')['segments'][0]['text']
        assert 'data-state="pending"' in pending
        assert raw == f'```{tag}\n<<BEGIN_ROWTEST123456\n{body}\n<<END_ROWTEST123456\n```'
    assert operation_state({'success': True}) == 'success'
    assert operation_state({'success': False}) == 'failed'
    assert operation_state({'success': False, 'state': 'stopped'}) == 'stopped'
    assert operation_state({}) == 'unknown'


def test_stop_during_operation_never_records_false_completion():
    result = ProbeMixin().run_probe(r'''
import asyncio, json, tempfile
from unittest.mock import patch
import xgent_server as bot
from tests.attachment_request_probe import Harness
from xgent_app.web_history import build_history_message
async def main():
    results=[]
    for mode in ('folded','detailed'):
        with tempfile.TemporaryDirectory() as root:
            async with Harness(bot,root) as h:
                await bot.UserDataManager.save_config('presentation_mode',mode)
                bot.UserDataManager.set('agent_mode',True)
                reply='Checking.\n```run-x\n<<BEGIN_STOPROW123456\nfixture-command\n<<END_STOPROW123456\n```'
                async def model(*args,**kwargs):
                    kwargs['usage_sink'].append({'input_tokens':100,'output_tokens':20,'cached_tokens':50})
                    return reply,None
                async def operation(*args,**kwargs):
                    bot.get_or_create_stop_event().set()
                    return {'kind':'run','success':False,'stopped':True,'return_code':-1,
                        'output':'partial output','notice':'partial output',
                        'context_message':{'role':'user','content':'partial output'}}
                with patch.object(bot.ModelClient,'think_and_reply',model),patch.object(bot,'dispatch_standard_protocol',operation):
                    await h.turn('stop during command')
                rows=await h.db.get_display_history(0)
                messages=[build_history_message(row,bot.ArtifactManager.ROOT_DIR,root) for row in rows]
                status=[r['content'] for r in rows if r['msg_type']=='agent_status']
                source=next(m for m in messages if m['msg_type']=='ai_reply')
                state=source['web_display']['segments'][1]['text']
                results.append({'mode':mode,'status':status,'source_state':state,
                    'visible_stop':sum('⏹ 已停止，已有结果保留。' in m['content'] for m in messages if not m.get('hidden')),
                    'stats':len(await h.db.get_token_stats())})
    print(json.dumps(results))
asyncio.run(main())
''')
    for case in result:
        assert not any('已完成' in status for status in case['status']), case
        assert sum('⏹ 已停止，已有结果保留。' in s for s in case['status']) == 1, case
        assert 'data-state="stopped"' in case['source_state'], case
        assert case['visible_stop'] == 1, case
        assert case['stats'] == 1


def test_real_shell_single_rows_inline_actions_and_copying(full_output_server, turns, tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    case = next(c for c in turns if c['name'] == 'folded/False/foreground')
    server = full_output_server[0]
    base = f'http://127.0.0.1:{server.config.port}'
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip('Chromium unavailable')
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(permissions=['clipboard-read','clipboard-write'])
        context.route('**/*', lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())
        assert context.request.post(base+'/api/login', data={'password':'temporary-test-only'}).ok
        mode = ['folded']
        def history(route):
            source = case['shown'] if mode[0] == 'folded' else case['switched']
            messages = json.loads(json.dumps(source))
            for m in messages:
                if m['msg_type']=='ai_reply':
                    m['conversation_actions']={'branch_mode':'assistant'}
            output = next(m for m in messages if m['msg_type']=='agent_result')
            output['web_display'] = build_operation_display(
                {'kind':'run','success':True,'return_code':0,'output':'line one\n  line two\nline three\nlast line',
                 'output_path':'fixture.txt'}, mode[0])
            live=server.outbox.snapshot()
            route.fulfill(json={'messages':messages,'ui_generation':case['final']['ui_generation'],
                'presentation_mode':mode[0],'busy':False,'next_cursor':None,'live':{**live,'frames':[]}})
        context.route('**/api/workbench/history?*',history)
        context.route('**/api/config',lambda r:r.fulfill(json={'values':{'presentation_mode':mode[0]},'options':{}}))
        context.route('**/api/output/page',lambda r:r.fulfill(json={
            'filename':'fixture.txt','offset':0,'next_offset':5,'size':5,'eof':True,'text':'FULL OUTPUT'}))
        page=context.new_page();errors=[];page.on('pageerror',lambda err:errors.append(str(err)))
        for width,height in [(1440,900),(390,844),(320,640)]:
            page.set_viewport_size({'width':width,'height':height})
            for current in ('folded','detailed'):
                mode[0]=current
                page.goto(base)
                page.locator('.token-hud').first.wait_for()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                first=page.locator('.execution-block').first
                assert 'run-x 已运行' in first.locator('.execution-label').inner_text()
                assert first.locator('.execution-preview').inner_text()=='uptime'
                assert first.locator('.execution-code').is_hidden()
                assert first.locator('.code-btn').first.is_hidden()
                assert first.locator('.execution-row').bounding_box()['height']<=31
                assert page.locator('.bubble.cmd .msg-actions,.bubble.sys .msg-actions').count()==0
                assert page.locator('.bubble.sys .bubble-header:visible,.bubble.cmd .bubble-header:visible').count()==0
                page.locator('.bubble.ai .copy-message-action').first.click()
                assert page.evaluate('navigator.clipboard.readText()').replace('\r\n','\n')==case['expected_raw'][0]
                for footer in page.locator('.bubble.ai .message-footer').all():
                    meta=footer.locator('.footer-meta').bounding_box();actions=footer.locator('.msg-actions').bounding_box()
                    assert meta and actions and actions['x']>=meta['x']+meta['width']-1
                    assert abs(meta['y']-actions['y'])<=4
                if width<768:
                    more=page.locator('.bubble.ai .more-message-actions').first
                    more.click();page.get_by_role('menuitem',name='引用XGent回复').click()
                    assert page.locator('#input').input_value().startswith('> ')
                    page.locator('#input').fill('')
                first.locator('.execution-row').focus();page.keyboard.press('Enter')
                assert first.locator('pre').is_visible()
                first.get_by_role('button',name='复制代码正文').click()
                assert page.evaluate('navigator.clipboard.readText()')=='uptime'
                first.get_by_role('button',name='复制完整协议').click()
                assert page.evaluate('navigator.clipboard.readText()').replace('\r\n','\n')==case['expected_raw'][0].split('```run-x',1)[1].split('```',1)[0].join(['```run-x','```'])
                assert first.locator('pre').is_visible()
                first.locator('.execution-row').focus();page.keyboard.press('Enter')
                assert first.locator('pre').is_hidden()
                output=page.locator('.bubble.cmd .execution-block').first
                assert output.locator('.execution-dot').get_attribute('class').endswith('state-success')
                if not output.get_attribute('class').split().count('expanded'):
                    output.locator('.execution-row').click();page.wait_for_timeout(300)
                assert output.locator('.execution-pre').evaluate('(n)=>getComputedStyle(n).fontSize')=='12px'
                assert output.locator('.execution-pre').evaluate('(n)=>getComputedStyle(n).lineHeight')=='17px'
                assert output.get_by_role('button',name='查看完整输出').count()==1, output.inner_html()
                output.get_by_role('button',name='查看完整输出').click()
                page.get_by_text('FULL OUTPUT',exact=True).wait_for()
                page.screenshot(path=str(tmp_path/f'approved-{width}-{current}.png'))
        assert not errors,errors
        browser.close()
