"""Browser acceptance for the two-column backup workbench, with isolated API fixtures."""
import time
from urllib.parse import parse_qs, urlparse

import pytest

from tests.test_workbench_browser import browser_context, workspace_url, ready  # noqa: F401
from xgent_app.data_contents import CATEGORIES, RECOMMENDED_CATEGORIES


def backup_api(page, import_keys=None):
    records = []
    categories = [{**item, "size": 0, "file_count": 0, "files": []} for item in CATEGORIES]
    by_id = {item["id"]: item for item in categories}
    for key, size, files in [
        ("secrets", 512, [{"name": "backup_parts/secrets.jsonl", "size": 512}]),
        ("ai_config", 2048, [{"name": "backup_parts/ai_config.jsonl", "size": 2048}]),
        ("memory", 2048, [{"name": "memory/note.md", "size": 1536}, {"name": "memory/second.md", "size": 512}]),
        ("command_outputs", 16384, [{"name": "xgent_storage/command_outputs/result.txt", "size": 16384}]),
    ]:
        by_id[key].update(size=size, file_count=len(files), files=files)
    formats = [{"id": "zip", "estimated_seconds": 1.2, "estimated_size": 1000},
               {"id": "tar.gz", "estimated_seconds": 0.8, "estimated_size": 1200},
               {"id": "tar.xz", "estimated_seconds": 2.5, "estimated_size": 640}]
    rules = {"enabled": True, "frequency": "6h", "mode": "accumulate", "max_backups": 7,
             "categories": list(RECOMMENDED_CATEGORIES), "archive_format": "zip", "include_command_outputs": False,
             "backup_directory": "/fixture/backups", "default_backup_directory": "/fixture/default"}
    names = ["manual-fixture.zip", "manual-output.tar.xz"]
    jobs = {}
    def catalog():
        return {"directory": rules["backup_directory"], "items": [
            {"name": name, "label": "手动备份", "size": 4096, "created_at": 1793000000,
             "file_count": 3, "archive_format": "zip" if name.endswith("zip") else "tar.xz",
             "categories": [by_id[key] for key in (["secrets", "ai_config", "memory"] if name.endswith("zip") else ["command_outputs"])]} for name in names]}
    def import_job(identifier, source, keys):
        rows = [{**item, "included": True, "current_size": item["size"] * 2,
                 "current_file_count": item["file_count"] + 1, "replace_paths": item["paths"]}
                for item in categories if item["id"] in keys]
        jobs[identifier] = {"id": identifier, "kind": "import", "state": "ready", "phase": "verified",
                            "source_name": source, "preview": {"restore_mode": "selection", "selectable": True,
                            "recommended_categories": keys,
                            "archive_format": "tar.xz" if source.endswith("xz") else "zip", "categories": rows,
                            "file_count": sum(item["file_count"] for item in rows),
                            "size": sum(item["size"] for item in rows)}}
        return {"id": identifier, "kind": "import", "state": "running", "phase": "verifying"}
    def handle(route):
        request = route.request
        url = urlparse(request.url)
        resource = url.path.split("/data/", 1)[1]
        query = parse_qs(url.query)
        data = {}
        if request.method == "POST" and resource != "import":
            data = request.post_data_json or {}
        records.append((resource, data))
        result = {}
        if resource == "estimate":
            selected = query.get("categories", [",".join(RECOMMENDED_CATEGORIES)])[0].split(",")
            rows = [{**item, "selected": item["id"] in selected} for item in categories]
            result = {"categories": rows, "selected_categories": selected, "recommended_categories": list(RECOMMENDED_CATEGORIES), "formats": formats,
                      "size": sum(item["size"] for item in rows if item["selected"]),
                      "file_count": sum(item["file_count"] for item in rows if item["selected"]),
                      "estimate_note": "实际抽样估算；时间和大小不是保证值。"}
        elif resource == "backup-settings":
            if request.method == "POST":rules.update(data)
            result = rules
        elif resource == "backups":result = catalog()
        elif resource == "backups/delete":
            names[:] = [name for name in names if name not in data["names"]]
            result = catalog()
        elif resource == "import-stored":
            output = data["name"].endswith("xz")
            result = import_job(("b" if output else "a") * 32, data["name"], import_keys or (["command_outputs"] if output else ["secrets", "ai_config", "memory"]))
        elif resource == "import":result = import_job("c" * 32, "uploaded.zip", ["memory"])
        elif resource == "import/cancel":
            if data["id"] in jobs:jobs[data["id"]]["state"] = "cancelled"
            result = {"id": data["id"], "state": "cancelled"}
        elif resource == "export":
            jobs["e" * 32] = {"id": "e" * 32, "kind": "export", "state": "completed", "phase": "completed",
                               "result": {"size": 640}, "download_url": "/api/workbench/data/download?id=" + "e" * 32}
            result = {"id": "e" * 32, "kind": "export", "state": "running"}
        elif resource == "activate":
            jobs[data["id"]].update(state="completed", phase="completed")
            result = {"id": data["id"], "state": "activating"}
        elif resource == "status":result = jobs[query["id"][0]]
        else:
            route.fulfill(status=404, json={"error": "unexpected fixture route: " + resource})
            return
        route.fulfill(json={"ok": True, **result})
    page.route("**/api/workbench/data/**", handle)
    return records


