import uuid
import pytest
from tests.test_workbench_browser import workspace_url,browser_context,ready


def open_tab(context,url,tab):
    page=context.new_page();ready(page,url,'models')
    page.get_by_role('button',name=tab,exact=True).click()
    page.get_by_role('button',name='新建技能' if tab=='技能库' else '新建记忆',exact=True).wait_for()
    return page


def test_skill_create_inline_rename_sections_and_editable_raw_source(browser_context,workspace_url):
    url=workspace_url['url'];page=open_tab(browser_context,url,'技能库')
    before={d['path'] for d in browser_context.request.get(url+'/api/workbench/skills').json()['items']}
    page.get_by_role('button',name='新建技能',exact=True).click()
    summary=page.get_by_role('textbox',name='技能简介',exact=True);summary.wait_for()
    items=browser_context.request.get(url+'/api/workbench/skills').json()['items'];created=next(d for d in items if d['path'] not in before)
    detail=browser_context.request.get(url+'/api/workbench/skills',params={'path':created['path']}).json()
    assert detail['content']=='' and detail['state']=='disabled'
    assert page.locator('#wb-detail .knowledge-section').count()==2
    summary.fill('**只需要简介也可以**')
    page.get_by_role('button',name='保存修改',exact=True).click()
    page.get_by_text('已保存，后续对话生效',exact=True).wait_for()
    saved=browser_context.request.get(url+'/api/workbench/skills',params={'path':created['path']}).json()
    assert saved['body']=='' and saved['summary']=='**只需要简介也可以**'
    page.locator('#wb-detail-close').click()
    card=page.locator('.knowledge-card[data-skill-path="'+created['path']+'"]')
    assert '只需要简介' in card.locator('.knowledge-card-preview').inner_text()
    card.get_by_role('button',name='隐藏',exact=True).click()
    pytest.importorskip('playwright.sync_api').expect(card.get_by_role('button',name='隐藏',exact=True)).to_have_attribute('aria-pressed','true')
    card.locator('.knowledge-rename-trigger').click()
    filename='浏览器技能-'+uuid.uuid4().hex[:6]+'.md'
    card.get_by_label('文件名',exact=True).fill(filename)
    card.get_by_role('button',name='保存名称',exact=True).click()
    renamed='private/'+filename;card=page.locator('.knowledge-card[data-skill-path="'+renamed+'"]');card.wait_for()
    assert browser_context.request.get(url+'/api/workbench/skills',params={'path':renamed}).json()['state']=='hidden'
    card.get_by_role('button',name='管理',exact=True).click()
    page.get_by_role('button',name='源码',exact=True).click()
    source=page.get_by_role('textbox',name='源码全文',exact=True)
    assert chr(96)*3+'!' in source.input_value()
    raw='# 正文标题\n\n'+chr(96)*3+'!\n源码简介\n'+chr(96)*3+'\n正文尾部\n<script>window.__knowledgeXss=1</script>\n'
    source.fill(raw)
    page.get_by_role('button',name='分块管理',exact=True).click()
    assert page.locator('#wb-detail .knowledge-section').count()==2
    assert not page.evaluate('Boolean(window.__knowledgeXss)')
    page.get_by_role('button',name='保存修改',exact=True).click();page.get_by_text('已保存，后续对话生效',exact=True).wait_for()
    assert browser_context.request.get(url+'/api/workbench/skills',params={'path':renamed}).json()['content']==raw
    page.get_by_role('button',name='编辑正文',exact=True).click();page.get_by_role('textbox',name='技能正文',exact=True).fill('不要丢弃的草稿')
    page.once('dialog',lambda d:d.dismiss());page.locator('#wb-detail-close').click()
    assert page.get_by_role('textbox',name='技能正文',exact=True).input_value()=='不要丢弃的草稿'
    page.once('dialog',lambda d:d.accept());page.locator('#wb-detail-close').click()
    card.get_by_role('button',name='删除',exact=True).click();page.locator('#wb-dialog').get_by_role('button',name='确认操作',exact=True).click()
    card.wait_for(state='detached')
    assert not browser_context.request.get(url+'/api/workbench/skills',params={'path':renamed}).ok
    page.close()


def test_memory_crud_and_shared_draft_survives_conversation_switch(browser_context,workspace_url):
    url=workspace_url['url'];page=open_tab(browser_context,url,'记忆')
    before={d['path'] for d in browser_context.request.get(url+'/api/workbench/memories').json()['items']}
    page.get_by_role('button',name='新建记忆',exact=True).click();text=page.get_by_role('textbox',name='记忆内容',exact=True);text.wait_for()
    text.fill('我喜欢中文回答\n第二条手工记忆')
    page.get_by_role('button',name='保存修改',exact=True).click();page.get_by_text('已保存，后续对话生效',exact=True).wait_for()
    items=browser_context.request.get(url+'/api/workbench/memories').json()['items'];item=next(d for d in items if d['path'] not in before)
    text.fill('切换时保留的共享记忆草稿')
    result=browser_context.request.post(url+'/api/workbench/conversations/create',data={'name':'编辑记忆时切换'}).json()
    page.wait_for_function('(id)=>window.XGentConversations.current===id',arg=result['current_chat_id'])
    assert text.is_visible() and text.input_value()=='切换时保留的共享记忆草稿'
    page.get_by_role('button',name='保存修改',exact=True).click();page.get_by_text('已保存，后续对话生效',exact=True).wait_for()
    assert browser_context.request.get(url+'/api/workbench/memories',params={'path':item['path']}).json()['content']=='切换时保留的共享记忆草稿'
    page.locator('#wb-detail-close').click()
    card=page.locator('.knowledge-card[data-memory-path="'+item['path']+'"]');assert '共享记忆草稿' in card.inner_text()
    card.get_by_role('button',name='查看 / 修改',exact=True).click();text.wait_for();assert text.input_value()=='切换时保留的共享记忆草稿'
    page.locator('#wb-detail-close').click();card.get_by_role('button',name='删除',exact=True).click();page.locator('#wb-dialog').get_by_role('button',name='确认操作',exact=True).click()
    card.wait_for(state='detached');page.close()


