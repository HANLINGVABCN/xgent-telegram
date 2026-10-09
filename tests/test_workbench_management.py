"""Direct trigger/skill actions on real isolated workbench APIs, including mobile QA."""
from pathlib import Path

import pytest
expect = pytest.importorskip('playwright.sync_api').expect
from tests.test_workbench_browser import browser_context, workspace_url, ready  # noqa: F401
from tests.test_workbench_page_cache import PROBE


def open_skills(context, fixture):
    page=context.new_page();page.add_init_script(PROBE)
    ready(page,fixture['url'],'models')
    page.get_by_role('button',name='技能库',exact=True).click()
    page.locator('.wb-skill-card').first.wait_for()
    return page


def test_skill_controls_are_outside_details_and_filters_do_not_fetch(browser_context,workspace_url):
    page=open_skills(browser_context,workspace_url)
    card=page.locator('.wb-skill-card').filter(has=page.get_by_role('heading',name='代码审查',exact=True))
    path=card.get_attribute('data-skill-path')
    card.get_by_role('button',name='隐藏',exact=True).click()
    expect(card.get_by_role('button',name='隐藏',exact=True)).to_have_attribute('aria-pressed','true')
    assert not page.locator('#wb-detail').is_visible()
    saved=browser_context.request.get(workspace_url['url']+'/api/workbench/skills',params={'path':path}).json()
    assert saved['state']=='hidden'
    reads=page.evaluate('window.__reads.length')
    search=page.get_by_role('searchbox',name='搜索技能名称或路径')
    search.fill('代码');page.get_by_label('技能状态筛选').select_option('hidden')
    assert page.locator('.wb-skill-card:visible').count()==1
    assert page.evaluate('window.__reads.length')==reads
    page.evaluate('location.hash="/tasks"');page.get_by_role('heading',name='任务中心',exact=True).wait_for()
    page.evaluate('location.hash="/models"')
    expect(search).to_have_value('代码')
    expect(page.get_by_label('技能状态筛选')).to_have_value('hidden')
    expect(card).to_be_visible()
    card.get_by_role('button',name='管理',exact=True).click()
    expect(page.locator('#wb-detail .knowledge-editor-content')).to_contain_text('检查实际缺陷')
    assert page.locator('#wb-detail').get_by_role('button',name='保存状态').count()==0
    page.keyboard.press('Escape')
    card.get_by_role('button',name='启用',exact=True).click()
    expect(card).to_be_hidden()
    expect(page.get_by_role('heading',name='没有匹配的技能')).to_be_visible()


def test_skill_failure_does_not_claim_success_or_retry(browser_context,workspace_url):
    page=open_skills(browser_context,workspace_url)
    card=page.locator('.wb-skill-card').first
    previous=card.locator('[aria-pressed=true]').text_content()
    chosen='关闭' if previous!='关闭' else '启用'
    writes=[]
    def fail(route):
        writes.append(route.request.post_data)
        route.fulfill(status=503,json={'error':'fixture unavailable'})
    page.route('**/api/workbench/skills/state',fail)
    card.get_by_role('button',name=chosen,exact=True).click()
    expect(card.locator('.wb-error')).to_contain_text('fixture unavailable')
    assert card.locator('[aria-pressed=true]').text_content()==previous
    expect(card.get_by_role('button',name=chosen,exact=True)).to_be_enabled()
    assert len(writes)==1