def open_backups(page, url):
    page.set_default_timeout(10000)
    page.on("pageerror", lambda error: print("Browser error:", str(error)))
    page.on("console", lambda msg: print("Console:", msg.text) if msg.type == "error" else None)
    try:
        ready(page, url, "settings")
        page.get_by_role("tab", name="数据与备份", exact=True).click(timeout=5000)
        page.locator(".wb-backup-config-card").wait_for(timeout=10000)
    except Exception:
        print("Backup page diagnostic:", page.locator("#wb-page").inner_text())
        print("URL:", page.url)
        raise


@pytest.mark.parametrize("width,height", [(1440, 900), (768, 1024), (390, 844)])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_backup_layout_estimates_and_accessible_selection(browser_context, workspace_url, tmp_path, width, height, theme):
    page = browser_context.new_page();page.set_viewport_size({"width": width, "height": height})
    errors = [];page.on("pageerror", lambda error: errors.append(str(error)))
    backup_api(page)
    open_backups(page, workspace_url["url"])
    page.evaluate("theme=>document.body.classList.toggle('dark',theme==='dark')", theme)
    assert page.locator(".wb-backup-format").count() == 3
    assert "1.2 秒" in page.locator(".wb-backup-format").first.inner_text()
    assert page.get_by_label("导出 密钥与登录凭据", exact=True).is_checked()
    assert page.get_by_label("导出 AI 与模型配置", exact=True).is_checked()
    assert page.locator("[data-export-category]").count() == len(CATEGORIES) > 20
    assert page.locator('[data-export-category="uploads"]').count() == 1
    assert "0 B" in page.locator('[data-export-category="uploads"]').inner_text()
    assert not page.get_by_label("导出 命令完整输出", exact=True).is_checked()
    assert page.get_by_label("压缩频率", exact=True).input_value() == "6h"
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
    page.screenshot(path=str(tmp_path / (theme + "-" + str(width) + "-config.png")))
    page.get_by_role("button", name="全不选", exact=True).click()
    assert page.get_by_role("button", name="立即压缩", exact=True).is_disabled()
    page.get_by_label("导出 记忆文件", exact=True).check()
    assert page.get_by_role("button", name="立即压缩", exact=True).is_enabled()
    assert "已选 1 类" in page.locator(".wb-backup-total").inner_text()
    left = page.locator(".wb-backup-catalog-card");right = page.locator(".wb-backup-import-card")
    left.scroll_into_view_if_needed()
    if width == 1440:
        assert left.bounding_box()["x"] + left.bounding_box()["width"] <= right.bounding_box()["x"]
    else:
        assert left.bounding_box()["y"] + left.bounding_box()["height"] <= right.bounding_box()["y"]
    page.screenshot(path=str(tmp_path / (theme + "-" + str(width) + "-columns.png")))
    assert not errors
    page.close()


