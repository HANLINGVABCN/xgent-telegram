from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from xgent_app import web_auth
from xgent_app.web_bridge import MediaTokenRegistry
from xgent_app.web_history import (
    build_history_message, delivered_file_reference, display_media_reference,
)
from xgent_app.web_server import WebChatConfig, WebChatServer


class HistoryDescriptorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = self.root / "storage with spaces"
        self.workspace = self.root / "workspace"

    def write(self, relative, data=b"original"):
        path = self.storage / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def build(self, kind="ai_reply", content="", metadata=None, row_id=7):
        return build_history_message({
            "id": row_id, "role": "user" if kind.startswith("user_") else "assistant",
            "timestamp": 1234567890, "msg_type": kind, "content": content,
            "metadata": json.dumps(metadata) if metadata is not None else None,
        }, self.storage, self.workspace)

    def test_markdown_identity_and_original_timestamp(self):
        for kind in ("ai_reply", "media_reply"):
            text = "| A | B |\n|---|---|\n| **yes** | x |\n\n- [x] done\n```py\nx = 1\n```"
            message = self.build(kind, text)
            self.assertEqual(text, message["content"])
            self.assertNotEqual("HTML", message.get("parse_mode"))
            self.assertEqual(7, message["id"])
            self.assertEqual(1234567890, message["timestamp"])
            self.assertNotIn("metadata", message)

    def test_display_metadata_does_not_replace_model_record(self):
        record = {
            "id": 3, "role": "system", "msg_type": "agent_result", "content": "shell notice",
            "metadata": {"display": {"content": "<b>Agent Shell</b>\n<pre>a\nb</pre>", "parse_mode": "HTML"}},
        }
        message = build_history_message(record, self.storage, self.workspace)
        self.assertEqual("shell notice", record["content"])
        self.assertEqual(record["metadata"]["display"]["content"], message["content"])
        self.assertEqual("HTML", message["parse_mode"])
        self.assertEqual("assistant", message["role"])

    def test_multiple_attachments_order_names_and_deduplication(self):
        first = self.write("uploads/day/one.bin")
        second = self.write("uploads/day/two")
        generated = self.write("generated_media/day/three.png")
        first_ref = {"path": "day/one.bin", "name": "original picture.png",
                     "mime_type": "image/png", "order": 0}
        metadata = {"attachments": [
            {"path": "day/two", "name": "README", "order": 1},
            first_ref, first_ref,
            {"path": "day/three.png", "name": "generated.png", "storage": "generated_media", "order": 2},
        ]}
        message = self.build("user_file", "keep the entire caption", metadata)
        self.assertEqual("keep the entire caption", message["content"])
        self.assertEqual([str(p) for p in (first, second, generated)], [m["path"] for m in message["media"]])
        self.assertEqual(["original picture.png", "README", "generated.png"],
                         [m["filename"] for m in message["media"]])
        self.assertEqual("photo", message["media"][0]["kind"])
        self.assertEqual([f"/api/history/media/7/{i}" for i in range(3)],
                         [m["download_url"] for m in message["media"]])
        self.assertNotIn("base64", json.dumps(message).lower())

    def test_more_than_token_registry_capacity_uses_stable_addresses(self):
        path = self.write("exports/report.html")
        metadata = {"display_media": [display_media_reference(str(path), "Token report.html")]}
        messages = [self.build("system_op", metadata=metadata, row_id=i) for i in range(1, 602)]
        self.assertEqual("/api/history/media/1/0", messages[0]["media"][0]["download_url"])
        self.assertEqual("/api/history/media/601/0", messages[-1]["media"][0]["download_url"])
        self.assertEqual("file", messages[0]["media"][0]["kind"])

    def test_missing_attachment_is_explicit_without_dead_link(self):
        message = self.build("user_file", metadata={"attachments": [
            {"path": "day/missing.txt", "name": "missing.txt"},
        ]})
        self.assertIn("error", message["media"][0])
        self.assertNotIn("download_url", message["media"][0])

    def test_invalid_metadata_does_not_break_other_history(self):
        for metadata in ({"attachments": "bad"}, {"attachments": []},
                         {"attachments": [None]}, {"attachments": [{"path": None}]}):
            message = self.build("user_file", metadata=metadata)
            self.assertTrue(message.get("media_error") or message["media"][0].get("error"))

    def test_invalid_mime_type_does_not_break_history_or_create_download(self):
        self.write("uploads/day/file")
        for mime in ({"type": "image/png"}, 42, "text/html\r\nSet-Cookie: injected"):
            message = self.build("user_file", metadata={"attachments": [
                {"path": "day/file", "mime_type": mime},
            ]})
            self.assertIn("error", message["media"][0])
            self.assertNotIn("download_url", message["media"][0])

    def test_processed_generated_text_does_not_authorize_quoted_marker(self):
        path = self.write("generated_media/2026-09-13/123456_abcde123_assistant_image.png")
        notice = f"\u3010\u7cfb\u7edf\u81ea\u52a8\u751f\u6210\uff1a\u672c\u56fe\u7247\u5df2\u81ea\u52a8\u5b58\u5165 {path}\uff0c\u9700\u8981\u65f6\u8bf7read\u4ee5\u8fd4\u56de\u4e0a\u4e0b\u6587\u3011"
        message = self.build("ai_reply", notice, {"generated_media_processed": True})
        self.assertEqual([], message["media"])

    def test_configuration_uploads_do_not_gain_download_links(self):
        path = self.write("uploads/2026-09-13/123456_abcde123_config.json")
        index = f"[\u6587\u4ef6] config.json\uff0c\u5df2\u4fdd\u5b58\u5230 {path}"
        for text, metadata in (
            (index, {"attachment_purpose": "configuration"}),
            ("[\u63d0\u4f9b\u5546\u914d\u7f6e\u6587\u4ef6]\n" + index, None),
        ):
            message = self.build("user_file", text, metadata)
            self.assertEqual([], message["media"])
            self.assertNotIn("media_error", message)

    def test_unrecoverable_old_records_report_error(self):
        for kind, text in (
            ("user_photo", "an old upload without its path"),
            ("ai_reply", "\u3010\u7cfb\u7edf\u81ea\u52a8\u751f\u6210\uff1a\u672c\u56fe\u7247 incomplete"),
            ("user_photo", "[\u76f8\u518c] \u51712\u5f20\u56fe\u7247"),
        ):
            self.assertIn("media_error", self.build(kind, text))

    def test_outside_attachment_root_is_rejected(self):
        secret = self.root / "secret"
        secret.write_text("private")
        for value in (str(secret), "../../secret"):
            message = self.build("user_file", metadata={"attachments": [{"path": value}]})
            self.assertIn("error", message["media"][0])
            self.assertNotIn("download_url", message["media"][0])

    def test_ordinary_chat_paths_are_not_download_capabilities(self):
        path = self.write("exports/2026-09-13/123456_1234abcd_export.zip")
        content = f"\u670d\u52a1\u5668\u6587\u4ef6\u8def\u5f84\uff1a{path}\uff088 bytes\uff09"
        for kind in ("user_text", "ai_reply", "system_op"):
            self.assertEqual([], self.build(kind, content)["media"])

    def test_legacy_upload_album_and_generated_notices(self):
        a = self.write("uploads/2026-09-13/123456_1234abcd_a.png")
        b = self.write("uploads/2026-09-13/123456_abcd1234_b.png")
        c = self.write("generated_media/2026-09-13/123456_abcde123_assistant_image.png")
        content = "\n".join(
            f"\u7b2c{i}\u5f20: [\u56fe\u7247] original {i}.png\uff0c\u5df2\u4fdd\u5b58\u5230 {p}\u3002\u8bf4\u660e\uff1afull caption"
            for i, p in enumerate((a, b), 1)
        )
        self.assertEqual(2, len(self.build("user_photo", content)["media"]))
        for suffix in (
            "\u9700\u8981\u65f6\u8bf7read\u4ee5\u8fd4\u56de\u4e0a\u4e0b\u6587",
            "\u539f\u56fe\u81ea\u52a8\u8fdb\u5165\u5f53\u524d\u672a\u6e05\u7a7a\u5bf9\u8bdd\u7684\u6bcf\u8f6e\u4e0a\u4e0b\u6587\uff0c\u65e0\u9700\u518d\u6b21read",
        ):
            notice = f"\u3010\u7cfb\u7edf\u81ea\u52a8\u751f\u6210\uff1a\u672c\u56fe\u7247\u5df2\u81ea\u52a8\u5b58\u5165 {c}\uff0c{suffix}\u3011"
            self.assertEqual(str(c), self.build("ai_reply", notice)["media"][0]["path"])

    def test_legacy_export_preserves_friendly_name(self):
        path = self.write("exports/2026-09-13/123456_1234abcd_export.zip")
        notice = f"\u5df2\u6210\u529f\u5bfc\u51fa\u5168\u90e8\u6570\u636e\uff0c\u670d\u52a1\u5668\u6587\u4ef6\u8def\u5f84\uff1a{path}\uff088 bytes\uff09"
        item = self.build("system_op", notice)["media"][0]
        self.assertEqual("\u7cfb\u7edf\u8bb0\u5fc6.zip", item["filename"])
        self.assertIn("download_url", item)
        path.unlink()
        self.assertIn("error", self.build("system_op", notice)["media"][0])

    def test_trusted_delivery_can_authorize_exact_external_file(self):
        path = self.root / "outside workspace file"
        path.write_bytes(b"export")
        notice = f"[sendfile\u7ed3\u679c] \u5df2\u53d1\u9001\u670d\u52a1\u5668\u6587\u4ef6\u7ed9\u7528\u6237: {path} (6 bytes)"
        refs = delivered_file_reference(notice)
        self.assertEqual(1, len(refs))
        self.assertEqual([], delivered_file_reference("output:\n" + notice))
        self.assertEqual([], delivered_file_reference("<pre>" + notice + "</pre>"))
        self.assertIn("download_url", self.build("agent_result", notice, {"display_media": refs})["media"][0])
        self.assertIn("error", self.build("agent_result", notice)["media"][0])


class DurableHistoryHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.records = {}
        cls.state = {"enabled": True, "busy": False}
        cls.loop = asyncio.new_event_loop()
        cls.thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.thread.start()

        async def history(limit):
            return [build_history_message(row, cls.root, cls.root / "workspace")
                    for row in list(cls.records.values())[:limit]]

        async def message(row_id):
            row = cls.records.get(row_id)
            return build_history_message(row, cls.root, cls.root / "workspace") if row else None

        async def settings(*args):
            return {"values": {}, "options": {}}

        cls.config = WebChatConfig(
            host="127.0.0.1", port=0, password_hash=web_auth.hash_password("test-password"),
            bot_token="", authorized_user_id=1, loop=cls.loop,
            submit_message=lambda *args: None, read_history=history, read_history_message=message,
            read_settings=settings, write_setting=settings, request_stop=lambda: None,
            is_busy=lambda: cls.state["busy"], is_web_enabled=lambda: cls.state["enabled"],
        )
        cls.start_server()

    @classmethod
    def start_server(cls):
        cls.server = WebChatServer(cls.config)
        cls.server.start()
        cls.base = f"http://127.0.0.1:{cls.server._httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.thread.join(timeout=5)
        cls.loop.close()
        cls.temp.cleanup()

    def setUp(self):
        self.records.clear()
        self.state.update(enabled=True, busy=False)
        self.cookie = self.login()

    def request(self, path, *, cookie=None, headers=None, body=None):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "Origin": self.base, **(headers or {})},
        )
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            data = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
            return response.status, data, response.headers

    def login(self):
        status, _, headers = self.request("/api/login", body={"password": "test-password"})
        self.assertEqual(200, status)
        return headers["Set-Cookie"].split(";")[0]

    def file_record(self, name="report.zip", content=b"0123456789", row_id=1, mime=None):
        path = self.root / name
        path.write_bytes(content)
        self.records[row_id] = {
            "id": row_id, "msg_type": "system_op", "role": "system", "content": "export",
            "metadata": {"display_media": [display_media_reference(str(path), name, mime)]},
        }
        return path

    def test_stable_link_survives_registry_reset_and_server_restart(self):
        self.file_record()
        status, data, headers = self.request("/api/history", cookie=self.cookie)
        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["Cache-Control"])
        url = data["messages"][0]["media"][0]["download_url"]
        with patch("xgent_app.web_server.MEDIA_TOKEN_REGISTRY", MediaTokenRegistry()):
            self.assertEqual(b"0123456789", self.request(url, cookie=self.cookie)[1])
        type(self).server.stop()
        type(self).start_server()
        self.cookie = self.login()
        self.assertEqual(b"0123456789", self.request(url, cookie=self.cookie)[1])

    def test_oldest_link_still_works_with_over_500_files(self):
        path = self.file_record()
        for row_id in range(2, 603):
            self.records[row_id] = {**self.records[1], "id": row_id}
        registry = MediaTokenRegistry()
        for _ in range(602):
            registry.register(str(path), path.name)
        with patch("xgent_app.web_server.MEDIA_TOKEN_REGISTRY", registry):
            status, data, _ = self.request("/api/history", cookie=self.cookie)
            self.assertEqual(602, len(data["messages"]))
            status, content, _ = self.request(data["messages"][0]["media"][0]["download_url"], cookie=self.cookie)
            self.assertEqual((200, b"0123456789"), (status, content))

    def test_clear_revokes_link_but_leaves_original(self):
        path = self.file_record()
        self.records.clear()
        self.assertEqual(404, self.request("/api/history/media/1/0", cookie=self.cookie)[0])
        self.assertTrue(path.is_file())

    def test_missing_original_reports_unavailable(self):
        self.file_record().unlink()
        status, data, _ = self.request("/api/history", cookie=self.cookie)
        self.assertEqual(200, status)
        self.assertIn("error", data["messages"][0]["media"][0])
        self.assertEqual(404, self.request("/api/history/media/1/0", cookie=self.cookie)[0])

    def test_auth_and_web_switch_protect_stable_downloads(self):
        self.file_record()
        self.assertEqual(401, self.request("/api/history/media/1/0")[0])
        self.state["enabled"] = False
        self.assertEqual(403, self.request("/api/history/media/1/0", cookie=self.cookie)[0])

    def test_unknown_or_malformed_reference_is_404(self):
        self.file_record()
        for suffix in ("1/1", "2/0", "-1/0", "1/-1", "1/0/extra", "../report.zip",
                       f"{2**63}/0", "9" * 100 + "/0", "1/" + "9" * 100):
            self.assertEqual(404, self.request("/api/history/media/" + suffix, cookie=self.cookie)[0])

    def test_html_svg_and_extensionless_downloads(self):
        for name, mime in (("Token report.html", "text/html"), ("diagram.svg", "image/svg+xml"),
                           ("README", "text/plain")):
            self.file_record(name, mime=mime)
            status, _, headers = self.request("/api/history/media/1/0", cookie=self.cookie)
            self.assertEqual(200, status)
            self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
            self.assertIn("sandbox", headers["Content-Security-Policy"])
            self.assertEqual("nosniff", headers["X-Content-Type-Options"])
            self.assertIn("no-store", headers["Cache-Control"])

    def test_image_inline_and_partial_content(self):
        self.file_record("image.png")
        status, content, headers = self.request("/api/history/media/1/0", cookie=self.cookie)
        self.assertEqual(200, status)
        self.assertTrue(headers["Content-Disposition"].startswith("inline;"))
        for value, expected in (("bytes=2-5", b"2345"), ("bytes=7-", b"789"), ("bytes=-3", b"789")):
            status, content, headers = self.request(
                "/api/history/media/1/0", cookie=self.cookie, headers={"Range": value},
            )
            self.assertEqual((206, expected), (status, content))
            self.assertEqual(str(len(expected)), headers["Content-Length"])
            self.assertIn("/10", headers["Content-Range"])
        for value in ("bytes=99-", "bytes=-0", "bytes=5-3", "bytes=0-1,3-4"):
            self.assertEqual(416, self.request(
                "/api/history/media/1/0", cookie=self.cookie, headers={"Range": value},
            )[0])

    def test_busy_state_is_available_after_refresh(self):
        self.state["busy"] = True
        self.assertIs(True, self.request("/api/history", cookie=self.cookie)[1]["busy"])


if __name__ == "__main__":
    unittest.main()