def test_task_source_search_finds_history_without_switching_chat(browser_context,workspace_url):
    url=workspace_url['url'];created=browser_context.request.post(url+'/api/workbench/conversations/create',data={'name':'保持当前搜索视图'}).json()['current_chat_id']
    page=browser_context.new_page();ready(page,url,'tasks')
    select=page.get_by_label('任务来源对话',exact=True);search=page.get_by_role('button',name='搜索对话记录',exact=True)
    assert search.bounding_box()['x']>=select.bounding_box()['x']+select.bounding_box()['width']
    search.click();page.get_by_role('searchbox',name='搜索来源对话',exact=True).fill('开发过程')
    result=page.locator('.wb-source-result').filter(has_text='默认会话');result.wait_for();assert '开发过程' in result.inner_text();result.click()
    pytest.importorskip('playwright.sync_api').expect(page.get_by_label('任务来源对话',exact=True)).to_have_value('global_memory')
    assert browser_context.request.get(url+'/api/workbench/conversations').json()['current_chat_id']==created
    page.close()


@pytest.mark.parametrize('size',[(360,640),(768,1024),(1440,900)])
def test_header_font_settings_and_removed_reset_card(browser_context,workspace_url,size,tmp_path):
    page=browser_context.new_page();page.set_viewport_size({'width':size[0],'height':size[1]});ready(page,workspace_url['url'])
    menu=page.get_by_role('button',name='对话记录',exact=True);title=page.locator('#wb-conversation-title');header=page.locator('#header')
    assert menu.bounding_box()['x']-header.bounding_box()['x']<24
    center=title.bounding_box()['x']+title.bounding_box()['width']/2
    assert abs(center-(header.bounding_box()['x']+header.bounding_box()['width']/2))<2
    assert not menu.inner_text().strip()
    colors=page.locator('#btn-composer-menu').evaluate('(e)=>({bg:getComputedStyle(e).backgroundColor,color:getComputedStyle(e).color})')
    assert colors['color']=='rgb(255, 255, 255)' and colors['bg'] not in ['rgba(0, 0, 0, 0)','rgb(255, 255, 255)']
    if size[0]>=768:
        menu.click();assert page.locator('#wb-nav').is_hidden();menu.click();assert page.locator('#wb-nav').is_visible()
    else:
        menu.click();assert page.locator('#wb-nav').is_visible();page.locator('#conversation-drawer-close').click()
    page.evaluate('location.hash="/settings"');page.get_by_role('heading',name='文字字号',exact=True).wait_for()
    slider=page.get_by_role('slider',name='消息字号滑块',exact=True);slider.focus();slider.press('Home')
    for _ in range(6):slider.press('ArrowRight')
    assert page.get_by_label('消息字号（px）',exact=True).input_value()=='18'
    page.get_by_role('button',name='保存字号',exact=True).click();page.get_by_text('字号已保存，其他网页设备同步生效。',exact=True).wait_for()
    assert browser_context.request.get(workspace_url['url']+'/api/config').json()['values']['web_message_font_size']==18
    page.screenshot(path=str(tmp_path/f'fonts-{size[0]}.png'))
    page.get_by_role('tab',name='对话与记忆',exact=True).click()
    assert page.get_by_text('当前会话上下文',exact=True).count()==0
    assert page.get_by_role('button',name='重置上下文',exact=True).count()==0
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.evaluate('window.XGentAppearance.save(window.XGentAppearance.defaults)');page.close()


def test_full_source_save_handles_json_expansion_without_raising_other_body_limits(browser_context,workspace_url):
    url=workspace_url['url'];created=browser_context.request.post(url+'/api/workbench/memories/create',data={}).json()['item']
    content='开头'+chr(10)*600000+'末尾'
    saved=browser_context.request.post(url+'/api/workbench/memories/update',data={'path':created['path'],'revision':created['revision'],'content':content})
    assert saved.ok,saved.text()
    item=saved.json()['item'];assert item['content']==content
    too_large=browser_context.request.post(url+'/api/workbench/memories/update',data={'path':item['path'],'revision':item['revision'],'content':'x'*(2*1024*1024+1)})
    assert too_large.status==413
    assert browser_context.request.get(url+'/api/workbench/memories',params={'path':item['path']}).json()['content']==content
    assert browser_context.request.post(url+'/api/workbench/memories/delete',data={'path':item['path'],'revision':item['revision'],'confirm':True}).ok
