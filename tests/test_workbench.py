"""Workbench service tested with real sections and temporary SQLite; no live services."""
import unittest
from tests.test_external_sync import SectionsProbeMixin

class WorkbenchIntegrationTests(SectionsProbeMixin, unittest.TestCase):
    def test_history_pagination_equal_timestamps_search_and_generation(self):
        result=self.run_probe(self.SECTIONS_PREAMBLE+r'''
import asyncio
from xgent_app.workbench import Workbench
async def run():
    w=Workbench(ns);db=await w.db();conn=await db._get_conn()
    await conn.executemany("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id) VALUES(1,1,'user_text','user',?,100,'global_memory')",[(f'message {i}',) for i in range(10000)])
    page=await w.history({'limit':50}); first=[m['id'] for m in page['messages']]
    second=await w.history({'limit':50,'before':page['next_cursor']})
    forward=await w.history({'after':second['last_cursor']})
    search=await w.search({'q':'message 9876'})
    location=await w.history({'anchor':search['messages'][0]['history_key']})
    await db.clear_all_conversation_memory()
    reset=await w.history({'before':page['next_cursor']})
    print(json.dumps({'first':len(first),'max':max(first),'second':len(second['messages']),
          'forward':[m['id'] for m in forward['messages']]==first,
          'overlap':bool(set(first)&{m['id'] for m in second['messages']}),
          'found':[m['content'] for m in search['messages']],
          'located':any(m['content']=='message 9876' for m in location['messages']), 'reset':reset.get('reset')}))
    await db.close()
asyncio.run(run())
''')
        self.assertEqual(50,result['first']);self.assertEqual(10000,result['max'])
        self.assertEqual(50,result['second']);self.assertFalse(result['overlap']);self.assertTrue(result['forward'])
        self.assertEqual(['message 9876'],result['found']);self.assertTrue(result['located']);self.assertTrue(result['reset'])

    def test_provider_write_only_key_import_selection_delete_and_missing_price(self):
        result=self.run_probe(self.SECTIONS_PREAMBLE+r'''
import asyncio,time
from xgent_app.workbench import Workbench
async def run():
    await ns['UserDataManager'].init()
    w=Workbench(ns);db=await w.db()
    original={'name':'Workbench','base_url':'https://example.invalid/v1','api_format':'openai','models':['model-a'],'api_key':'SUPER-SECRET-FIXTURE'}
    saved=await w.save_provider(original)
    await w.save_provider({k:v for k,v in original.items() if k!='api_key'})
    retained=(await db.get_providers())['Workbench']['api_key']
    await w.select_model({'target':'chat','provider':'Workbench','model':'model-a'})
    await w.bootstrap({})
    state_matches=ns['get_model_target_provider_name']('chat')=='Workbench'
    await w.save_provider({**original,'original_name':'Workbench','name':'Renamed'})
    renamed=ns['get_model_target_provider_name']('chat')=='Renamed'
    await w.save_provider({**original,'original_name':'Renamed'})
    exported=await w.export_providers({})
    await w.import_providers({'config':exported,'confirm':True})
    imported=(await db.get_providers())['Workbench']['api_key']
    await db.add_token_stat('unpriced',{'input_tokens':10,'output_tokens':20,'total_tokens':30},time.time())
    usage=await w.usage({})
    await w.save_provider({**original,'api_key':'REPLACED-SECRET'})
    await w.save_provider({**{k:v for k,v in original.items() if k!='api_key'},'clear_key':True})
    cleared=(await db.get_providers())['Workbench']['api_key']
    await w.delete_provider({'name':'Workbench','confirm':True})
    print(json.dumps({'secret_leak':'SUPER-SECRET-FIXTURE' in json.dumps(saved)+json.dumps(exported),
      'retained':retained=='SUPER-SECRET-FIXTURE','imported':imported=='SUPER-SECRET-FIXTURE','cleared':not cleared,
      'state_matches':state_matches,'renamed':renamed,'selection_cleared':ns['get_model_target_provider_name']('chat') is None,
      'deleted':'Workbench' not in await db.get_providers(),'cost':usage['summary']['cost'],'tokens':usage['summary']['total']}))
    await db.close()
asyncio.run(run())
''')
        self.assertFalse(result['secret_leak']);self.assertTrue(result['retained']);self.assertTrue(result['imported'])
        self.assertTrue(result['cleared']);self.assertTrue(result['deleted']);self.assertTrue(result['selection_cleared']);self.assertTrue(result['state_matches']);self.assertTrue(result['renamed']);self.assertIsNone(result['cost']);self.assertEqual(30,result['tokens'])

    def test_task_registration_idempotency_and_confirmation(self):
        result=self.run_probe(self.SECTIONS_PREAMBLE+r'''
import asyncio
from xgent_app.workbench import Workbench,WorkbenchError
async def run():
    await ns['UserDataManager'].init();w=Workbench(ns);db=await w.db()
    fields={'request_id':'same_request_id_123456','command':'echo unit-test','after':'30s','task':'scheduled only'}
    one=await w.create_task(fields);two=await w.create_task(fields)
    conflict=False
    try:await w.create_task({**fields,'command':'different'})
    except WorkbenchError:conflict=True
    tasks=await w.tasks({})
    denied=False
    try:await w.cancel_tasks({'ids':[tasks['items'][0]['id']]})
    except WorkbenchError:denied=True
    await w.cancel_tasks({'ids':[tasks['items'][0]['id']],'confirm':True})
    print(json.dumps({'same':one==two,'count':len(tasks['items']),'conflict':conflict,'denied':denied,
       'status':(await w.tasks({}))['items'][0]['status']}))
    await db.close()
asyncio.run(run())
''')
        self.assertTrue(result['same']);self.assertEqual(1,result['count']);self.assertTrue(result['conflict']);self.assertTrue(result['denied']);self.assertEqual('cancelled',result['status'])

    def test_skill_state_and_bootstrap_commands(self):
        result=self.run_probe(self.SECTIONS_PREAMBLE+r'''
import asyncio
from pathlib import Path
from xgent_app.workbench import Workbench
async def run():
    await ns['UserDataManager'].init()
    root=Path(ns['SKILL_PUBLIC_DIR']);root.mkdir(exist_ok=True)
    (root/'test.md').write_text('# Skill body',encoding='utf-8')
    w=Workbench(ns);boot=await w.bootstrap({});skills=await w.skills({})
    path=skills['items'][0]['path'];await w.skill_state({'path':path,'state':'hidden'})
    detail=await w.skills({'path':path})
    print(json.dumps({'first':boot['commands'][0]['cmd'],'state':detail['state'],'content':detail['content']}))
    await (await w.db()).close()
asyncio.run(run())
''')
        self.assertEqual('/start',result['first']);self.assertEqual('hidden',result['state']);self.assertIn('Skill body',result['content'])

    def test_ui_interleaving_does_not_skip_records_and_settings_validate(self):
        result=self.run_probe(self.SECTIONS_PREAMBLE+r'''
import asyncio,time
from xgent_app.workbench import Workbench,WorkbenchError
async def run():
    await ns['UserDataManager'].init();w=Workbench(ns);db=await w.db();conn=await db._get_conn()
    await conn.executemany("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id) VALUES(1,1,'user_text','user',?,100,'global_memory')",[(str(i),) for i in range(260)])
    for i in range(160):
        await conn.execute("INSERT INTO ui_messages(ui_message_id,source,chat_id,message_id,generation,revision,timestamp,payload,conversation_id) VALUES(?, 'test',1,?,0,1,100,?,'global_memory')",(f'{i:032x}',i,json.dumps({'content':'menu '+str(i)})))
    cursor=None;ids=[]
    while True:
        page=await w.history({'before':cursor} if cursor else {})
        ids.extend(m['history_key'] for m in page['messages']);cursor=page['next_cursor']
        if not cursor:break
    invalid=0
    for value in (-1,float('inf'),float('nan')):
        try:await w.settings({'key':'model_price_table','value':{'x':{'input':value}}})
        except WorkbenchError:invalid+=1
    await conn.executemany("INSERT INTO token_usage_stats(ts,model,input_tokens,output_tokens,total_tokens) VALUES(?, 'all-records',2,3,5)",[(time.time(),)]*2010)
    usage=await w.usage({});details=await w.usage_records({})
    print(json.dumps({'count':len(ids),'unique':len(set(ids)),'invalid':invalid,'total':usage['summary']['total'],'detail_page':len(details['items']),'more':bool(details['next_cursor'])}))
    await db.close()
asyncio.run(run())
''')
        self.assertEqual(420,result['count']);self.assertEqual(420,result['unique'])
        self.assertEqual(3,result['invalid']);self.assertEqual(10050,result['total'])
        self.assertEqual(50,result['detail_page']);self.assertTrue(result['more'])


