import sqlite3
import time

from tests import test_workbench_browser as fixture_helpers

workspace_url = fixture_helpers.workspace_url
browser_context = fixture_helpers.browser_context
ready = fixture_helpers.ready


def test_legacy_history_can_branch_with_draft_and_lineage_warning(browser_context, workspace_url):
    base = workspace_url['url']
    created = browser_context.request.post(base + '/api/workbench/conversations/create',
                                             data={'name': '分支界面验收'}).json()
    source = created['current_chat_id']
    with sqlite3.connect(workspace_url['database_path']) as conn:
        now = time.time()
        conn.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id) "
                     "VALUES(1,1,'user_text','user','需要修改的旧消息',?,?)", (now, source))
        conn.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id) "
                     "VALUES(1,0,'ai_reply','assistant','旧回答',?,?)", (now + 1, source))
        conn.execute("INSERT INTO global_messages(chat_id,user_id,msg_type,role,content,timestamp,session_id) "
                     "VALUES(1,1,'command','user','/update',?,?)", (now + 2, source))
        conn.commit()
    page = browser_context.new_page()
    ready(page, base)
    page.get_by_text('需要修改的旧消息', exact=True).wait_for()
    cmd_row = page.get_by_text('/update', exact=True).locator('xpath=ancestor::div[contains(@class,"msg-row")]')
    assert cmd_row.get_by_role('button', name='撤回此消息及之后').is_visible()
    row = page.get_by_text('需要修改的旧消息', exact=True).locator('xpath=ancestor::div[contains(@class,"msg-row")]')
    row.get_by_role('button', name='编辑并创建分支').click()
    dialog = page.locator('#wb-dialog')
    dialog.get_by_text('此历史没有文件快照', exact=False).wait_for()
    dialog.get_by_role('button', name='创建分支', exact=True).click()
    page.wait_for_function('(source)=>window.XGentConversations.current!==source', arg=source)
    assert page.locator('#log').get_by_text('/update', exact=True).count() == 0
    page.locator('#conversation-list .conv-row.is-branch .conv-branch-badge').first.wait_for()
    page.locator('#composer-generation-options.has-lineage').wait_for()
    page.get_by_role('button', name='生成选项', exact=True).click()
    page.locator('#conversation-lineage').get_by_text('仅上下文', exact=False).wait_for()
    assert page.locator('#input').input_value() == '需要修改的旧消息'
    assert page.locator('#conversation-lineage').get_by_role('button', name='打开来源').is_visible()
    page.locator('#conversation-lineage').get_by_role('button', name='打开来源').click()
    page.wait_for_function('(source)=>window.XGentConversations.current===source', arg=source)
    cmd_row = page.get_by_text('/update', exact=True).locator('xpath=ancestor::div[contains(@class,"msg-row")]')
    cmd_row.get_by_role('button', name='撤回此消息及之后').click()
    dialog.get_by_role('button', name='确认撤回', exact=True).click()
    page.wait_for_function('()=>!Array.from(document.querySelectorAll("#log .bubble")).some(n=>n.textContent.includes("/update"))')
    page.close()


def test_old_withdraw_explains_and_offers_safe_branch(browser_context, workspace_url):
    base = workspace_url['url']
    browser_context.request.post(base + '/api/workbench/conversations/switch', data={'id': 'global_memory'})
    page = browser_context.new_page(); ready(page, base)
    user = page.locator('#log .msg-row.user').last
    user.get_by_role('button', name='撤回此消息及之后').click()
    dialog = page.locator('#wb-dialog')
    dialog.get_by_text('没有可靠文件现场', exact=False).wait_for()
    assert dialog.get_by_role('button', name='改为从此前创建分支').is_visible()
    page.close()
