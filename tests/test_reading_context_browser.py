"""Browser acceptance of compact reading, global typography and scoped management."""
import uuid
import pytest
from tests.test_workbench_browser import workspace_url, browser_context, ready


def create(context,url,name):
    result=context.request.post(url+'/api/workbench/conversations/create',data={'name':name})
    assert result.ok,result.text()
    return result.json()['current_chat_id']


@pytest.mark.parametrize('size',[(360,640),(390,844),(844,390),(1440,900)])
def test_compact_header_fonts_and_options(browser_context,workspace_url,size,tmp_path):
    page=browser_context.new_page();page.set_viewport_size({'width':size[0],'height':size[1]});ready(page,workspace_url['url'])
    page.wait_for_function('window.XGentAppearance && document.querySelectorAll("#log .msg-row").length>0')
    page.evaluate('window.XGentAppearance.save(window.XGentAppearance.defaults)')
    metrics=page.locator('#log').bounding_box()
    assert metrics['y']==56
    if size==(360,640):assert metrics['height']>=390
    assert page.locator('.bubble').first.evaluate('(e)=>getComputedStyle(e).fontSize')=='16px'
    assert page.locator('#btn-menu,#menu-panel').count()==0
    assert page.locator('#chat-options').count()==0
    assert page.get_by_role('button',name='切换主题',exact=True).is_visible()
    assert page.get_by_role('combobox',name='对话模型',exact=True).is_visible()
    page.keyboard.press('Escape')
    for message,ui in [(12,12),(24,20)]:
        page.evaluate('([message,ui])=>window.XGentAppearance.save({web_message_font_size:message,web_ui_font_size:ui})',[message,ui])
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        assert page.locator('#input').is_visible()
    page.screenshot(path=str(tmp_path/f'reading-{size[0]}.png'))
    page.evaluate('window.XGentAppearance.save(window.XGentAppearance.defaults)')
    page.close()


def test_fonts_sync_other_tab_without_losing_draft(browser_context,workspace_url):
    url=workspace_url['url'];a=browser_context.new_page();b=browser_context.new_page();ready(a,url);ready(b,url)
    b.locator('#input').fill('不可丢失的草稿')
    a.evaluate('window.XGentAppearance.save({web_message_font_size:19,web_ui_font_size:15})')
    b.wait_for_function('window.XGentAppearance.state.values.web_message_font_size===19',timeout=5000)
    assert b.locator('#input').input_value()=='不可丢失的草稿'
    b.reload();ready(b,url)
    b.wait_for_function('window.XGentAppearance.state.values.web_ui_font_size===15')
    assert browser_context.request.get(url+'/api/config').json()['values']['web_message_font_size']==19
    rejected=browser_context.request.post(url+'/api/config',data={'key':'web_message_font_size','value':999})
    assert not rejected.ok
    a.evaluate('window.XGentAppearance.save(window.XGentAppearance.defaults)')
    a.close();b.close()


def test_all_task_sources_archive_filter_detail_cancel_and_delete(browser_context,workspace_url):
    url=workspace_url['url'];a=create(browser_context,url,'来源 A');b=None;ids=[]
    for name in ['任务 A','任务 B']:
        cid=a if name=='任务 A' else create(browser_context,url,'来源 B')
        if name=='任务 B':b=cid
        response=browser_context.request.post(url+'/api/workbench/tasks/create',data={'conversation_id':cid,'request_id':uuid.uuid4().hex,'command':'echo test','task':name,'after':'5m','timezone':'Asia/Shanghai'})
        assert response.ok,response.text()
        listed=browser_context.request.get(url+'/api/workbench/tasks',params={'source_conversation_id':cid}).json()
        assert len(listed['items'])==1
        ids.append(listed['items'][0]['id'])
    assert browser_context.request.post(url+'/api/workbench/conversations/archive',data={'id':a}).ok
    all_tasks=browser_context.request.get(url+'/api/workbench/tasks').json()
    assert set(ids)<=set(t['id'] for t in all_tasks['items'])
    detail=browser_context.request.get(url+'/api/workbench/tasks',params={'id':ids[0]}).json()
    assert detail['task']['conversation_archived']
    assert detail['task']['conversation_name']=='来源 A'
    page=browser_context.new_page();ready(page,url,'tasks')
    page.get_by_label('任务来源对话',exact=True).select_option(a)
    page.wait_for_function('document.querySelectorAll(".wb-task-table tbody tr").length===1')
    assert '任务 A' in page.locator('.wb-task-table').inner_text()
    assert '已归档' in page.locator('.wb-task-table').inner_text()
    assert browser_context.request.post(url+'/api/workbench/tasks/cancel',data={'ids':[ids[0]],'confirm':True}).ok
    assert browser_context.request.get(url+'/api/workbench/conversations').json()['current_chat_id']==b
    assert browser_context.request.post(url+'/api/workbench/conversations/delete',data={'id':a,'confirm':True}).ok
    assert not browser_context.request.get(url+'/api/workbench/tasks',params={'id':ids[0]}).ok
    assert browser_context.request.get(url+'/api/workbench/conversations').json()['current_chat_id']==b
    page.close()


