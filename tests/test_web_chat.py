"""Web Chat 的认证、垫片与 HTTP 端到端测试。

web_auth / web_bridge / web_server 都是可导入模块，不依赖 sections 共享
命名空间，所以这里是纯单测，不需要子进程。
"""

import asyncio
import hashlib
import os
import shutil
import tempfile
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


def _async_result(value):
    async def _read(*_args, **_kwargs):
        return value
    return _read


def _async_value(value):
    async def _write(*_args, **_kwargs):
        return value
    return _write


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
        # 广播总线只投给「已经订阅」的消费者，所以订阅要在 put 之前建立。
        self.stream = self.outbox.subscribe()
        self.addCleanup(self.stream.close)
        self.bot = WebBot(self.outbox, chat_id=7)

    def drain(self):
        frames = []
        while True:
            frame = self.stream.get(timeout=0.01)
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
        outbox = WebOutbox()
        stream = outbox.subscribe()
        self.addCleanup(stream.close)
        self.assertIsNone(stream.get(timeout=0.01))

    def test_full_queue_drops_oldest_and_never_blocks(self):
        """队列满时绝不能阻塞——对话核心持有全局锁，卡住等于整个 bot 挂掉。"""
        outbox = WebOutbox(maxsize=2)
        stream = outbox.subscribe()
        self.addCleanup(stream.close)
        for index in range(5):
            outbox.put({"type": "message", "n": index})
        frames = []
        while True:
            frame = stream.get(timeout=0.01)
            if frame is None:
                break
            frames.append(frame)
        self.assertEqual(2, len(frames))
        self.assertEqual(4, frames[-1]["n"])

    def test_close_wakes_consumer(self):
        outbox = WebOutbox()
        stream = outbox.subscribe()
        outbox.close()
        self.assertIsNone(stream.get(timeout=1.0))
        self.assertTrue(outbox.closed)

    def test_every_subscriber_gets_every_frame(self):
        """两个 SSE 连接必须各收到全量帧。

        回归：早期实现是所有连接共用一个 queue.Queue，get() 取走而非广播，
        于是每帧只落到其中一个连接——多标签页互抢消息，断线重连时旧连接的
        线程还会把帧吞进死 socket，表现为网页消息缺失、错乱、要刷新才恢复。
        """
        outbox = WebOutbox()
        a = outbox.subscribe()
        b = outbox.subscribe()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        for index in range(6):
            outbox.put({"type": "message", "n": index})

        def drain(stream):
            out = []
            while True:
                frame = stream.get(timeout=0.01)
                if frame is None:
                    return out
                out.append(frame["n"])

        expected = list(range(6))
        self.assertEqual(expected, drain(a))
        self.assertEqual(expected, drain(b))

    def test_unsubscribed_consumer_stops_stealing_frames(self):
        """退订后不再占广播位——否则死连接会一直分走帧。"""
        outbox = WebOutbox()
        gone = outbox.subscribe()
        alive = outbox.subscribe()
        self.addCleanup(alive.close)
        gone.close()
        self.assertEqual(1, outbox.subscriber_count)
        outbox.put({"type": "message", "n": 1})
        self.assertEqual(1, alive.get(timeout=0.5)["n"])

    def test_put_without_subscribers_is_dropped(self):
        """没人在线就不该攒帧：攒下的旧帧会盖在新拉的 history 上造成错乱。"""
        outbox = WebOutbox()
        outbox.put({"type": "message", "n": 1})
        late = outbox.subscribe()
        self.addCleanup(late.close)
        self.assertIsNone(late.get(timeout=0.05))


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

    def test_vendor_marked_served(self):
        """marked 是 Markdown 渲染的硬依赖，必须能免登录取到。

        VENDOR_ASSETS 是硬编码白名单，不做目录遍历——文件放进 webui/vendor/
        但忘了登记就是 404，页面上表现为整个 Markdown 退化成纯文本。
        """
        with urllib.request.urlopen(self.base + "/vendor/marked.umd.js", timeout=10) as resp:
            self.assertEqual(200, resp.status)
            self.assertIn("javascript", resp.headers.get("Content-Type", ""))
            body = resp.read().decode("utf-8")
        self.assertIn("marked", body)
        # UMD wrapper 要挂到全局，否则页面里的 window.marked 拿不到
        self.assertIn('g["marked"]', body)

    def test_vendor_assets_exist_on_disk(self):
        """白名单里登记的文件都得真的在磁盘上。"""
        from xgent_app.web_server import VENDOR_ASSETS, VENDOR_DIR

        for route, (filename, _ctype) in VENDOR_ASSETS.items():
            path = os.path.join(VENDOR_DIR, filename)
            self.assertTrue(os.path.isfile(path), f"{route} 指向的 {filename} 不存在")