def test_click_and_upload_scan_without_import_until_confirmation(browser_context, workspace_url):
    page = browser_context.new_page();records = backup_api(page);dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
    open_backups(page, workspace_url["url"])
    page.locator('[data-archive-name="manual-fixture.zip"] .wb-backup-entry-select').click()
    page.get_by_role("button", name="确认覆盖并导入", exact=True).wait_for()
    assert not dialogs
    assert "密钥与登录凭据" in page.locator(".wb-import-content-groups").inner_text()
    assert "AI 与模型配置" in page.locator(".wb-import-content-groups").inner_text()
    assert "记忆文件" in page.locator(".wb-import-content-groups").inner_text()
    assert "删除" in page.locator(".wb-backup-overwrite-warning").inner_text()
    assert not any(resource == "activate" for resource, _ in records)
    page.get_by_role("button", name="全不选导入部分", exact=True).click()
    assert page.get_by_role("button", name="确认覆盖并导入", exact=True).is_disabled()
    page.get_by_label("导入 AI 与模型配置", exact=True).check()
    assert "已选导入 1 部分" in page.locator(".wb-import-selected-summary").inner_text()
    assert not page.get_by_label("导入 密钥与登录凭据", exact=True).is_checked()
    page.get_by_label("上传压缩包以扫描内容", exact=True).set_input_files(
        {"name": "uploaded.zip", "mimeType": "application/zip", "buffer": b"isolated mock archive"})
    page.wait_for_function("document.querySelector('.wb-import-source')?.textContent.includes('uploaded.zip')")
    page.get_by_role("button", name="确认覆盖并导入", exact=True).wait_for()
    assert page.locator(".wb-import-content-group").count() == 1
    assert "记忆文件" in page.locator(".wb-import-content-groups").inner_text()
    assert not any(resource == "activate" for resource, _ in records)
    page.get_by_role("button", name="确认覆盖并导入", exact=True).click()
    page.get_by_text("导入完成，所覆盖的旧内容已清理。", exact=True).wait_for()
    assert len(dialogs) == 1
    assert any(resource == "activate" and data.get("confirm_replace") and data.get("categories") == ["memory"] for resource, data in records)
    assert any(resource == "import/cancel" for resource, _ in records)
    page.close()


def test_latest_archive_wins_when_switching_quickly(browser_context, workspace_url):
    page = browser_context.new_page();records = backup_api(page)
    open_backups(page, workspace_url["url"])
    page.evaluate("()=>{const buttons=document.querySelectorAll('.wb-backup-entry-select');buttons[0].click();buttons[1].click();}")
    page.get_by_role("button", name="确认覆盖并导入", exact=True).wait_for()
    assert "manual-output.tar.xz" in page.locator(".wb-import-source").inner_text()
    assert "命令完整输出" in page.locator(".wb-import-content-groups").inner_text()
    assert "环境变量" not in page.locator(".wb-import-content-groups").inner_text()
    assert any(resource == "import/cancel" for resource, _ in records)
    page.close()


def test_immediate_compression_saves_current_categories_format_and_frequency(browser_context, workspace_url):
    page = browser_context.new_page();records = backup_api(page)
    open_backups(page, workspace_url["url"])
    page.get_by_role("button", name="全不选", exact=True).click()
    page.get_by_label("导出 命令完整输出", exact=True).check()
    page.locator('input[name="archive_format"][value="tar.xz"]').check()
    page.get_by_label("压缩频率", exact=True).select_option("custom")
    page.get_by_label("自定义间隔", exact=True).fill("2h")
    page.get_by_role("button", name="立即压缩", exact=True).click()
    page.get_by_role("link", name="下载压缩包", exact=True).wait_for()
    saved = next(data for resource, data in records if resource == "backup-settings" and data)
    exported = next(data for resource, data in records if resource == "export")
    assert saved["categories"] == exported["categories"] == ["command_outputs"]
    assert saved["archive_format"] == exported["archive_format"] == "tar.xz"
    assert saved["frequency"] == "2h"
    assert saved["include_command_outputs"] is True
    assert page.get_by_role("button", name="立即压缩", exact=True).is_enabled()
    page.close()


