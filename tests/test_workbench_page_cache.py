"""Cached workbench navigation: latency, invalidation, races, and edit preservation."""
import pytest

from tests.test_workbench_browser import browser_context, workspace_url, ready  # noqa: F401


PROBE = r"""
window.__reads=[];window.__delay={};window.__fail={};
const nativeFetch=window.fetch.bind(window);
window.fetch=async function(input,options={}){
 const url=new URL(typeof input==='string'?input:input.url,location.href);
 const path=url.pathname;
 if(path.startsWith('/api/workbench/') && (!options.method||options.method==='GET')){
   window.__reads.push(path+url.search);
   const response=await nativeFetch(input,options);
   const delay=window.__delay[path]||0;
   if(delay)await new Promise(resolve=>setTimeout(resolve,delay));
   if(window.__fail[path])return new Response(JSON.stringify({error:'fixture unavailable'}),{status:503,headers:{'Content-Type':'application/json'}});
   return response;
 }
 return nativeFetch(input,options);
};
window.__realNow=Date.now.bind(Date);
window.__age=0;Date.now=()=>window.__realNow()+window.__age;
"""


def open_page(context, fixture, route='tasks'):
    page=context.new_page()
    page.add_init_script(PROBE)
    ready(page,fixture['url'],route)
    if route!='chat':
        page.wait_for_function("!document.querySelector('#wb-page > .wb-loading')")
    return page


def go(page, route, title=None):
    page.evaluate('(route)=>{document.activeElement?.blur();location.hash="/"+route;}',route)
    page.wait_for_function('(route)=>document.body.dataset.page===route',arg=route)
    if title:page.get_by_role('heading',name=title,exact=True).wait_for()


def count(page, path):
    return page.evaluate('(path)=>window.__reads.filter(p=>p.split("?")[0]===path).length',path)


def test_back_navigation_restores_dom_scroll_and_avoids_loading_requests(browser_context,workspace_url):
    page=open_page(browser_context,workspace_url,'models')
    page.set_viewport_size({'width':900,'height':500})
    page.locator('#wb-page').evaluate('(node)=>{node.scrollTop=150;window.__scroll=node.scrollTop;window.__cached=node.firstElementChild;}')
    providers=count(page,'/api/workbench/providers')
    bootstrap=count(page,'/api/workbench/bootstrap')
    go(page,'tasks','任务中心')
    # Even if the network would take a second, the previous page returns before a frame.
    page.evaluate("window.__delay['/api/workbench/providers']=1000;window.__delay['/api/workbench/bootstrap']=1000")
    elapsed=page.evaluate("""() => new Promise(resolve=>{
      const start=performance.now();location.hash='/models';
      const poll=()=>{if(document.body.dataset.page==='models'&&document.querySelector('#wb-page').firstElementChild===window.__cached)resolve(performance.now()-start);else requestAnimationFrame(poll);};
      requestAnimationFrame(poll);
    })""")
    assert elapsed<200, elapsed
    assert page.locator('#wb-page > .wb-loading').count()==0
    assert page.evaluate('Math.abs(document.querySelector("#wb-page").scrollTop-window.__scroll)<2')
    assert count(page,'/api/workbench/providers')==providers
    assert count(page,'/api/workbench/bootstrap')==bootstrap
    # Navigating through chat must not overwrite another page's saved position.
    go(page,'chat');go(page,'models','模型与技能')
    assert page.evaluate('document.querySelector("#wb-page").firstElementChild===window.__cached')
    assert page.evaluate('Math.abs(document.querySelector("#wb-page").scrollTop-window.__scroll)<2')
    assert count(page,'/api/workbench/providers')==providers


def test_expired_cache_stays_visible_while_background_refreshes(browser_context,workspace_url):
    page=open_page(browser_context,workspace_url)
    page.evaluate("window.__cached=document.querySelector('#wb-page').firstElementChild")
    go(page,'files','文件与输出')
    reads=count(page,'/api/workbench/tasks')
    page.evaluate("window.__age+=20000;window.__delay['/api/workbench/tasks']=650")
    go(page,'tasks','任务中心')
    assert page.evaluate('document.querySelector("#wb-page").firstElementChild===window.__cached')
    assert page.locator('#wb-page > .wb-loading').count()==0
    page.wait_for_timeout(750)
    assert page.evaluate('document.querySelector("#wb-page").firstElementChild===window.__cached')
    assert count(page,'/api/workbench/tasks')==reads+1
    assert page.locator('.wb-cache-notice').count()==0


