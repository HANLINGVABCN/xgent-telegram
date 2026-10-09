"""Isolated, deterministic workspace preview. Never starts Telegram, models or task runners."""
import argparse
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

async def serve(port, count, conversation_count=0):
    from xgent_app.bootstrap import load_sections
    from xgent_app.web_auth import hash_password
    from xgent_app.web_server import WebChatConfig, WebChatServer
    from xgent_app.workbench import Workbench
    from xgent_app.agent_presenter import build_run_presentation
    from xgent_app.protocols import ProtocolParser
    os.environ['BOT_TOKEN'] = ''
    os.environ['AUTHORIZED_USER_ID'] = '1'
    os.environ['XGENT_TRACE_LOG_FILE'] = str(Path.cwd()/'trace.log')
    ns = {'__file__': str(Path.cwd()/'xgent_server.py')}
    with contextlib.redirect_stdout(io.StringIO()):
        load_sections(ns)
        await ns['UserDataManager'].init()
    import types
    module=types.ModuleType('xgent_server');module.__dict__.update(ns);sys.modules['xgent_server']=module
    db = await ns['BotMemoryDB'].get_instance()
    for name,base,models,fmt in [('OpenAI','https://api.openai.com/v1',['gpt-4.1','gpt-4.1-mini'],'openai'),
                                ('Anthropic','https://api.anthropic.com',['claude-sonnet-4'],'claude'),
                                ('Local models','http://127.0.0.1:11434/v1',['qwen3'],'openai_compatible')]:
        await db.save_provider(name,base,'fixture-not-a-real-key',models,fmt)
    await ns['UserDataManager'].reload_providers()
    await ns['save_model_target_selection']('chat','OpenAI','gpt-4.1')
    ns['UserDataManager'].set('agent_mode', True)
    skill_dir=Path(ns['SKILL_PUBLIC_DIR']);skill_dir.mkdir(exist_ok=True)
    for name,description in [('代码审查','检查实际缺陷，提供可复现的验证。'),('部署助手','检查部署配置和服务状态。'),('数据分析','从结构化数据中提炼有用的信息。')]:
        (skill_dir/(name+'.md')).write_text('# '+name+'\n\n'+description,encoding='utf-8')
    output=Path(ns['COMMAND_OUTPUT_DIR'])/'2026-10-07';output.mkdir(parents=True,exist_ok=True)
    log=output/'review.txt';log.write_text('Review completed\n'+'All checks passed. 中文输出完整保留。\n'*4000,encoding='utf-8')
    now=time.time()
    conn=await db._get_conn()
    for i in range(max(0,count-4)):
        await conn.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id) VALUES(1,1,?,?,?,?,'global_memory')",
                           ('user_text' if i%2==0 else 'ai_reply','user' if i%2==0 else 'assistant',f'历史记录 {i}：开发过程与验证结果。',now-10000+i))
    raw='```run-x\n<<BEGIN_PREVIEW1234\n'+'\n'.join('test_case_%03d ... passed'%i for i in range(85))+'\n<<END_PREVIEW1234\n```'
    records=[('user_text','user','帮我检查项目的部署状态，整理需要关注的问题。',None),
             ('ai_reply','assistant','## 部署检查完成\n\n核心服务运行正常。我检查了启动配置、端口与最近的执行记录。\n\n| 检查项目 | 状态 | 说明 |\n|:---|:---|:---|\n| 应用服务 | 正常 | 进程健康，启动配置一致 |\n| 数据库 | 正常 | WAL 模式，连接可用 |\n| 网络通道 | 需关注 | Telegram 偶尔重连 |\n\n### 建议\n\n- 保留当前可用版本，先验证再发布。\n- 关注通道状态，不要把网络抖动误判为应用退出。\n\n'+raw, None),
             ('agent_result','assistant','[Agent run]\n检查完成',{'display':{'content':build_run_presentation({'success':True,'return_code':0,'output':'85 checks passed · no critical issues','output_path':str(log)}),'parse_mode':'HTML'}}),
             ('ai_reply','assistant','所有检查已经结束。完整日志保存在输出中心，你可以继续检查细节，或创建一个定时健康检查任务。',None)]
    for i,(kind,role,text,meta) in enumerate(records):
        await conn.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,metadata,session_id) VALUES(1,1,?,?,?,?,?,'global_memory')",(kind,role,text,now-240+i*30,json.dumps(meta) if meta else None))
    storage=Path(ns['ArtifactManager'].ROOT_DIR);uploads=storage/'uploads';uploads.mkdir(parents=True,exist_ok=True)
    document=uploads/'部署报告.md';document.write_text('# 检查报告\n\n所有服务正常。\n'+'中文测试报告\n'*12000,encoding='utf-8')
    meta={'attachments':[{'version':1,'path':'部署报告.md','filename':'部署报告.md','mime_type':'text/markdown','storage':'uploads'}]}
    await conn.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,metadata,session_id) VALUES(1,1,?,?,?,?,?,'global_memory')",('user_file','user','报告附件',now-300,json.dumps(meta)))
    for i,(summary,status) in enumerate([('每日服务健康检查','scheduled'),('日志异常持续监控','running'),('生成项目备份','completed'),('外部接口连接检查','failed')]):
        await db.create_trigger_task({'id':f'trg_fixture{i}','chat_id':1,'conversation_id':'global_memory','command':'echo preview-only',
           'summary':summary,'schedule_type':'cron','schedule_expr':'0 9 * * *','timezone':'Asia/Shanghai','next_run_at':now+3600*(i+1),
           'status':status,'created_at':now-86400*i,'updated_at':now,'fire_count':i*6})
    for day in range(7):
        for i in range(12+day*2):
            await db.add_token_stat(['gpt-4.1','claude-sonnet-4','qwen3'][i%3], {'input_tokens':2400+day*100,'output_tokens':1300+day*50,'total_tokens':3700+day*150},now-day*86400-i*120)
    await ns['UserDataManager'].save_config('model_price_table',{'default':{'input':2,'output':8,'cached':.5}})
    if conversation_count:
        await db.update_session('global_memory', name='检查项目部署状态')
        titles = ['排查 Telegram 网络连接', '给 Python 项目补充测试', '整理本周的开发任务',
                  '设计多对话管理界面', '更新服务器备份策略', '优化消息同步的延迟',
                  '分析上周的模型调用成本', '整理项目发布说明', '配置自动化健康检查',
                  '讨论数据库迁移方案', '阅读部署日志中的错误', '编写文件上传功能',
                  '规划下一阶段的开发', '一些随手记下的想法']
        for index in range(conversation_count):
            name = titles[index % len(titles)] + ((' · ' + str(index + 1)) if index >= len(titles) else '')
            cid = (await db.manage_conversation('create', name=name))['conversation_id']
            at = now - ((0, 0, 1, 1, 2, 3, 5, 6, 9, 15, 20, 35, 40, 50)[index % 14] * 86400) - index * 300 - 600
            await conn.execute('UPDATE chat_sessions SET created_at=?,last_active=? WHERE id=?', (at, at, cid))
            await conn.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id) VALUES(1,1,'user_text','user',?,?,?)", (name, at, cid))
            if index >= conversation_count - 2:
                await db.manage_conversation('archive', cid)
        await db.manage_conversation('switch', 'global_memory')
    service=Workbench(ns)
    async def history(limit):return (await service.handle('GET','history',{'limit':limit}))['messages']
    config=WebChatConfig(host='127.0.0.1',port=port,password_hash=hash_password('preview-only'),bot_token='',authorized_user_id=1,
        loop=asyncio.get_running_loop(),submit_message=lambda *args,**kwargs:None,read_history=history,
        submit_callback=ns['_web_submit_callback'],submit_ui_callback=ns['_web_submit_ui_callback'],submit_command=ns['_web_submit_command'],
        read_settings=ns['_web_read_settings'],write_setting=ns['_web_write_setting'],request_stop=lambda:None,is_busy=lambda:False,
        read_health=lambda:{'components':{'web':{'state':'up'},'telegram':{'state':'degraded','error':'演示：连接重试中'},'triggers':{'state':'up'}}},
        media_allowed_roots=[ns['ArtifactManager'].ROOT_DIR],is_terminal_enabled=lambda:True,workbench=service.handle)
    server=WebChatServer(config);server.start()
    ns['_web_chat_server']=server
    ns['get_conversations']().start()
    print(json.dumps({'url':f'http://127.0.0.1:{server._httpd.server_address[1]}','password':'preview-only'}),flush=True)
    try:await asyncio.Event().wait()
    finally:server.stop();await ns['get_conversations']().close();await db.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--port',type=int,default=0);parser.add_argument('--messages',type=int,default=4)
    parser.add_argument('--conversations',type=int,default=0)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='xgent-workbench-') as temporary:
        os.chdir(temporary)
        try:asyncio.run(serve(args.port,args.messages,args.conversations))
        except KeyboardInterrupt:pass
