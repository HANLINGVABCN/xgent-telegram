"""Actual turn model selection, queued input ordering and live Web controls."""
import json
from pathlib import Path
import unittest

import pytest

from tests.test_external_sync import ProbeMixin
from tests.test_full_output_web import full_output_server  # noqa: F401
from xgent_app.web_display import build_web_display


class Probe(ProbeMixin, unittest.TestCase):
    pass


def test_three_way_response_setting_rolls_back_on_partial_database_failure():
    result=Probe().run_probe(r'''
import asyncio,tempfile
import xgent_server as bot
from tests.attachment_request_probe import Harness
async def main():
    with tempfile.TemporaryDirectory() as root:
        async with Harness(bot,root) as h:
            await bot._web_write_setting('response_mode','nonstream')
            async with h.db._write() as conn:
                await conn.execute("CREATE TRIGGER reject_style BEFORE INSERT ON config WHEN NEW.key='stream_style' AND NEW.value='\"background\"' BEGIN SELECT RAISE(ABORT,'fixture write failure'); END")
            failed=False
            try:await bot._web_write_setting('response_mode','background')
            except Exception:failed=True
            values=(await bot._web_read_settings())['values']
            saved=[await h.db.get_config_fresh(key) for key in ('stream_mode','stream_style')]
            invalid=False
            try:await bot._web_write_setting('response_mode','invalid')
            except ValueError:invalid=True
            print(json.dumps({'failed':failed,'saved':saved,'memory':[values['stream_mode'],values['stream_style']],
                'response_mode':values['response_mode'],'invalid':invalid}))
asyncio.run(main())
''')
    assert result=={'failed':True,'saved':[False,'foreground'],'memory':[False,'foreground'],
                    'response_mode':'nonstream','invalid':True}


@pytest.fixture(scope='module')
def queued_model_cases():
    return Probe().run_probe(r'''
import asyncio,json,tempfile
from unittest.mock import patch
import xgent_server as bot
from tests.attachment_request_probe import Harness, MODEL
from xgent_app.message_queue import MessageQueue
from xgent_app.web_history import build_history_message
async def main():
    cases=[]
    for style in ('foreground','background','nonstream'):
        with tempfile.TemporaryDirectory() as root:
            async with Harness(bot,root) as h:
                manager=bot.get_conversations()
                manager.text_queue=MessageQueue(manager,lambda batch:None)
                await manager.text_queue.start()
                await h.db.save_provider('next-provider','https://invalid.example/v1','fixture-key',['next-model'])
                await bot.UserDataManager.reload_providers()
                await bot._web_write_setting('response_mode',style)
                bot.UserDataManager.set('agent_mode',True)
                calls=[]
                replies=['开始检查。\n```run-x\n<<BEGIN_QUEUECTRL123456\necho fixture\n<<END_QUEUECTRL123456\n```',
                         '本轮接入排队消息，原模型继续。','新请求使用新模型。']
                def response(args,kwargs):
                    calls.append([args[0],args[3]])
                    kwargs['usage_sink'].append({'input_tokens':10,'output_tokens':3,'cached_tokens':0})
                    return replies.pop(0)
                async def complete(*args,**kwargs):return response(args,kwargs),None
                async def chunks(*args,**kwargs):yield response(args,kwargs)
                async def operation(*args,**kwargs):
                    await manager.text_queue.submit('排队消息内容',chat_id=1)
                    await bot._web_write_setting('chat_model','next-provider|next-model')
                    return {'kind':'run','success':True,'return_code':0,'output':'command result','notice':'command result',
                        'context_message':{'role':'user','content':'command result'}}
                with patch.object(bot.ModelClient,'think_and_reply',complete),patch.object(bot.ModelClient,'think_and_reply_stream',chunks),patch.object(bot,'dispatch_standard_protocol',operation):
                    await h.turn('第一次新请求')
                    frames=h.drain_frames()
                    rows=await h.db.get_display_history(0)
                    messages=[build_history_message(row,bot.ArtifactManager.ROOT_DIR,root) for row in rows]
                    await h.turn('第二次新请求')
                cases.append({'style':style,'calls':calls,'initial_model':MODEL,'frames':frames,'messages':messages,
                    'user_text':[row['content'] for row in rows if row['msg_type']=='user_text']})
    print(json.dumps(cases,ensure_ascii=False))
asyncio.run(main())
''')