def test_failed_background_refresh_keeps_content_and_can_retry(browser_context,workspace_url):
    page=open_page(browser_context,workspace_url)
    page.evaluate("window.__cached=document.querySelector('#wb-page').firstElementChild")
    go(page,'files','文件与输出')
    page.evaluate("window.__age+=20000;window.__fail['/api/workbench/tasks']=true")
    go(page,'tasks','任务中心')
    page.get_by_role('button',name='重试更新',exact=True).wait_for()
    assert page.evaluate('document.querySelector("#wb-page").firstElementChild===window.__cached')
    assert page.locator('#wb-page table').count()==1
    page.evaluate("window.__fail['/api/workbench/tasks']=false")
    page.get_by_role('button',name='重试更新',exact=True).click()
    page.wait_for_function('!document.querySelector(".wb-cache-notice")')
    assert page.evaluate('document.querySelector("#wb-page").firstElementChild===window.__cached')
    assert page.locator('#wb-page > .wb-loading').count()==0


def test_late_background_response_cannot_overwrite_destination(browser_context,workspace_url):
    page=open_page(browser_context,workspace_url)
    go(page,'files','文件与输出')
    page.evaluate("window.__age+=20000;window.__delay['/api/workbench/tasks']=600")
    go(page,'tasks','任务中心')
    go(page,'models','模型与技能')
    page.wait_for_timeout(750)
    assert page.locator('#wb-page h1').text_content()=='模型与技能'
    assert page.locator('#wb-page .wb-error').count()==0


def test_mutation_invalidates_settings_but_not_unrelated_files(browser_context,workspace_url):
    page=open_page(browser_context,workspace_url,'settings')
    page.get_by_role('tab',name='对话与记忆',exact=True).click()
    page.locator('[name=global_depth]').wait_for()
    go(page,'files','文件与输出')
    artifacts=count(page,'/api/workbench/artifacts')
    # Same API used by quick setting buttons; no private cache globals needed.
    page.evaluate("async()=>{const {wb}=await import('/assets/components.js');await wb('settings',{data:{key:'global_depth',value:53}});}")
    go(page,'settings','设置')
    page.wait_for_function('document.querySelector("[name=global_depth]")?.value==="53"')
    go(page,'files','文件与输出')
    assert count(page,'/api/workbench/artifacts')==artifacts


def test_refresh_cannot_overwrite_a_form_edited_during_fetch(browser_context,workspace_url):
    page=open_page(browser_context,workspace_url,'settings')
    page.get_by_role('tab',name='对话与记忆',exact=True).click()
    page.locator('[name=global_depth]').wait_for()
    go(page,'files','文件与输出')
    page.evaluate("window.__age+=61000;window.__delay['/api/workbench/bootstrap']=700")
    go(page,'settings','设置')
    page.locator('[name=global_depth]').fill('79')
    page.wait_for_timeout(800)
    assert page.locator('[name=global_depth]').input_value()=='79'
    # Explicit discard must evict this edited DOM, not restore it as saved.
    page.on('dialog',lambda dialog:dialog.accept())
    go(page,'files','文件与输出')
    go(page,'settings','设置')
    page.locator('[name=global_depth]').wait_for()
    assert page.locator('[name=global_depth]').input_value()!='79'


def test_checked_tasks_survive_navigation_and_ttl_refresh(browser_context,workspace_url):
    page=open_page(browser_context,workspace_url)
    box=page.locator('#wb-page tbody input[type=checkbox]').first
    box.check()
    reads=count(page,'/api/workbench/tasks')
    go(page,'files','文件与输出')
    page.evaluate('window.__age+=20000')
    go(page,'tasks','任务中心')
    assert page.locator('#wb-page tbody input[type=checkbox]').first.is_checked()
    assert count(page,'/api/workbench/tasks')==reads
    assert page.get_by_role('button',name='取消选中',exact=True).is_enabled()


def test_auth_expiry_clears_authenticated_page_cache(browser_context,workspace_url):
    page=open_page(browser_context,workspace_url)
    go(page,'files','文件与输出')
    page.evaluate("window.dispatchEvent(new Event('xgent-auth-expired'))")
    assert page.locator('#wb-page').inner_text()==''
    go(page,'tasks')
    assert page.locator('#wb-page h1').count()==0
    assert page.locator('#login').is_visible()