class WebOpenButtonTests(unittest.TestCase):
    """没设密码时「打开网页」不能是个死链接。

    start_web_chat_if_enabled 在没密码时直接跳过启动，地址上没有服务在监听。
    而 url / web_app 按钮没有 callback_data，永远回不到 bot，也就没法解释原因,
    用户看到的就是"点了毫无反应"。所以这种情况必须给回调按钮。
    """

    def _build(self, **state):
        """在独立命名空间里跑 ui.py 里的按钮构建函数，避开 sections 全局加载。"""
        import re

        ui_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "xgent_app", "sections", "ui.py",
        )
        with open(ui_path, encoding="utf-8") as handle:
            src = handle.read()
        start = src.index("def _build_web_open_button():")
        end = src.index("def get_web_menu():")
        snippet = src[start:end]

        from telegram import InlineKeyboardButton, WebAppInfo

        ns = {
            "InlineKeyboardButton": InlineKeyboardButton,
            "WebAppInfo": WebAppInfo,
            "normalize_bool": lambda v, d: bool(v) if v is not None else d,
            "normalize_web_port": lambda v: int(v or 8790),
            "DEFAULT_WEB_PORT": 8790,
            "DEFAULT_WEB_HOST": "127.0.0.1",
            "UserDataManager": type(
                "UDM", (), {"get": staticmethod(lambda k, d=None: state.get(k, d))}
            ),
        }
        exec(compile(snippet, "ui_snippet", "exec"), ns)
        self.assertTrue(re.search(r"_web_has_password", snippet), "密码闸不在源码里")
        return ns

    def test_no_password_yields_callback_button(self):
        ns = self._build(web_enabled=True, _web_has_password=False)
        btn = ns["_build_web_open_button"]()
        self.assertIsNotNone(btn, "开启状态下不该藏起按钮")
        self.assertEqual("web_need_password", btn.callback_data)
        self.assertIsNone(btn.url)
        self.assertIsNone(btn.web_app)

    def test_with_password_yields_openable_button(self):
        ns = self._build(web_enabled=True, _web_has_password=True, web_port=8790)
        btn = ns["_build_web_open_button"]()
        self.assertIsNone(btn.callback_data)
        self.assertEqual("http://127.0.0.1:8790", btn.url)

    def test_disabled_still_hides_row(self):
        ns = self._build(web_enabled=False, _web_has_password=False)
        self.assertIsNone(ns["_build_web_open_button"]())

    def test_terminal_button_same_guard(self):
        ns = self._build(terminal_enabled=True, _web_has_password=False)
        btn = ns["_build_terminal_open_button"]()
        self.assertEqual("web_need_password", btn.callback_data)


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