def test_running_agent_keeps_model_and_places_round_before_queued_input(queued_model_cases):
    for case in queued_model_cases:
        assert case['calls']==[['p',case['initial_model']],['p',case['initial_model']],['next-provider','next-model']],case
        messages=[message for message in case['messages'] if not message.get('hidden') and message['msg_type']!='ui_message']
        assert [message['msg_type'] for message in messages]==[
            'user_text','ai_reply','agent_result','agent_status','user_text','ai_reply'],case['style']
        assert messages[3]['content']=='Agent 第 1 轮 · 1 个操作'
        assert messages[4]['content']=='排队消息内容'
        assert messages[1]['run_id']==messages[3]['run_id']==messages[4]['run_id']==messages[5]['run_id']
        assert messages[1]['ai_generation']['model']==messages[5]['ai_generation']['model']==case['initial_model']
        frames=case['frames']
        status=next(index for index,frame in enumerate(frames) if frame['type']=='message' and frame.get('msg_type')=='agent_status')
        queued=next(index for index,frame in enumerate(frames) if frame.get('text')=='排队消息内容')
        assert status<queued,case['style']
        assert case['user_text']==['第一次新请求','排队消息内容']


def test_live_model_header_system_menu_and_stop_target(full_output_server,tmp_path):
    playwright=pytest.importorskip('playwright.sync_api')
    server=full_output_server[0]
    base=f'http://127.0.0.1:{server.config.port}'
    cid='A'; generation=7; run_id='live-run-A'
    modes=['前台流式输出中...','后台流式输出中...','非流式输出中...']
    body='最终正文，保留原文。'
    display=build_web_display(body,presentation={'usage':{'input_tokens':10,'output_tokens':3,'cached_tokens':0},'elapsed_seconds':1})
    record={'id':2,'role':'assistant','msg_type':'ai_reply','content':body,'timestamp':1793275200,
            'ai_generation':{'provider':'p','model':'current-model','generated_at':1793275200},
            'run_id':run_id,'conversation_id':cid,'web_display':display}
    initial={'id':1,'role':'user','msg_type':'user_text','content':'开始任务','timestamp':1793275190}
    history_state={'saved':False}
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():pytest.skip('Chromium unavailable')
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(reduced_motion='reduce')
        context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(base) else route.abort())
        assert context.request.post(base+'/api/login',data={'password':'temporary-test-only'}).ok
        context.route('**/api/workbench/history?*',lambda route:route.fulfill(json={
            'conversation_id':cid,'ui_generation':generation,'busy':False,'queue_enabled':True,'next_cursor':None,
            'messages':[initial,record] if history_state['saved'] else [initial],
            'live':{**server.outbox.snapshot(),'frames':[]}}))
        context.route('**/api/config',lambda route:route.fulfill(json={'values':{},'options':{}}))
        context.route('**/api/queue?*',lambda route:route.fulfill(json={
            'conversation_id':cid,'generation':generation,'revision':0,'items':[],'paused':False}))
        stops=[]
        context.route('**/api/stop',lambda route:(stops.append(route.request.post_data_json),route.fulfill(json={'ok':True})))
        page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        for index,(width,height) in enumerate(((1440,900),(390,844),(320,640))):
            page.set_viewport_size({'width':width,'height':height})
            history_state['saved']=False
            page.goto(base)
            page.locator('[data-record-id="1"]').wait_for()
            assert page.locator('#btn-stop').is_hidden()
            server.outbox.put({'type':'message','conversation_id':cid,'run_id':run_id,'message_id':91,
                'text':modes[index],'msg_type':'ai_reply','ai_generation':record['ai_generation'],
                'reply_markup':[[{'text':'停止回答','callback_data':'act_stop_generation:'+run_id}]]})
            page.get_by_text(modes[index],exact=True).wait_for()
            assert page.locator('.bubble.ai .bubble-header .ai-generation-meta').inner_text()=='p / current-model'
            assert page.locator('.bubble.ai .reply-timestamp').is_hidden()
            assert page.locator('.footer-meta .ai-generation-meta').count()==0
            assert '历史记录未记录模型' not in page.locator('#log').inner_text()
            page.locator('#btn-stop').wait_for(state='visible')
            assert page.locator('#btn-send').is_visible()
            bounds=page.locator('#btn-stop').bounding_box()
            assert bounds['x']>=0 and bounds['x']+bounds['width']<=width,bounds
            page.locator('#btn-stop').click()
            assert stops[-1]=={'conversation_id':cid,'run_id':run_id}
            server.outbox.put({**record,'type':'record_snapshot','record_id':2,'text':body,'ui_generation':generation,
                              'replace_message_ids':[91]})
            page.locator('[data-record-id="2"]').wait_for()
            assert page.locator('[data-record-id="2"] .reply-timestamp').is_visible()
            assert page.locator('.bubble.ai .bubble-header .ai-generation-meta:visible').count()==1
            assert page.locator('#btn-stop').is_visible()
            # Durable system menus keep only their dedicated controls.
            server.outbox.put({'type':'message','conversation_id':cid,'run_id':run_id,'msg_type':'ui_message',
                'ui_message_id':'system-menu','ui_generation':generation,'revision':1,'text':'专属系统提示',
                'ai_generation':record['ai_generation'],'reply_markup':[[{'text':'查看设置','callback_data':'cmd_config'}]]})
            menu=page.locator('[data-ui-message-id="system-menu"]');menu.wait_for()
            assert menu.locator('.ai-generation-meta,.reply-timestamp,.msg-actions').count()==0
            assert menu.get_by_role('button',name='查看设置',exact=True).count()==1
            for kind,text in [('system_op','系统已保存'),('runtime_error','⚠️ 操作失败'),('agent_status','Agent 第 1 轮 · 1 个操作')]:
                page.evaluate('''message=>window.XGentChat.renderRecord(message,document.getElementById('log'))''',
                    {'id':kind,'role':'assistant','msg_type':kind,'content':text,'timestamp':1793275200,
                     'ai_generation':record['ai_generation']})
                system=page.locator('.msg-row').filter(has=page.get_by_text(text,exact=True))
                assert system.locator('.ai-generation-meta,.reply-timestamp,.msg-actions').count()==0
            server.outbox.put({'type':'generation_end','conversation_id':cid,'run_id':run_id})
            page.locator('#btn-stop').wait_for(state='hidden')
            # A delayed old reply cannot revive a completed run's stop button.
            server.outbox.put({'type':'edit','conversation_id':cid,'run_id':run_id,'message_id':91,
                              'text':body,'msg_type':'ai_reply'})
            page.wait_for_timeout(80)
            assert page.locator('#btn-stop').is_hidden()
            next_run='next-run-A'
            server.outbox.put({'type':'message','conversation_id':cid,'run_id':next_run,'message_id':92,
                              'text':'下一次任务正在生成','msg_type':'ai_reply'})
            page.get_by_text('下一次任务正在生成',exact=True).wait_for()
            page.locator('#btn-stop').wait_for(state='visible')
            server.outbox.put({'type':'turn_end','conversation_id':cid,'run_id':run_id})
            page.wait_for_timeout(80)
            assert page.locator('#btn-stop').is_visible()
            page.locator('#btn-stop').click()
            assert stops[-1]=={'conversation_id':cid,'run_id':next_run}
            server.outbox.put({'type':'generation_end','conversation_id':cid,'run_id':next_run})
            page.locator('#btn-stop').wait_for(state='hidden')
            history_state['saved']=True
            page.reload();page.locator('[data-record-id="2"]').wait_for()
            assert page.locator('[data-record-id="2"] .bubble-header .ai-generation-meta').inner_text()=='p / current-model'
            assert page.locator('#btn-stop').is_hidden()
            page.screenshot(path=str(tmp_path/f'header-stop-{width}.png'))
        assert not errors,errors
        browser.close()
