"""Web Chat 的认证、垫片与 HTTP 端到端测试。

web_auth / web_bridge / web_server 都是可导入模块，不依赖 sections 共享
命名空间，所以这里是纯单测，不需要子进程。
"""

import asyncio
import hashlib
import hmac
import json
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

from xgent_app import web_auth
from xgent_app.web_bridge import WebBot, WebOutbox, build_web_conversation_objects
from xgent_app.web_server import WebChatConfig, WebChatServer


class PasswordTests(unittest.TestCase):
    def test_hash_verify_roundtrip(self):
        digest = web_auth.hash_password("correct horse")
        self.assertTrue(web_auth.verify_password("correct horse", digest))
        self.assertFalse(web_auth.verify_password("wrong", digest))

    def test_hash_is_salted(self):
        """同一密码两次哈希必须不同，否则等于泄漏"两个账号密码相同"。"""
        self.assertNotEqual(
            web_auth.hash_password("same"),
            web_auth.hash_password("same"),
        )

    def test_empty_password_rejected(self):
        with self.assertRaises(ValueError):
            web_auth.hash_password("")

    def test_malformed_stored_hash_is_false_not_crash(self):
        for bad in ("", "garbage", "pbkdf2_sha256$notanint$a$b", "a$b$c$d"):
            self.assertFalse(web_auth.verify_password("x", bad), bad)


class SessionCookieTests(unittest.TestCase):
    def setUp(self):
        self.key = web_auth.generate_session_secret()

    def test_valid_cookie_roundtrip(self):
        value = web_auth.make_session_cookie_value(self.key)
        self.assertTrue(web_auth.verify_session_cookie(self.key, value))

    def test_tampered_cookie_rejected(self):
        value = web_auth.make_session_cookie_value(self.key)
        payload, _, signature = value.partition(".")
        forged = f"{int(payload) + 1}.{signature}"
        self.assertFalse(web_auth.verify_session_cookie(self.key, forged))

    def test_other_key_rejected(self):
        value = web_auth.make_session_cookie_value(self.key)
        self.assertFalse(web_auth.verify_session_cookie(web_auth.generate_session_secret(), value))

    def test_ttl_enforced_server_side(self):
        """TTL 必须服务端判定——cookie 的 Max-Age 由浏览器执行，可以被无视。"""
        old = web_auth.make_session_cookie_value(self.key, issued_at=time.time() - 10_000)
        self.assertFalse(web_auth.verify_session_cookie(self.key, old, ttl_seconds=100))
        self.assertTrue(web_auth.verify_session_cookie(self.key, old, ttl_seconds=100_000))

    def test_future_issued_at_rejected(self):
        future = web_auth.make_session_cookie_value(self.key, issued_at=time.time() + 9999)
        self.assertFalse(web_auth.verify_session_cookie(self.key, future))

    def test_garbage_rejected(self):
        for bad in ("", "nodot", ".", "abc.def"):
            self.assertFalse(web_auth.verify_session_cookie(self.key, bad), bad)

    def test_secure_flag_only_when_https(self):
        """直连 HTTP 时加 Secure 会让浏览器丢掉 cookie，表现为登录死循环。"""
        self.assertIn("Secure", web_auth.build_set_cookie("v", secure=True))
        self.assertNotIn("Secure", web_auth.build_set_cookie("v", secure=False))

    def test_cookie_flags(self):
        cookie = web_auth.build_set_cookie("v", secure=False)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_parse_cookie_header(self):
        parsed = web_auth.parse_cookie_header("a=1; xgent_web_session=abc.def; b=2")
        self.assertEqual("abc.def", parsed["xgent_web_session"])

    def test_is_https_request(self):
        self.assertTrue(web_auth.is_https_request({"X-Forwarded-Proto": "https"}))
        self.assertTrue(web_auth.is_https_request({"x-forwarded-ssl": "on"}))
        self.assertFalse(web_auth.is_https_request({}))
        self.assertFalse(web_auth.is_https_request({"X-Forwarded-Proto": "http"}))


