"""终端功能的单元 + HTTP 集成测试。

Windows 上 pty 不可用，所以真实 pty 流程只在 posix 跑；非 posix 测错误分支与
路由的认证 / 开关 / 平台兼容性。
"""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
import urllib.error
import urllib.request

from xgent_app import web_auth, web_server, web_terminal


# --- ☆ TerminalManager 单元（跨平台错误分支）☆ ---


class TerminalManagerUnitTests(unittest.TestCase):
    def setUp(self):
        # 每个测试用独立实例，不污染全局单例。
        self.mgr = web_terminal.TerminalManager()

    def test_open_rejects_unsupported_platform(self):
        if web_terminal.is_terminal_supported():
            self.skipTest("posix 平台测真实 open 在集成测试里覆盖")
        with self.assertRaises(RuntimeError):
            self.mgr.open()

    def test_unknown_session_get_returns_none(self):
        self.assertIsNone(self.mgr.get("does-not-exist"))

    def test_unknown_session_get_empty_returns_none(self):
        self.assertIsNone(self.mgr.get(""))

    def test_unknown_session_write_returns_false(self):
        self.assertFalse(self.mgr.write("nope", b"ls"))

    def test_unknown_session_read_returns_none(self):
        self.assertIsNone(self.mgr.read("nope", timeout=0.01))

    def test_unknown_session_close_returns_false(self):
        self.assertFalse(self.mgr.close("nope"))

    def test_unknown_session_resize_returns_false(self):
        self.assertFalse(self.mgr.resize("nope", 100, 30))

    def test_close_all_empty_is_noop(self):
        self.mgr.close_all()
        self.assertEqual(0, self.mgr.session_count)

    def test_cleanup_idle_empty_returns_zero(self):
        self.assertEqual(0, self.mgr.cleanup_idle())

    def test_singleton_is_stable(self):
        self.assertIs(web_terminal.get_terminal_manager(), web_terminal.get_terminal_manager())


# --- ☆ 终端 HTTP 路由（认证 / 开关 / 平台）☆ ---


def _make_config(*, terminal_enabled, port=0):
    """构造一个最小可用的 WebChatConfig。"""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    config = web_server.WebChatConfig(
        host="127.0.0.1",
        port=port,
        password_hash=web_auth.hash_password("pw"),
        bot_token="123456:ABCdef",
        authorized_user_id=42,
        loop=loop,
        submit_message=lambda *a: None,
        read_history=lambda limit: asyncio.sleep(0, result=[]),
        read_settings=lambda: asyncio.sleep(0, result={"values": {}, "options": {}}),
        write_setting=lambda k, v: asyncio.sleep(0, result={"values": {}, "options": {}}),
        request_stop=lambda: None,
        is_busy=lambda: False,
        is_terminal_enabled=lambda: terminal_enabled,
    )
    return config, loop, t


class TerminalHttpTests(unittest.TestCase):
    """终端开（is_terminal_enabled=True）下的路由行为。"""

    PASSWORD = "pw"

    @classmethod
    def setUpClass(cls):
        cls.config, cls.loop, cls.loop_thread = _make_config(terminal_enabled=True)
        cls.server = web_server.WebChatServer(cls.config)
        cls.server.start()
        cls.port = cls.server._httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=5)

    def request(self, path, method="GET", body=None, cookie=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Origin", self.base)
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                try:
                    parsed = json.loads(raw.decode())
                except json.JSONDecodeError:
                    parsed = {"raw": raw}
                return resp.status, parsed, resp.headers
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return exc.code, parsed, exc.headers

    def login(self):
        status, _body, headers = self.request("/api/login", method="POST", body={"password": self.PASSWORD})
        self.assertEqual(200, status)
        return headers["Set-Cookie"].split(";")[0]

    def test_terminal_page_served(self):
        # 页面不要求认证，与 / 一致。
        with urllib.request.urlopen(self.base + "/terminal", timeout=10) as resp:
            self.assertEqual(200, resp.status)
            self.assertIn(b"xterm", resp.read())

    def test_term_open_requires_auth(self):
        status, _body, _h = self.request("/api/term/open", method="POST", body={"cols": 80, "rows": 24})
        self.assertEqual(401, status)

    def test_term_input_requires_auth(self):
        status, _body, _h = self.request("/api/term/input", method="POST", body={"session_id": "x", "data": ""})
        self.assertEqual(401, status)

    def test_term_output_requires_auth(self):
        status, _body, _h = self.request("/api/term/output?session_id=x")
        self.assertEqual(401, status)

    def test_term_open_unsupported_or_real(self):
        cookie = self.login()
        status, body, _h = self.request(
            "/api/term/open", method="POST", body={"cols": 80, "rows": 24}, cookie=cookie
        )
        if web_terminal.is_terminal_supported():
            # posix：真实开 pty，应成功并返回 session_id，随后清理。
            self.assertEqual(200, status, body)
            self.assertIn("session_id", body)
            self.request(
                "/api/term/close", method="POST",
                body={"session_id": body["session_id"]}, cookie=cookie,
            )
        else:
            # 非 posix：平台不支持。
            self.assertEqual(503, status, body)

    def test_term_output_unknown_session(self):
        cookie = self.login()
        status, _body, _h = self.request("/api/term/output?session_id=nope", cookie=cookie)
        self.assertEqual(404, status)

    def test_term_close_requires_auth(self):
        status, _body, _h = self.request("/api/term/close", method="POST", body={"session_id": "x"})
        self.assertEqual(401, status)


class TerminalDisabledHttpTests(unittest.TestCase):
    """终端关（is_terminal_enabled=False）下，所有终端 API 返回 403。"""

    PASSWORD = "pw"

    @classmethod
    def setUpClass(cls):
        cls.config, cls.loop, cls.loop_thread = _make_config(terminal_enabled=False)
        cls.server = web_server.WebChatServer(cls.config)
        cls.server.start()
        cls.port = cls.server._httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=5)

    def _request(self, path, method="GET", body=None, cookie=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Origin", self.base)
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                try:
                    parsed = json.loads(raw.decode())
                except json.JSONDecodeError:
                    parsed = {"raw": raw}
                return resp.status, parsed, resp.headers
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return exc.code, parsed, exc.headers

    def _login(self):
        status, _b, h = self._request("/api/login", method="POST", body={"password": self.PASSWORD})
        self.assertEqual(200, status)
        return h["Set-Cookie"].split(";")[0]

    def test_disabled_returns_403(self):
        cookie = self._login()
        for path, body in [
            ("/api/term/open", {"cols": 80, "rows": 24}),
            ("/api/term/input", {"session_id": "x", "data": ""}),
            ("/api/term/resize", {"session_id": "x", "cols": 80, "rows": 24}),
            ("/api/term/close", {"session_id": "x"}),
        ]:
            status, _body, _h = self._request(path, method="POST", body=body, cookie=cookie)
            self.assertEqual(403, status, path)
        # SSE 输出端点也必须挡。
        status, _body, _h = self._request("/api/term/output?session_id=x", cookie=cookie)
        self.assertEqual(403, status)


if __name__ == "__main__":
    unittest.main()