def test_filter_change_does_not_restore_mismatched_form_state(browser_context,workspace_url):
    page=open_page(browser_context,workspace_url,'files')
    page.locator('[data-filter=kind]').select_option('outputs')
    page.get_by_role('button',name='查看日志',exact=True).first.wait_for()
    reads=count(page,'/api/workbench/artifacts')
    go(page,'tasks','任务中心');go(page,'files','文件与输出')
    assert page.locator('[data-filter=kind]').input_value()=='outputs'
    assert page.get_by_role('button',name='查看日志',exact=True).first.is_visible()
    assert count(page,'/api/workbench/artifacts')==reads
    page.locator('[data-filter=kind]').select_option('files')
    page.get_by_role('button',name='部署报告.md',exact=True).wait_for()
    go(page,'tasks','任务中心');go(page,'files','文件与输出')
    assert page.locator('[data-filter=kind]').input_value()=='files'
    assert page.get_by_role('button',name='部署报告.md',exact=True).is_visible()


def test_page_cache_lru_ttl_and_dependency_invalidation(browser_context,workspace_url):
    page=open_page(browser_context,workspace_url)
    result=page.evaluate("""async()=>{
      const {PageCache,affectedPages}=await import('/assets/page-cache.js');
      let now=0;const cache=new PageCache(2,()=>now);
      const task=cache.key('tasks',{status:'running'}),file=cache.key('files'),model=cache.key('models');
      const entry=cache.set(task,'tasks',document.createElement('div'));cache.set(file,'files',document.createElement('div'));
      const fresh=cache.fresh(entry);now=15000;const expired=!cache.fresh(entry);
      cache.get(task);cache.set(model,'models',document.createElement('div'));
      const evicted=!cache.get(file),bounded=cache.entries.size===2;
      cache.invalidate(['tasks']);const invalid=!cache.fresh(entry);
      const before=cache.revision('tasks');cache.invalidate(['tasks']);
      const concurrent=cache.set(task,'tasks',document.createElement('div'),before);
      const mutationWins=!cache.fresh(concurrent);
      cache.clear();return {fresh,expired,evicted,bounded,invalid,mutationWins,cleared:cache.entries.size===0,
        readOnly:affectedPages('/api/workbench/providers/export').length===0,
        settings:affectedPages('/api/workbench/settings','global_depth'),
        pricing:affectedPages('/api/workbench/settings','model_price_table'),
        providers:affectedPages('/api/workbench/providers/save')};
    }""")
    for key in ('fresh','expired','evicted','bounded','invalid','mutationWins','cleared','readOnly'):
        assert result[key],key
    assert result['settings']==['settings']
    assert result['pricing']==['settings','usage']
    assert result['providers']==['models','settings','usage']


def test_mobile_back_navigation_reuses_page_without_flash(browser_context,workspace_url):
    page=open_page(browser_context,workspace_url,'files')
    page.set_viewport_size({'width':390,'height':844})
    page.evaluate("window.__cached=document.querySelector('#wb-page').firstElementChild")
    reads=count(page,'/api/workbench/artifacts')
    page.locator('#wb-mobile-nav [data-route=tasks]').click()
    page.get_by_role('heading',name='任务中心',exact=True).wait_for()
    page.locator('#wb-mobile-nav [data-route=files]').click()
    page.wait_for_function('document.querySelector("#wb-page").firstElementChild===window.__cached')
    assert page.locator('#wb-page > .wb-loading').count()==0
    assert count(page,'/api/workbench/artifacts')==reads
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')


def test_external_settings_change_is_loaded_after_ttl(browser_context,workspace_url):
    page=open_page(browser_context,workspace_url,'settings')
    page.get_by_role('tab',name='对话与记忆',exact=True).click()
    page.locator('[name=global_depth]').wait_for()
    go(page,'files','文件与输出')
    response=browser_context.request.post(workspace_url['url']+'/api/workbench/settings',data={'key':'global_depth','value':64})
    assert response.ok
    page.evaluate('window.__age+=61000')
    go(page,'settings','设置')
    page.wait_for_function('document.querySelector("[name=global_depth]")?.value==="64"')
    assert page.locator('#wb-page > .wb-loading').count()==0