class RateLimiterTests(unittest.TestCase):
    def test_locks_after_max_fails(self):
        limiter = web_auth.LoginRateLimiter(max_fails=3, lockout_seconds=60)
        self.assertFalse(limiter.is_locked("1.2.3.4"))
        for _ in range(3):
            limiter.record_failure("1.2.3.4")
        self.assertTrue(limiter.is_locked("1.2.3.4"))

    def test_lock_is_per_ip(self):
        limiter = web_auth.LoginRateLimiter(max_fails=2, lockout_seconds=60)
        limiter.record_failure("1.1.1.1")
        limiter.record_failure("1.1.1.1")
        self.assertTrue(limiter.is_locked("1.1.1.1"))
        self.assertFalse(limiter.is_locked("2.2.2.2"))

    def test_success_resets_counter(self):
        limiter = web_auth.LoginRateLimiter(max_fails=3, lockout_seconds=60)
        limiter.record_failure("1.2.3.4")
        limiter.record_failure("1.2.3.4")
        limiter.record_success("1.2.3.4")
        limiter.record_failure("1.2.3.4")
        self.assertFalse(limiter.is_locked("1.2.3.4"))

    def test_lock_expires(self):
        limiter = web_auth.LoginRateLimiter(max_fails=1, lockout_seconds=10)
        now = time.time()
        limiter.record_failure("1.2.3.4", now=now)
        self.assertTrue(limiter.is_locked("1.2.3.4", now=now + 1))
        self.assertFalse(limiter.is_locked("1.2.3.4", now=now + 20))


def _sign_init_data(token: str, fields: dict) -> str:
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**fields, "hash": signature})


class InitDataTests(unittest.TestCase):
    TOKEN = "123456:ABCdefGhIJKlmNoPQRsTUVwxyZ"

    def _fields(self, user_id=42, auth_date=None):
        return {
            "auth_date": str(int(auth_date if auth_date is not None else time.time())),
            "query_id": "AAF",
            "user": json.dumps({"id": user_id, "first_name": "T"}, separators=(",", ":")),
        }

    def test_valid_init_data_accepted(self):
        init = _sign_init_data(self.TOKEN, self._fields())
        parsed = web_auth.verify_telegram_init_data(init, self.TOKEN)
        self.assertIsNotNone(parsed)
        self.assertEqual(42, web_auth.init_data_user_id(parsed))

    def test_wrong_token_rejected(self):
        init = _sign_init_data(self.TOKEN, self._fields())
        self.assertIsNone(web_auth.verify_telegram_init_data(init, "999:OTHER"))

    def test_tampered_payload_rejected(self):
        """改了 user 但没重签名——必须拒绝，否则任何人都能冒充授权用户。"""
        fields = self._fields()
        init = _sign_init_data(self.TOKEN, fields)
        forged = init.replace(
            urllib.parse.quote(fields["user"], safe=""),
            urllib.parse.quote(json.dumps({"id": 999}, separators=(",", ":")), safe=""),
        )
        self.assertIsNone(web_auth.verify_telegram_init_data(forged, self.TOKEN))

    def test_expired_init_data_rejected(self):
        init = _sign_init_data(self.TOKEN, self._fields(auth_date=time.time() - 999_999))
        self.assertIsNone(web_auth.verify_telegram_init_data(init, self.TOKEN))

    def test_missing_hash_rejected(self):
        self.assertIsNone(web_auth.verify_telegram_init_data("auth_date=1&user=%7B%7D", self.TOKEN))

    def test_empty_inputs_rejected(self):
        self.assertIsNone(web_auth.verify_telegram_init_data("", self.TOKEN))
        self.assertIsNone(web_auth.verify_telegram_init_data("x=1", ""))


class SameOriginTests(unittest.TestCase):
    def test_matching_origin_allowed(self):
        self.assertTrue(web_auth.is_same_origin("http://h:8790", "", "h:8790"))

    def test_cross_origin_rejected(self):
        self.assertFalse(web_auth.is_same_origin("http://evil.com", "", "h:8790"))

    def test_referer_used_when_origin_absent(self):
        self.assertTrue(web_auth.is_same_origin("", "http://h:8790/page", "h:8790"))
        self.assertFalse(web_auth.is_same_origin("", "http://evil.com/p", "h:8790"))

    def test_no_origin_no_referer_allowed(self):
        """curl/脚本客户端没有这两个头，浏览器也不会给它们自动附 Cookie。"""
        self.assertTrue(web_auth.is_same_origin("", "", "h:8790"))


class WebBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.outbox = WebOutbox()
        self.bot = WebBot(self.outbox, chat_id=7)

    def drain(self):
        frames = []
        while True:
            frame = self.outbox.get(timeout=0.01)
            if frame is None:
                break
            frames.append(frame)
        return frames

    async def test_send_message_emits_frame_with_id(self):
        message = await self.bot.send_message(chat_id=7, text="hi")
        frames = self.drain()
        self.assertEqual("message", frames[0]["type"])
        self.assertEqual("hi", frames[0]["text"])
        self.assertEqual(message.message_id, frames[0]["message_id"])

    async def test_message_ids_are_unique(self):
        first = await self.bot.send_message(chat_id=7, text="a")
        second = await self.bot.send_message(chat_id=7, text="b")
        self.assertNotEqual(first.message_id, second.message_id)

    async def test_edit_targets_message_id(self):
        message = await self.bot.send_message(chat_id=7, text="a")
        self.drain()
        await self.bot.edit_message_text(text="b", chat_id=7, message_id=message.message_id)
        frames = self.drain()
        self.assertEqual("edit", frames[0]["type"])
        self.assertEqual(message.message_id, frames[0]["message_id"])

    async def test_reply_text_on_message_object(self):
        """process_conversation 的错误分支走的是 update.message.reply_text。"""
        update, _context, _bot = build_web_conversation_objects(7, self.outbox)
        await update.message.reply_text("failed")
        frames = self.drain()
        self.assertEqual("message", frames[0]["type"])
        self.assertEqual("failed", frames[0]["text"])

    async def test_keyboard_is_flattened(self):
        class Button:
            def __init__(self, text, callback_data):
                self.text = text
                self.callback_data = callback_data

        class Markup:
            inline_keyboard = [[Button("停止", "act_stop_generation")]]

        await self.bot.send_message(chat_id=7, text="x", reply_markup=Markup())
        frames = self.drain()
        self.assertEqual(
            [[{"text": "停止", "callback_data": "act_stop_generation"}]],
            frames[0]["reply_markup"],
        )

    async def test_chat_action_and_document(self):
        await self.bot.send_chat_action(chat_id=7, action="typing")
        await self.bot.send_document(chat_id=7, document=None, filename="a.txt", caption="c")
        types = [f["type"] for f in self.drain()]
        self.assertEqual(["chat_action", "document"], types)

    async def test_update_shape_matches_conversation_core(self):
        update, context, bot = build_web_conversation_objects(99, self.outbox)
        self.assertEqual(99, update.effective_chat.id)
        self.assertIsNone(update.callback_query)
        self.assertIs(bot, context.bot)


class WebOutboxTests(unittest.TestCase):
    def test_get_timeout_returns_none(self):
        self.assertIsNone(WebOutbox().get(timeout=0.01))

    def test_full_queue_drops_oldest_and_never_blocks(self):
        """队列满时绝不能阻塞——对话核心持有全局锁，卡住等于整个 bot 挂掉。"""
        outbox = WebOutbox(maxsize=2)
        for index in range(5):
            outbox.put({"type": "message", "n": index})
        frames = []
        while True:
            frame = outbox.get(timeout=0.01)
            if frame is None:
                break
            frames.append(frame)
        self.assertEqual(2, len(frames))
        self.assertEqual(4, frames[-1]["n"])

    def test_close_wakes_consumer(self):
        outbox = WebOutbox()
        outbox.close()
        self.assertIsNone(outbox.get(timeout=1.0))
        self.assertTrue(outbox.closed)