def test_import_posts_only_checked_parts(browser_context, workspace_url):
    page = browser_context.new_page();records = backup_api(page)
    page.on("dialog", lambda dialog: dialog.accept())
    open_backups(page, workspace_url["url"])
    page.locator('[data-archive-name="manual-fixture.zip"] .wb-backup-entry-select').click()
    page.get_by_role("button", name="确认覆盖并导入", exact=True).wait_for()
    page.get_by_role("button", name="全不选导入部分", exact=True).click()
    page.get_by_label("导入 AI 与模型配置", exact=True).check()
    page.get_by_role("button", name="确认覆盖并导入", exact=True).click()
    page.get_by_text("导入完成，所覆盖的旧内容已清理。", exact=True).wait_for()
    exported = next(data for resource, data in records if resource == "activate")
    assert exported["categories"] == ["ai_config"]
    page.close()


def test_advanced_export_deselects_conflicting_independent_parts(browser_context, workspace_url):
    page = browser_context.new_page();backup_api(page)
    open_backups(page, workspace_url["url"])
    page.get_by_label("导出 完整数据库（高级）", exact=True).check()
    assert not page.get_by_label("导出 AI 与模型配置", exact=True).is_checked()
    assert not page.get_by_label("导出 密钥与登录凭据", exact=True).is_checked()
    page.get_by_label("导出 AI 与模型配置", exact=True).check()
    assert not page.get_by_label("导出 完整数据库（高级）", exact=True).is_checked()
    page.close()


@pytest.mark.parametrize('keys', [
    ['secrets', 'runtime_state', 'memory'],
    ['secrets', 'ai_config', 'app_settings', 'conversation_history', 'runtime_state', 'env', 'database', 'memory'],
])
def test_import_all_checks_advanced_and_covered_parts_without_duplicate_restore(browser_context, workspace_url, keys):
    page = browser_context.new_page(); records = backup_api(page, keys)
    page.on('dialog', lambda dialog: dialog.accept())
    open_backups(page, workspace_url['url'])
    page.locator('[data-archive-name="manual-fixture.zip"] .wb-backup-entry-select').click()
    page.get_by_role('button', name='确认覆盖并导入', exact=True).wait_for()
    page.get_by_role('button', name='全不选导入部分', exact=True).click()
    assert page.locator('[data-import-category]:checked').count() == 0
    page.get_by_role('button', name='全选可导入部分', exact=True).click()
    assert page.locator('[data-import-category]:checked').count() == len(keys)
    assert '已选导入 '+str(len(keys))+' 部分' in page.locator('.wb-import-selected-summary').inner_text()
    if 'database' in keys:
        assert page.locator('.wb-import-covered:not([hidden])').count() == 5
        assert page.get_by_label('导入 待发送队列与运行状态', exact=True).is_checked()
    page.get_by_role('button', name='确认覆盖并导入', exact=True).click()
    page.get_by_text('导入完成，所覆盖的旧内容已清理。', exact=True).wait_for()
    submitted = next(data['categories'] for resource, data in records if resource == 'activate')
    assert set(submitted) == ({'env','database','memory'} if 'database' in keys else set(keys))
    page.close()


