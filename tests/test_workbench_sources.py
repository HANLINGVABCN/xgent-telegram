"""Resource provenance/filter tests use real temporary SQLite, not deployment data."""
import unittest
from tests.test_external_sync import SectionsProbeMixin


class WorkbenchSourcesTests(SectionsProbeMixin, unittest.TestCase):
    def test_cross_conversation_sources_filters_and_read_only_history(self):
        result=self.run_probe(self.SECTIONS_PREAMBLE+r'''
import asyncio,time,hashlib
from pathlib import Path
from xgent_app.workbench import Workbench
async def main():
    await ns['UserDataManager'].init();w=Workbench(ns);db=await w.db();conn=await db._get_conn()
    a='global_memory';b=(await db.manage_conversation('create',name='来源 B'))['conversation_id']
    now=time.time();uploads=Path(ns['ArtifactManager'].ROOT_DIR)/'uploads';uploads.mkdir(parents=True,exist_ok=True)
    async def message(cid,text,kind='user_file',meta=None):
        cursor=await conn.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id,metadata) VALUES(1,1,?,'assistant',?,?,?,?)",(kind,text,now,cid,json.dumps(meta) if meta else None));return cursor.lastrowid
    ids=[]
    for index in range(65):
        name=f'file-{index:03}.txt';(uploads/name).write_text('file content',encoding='utf-8')
        cid=b if index==64 else a
        ids.append(await message(cid,'文件 '+name,meta={'attachments':[{'version':1,'path':name,'filename':name,'mime_type':'text/plain','storage':'uploads'}]}))
    task={'id':'trg_sourceA','chat_id':1,'conversation_id':a,'command':'echo source','summary':'有来源任务','schedule_type':'once','timezone':'Asia/Shanghai','status':'scheduled','created_at':now,'updated_at':now}
    await db.create_trigger_task(task)
    task_id=await message(a,'已登记触发任务 trg_sourceA','agent_result')
    output=Path(ns['COMMAND_OUTPUT_DIR'])/'2026-10-10';output.mkdir(parents=True,exist_ok=True)
    log=output/'source.txt';log.write_text('full output',encoding='utf-8')
    output_id=await message(a,'Agent Run\n完整输出: <code>'+str(log)+'</code>','agent_result')
    before=[tuple(row) for row in await (await conn.execute('SELECT id,content,metadata FROM global_messages ORDER BY id')).fetchall()]
    all_files=await w.artifacts({'source_conversation_id':'all'})
    second=await w.artifacts({'source_conversation_id':'all','offset':50})
    filtered=await w.artifacts({'source_conversation_id':'all','q':'file-064'})
    own=await w.artifacts({'source_conversation_id':b})
    tasks=await w.tasks({});found=next(t for t in tasks['items'] if t['id']=='trg_sourceA')
    outputs=await w.artifacts({'kind':'outputs','source_conversation_id':a})
    item=next(f for f in outputs['items'] if f['filename']=='source.txt')
    await db.manage_conversation('archive',b)
    located=await w.handle('GET','history',{'conversation_id':b,'anchor':'history:'+str(ids[64])})
    state=await db.get_session(b)
    after=[tuple(row) for row in await (await conn.execute('SELECT id,content,metadata FROM global_messages ORDER BY id')).fetchall()]
    print(json.dumps({'total':all_files['total'],'pages':[len(all_files['items']),len(second['items'])],
      'filtered':filtered['total'],'filtered_source':filtered['items'][0]['source']['conversation_id']==b,
      'own':own['total'],'task_anchor':found['source']['message_key']=='history:'+str(task_id),
      'output_anchor':item['source']['message_key']=='history:'+str(output_id),'archived':bool(state['archived']),
      'located':any(m['id']==ids[64] for m in located['messages']),'unchanged':before==after}))
    await db.close()
asyncio.run(main())
''')
        self.assertEqual(result['total'],65)
        self.assertEqual(result['pages'],[50,15])
        self.assertEqual(result['filtered'],1)
        self.assertEqual(result['own'],1)
        for key in ('filtered_source','task_anchor','output_anchor','archived','located','unchanged'):self.assertTrue(result[key],key)

    def test_blacklist_editor_conflict_and_clear_confirmation(self):
        result=self.run_probe(self.SECTIONS_PREAMBLE+r'''
import asyncio
from xgent_app.workbench import Workbench,WorkbenchError
async def main():
    await ns['UserDataManager'].init();w=Workbench(ns);db=await w.db()
    current=await w.blacklist({});saved=await w.save_blacklist({'revision':current['revision'],'patterns':['demo-danger']})
    stale=clear=False
    try:await w.save_blacklist({'revision':current['revision'],'patterns':['other']})
    except WorkbenchError as e:stale=e.status==409
    try:await w.save_blacklist({'revision':saved['revision'],'patterns':[]})
    except WorkbenchError:clear=True
    final=await w.save_blacklist({'revision':saved['revision'],'patterns':[],'confirm_clear':True})
    print(json.dumps({'stale':stale,'clear_guard':clear,'empty':final['patterns']==[],'saved':saved['patterns']==['demo-danger']}));await db.close()
asyncio.run(main())
''')
        self.assertTrue(all(result.values()),result)
