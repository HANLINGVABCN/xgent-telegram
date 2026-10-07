"""Token report data and navigation controls on isolated SQL/HTTP fixtures."""
from pathlib import Path
import pytest
from tests.test_external_sync import SectionsProbeMixin
from tests.test_workbench_browser import browser_context,workspace_url,ready  # noqa: F401


def test_usage_all_custom_zone_prices_and_report_parity():
    result=SectionsProbeMixin().run_probe(SectionsProbeMixin.SECTIONS_PREAMBLE+r'''
import asyncio,time
from datetime import datetime
from zoneinfo import ZoneInfo
from xgent_app.workbench import Workbench,WorkbenchError
async def main():
    await ns['UserDataManager'].init();w=Workbench(ns);db=await w.db();conn=await db._get_conn()
    zone=ZoneInfo('Asia/Shanghai');start=datetime(2023,1,1,tzinfo=zone).timestamp()
    for ts,model in [(start-1,'outside'),(start,'alias-a'),(start+86399,'alias-b'),(start+86400,'outside')]:
        await db.add_token_stat(model,{'input_tokens':1000,'output_tokens':200,'cached_tokens':400,'reasoning_tokens':50,'total_tokens':1200},ts)
    await ns['UserDataManager'].save_config('stats_auto_merge',False)
    await ns['UserDataManager'].save_config('model_merge_map',{'merged':['alias-a','alias-b']})
    await ns['UserDataManager'].save_config('model_price_table',{'merged':{'input':2,'output':4,'cached':.5}})
    d=await w.usage({'range':'custom','start_date':'2023-01-01','end_date':'2023-01-01'})
    expected=ns['_compute_cost']({'input':2000,'output':400,'cached':800},ns['_model_price']('merged',ns['_get_price_table']()))
    all_data=await w.usage({'range':'all'});week=await w.usage({'range':'7'})
    zero=await w.usage_range({'start':0,'end':0})
    invalid=0
    for value in [{'range':'custom','start_date':'2023-02-30','end_date':'2023-03-01'},{'range':'custom','start_date':'2023-02-01','end_date':'2023-01-01'},{'range':'fake'}]:
        try: await w.usage(value)
        except WorkbenchError: invalid+=1
    print(json.dumps({'summary':d['summary'],'expected':expected,'per_model':d['per_model'],'records':d['records'],
        'all_count':all_data['summary']['count'],'missing':all_data['missing_prices'],'all_cost':all_data['summary']['cost'],
        'bucket':all_data['bucket'],'week_count':week['summary']['count'],'start':d['start'],'correct_start':start,'end':d['end'],
        'zero':zero,'invalid':invalid}))
    await db.close()
asyncio.run(main())
''')
    assert result['summary']['count']==2
    assert result['summary']['cost']==pytest.approx(result['expected'])
    assert result['summary']['cache_saved']==pytest.approx(.0012)
    assert result['summary']['average_cost']==pytest.approx(result['expected']/2)
    assert result['summary']['cached']==800 and result['summary']['reasoning']==100
    assert result['per_model'][0]['members']==['alias-a','alias-b']
    assert all(r['display_model']=='merged' and r['cost'] is not None for r in result['records'])
    assert result['all_count']==4 and result['week_count']==0 and result['bucket']=='month'
    assert result['missing']==['outside'] and result['all_cost'] is None
    assert result['start']==result['correct_start'] and result['start']+86399<result['end']<result['start']+86400
    assert result['zero']==[0,0] and result['invalid']==3