def test_catalog_select_all_none_and_refresh_do_not_scan_archives(browser_context, workspace_url):
    page = browser_context.new_page(); records = backup_api(page)
    page.on('dialog', lambda dialog: dialog.accept())
    open_backups(page, workspace_url['url'])
    page.get_by_role('button', name='全选压缩包', exact=True).click()
    assert page.locator('[data-backup-name]:checked').count() == 2
    page.get_by_role('button', name='刷新列表', exact=True).click()
    assert page.locator('[data-backup-name]:checked').count() == 2
    assert not any(resource == 'import-stored' for resource, _ in records)
    page.get_by_role('button', name='全不选压缩包', exact=True).click()
    assert page.locator('[data-backup-name]:checked').count() == 0
    assert page.get_by_role('button', name='删除选中压缩包', exact=True).is_disabled()
    page.get_by_role('button', name='全选压缩包', exact=True).click()
    page.get_by_role('button', name='删除选中压缩包', exact=True).click()
    page.get_by_text('还没有压缩包', exact=True).wait_for()
    assert page.get_by_role('button', name='全选压缩包', exact=True).is_disabled()
    assert set(next(data['names'] for resource, data in records if resource=='backups/delete')) == {'manual-fixture.zip','manual-output.tar.xz'}
    page.close()


def test_real_api_all_categories_export_scan_and_upload(browser_context, workspace_url, tmp_path):
    """No mocked data endpoints: exercise the actual DB snapshot and part scanner."""
    url = workspace_url["url"]
    estimate = browser_context.request.get(url + "/api/workbench/data/estimate").json()
    assert len(estimate["categories"]) == len(CATEGORIES) > 20
    selected = ["secrets", "ai_config", "memory", "prompts", "command_outputs"]
    response = browser_context.request.post(url + "/api/workbench/data/export", data={
        "request_id": "real-browser-export-" + str(time.time_ns()), "categories": selected,
        "archive_format": "tar.gz", "confirm_secrets": True})
    assert response.ok, response.text()
    identifier = response.json()["id"]
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        job = browser_context.request.get(url + "/api/workbench/data/status?id=" + identifier).json()
        if job["state"] in {"completed", "failed"}:
            break
        time.sleep(.1)
    assert job["state"] == "completed", job
    catalog = browser_context.request.get(url + "/api/workbench/data/backups").json()
    name = next(item["name"] for item in catalog["items"] if item["path"] == job["result"]["path"])
    page = browser_context.new_page()
    open_backups(page, url)
    assert page.locator("[data-export-category]").count() == len(CATEGORIES)
    page.locator('[data-archive-name="' + name + '"] .wb-backup-entry-select').click()
    page.get_by_role("button", name="确认覆盖并导入", exact=True).wait_for(timeout=45000)
    assert page.get_by_label("导入 密钥与登录凭据", exact=True).is_checked()
    assert page.get_by_label("导入 AI 与模型配置", exact=True).is_checked()
    page.get_by_role("button", name="全不选导入部分", exact=True).click()
    page.get_by_label("导入 AI 与模型配置", exact=True).check()
    page.locator('[data-import-row="ai_config"] summary').click()
    assert "fixture-not-a-real-key" not in page.locator(".wb-import-preview").inner_text()
    assert "已选导入 1 部分" in page.locator(".wb-import-selected-summary").inner_text()
    page.locator(".wb-backup-import-card").scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / "real-selective-import.png"))
    archive = browser_context.request.get(url + job["download_url"])
    assert archive.ok
    page.get_by_label("上传压缩包以扫描内容", exact=True).set_input_files({
        "name": "uploaded-real.tar.gz", "mimeType": "application/gzip", "buffer": archive.body()})
    page.wait_for_function("document.querySelector('.wb-import-source')?.textContent.includes('uploaded-real.tar.gz')")
    page.get_by_role("button", name="确认覆盖并导入", exact=True).wait_for(timeout=45000)
    assert page.get_by_label("导入 AI 与模型配置", exact=True).is_checked()
    assert page.get_by_label("导入 密钥与登录凭据", exact=True).is_checked()
    scan_id = page.evaluate("sessionStorage.getItem('xgent-data-import-job')")
    assert browser_context.request.post(url + "/api/workbench/data/import/cancel", data={"id": scan_id}).ok
    assert browser_context.request.post(url + "/api/workbench/data/backups/delete", data={"names": [name], "confirm": True}).ok
    page.close()