def test_card_action_scrolls_to_new_reply(browser_context,workspace_url):
    url=workspace_url['url'];page=browser_context.new_page();ready(page,url)
    cid=page.evaluate('window.XGentConversations.current')
    response=browser_context.request.post(url+'/api/command',data={'command':'/start','conversation_id':cid});assert response.ok
    page.get_by_role('button',name='⚙️ 更多',exact=True).last.wait_for()
    page.get_by_role('button',name='⚙️ 更多',exact=True).last.click()
    status=page.get_by_role('button',name='ℹ️ 状态',exact=True).last;status.wait_for()
    # Make the clicked card not be the last message, reproducing reading away from bottom.
    assert browser_context.request.post(url+'/api/command',data={'command':'/start','conversation_id':cid}).ok
    page.get_by_role('button',name='⚙️ 更多',exact=True).last.wait_for()
    status.scroll_into_view_if_needed();status.click()
    page.wait_for_function('document.querySelector("#log").scrollHeight-document.querySelector("#log").scrollTop-document.querySelector("#log").clientHeight<4')
    assert not status.is_disabled() if status.count() else True
    page.close()


def test_clear_retains_visible_history_and_old_menu_becomes_readonly(browser_context,workspace_url):
    url=workspace_url['url'];cid=create(browser_context,url,'重置历史测试')
    page=browser_context.new_page();ready(page,url)
    assert browser_context.request.post(url+'/api/command',data={'command':'/chats','conversation_id':cid}).ok
    page.get_by_role('button',name='🏷 重命名当前会话',exact=True).wait_for()
    before=browser_context.request.get(url+'/api/workbench/history',params={'conversation_id':cid,'limit':50}).json()
    saved=next(m for m in before['messages'] if m.get('ui_message_id'))
    assert browser_context.request.post(url+'/api/workbench/conversations/reset_context',data={'id':cid,'confirm':True}).ok
    page.get_by_role('button',name='🏷 重命名当前会话',exact=True).wait_for(state='visible')
    page.wait_for_function('Array.from(document.querySelectorAll(".ik-btn")).some(b=>b.textContent.includes("重命名")&&b.disabled)')
    after=browser_context.request.get(url+'/api/workbench/history',params={'conversation_id':cid,'limit':50}).json()
    assert any(m.get('ui_message_id')==saved['ui_message_id'] and m.get('readonly') for m in after['messages'])
    stale=browser_context.request.post(url+'/api/callback',data={'conversation_id':cid,'ui_message_id':saved['ui_message_id'],'revision':saved['revision'],'button_id':'0:0'})
    assert stale.ok # Admission is async; validated button sends an alert, never executes.
    page.close()


def test_font_form_preview_save_cancel_and_remote_edit_protection(browser_context,workspace_url):
    url=workspace_url['url'];page=browser_context.new_page();ready(page,url,'settings')
    field=page.get_by_label('消息字号（px）',exact=True)
    field.wait_for()
    saved=browser_context.request.get(url+'/api/config').json()['values']['web_message_font_size']
    field.fill('22')
    assert page.evaluate('getComputedStyle(document.documentElement).getPropertyValue("--web-message-font-size").trim()')=='22px'
    page.once('dialog',lambda dialog:dialog.accept())
    page.evaluate('location.hash="/chat"')
    page.wait_for_function('(saved)=>getComputedStyle(document.documentElement).getPropertyValue("--web-message-font-size").trim()===saved+"px"',arg=saved)
    page.evaluate('location.hash="/settings"');field.wait_for()
    assert field.input_value()==str(saved)
    field.fill('18');page.get_by_role('button',name='保存字号',exact=True).click()
    page.get_by_text('字号已保存，其他网页设备同步生效。',exact=True).wait_for()
    assert browser_context.request.get(url+'/api/config').json()['values']['web_message_font_size']==18
    field.fill('22')
    assert browser_context.request.post(url+'/api/config',data={'key':'web_message_font_size','value':19}).ok
    page.wait_for_function('window.XGentAppearance.state.values.web_message_font_size===19')
    assert field.input_value()=='22'
    page.get_by_role('button',name='取消预览',exact=True).click()
    assert field.input_value()=='19'
    page.evaluate('window.XGentAppearance.save(window.XGentAppearance.defaults)')
    page.close()