class MediaResolveHttpTests(unittest.TestCase):
    """/api/media/resolve：历史文本挖出的服务器路径 -> 新下载 token。

    这是"刷新后媒体卡片不丢"的服务端半边：前端从历史文本里挖出路径，
   来这里换 token。信任边界必须钉死——白名单根之外的路不给换（否则等于
    开放任意文件读取），不存在的文件不给换。
    """

    PASSWORD = "test-password"

    @classmethod
    def setUpClass(cls):
        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()

        # 白名单根：一个临时目录当 xgent_storage；系统临时目录本身当"外部"。
        cls.storage_root = tempfile.mkdtemp(prefix="xgent-media-root-")
        cls.outside_root = tempfile.mkdtemp(prefix="xgent-outside-")

        cls.config = WebChatConfig(
            host="127.0.0.1",
            port=0,
            password_hash=web_auth.hash_password(cls.PASSWORD),
            bot_token="123456:ABCdefGhIJKlmNoPQRsTUVwxyZ",
            authorized_user_id=42,
            loop=cls.loop,
            submit_message=lambda text, outbox: None,
            read_history=_async_result([]),
            read_settings=_async_result({"values": {}, "options": {}}),
            write_setting=_async_value({"values": {}, "options": {}}),
            request_stop=lambda: None,
            is_busy=lambda: False,
            media_allowed_roots=[cls.storage_root],
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
        shutil.rmtree(cls.storage_root, ignore_errors=True)
        shutil.rmtree(cls.outside_root, ignore_errors=True)

    def request(self, path, method="POST", body=None, cookie=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Origin", self.base)
        if cookie:
            req.add_header("Cookie", cookie)
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

    def _write(self, root, name, content):
        path = os.path.join(root, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def test_resolve_in_allowed_root_returns_download_url(self):
        path = self._write(self.storage_root, "exports", b"PK-zip-bytes")
        cookie = self.login()
        status, body, _h = self.request(
            "/api/media/resolve", body={"path": path}, cookie=cookie
        )
        self.assertEqual(200, status, body)
        self.assertIn("/api/media/", body["url"])
        self.assertEqual("exports", body["filename"])
        self.assertEqual(len(b"PK-zip-bytes"), body["size"])

    def test_resolved_url_downloads_bytes_with_attachment_for_zip(self):
        path = self._write(self.storage_root, "exports", b"PK-zip-bytes")
        cookie = self.login()
        _s, body, _h = self.request("/api/media/resolve", body={"path": path}, cookie=cookie)
        req = urllib.request.Request(self.base + body["url"])
        req.add_header("Cookie", cookie)
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(200, resp.status)
            self.assertEqual(b"PK-zip-bytes", resp.read())
            self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))

    def test_image_gets_inline_disposition(self):
        path = self._write(self.storage_root, "pic.png", b"FAKEPNGDATA")
        cookie = self.login()
        _s, body, _h = self.request("/api/media/resolve", body={"path": path}, cookie=cookie)
        req = urllib.request.Request(self.base + body["url"])
        req.add_header("Cookie", cookie)
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertIn("inline", resp.headers.get("Content-Disposition", ""))
            self.assertTrue(resp.headers.get("Content-Type", "").startswith("image/"))

    def test_resolve_rejects_paths_outside_allowed_roots(self):
        secret = self._write(self.outside_root, "secret.txt", b"nope")
        cookie = self.login()
        status, body, _h = self.request(
            "/api/media/resolve", body={"path": secret}, cookie=cookie
        )
        self.assertEqual(403, status, body)

    def test_traversal_escape_is_rejected(self):
        # 指向白名单内不存在但借 ../ 逃逸的路径：realpath 归一化后落在根之外。
        cookie = self.login()
        sneaky = os.path.join(self.storage_root, "..", "escape.txt")
        status, _body, _h = self.request(
            "/api/media/resolve", body={"path": sneaky}, cookie=cookie
        )
        self.assertEqual(403, status)

    def test_resolve_missing_file_is_404(self):
        cookie = self.login()
        missing = os.path.join(self.storage_root, "gone.zip")
        status, body, _h = self.request(
            "/api/media/resolve", body={"path": missing}, cookie=cookie
        )
        self.assertEqual(404, status, body)

    def test_resolve_requires_auth(self):
        path = self._write(self.storage_root, "auth.zip", b"x")
        status, _body, _h = self.request("/api/media/resolve", body={"path": path})
        self.assertEqual(401, status)


class MirrorBotFrameIdTests(unittest.TestCase):
    """MirrorBot 网端帧的 id 一致性。

    消息身份归对话核心所有：send_message 立刻分配一个逻辑 id（≥1,000,000）并
    推帧，真实 Telegram message_id 由出站通道在投递成功后自己记住。所以正常
    路径上"网页帧的 id"天然就是网页见过的那个，不再依赖 Telegram 先回一个 id
    ——TG 不通时网页照样有完整、可编辑的消息流。

    仍然要钉住少数拿着真实 TG id 回来的调用点（转发-读取-删除那套取内容的
    trick、MirrorMessage 包装真实消息）：那时必须反查回逻辑 id，否则前端
    byMessageId 落空、流式编辑每次新建气泡——"上一条消息无限刷屏"。
    """

    def test_edit_and_delete_frames_use_web_facing_logical_id(self):
        import asyncio
        from xgent_app.web_bridge import MirrorBot

        frames = []
        outbox = WebOutbox()
        outbox.put = lambda frame: frames.append(frame)  # 直接截获帧

        tg_calls = []

        class FakeRealMessage:
            message_id = 777  # Telegram 真实 id

        class FakeRealBot:
            async def send_message(self, *args, **kwargs):
                tg_calls.append(("send", kwargs.get("message_id")))
                return FakeRealMessage()
            async def edit_message_text(self, *args, **kwargs):
                tg_calls.append(("edit", kwargs.get("message_id")))
                return FakeRealMessage()
            async def edit_message_reply_markup(self, *args, **kwargs):
                tg_calls.append(("edit_markup", kwargs.get("message_id")))
                return FakeRealMessage()
            async def delete_message(self, *args, **kwargs):
                tg_calls.append(("delete", kwargs.get("message_id")))
                return True

        async def scenario():
            bot = MirrorBot(outbox, 42, FakeRealBot())
            # 对话核心第一次发消息（流式首块）——网页立刻收到逻辑 id 的 message 帧
            returned = await bot.send_message(chat_id=42, text="流式首块")
            logical = frames[-1]["message_id"]
            # 出站是异步的：等通道把真实 id 记下来，才能测真实 id 的反查
            await bot.flush_telegram(timeout=5.0)
            # 正常路径：核心手上就是逻辑 id
            await bot.edit_message_text(text="流式更新", chat_id=42, message_id=logical)
            # 真实 id 路径（MirrorMessage 包装真实 TG 消息）：必须反查回逻辑 id
            await bot.edit_message_reply_markup(chat_id=42, message_id=777, reply_markup=None)
            await bot.delete_message(chat_id=42, message_id=777)
            await bot.flush_telegram(timeout=5.0)
            return logical, returned.message_id

        logical, returned_id = asyncio.run(scenario())
        kinds = [(f["type"], f.get("message_id")) for f in frames]
        self.assertEqual("message", kinds[0][0])
        self.assertGreaterEqual(logical, 1_000_000,
                               "逻辑 id 必须落在避开真实 Telegram id 的区间")
        self.assertEqual(logical, returned_id,
                         "send_message 返回的消息要带逻辑 id——身份不许由 Telegram 决定")
        self.assertEqual(logical, kinds[1][1], "edit 帧必须用网页见过的逻辑 id")
        self.assertEqual(logical, kinds[2][1], "传真实 id 时也要反查回逻辑 id")
        self.assertEqual(logical, kinds[3][1], "delete 帧同样")
        self.assertNotEqual(logical, 777)
        # Telegram 那一侧收到的必须是真实 id，不是逻辑 id
        self.assertEqual([("send", None), ("edit", 777), ("edit_markup", 777),
                          ("delete", 777)], tg_calls)


if __name__ == "__main__":
    unittest.main()