def test_usage_range_buttons_always_show_dates_and_detail_paging(browser_context,workspace_url):
    expect=pytest.importorskip('playwright.sync_api').expect
    page=browser_context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    ready(page,workspace_url['url'],'usage')
    for name in ['最近 30 天','最近 90 天','全部','最近 7 天']:
        with page.expect_response(lambda r:'/api/workbench/usage?' in r.url):
            page.get_by_role('button',name=name,exact=True).click()
        expect(page.get_by_role('button',name=name,exact=True)).to_have_attribute('aria-pressed','true')
        assert page.locator('[name=start_date]').input_value()
        assert page.locator('[name=end_date]').input_value()
        assert page.locator('[name=start_date]').evaluate('(n)=>n.readOnly')
        expect(page.locator('.wb-range-exact')).to_contain_text('Asia/Shanghai')
    page.get_by_role('button',name='自定义范围',exact=True).click()
    page.locator('[name=start_date]').fill('2020-01-02');page.locator('[name=end_date]').fill('2020-01-01')
    page.get_by_role('button',name='应用日期',exact=True).click()
    expect(page.locator('.wb-range-feedback')).to_contain_text('开始日期不能晚于结束日期')
    page.locator('[name=end_date]').fill('2030-01-01')
    with page.expect_response(lambda r:'/api/workbench/usage?' in r.url):page.get_by_role('button',name='应用日期',exact=True).click()
    expect(page.get_by_role('button',name='自定义范围',exact=True)).to_have_attribute('aria-pressed','true')
    expect(page.locator('.wb-range-exact')).to_contain_text('2020/1/2')
    expect(page.get_by_role('heading',name='模型用量明细')).to_be_visible()
    for label in ['缓存 Token','其中思考','单次均费','缓存省下']:assert page.locator('.wb-usage-stats').get_by_text(label,exact=True).count()==1
    calls=page.get_by_role('region',name='调用明细（可横向滚动）')
    before=calls.locator('tbody tr').count()
    with page.expect_response(lambda r:'/api/workbench/usage/records?' in r.url):page.get_by_role('button',name='加载更早的调用明细').click()
    expect(calls.locator('tbody tr')).to_have_count(before+50)
    assert calls.locator('thead').count()==1 and calls.locator('thead th').count()==8
    assert not errors


def test_menu_all_entries_and_busy_feedback_preserve_draft(browser_context,workspace_url):
    expect=pytest.importorskip('playwright.sync_api').expect
    page=browser_context.new_page();page.set_viewport_size({'width':390,'height':844});ready(page,workspace_url['url'])
    for selector in ['#btn-menu','#btn-composer-menu']:
        page.locator('#input').fill('保留草稿');page.locator(selector).click()
        expect(page.locator('#composer-commands')).to_be_visible()
        expect(page.locator('#composer-commands .composer-command strong').first).to_have_text('/start')
        assert 'open' not in page.locator('#menu-panel').get_attribute('class')
        page.keyboard.press('Escape');assert page.locator('#input').input_value()=='保留草稿'
    page.evaluate('document.getElementById("btn-send").disabled=true')
    # Pending attachment is a real blocked-command state, not a fake disabled attribute.
    page.locator('#file-input').set_input_files({'name':'pending.txt','mimeType':'text/plain','buffer':b'pending'})
    page.locator('#btn-composer-menu').click();page.locator('#composer-commands .composer-command').first.click()
    expect(page.locator('#composer-commands')).to_be_visible()
    expect(page.locator('#composer-commands .wb-error')).to_contain_text('待上传附件')
    assert page.locator('#input').input_value()=='保留草稿'


def test_logout_is_visible_clears_draft_and_does_not_reload_auto_login(browser_context,workspace_url):
    expect=pytest.importorskip('playwright.sync_api').expect
    page=browser_context.new_page();ready(page,workspace_url['url'])
    page.locator('#input').fill('应当清理的草稿')
    page.locator('#wb-logout').click()
    expect(page.locator('#login')).to_be_visible()
    assert not browser_context.request.get(workspace_url['url']+'/api/session').json()['authenticated']
    assert page.locator('#input').input_value()==''
    assert page.evaluate('sessionStorage.getItem("xgent-draft")') is None
    assert page.locator('#log .msg-row').count()==0
    assert page.evaluate('performance.getEntriesByType("navigation")[0].type')=='navigate'


