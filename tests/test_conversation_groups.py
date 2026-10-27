import unittest

from tests.test_external_sync import SectionsProbeMixin


class ConversationGroupsTests(SectionsProbeMixin, unittest.TestCase):
    def test_groups_are_transactional_scoped_and_never_change_selectors(self):
        result = self.run_probe(
            self.SECTIONS_PREAMBLE
            + r"""
import asyncio
from xgent_app.workbench import Workbench, WorkbenchError
async def main():
    await ns['UserDataManager'].init();w=Workbench(ns);db=await w.db()
    a='global_memory';b=(await db.manage_conversation('create',name='B'))['conversation_id']
    before=await w.conversations({})
    async def change(resource,**data):
        current=await w.conversations({})
        return await w.handle('POST',resource,{'revision':current['revision'],**data})
    created=await change('conversation-groups/create',name='项目 A');gid=created['group_id']
    other=await change('conversation-groups/create',name='项目 B');bid=other['group_id']
    moved=await change('conversations/move',id=a,group_id=gid)
    pinned=await change('conversations/pin',id=a,group_id=gid)
    p=next(c for c in pinned['items'] if c['id']==a)
    conflict=False
    try:await w.handle('POST','conversations/pin',{'revision':moved['revision'],'id':a,'group_id':gid})
    except WorkbenchError as e:conflict=e.status==409
    new=await change('conversations/move',id=a,group_id=bid)
    reset_pin=next(c for c in new['items'] if c['id']==a)['pinned']==0
    wrong_group=False
    try:await change('conversations/pin',id=a,group_id=gid)
    except WorkbenchError as e:wrong_group=e.status==409
    await change('conversations/pin',id=a,group_id=bid)
    deleted=await change('conversation-groups/delete',group_id=bid)
    d=next(c for c in deleted['items'] if c['id']==a)
    reserved=False
    try:await change('conversation-groups/delete',group_id='ungrouped')
    except WorkbenchError:reserved=True
    state=await db.get_conversation_state()
    print(json.dumps({'pinned':p['pinned']==1 and p['group_id']==gid,'conflict':conflict,
      'reset_pin':reset_pin,'wrong_group':wrong_group,'deleted_not_conversation':d['group_id']=='ungrouped' and d['pinned']==0,
      'selector_unchanged':state['current_chat_id']==before['current_chat_id'] and state['telegram_conversation_id']==before['telegram_conversation_id'],
      'reserved':reserved,'still_other':any(g['id']==gid for g in deleted['groups'])}))
    await db.close()
asyncio.run(main())
"""
        )
        self.assertTrue(all(result.values()), result)

    def test_anchor_returns_both_directions_and_generation_metadata(self):
        result = self.run_probe(
            self.SECTIONS_PREAMBLE
            + r"""
import asyncio,time
from xgent_app.workbench import Workbench
async def main():
    await ns['UserDataManager'].init();w=Workbench(ns);db=await w.db();conn=await db._get_conn()
    ids=[]
    for i in range(120):
        row=await ns['GlobalRecorder'].record_ai_reply('reply '+str(i),provider='provider-at-time',model='model-at-time')
        ids.append(row)
    result=await w.history({'anchor':'history:'+str(ids[60]),'limit':20})
    before=await w.history({'before':result['before']['cursor'],'limit':20})
    after=await w.history({'after':result['after']['cursor'],'limit':20})
    middle=[m['id'] for m in result['messages']]
    sample=result['messages'][0]
    print(json.dumps({'both':min(middle)<ids[60]<max(middle),'anchor_once':middle.count(ids[60])==1,
       'before':max(m['id'] for m in before['messages'])<min(middle),'after':min(m['id'] for m in after['messages'])>max(middle),
       'generation':sample['ai_generation']['model']=='model-at-time' and sample['ai_generation']['provider']=='provider-at-time',
       'has_both':result['before']['has_more'] and result['after']['has_more']}))
    await db.close()
asyncio.run(main())
"""
        )
        self.assertTrue(all(result.values()), result)
