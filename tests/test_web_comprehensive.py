"""New UI behaviors on the real loopback server, never production data."""
# ruff: noqa: F811 - imported pytest fixtures are dependency injection parameters

import time

from tests.test_workbench_browser import (  # noqa: F401
    browser_context,
    ready,
    workspace_url,
)


def test_group_pins_are_isolated_and_switching_groups_never_switches_chat(
    browser_context, workspace_url
):
    base = workspace_url["url"]
    page = browser_context.new_page()
    ready(page, base)

    def action(resource, **data):
        state = browser_context.request.get(
            base + "/api/workbench/conversations"
        ).json()
        response = browser_context.request.post(
            base + "/api/workbench/" + resource,
            data={**data, "revision": state["revision"]},
        )
        assert response.ok, response.text()
        return response.json()

    a = action("conversations/create", name="pin isolation old")["current_chat_id"]
    b = action("conversations/create", name="pin isolation new")["current_chat_id"]
    ga = action("conversation-groups/create", name="UI project A")["group_id"]
    gb = action("conversation-groups/create", name="UI project B")["group_id"]
    action("conversations/move", id=a, group_id=ga)
    action("conversations/move", id=b, group_id=ga)
    action("conversations/pin", id=a, group_id=ga)
    page.evaluate("window.XGentConversations.refresh()")
    page.locator("#conversation-group-select").select_option(ga)
    page.wait_for_function('document.querySelectorAll(".conv-row").length===2')
    assert page.locator(".conv-row").first.get_attribute("data-conversation-id") == a
    selected = page.evaluate("window.XGentConversations.current")
    page.locator("#conversation-group-select").select_option(gb)
    assert page.locator(".conv-row").count() == 0
    assert page.evaluate("window.XGentConversations.current") == selected
    page.locator("#conversation-group-select").select_option("all")
    assert page.locator(".conv-row.is-pinned").count() == 0
    ids = page.locator(".conv-row").evaluate_all(
        "(rows)=>rows.map(row=>row.dataset.conversationId)"
    )
    assert ids.index(b) < ids.index(a)
    page.locator("#conversation-search").fill("pin isolation")
    result = page.locator(".global-search-hit").first
    result.wait_for()
    assert result.get_attribute("data-search-key").startswith(b + ":")
    page.close()


def test_backup_ui_is_explicit_separate_from_chat(browser_context, workspace_url):
    page = browser_context.new_page()
    ready(page, workspace_url["url"], "settings")
    page.get_by_role("tab", name="数据与备份", exact=True).click()
    page.get_by_role("button", name="立即压缩", exact=True).wait_for()
    checkbox = page.get_by_role("checkbox", name="导出 命令完整输出", exact=True)
    assert not checkbox.is_checked()
    text = page.locator("#wb-page").inner_text()
    assert "后台压缩，不停服、不重启" in text
    assert "点击名称，右侧立即扫描内容" in text
    assert "选择后立即上传并扫描，不会直接导入" in text
    assert page.get_by_role("button", name="上传压缩包", exact=True).count() == 1
    assert page.get_by_label("上传压缩包以扫描内容", exact=True).count() == 1
    page.close()


def test_ai_metadata_and_copy_buttons_are_inside_own_card(
    browser_context, workspace_url, tmp_path
):
    page = browser_context.new_page()
    ready(page, workspace_url["url"])
    page.wait_for_function('!document.getElementById("btn-send").disabled')
    timestamp = time.time() - 60
    page.evaluate(
        """(ts)=>{
      window.XGentChat.renderRecord({role:'user',msg_type:'user_text',content:'user copy target',timestamp:ts},document.getElementById('log'));
      window.XGentChat.renderRecord({role:'assistant',msg_type:'ai_reply',content:'assistant copy target',timestamp:ts,ai_generation:{provider:'test provider',model:'exact-model',generated_at:ts}},document.getElementById('log'));
      document.getElementById('log').scrollTop=document.getElementById('log').scrollHeight;
      document.getElementById('empty-state').style.display='none';
    }""",
        timestamp,
    )
    row = page.locator(".msg-row").filter(has_text="assistant copy target")
    assert (
        "test provider / exact-model" in row.locator(".ai-generation-meta").inner_text()
    )
    card = row.locator(".bubble").bounding_box()
    copy = row.get_by_role("button", name="复制 AI 回复").bounding_box()
    assert (
        card["y"] <= copy["y"]
        and copy["y"] + copy["height"] <= card["y"] + card["height"]
    )
    page.screenshot(path=str(tmp_path / "message-metadata-and-copy.png"))
    page.close()