def test_menu_bootstrap_failure_has_visible_retry(browser_context,workspace_url):
    expect=pytest.importorskip('playwright.sync_api').expect
    page=browser_context.new_page();page.route('**/api/workbench/bootstrap',lambda r:r.fulfill(status=503,json={'error':'fixture bootstrap failure'}))
    page.goto(workspace_url['url']);page.locator('#btn-composer-menu').click()
    expect(page.locator('#composer-commands')).to_contain_text('命令列表读取失败')
    page.unroute('**/api/workbench/bootstrap');page.get_by_role('button',name='重新读取',exact=True).click()
    expect(page.locator('#composer-commands .composer-command strong').first).to_have_text('/start')


@pytest.mark.parametrize('width,height',[(390,844),(768,1024),(1440,900)])
def test_usage_visuals(browser_context,workspace_url,width,height):
    page=browser_context.new_page();page.set_viewport_size({'width':width,'height':height});page.emulate_media(reduced_motion='reduce')
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    output=Path(__file__).resolve().parents[1]/'workspace'/'web-usage-qa';output.mkdir(parents=True,exist_ok=True)
    for theme in ['light','dark']:
        ready(page,workspace_url['url'],'usage');page.evaluate('(t)=>document.body.classList.toggle("dark",t==="dark")',theme)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        assert page.locator('#wb-page').evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
        page.locator('#wb-page').evaluate('(n)=>n.scrollTop=0')
        page.screenshot(path=str(output/f'{width}-{theme}-usage.png'))
        page.locator('#wb-page').evaluate('(n)=>n.scrollTop=650')
        page.screenshot(path=str(output/f'{width}-{theme}-usage-details.png'))
    assert not errors


def test_logout_failure_keeps_session_and_shows_retry(browser_context,workspace_url):
    expect=pytest.importorskip('playwright.sync_api').expect
    page=browser_context.new_page();ready(page,workspace_url['url'])
    page.locator('#input').fill('退出失败时保留')
    page.route('**/api/logout',lambda r:r.fulfill(status=503,json={'error':'fixture logout unavailable'}))
    page.locator('#wb-logout').click()
    expect(page.locator('#wb-dialog')).to_contain_text('退出失败')
    assert browser_context.request.get(workspace_url['url']+'/api/session').json()['authenticated']
    assert page.locator('#input').input_value()=='退出失败时保留'
    page.unroute('**/api/logout');page.get_by_role('button',name='重试退出',exact=True).click()
    expect(page.locator('#login')).to_be_visible()


def test_shell_and_usage_asset_revalidate(browser_context,workspace_url):
    for path in ['/', '/assets/usage.js']:
        response=browser_context.request.get(workspace_url['url']+path)
        assert response.ok and response.headers['cache-control']=='no-cache'
    page=browser_context.new_page();ready(page,workspace_url['url'],'settings')
    assert page.locator('#wb-page').get_by_role('button',name='退出登录',exact=True).is_visible()


def test_presets_use_existing_dates_when_no_results_and_survive_navigation(browser_context,workspace_url):
    expect=pytest.importorskip('playwright.sync_api').expect
    page=browser_context.new_page();ready(page,workspace_url['url'],'usage')
    page.get_by_role('button',name='自定义范围',exact=True).click()
    page.locator('[name=start_date]').fill('2000-01-01');page.locator('[name=end_date]').fill('2000-01-02')
    page.get_by_role('button',name='应用日期',exact=True).click()
    expect(page.get_by_role('heading',name='没有调用记录',exact=True)).to_be_visible()
    assert page.locator('[name=start_date]').input_value()=='2000-01-01'
    page.evaluate('location.hash="/tasks"');page.get_by_role('heading',name='任务中心',exact=True).wait_for()
    page.evaluate('location.hash="/usage"');page.get_by_role('heading',name='用量与成本',exact=True).wait_for()
    expect(page.locator('[name=end_date]')).to_have_value('2000-01-02')
    page.get_by_role('button',name='全部',exact=True).click()
    expect(page.get_by_role('button',name='全部',exact=True)).to_have_attribute('aria-pressed','true')
    assert page.locator('[name=start_date]').input_value()!='2000-01-01'
