"""Isolated Mini App HTTP session and original Agent form continuation."""
import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
from unittest.mock import patch

from tests.attachment_request_probe import Harness
from xgent_app.agent_ask import PENDING_ASKS, SECRET_STORE
from xgent_app.web_server import WebChatConfig, WebChatServer
from xgent_app.web_auth import hash_password
from xgent_app.workbench import Workbench


def init_data(token, user_id=1):
    data={'auth_date':str(int(time.time())),'query_id':'mini-probe','user':json.dumps({'id':user_id})}
    key=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    data['hash']=hmac.new(key,'\n'.join(k+'='+data[k] for k in sorted(data)).encode(),hashlib.sha256).hexdigest()
    return urlencode(data)


async def exercise(bot, root):
    async with Harness(bot,root) as h:
        bot.UserDataManager.set('agent_mode',True)
        calls=[]
        form='Q1|single|选择\n- 一\n- 二\n+other\nQ2|text|备注\nQ3|secret:MINI_TEST_KEY|密钥'
        async def complete(*args,**kwargs):
            calls.append(args[5])
            return ('```ask-x\n<<BEGIN_MINIASK123456\n'+form+'\n<<END_MINIASK123456\n```' if len(calls)==1 else '继续完成。'),None
        service=Workbench(vars(bot))
        config=WebChatConfig(host='127.0.0.1',port=0,password_hash=hash_password('mini-test-only'),
            bot_token='123456:mini-test-only',authorized_user_id=1,loop=asyncio.get_running_loop(),
            submit_message=lambda *args:None,read_history=bot._web_read_history,read_settings=bot._web_read_settings,
            write_setting=bot._web_write_setting,request_stop=bot._web_request_stop,is_busy=bot._web_is_busy,
            read_conversations=bot.get_conversations().state,workbench=service.handle)
        server=WebChatServer(config);server.outbox=h.outbox;server.start()
        base='http://127.0.0.1:'+str(server._httpd.server_address[1])
        opener=build_opener(ProxyHandler({}))
        def fetch(path,body=None,token=None):
            headers={'Origin':base,'Content-Type':'application/json'}
            if token:headers['Authorization']='Bearer '+token
            request=Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
            try:
                with opener.open(request,timeout=10) as response:
                    content=response.read().decode()
                    return response.status,json.loads(content) if 'application/json' in response.headers['Content-Type'] else content
            except HTTPError as error:
                return error.code,json.loads(error.read())
        async def request(*args,**kwargs):return await asyncio.to_thread(fetch,*args,**kwargs)
        try:
            with patch.object(bot.ModelClient,'think_and_reply',complete):
                await h.turn('发起独立表单')
                pending=next(iter(PENDING_ASKS._store.values()))
                resource={'page':'form','id':pending.ask_id,'conversation_id':pending.conversation_id,
                          'init_data':init_data(config.bot_token)}
                assert (await request('/api/mini/session',{**resource,'init_data':init_data(config.bot_token,2)}))[0]==401
                code,session=await request('/api/mini/session',resource)
                assert code==200,(code,session)
                token=session['token']
                assert (await request('/mini/form'))[0]==200
                assert (await request('/api/mini/form',token=token))[0]==200
                assert (await request('/api/term/sessions',token=token))[0]==401
                assert (await request('/api/workbench/settings',token=token))[0]==401
                assert (await request('/api/mini/form',{'id':'another','revision':0,'draft':{}},token))[0]==403
                draft={'0':{'custom':'自定义答案','selected':[]},'1':{'custom':'备注 原文  两个空格'}}
                code,saved=await request('/api/mini/form',{'revision':0,'action':'save','draft':draft},token)
                assert code==200,(code,saved)
                assert (await request('/api/mini/form',token=token))[1]['draft']['1']['custom']=='备注 原文  两个空格'
                code,submitted=await request('/api/mini/form',{'revision':saved['revision'],'action':'submit',
                    'draft':draft,'secrets':{'2':'NEVER_STORE_MINI_TEST_SECRET'}},token)
                assert code==200 and submitted['status']=='submitted',(code,submitted)
                for _ in range(100):
                    if len(calls)==2 and not service._background:break
                    await asyncio.sleep(.02)
                code,retry=await request('/api/mini/form',{'revision':saved['revision'],'action':'submit','draft':draft},token)
                assert code==200 and retry['status']=='submitted',(code,retry)
                assert len(calls)==2
                assert (await request('/api/mini/form',token=token))[1]['status']=='submitted'
                rows=await h.db.get_global_messages(100)
                encoded=json.dumps(rows,ensure_ascii=False)
                assert 'NEVER_STORE_MINI_TEST_SECRET' not in encoded and 'NEVER_STORE_MINI_TEST_SECRET' not in json.dumps(calls)
                assert SECRET_STORE.has(pending.conversation_id,'MINI_TEST_KEY')
                assert not any(row.get('msg_type')=='user_text' and row['content']=='自定义答案' for row in rows)
                return {'scoped':True,'refresh':True,'submit_once':True,'secret_safe':True,'page':True}
        finally:
            if service._background:await asyncio.gather(*service._background,return_exceptions=True)
            await asyncio.to_thread(server.stop)


async def disconnected_submit(bot, root):
    from xgent_app import web_ask
    async with Harness(bot,root) as h:
        bot.UserDataManager.set('agent_mode',True)
        calls=[]
        async def complete(*args,**kwargs):
            calls.append(args[5]);return ('```ask-x\n<<BEGIN_DISCONNECT123\nQ1|text|输入\n<<END_DISCONNECT123\n```' if len(calls)==1 else '继续。'),None
        service=Workbench(vars(bot))
        entered,release=asyncio.Event(),asyncio.Event()
        original=web_ask.sync_ask
        async def slow_sync(*args,**kwargs):
            if not entered.is_set():entered.set();await release.wait()
            return await original(*args,**kwargs)
        with patch.object(bot.ModelClient,'think_and_reply',complete):
            await h.turn('开始')
            pending=next(iter(PENDING_ASKS._store.values()))
            data={'id':pending.ask_id,'conversation_id':pending.conversation_id,'action':'submit',
                  'revision':0,'draft':{'0':{'custom':'唯一答案'}}}
            with patch.object(web_ask,'sync_ask',slow_sync):
                caller=asyncio.create_task(service.handle('POST','mini-ask',data))
                await entered.wait();caller.cancel()
                try:await caller
                except asyncio.CancelledError:pass
                release.set()
                for _ in range(200):
                    if len(calls)==2 and not service._background:break
                    await asyncio.sleep(.01)
                snapshot=await service.handle('POST','mini-ask',data)
                assert snapshot['status']=='submitted' and len(calls)==2
            return {'submitted':True,'resumed_once':len(calls)==2}