class WebServerHttpTests(unittest.TestCase):
    """真起一个服务器，走真实 HTTP 请求。"""

    PASSWORD = "test-password"

    @classmethod
    def setUpClass(cls):
        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()

        cls.submitted = []
        cls.stopped = []
        cls.busy = {"value": False}

        async def read_history(limit):
            return [{"role": "user", "content": "hello"}][:limit]

        async def read_settings():
            return {"values": {"global_depth": 30}, "options": {}}

        async def write_setting(key, value):
            if key == "nope":
                raise ValueError("不可修改的配置项: nope")
            return {"values": {key: value}, "options": {}}

        cls.config = WebChatConfig(
            host="127.0.0.1",
            port=0,  # 让内核分配空闲端口
            password_hash=web_auth.hash_password(cls.PASSWORD),
            bot_token="123456:ABCdefGhIJKlmNoPQRsTUVwxyZ",
            authorized_user_id=42,
            loop=cls.loop,
            submit_message=lambda text, outbox: cls.submitted.append(text),
            read_history=read_history,
            read_settings=read_settings,
            write_setting=write_setting,
            request_stop=lambda: cls.stopped.append(True),
            is_busy=lambda: cls.busy["value"],
        )
        cls.server = WebChatServer(cls.config)
        cls.server.start()
        cls.port = cls.server._httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=5)

    def request(self, path, method="GET", body=None, cookie=None, headers=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        # 浏览器会自动带 Origin；测试里显式补上以通过同源校验。
        req.add_header("Origin", self.base)
        if cookie:
            req.add_header("Cookie", cookie)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode()), resp.headers
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return exc.code, parsed, exc.headers

    def login(self):
        status, _body, headers = self.request(
            "/api/login", method="POST", body={"password": self.PASSWORD}
        )
        self.assertEqual(200, status)
        return headers["Set-Cookie"].split(";")[0]

    def test_index_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            self.assertEqual(200, resp.status)
            self.assertIn(b"XGent Web Chat", resp.read())

    def test_api_requires_auth(self):
        for path in ("/api/history", "/api/config", "/api/stream"):
            status, _body, _h = self.request(path)
            self.assertEqual(401, status, path)

    def test_chat_requires_auth(self):
        status, _body, _h = self.request("/api/chat", method="POST", body={"text": "x"})
        self.assertEqual(401, status)

    def test_login_wrong_password_rejected(self):
        status, _body, _h = self.request(
            "/api/login", method="POST", body={"password": "wrong"}
        )
        self.assertEqual(401, status)

    def test_login_then_access(self):
        cookie = self.login()
        status, body, _h = self.request("/api/history", cookie=cookie)
        self.assertEqual(200, status)
        self.assertEqual([{"role": "user", "content": "hello"}], body["messages"])

    def test_forged_cookie_rejected(self):
        status, _body, _h = self.request("/api/history", cookie="xgent_web_session=1.forged")
        self.assertEqual(401, status)

    def test_cross_origin_post_rejected(self):
        """SameSite=Lax 不拦顶层表单 POST，必须靠同源校验兜住。"""
        cookie = self.login()
        status, _body, _h = self.request(
            "/api/chat", method="POST", body={"text": "x"},
            cookie=cookie, headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(403, status)

    def test_chat_submits_message(self):
        cookie = self.login()
        before = len(self.submitted)
        status, _body, _h = self.request(
            "/api/chat", method="POST", body={"text": "hello there"}, cookie=cookie
        )
        self.assertEqual(200, status)
        self.assertEqual(before + 1, len(self.submitted))
        self.assertEqual("hello there", self.submitted[-1])

    def test_chat_rejects_empty_text(self):
        cookie = self.login()
        status, _body, _h = self.request(
            "/api/chat", method="POST", body={"text": "   "}, cookie=cookie
        )
        self.assertEqual(400, status)

    def test_chat_conflicts_when_busy(self):
        """全局对话锁被占用时返回 409，语义与 Telegram 侧一致。"""
        cookie = self.login()
        self.busy["value"] = True
        try:
            status, _body, _h = self.request(
                "/api/chat", method="POST", body={"text": "x"}, cookie=cookie
            )
        finally:
            self.busy["value"] = False
        self.assertEqual(409, status)

    def test_stop_endpoint(self):
        cookie = self.login()
        before = len(self.stopped)
        status, _body, _h = self.request("/api/stop", method="POST", cookie=cookie)
        self.assertEqual(200, status)
        self.assertEqual(before + 1, len(self.stopped))

    def test_config_read_and_write(self):
        cookie = self.login()
        status, body, _h = self.request("/api/config", cookie=cookie)
        self.assertEqual(200, status)
        self.assertIn("values", body)

        status, body, _h = self.request(
            "/api/config", method="POST",
            body={"key": "global_depth", "value": 42}, cookie=cookie,
        )
        self.assertEqual(200, status)
        self.assertEqual(42, body["settings"]["values"]["global_depth"])

    def test_config_write_rejects_unknown_key(self):
        cookie = self.login()
        status, body, _h = self.request(
            "/api/config", method="POST", body={"key": "nope", "value": 1}, cookie=cookie
        )
        self.assertEqual(400, status)
        self.assertIn("nope", body["error"])

    def test_oversized_body_rejected_before_auth(self):
        """请求体上限必须在认证之前生效，否则未授权请求就能打满内存。

        只发头、不发 body：服务端看 Content-Length 就该拒绝，这也正是要断言的
        行为（真读了 2MB 才拒绝就失去意义了）。发 body 反而会引入竞态——服务端
        不排空就关连接，Windows 上 RST 会把已收到的响应从接收缓冲里抹掉。
        """
        import socket

        oversize = 2 * 1024 * 1024
        request = (
            b"POST /api/login HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{self.port}\r\n".encode()
            + f"Origin: {self.base}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {oversize}\r\n".encode()
            + b"Connection: close\r\n\r\n"
        )
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            sock.sendall(request)
            status_line = b""
            while b"\r\n" not in status_line:
                chunk = sock.recv(256)
                if not chunk:
                    break
                status_line += chunk
        finally:
            sock.close()
        self.assertIn(b"413", status_line, status_line[:120])

    def test_unknown_route_404(self):
        status, _body, _h = self.request("/api/nope")
        self.assertEqual(404, status)

    def test_logout_clears_cookie(self):
        cookie = self.login()
        status, _body, headers = self.request("/api/logout", method="POST", cookie=cookie)
        self.assertEqual(200, status)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_no_cors_headers(self):
        """不发 CORS 头，让浏览器默认同源策略生效。"""
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))