class WorkbenchTaskManagementTests(SectionsProbeMixin, unittest.TestCase):
    def test_task_filters_literal_search_stable_pages_counts_and_offset_clamp(self):
        result=self.run_probe(self.SECTIONS_PREAMBLE+r'''
import asyncio
from xgent_app.workbench import Workbench, WorkbenchError
async def run():
    await ns['UserDataManager'].init(); w=Workbench(ns); db=await w.db()
    for i in range(63):
        await db.create_trigger_task({'id':f'trg_manage_{i:03d}','chat_id':1,'conversation_id':'global_memory',
          'command':'echo safe-not-executed', 'summary':f'case {i}', 'schedule_type':'cron' if i%2 else 'once',
          'schedule_expr':'0 9 * * *' if i%2 else '5m', 'timezone':'Asia/Shanghai',
          'status':'scheduled' if i%2 else 'completed','created_at':100,'updated_at':100,
          'condition_expr':'READY' if i==4 else None})
    await db.create_trigger_task({'id':'literal','chat_id':1,'conversation_id':'global_memory','command':'echo 100%_ready',
      'summary':'literal wildcard','schedule_type':'immediate','timezone':'Asia/Shanghai','status':'waiting_delivery',
      'created_at':100,'updated_at':100})
    first=await w.tasks({}); second=await w.tasks({'offset':first['next_offset']})
    selected=await w.tasks({'status':'scheduled','kind':'cron','q':'case'})
    literal=await w.tasks({'q':'%_'}); condition=await w.tasks({'kind':'condition'})
    clamped=await w.tasks({'offset':5000,'status':'waiting_delivery'})
    missing=await w.tasks({'q':'not found'})
    invalid=[]
    for query in [{'offset':'oops'},{'kind':'fake'}]:
        try: await w.tasks(query)
        except WorkbenchError: invalid.append(True)
    print(json.dumps({'total':first['total'],'sizes':[len(first['items']),len(second['items'])],
      'overlap':bool({t['id'] for t in first['items']}&{t['id'] for t in second['items']}),
      'ids':[t['id'] for t in first['items']+second['items']], 'counts':first['counts'],
      'filtered':selected['total'],'literal':[t['id'] for t in literal['items']],
      'condition':[t['id'] for t in condition['items']], 'offset':clamped['offset'],
      'missing':missing['total'],'invalid':len(invalid)}))
    await db.close()
asyncio.run(run())
''')
        self.assertEqual(64,result['total']);self.assertEqual([50,14],result['sizes'])
        self.assertFalse(result['overlap']);self.assertEqual(sorted(result['ids'],reverse=True),result['ids'])
        self.assertEqual(31,result['filtered']);self.assertEqual(1,result['counts']['waiting_delivery'])
        self.assertEqual(['literal'],result['literal']);self.assertEqual(['trg_manage_004'],result['condition'])
        self.assertEqual(0,result['offset']);self.assertEqual(0,result['missing']);self.assertEqual(2,result['invalid'])