def test_task_filter_selection_and_cancel_updates_open_detail(browser_context,workspace_url):
    page=browser_context.new_page();page.add_init_script(PROBE);ready(page,workspace_url['url'],'tasks')
    for row in page.locator('.wb-task-table tbody tr').all():
        if row.locator('.wb-tag').text_content() in ['已完成','失败','已取消']:
            expect(row.locator('input[type=checkbox]')).to_be_disabled()
    page.get_by_label('选择本页全部未结束任务').check()
    assert page.locator('tbody input:enabled:not(:checked)').count()==0
    expect(page.get_by_role('button',name='取消选中',exact=True)).to_be_enabled()
    page.get_by_label('选择本页全部未结束任务').uncheck()
    page.evaluate("window.__delay['/api/workbench/tasks']=400")
    search=page.get_by_role('searchbox',name='搜索任务名称、命令或 ID')
    search.fill('每日服务')
    page.wait_for_timeout(350)
    assert page.locator('#wb-page > .wb-loading').count()==0
    expect(page.locator('.wb-task-table tbody tr')).to_have_count(1)
    expect(search).to_be_focused()
    page.get_by_role('button',name='每日服务健康检查',exact=True).click()
    page.locator('#wb-detail').get_by_role('button',name='取消任务',exact=True).click()
    expect(page.locator('#wb-dialog')).to_contain_text('Telegram、网页和 CLI')
    page.locator('#wb-dialog').get_by_role('button',name='确认操作',exact=True).click()
    expect(page.locator('#wb-detail .wb-tag').first).to_have_text('已取消')
    assert page.locator('#wb-detail').get_by_role('button',name='取消任务',exact=True).count()==0


def test_trigger_form_remembers_modes_timezone_and_live_summary(browser_context,workspace_url):
    page=browser_context.new_page();ready(page,workspace_url['url'],'tasks')
    page.get_by_role('button',name='创建任务',exact=True).click();dialog=page.locator('#wb-dialog')
    dialog.locator('[name=expression]').fill('17m')
    dialog.locator('[name=timezone]').fill('UTC')
    dialog.locator('[name=mode]').select_option('cron')
    dialog.get_by_role('button',name='工作日 09:00',exact=True).click()
    expect(dialog.locator('[name=expression]')).to_have_value('0 9 * * mon-fri')
    dialog.locator('[name=mode]').select_option('when')
    dialog.locator('[name=expression]').fill('DONE');dialog.locator('[name=repeat]').check()
    dialog.locator('[name=mode]').select_option('after')
    expect(dialog.locator('[name=expression]')).to_have_value('17m')
    expect(dialog.locator('[name=timezone]')).to_have_value('UTC')
    expect(dialog.locator('.wb-callout')).to_contain_text('17m · 时区：UTC')
    dialog.locator('[name=mode]').select_option('when')
    expect(dialog.locator('[name=expression]')).to_have_value('DONE')
    expect(dialog.locator('[name=repeat]')).to_be_checked()
    page.on('dialog',lambda d:d.accept());page.keyboard.press('Escape')


@pytest.mark.parametrize('width,height',[(390,844),(768,1024),(1440,900)])
def test_management_visuals_no_overflow_and_touch_targets(browser_context,workspace_url,width,height):
    page=browser_context.new_page();page.set_viewport_size({'width':width,'height':height});page.emulate_media(reduced_motion='reduce')
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    output=Path(__file__).resolve().parents[1]/'workspace'/'web-management-qa';output.mkdir(parents=True,exist_ok=True)
    for theme in ['light','dark']:
        ready(page,workspace_url['url'],'tasks')
        page.evaluate('(theme)=>{localStorage.setItem("xgent-theme",theme);document.body.classList.toggle("dark",theme==="dark");}',theme)
        for mode in ['tasks','skills']:
            if mode=='skills':
                page.evaluate('location.hash="/models"');page.get_by_role('button',name='技能库',exact=True).wait_for()
                page.get_by_role('button',name='技能库',exact=True).click();page.locator('.wb-skill-card').first.wait_for()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            assert page.locator('#wb-page').evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
            if width==390 and mode=='skills':
                assert page.locator('.wb-skill-switch button').first.bounding_box()['height']>=44
                assert page.get_by_label('技能状态筛选').bounding_box()['width']>=100
            page.screenshot(path=str(output/f'{width}-{theme}-{mode}.png'))
    assert errors==[]