class WebServerStartupGuardTests(unittest.TestCase):
    def _config(self, **overrides):
        base = dict(
            host="127.0.0.1", port=0, password_hash=web_auth.hash_password("pw"),
            bot_token="t", authorized_user_id=1, loop=asyncio.new_event_loop(),
            submit_message=lambda *a: None,
            read_history=lambda limit: None,
            read_settings=lambda: None,
            write_setting=lambda k, v: None,
            request_stop=lambda: None,
            is_busy=lambda: False,
        )
        base.update(overrides)
        return WebChatConfig(**base)

    def test_refuses_to_start_without_password(self):
        """能驱动 Agent 执行命令的界面，绝不能无密码监听。"""
        config = self._config(password_hash="")
        try:
            with self.assertRaises(RuntimeError):
                WebChatServer(config).start()
        finally:
            config.loop.close()

    def test_port_conflict_raises_instead_of_silently_hijacking(self):
        """端口冲突必须报错。

        Windows 上 SO_REUSEADDR 允许抢占正在监听的端口——HTTPServer 默认开着
        这个标志，会让端口冲突静默通过，两个进程争抢同一端口。
        """
        first = WebChatServer(self._config())
        first.start()
        port = first._httpd.server_address[1]
        second = WebChatServer(self._config(port=port))
        try:
            with self.assertRaises(OSError):
                second.start()
        finally:
            second.config.loop.close()
            first.stop()
            first.config.loop.close()

    def test_stop_is_idempotent(self):
        config = self._config()
        server = WebChatServer(config)
        server.start()
        server.stop()
        server.stop()  # 二次调用不该抛
        self.assertFalse(server.running)
        config.loop.close()


if __name__ == "__main__":
    unittest.main()
