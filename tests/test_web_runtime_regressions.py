"""密码撤销、长连接停服和 HTTP/asyncio 超时边界；只开本机临时端口。"""
import ast
import asyncio
import http.client
import json
import logging
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from xgent_app import web_auth
from xgent_app.web_server import WebChatConfig, WebChatServer, WebOperationTimeout, _Handler

ROOT = Path(__file__).resolve().parents[1]


async def empty(*args):
    return {}


class WebTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_cancels_queued_write(self):
        cancelled = asyncio.Event()
        writes = []
        async def write():
            try:
                await asyncio.sleep(30)
                writes.append('late write')
            finally:
                cancelled.set()
        handler = SimpleNamespace(config=SimpleNamespace(loop=asyncio.get_running_loop()))
        with self.assertRaises(WebOperationTimeout):
            await asyncio.to_thread(_Handler._run_coro, handler, write(), 0.02)
        await asyncio.wait_for(cancelled.wait(), 1)
        self.assertEqual([], writes)

    async def test_timeout_does_not_claim_already_committed_write_was_rolled_back(self):
        writes = []
        cancelled = asyncio.Event()
        async def write():
            try:
                writes.append('already committed')
                await asyncio.sleep(30)
            finally:
                cancelled.set()
        handler = SimpleNamespace(config=SimpleNamespace(loop=asyncio.get_running_loop()))
        with self.assertRaisesRegex(WebOperationTimeout, '可能已完成'):
            await asyncio.to_thread(_Handler._run_coro, handler, write(), 0.02)
        await asyncio.wait_for(cancelled.wait(), 1)
        self.assertEqual(['already committed'], writes)


class WebRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.password_hash = web_auth.hash_password('old-password')
        self.server = self.make_server()
        self.server.start()
        self.restarts = 0

    def tearDown(self):
        self.server.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()

    def make_server(self):
        return WebChatServer(WebChatConfig(
            host='127.0.0.1', port=0, password_hash=self.password_hash,
            bot_token='', authorized_user_id=1, loop=self.loop,
            submit_message=lambda *a: None, read_history=empty, read_settings=empty,
            write_setting=empty, request_stop=lambda: None, is_busy=lambda: False))

    def request(self, path, body=None, cookie=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server._httpd.server_address[1], timeout=3)
        headers = {'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        connection.request('POST' if body is not None else 'GET', path,
                           body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        result = response.status, response.getheader('Set-Cookie'), json.loads(response.read())
        connection.close()
        return result

    def login(self, password='old-password'):
        status, cookie, _ = self.request('/api/login', {'password': password})
        self.assertEqual(200, status)
        return cookie.split(';', 1)[0]

    def reconcile(self):
        test = self
        class DB:
            async def get_config_fresh(self, key, default=None):
                return {'web_enabled': True, 'terminal_enabled': False, 'web_port': 0}.get(key, default)
        async def get_db(): return DB()
        async def read_hash(**kwargs): return test.password_hash
        async def restart(app):
            test.restarts += 1
            await asyncio.to_thread(test.server.stop)
            test.server = test.make_server()
            await asyncio.to_thread(test.server.start)
            return None
        ns = dict(BotMemoryDB=SimpleNamespace(get_instance=get_db),
            normalize_bool=lambda v,d: bool(v), normalize_web_port=lambda v: v,
            DEFAULT_WEB_PORT=0, DEFAULT_WEB_HOST='127.0.0.1', read_web_password_hash=read_hash,
            UserDataManager=SimpleNamespace(set=lambda *a: None),
            component_state=lambda name: SimpleNamespace(set=lambda *a,**kw: None),
            COMPONENT_DOWN='down', COMPONENT_UP='up',
            is_web_chat_running=lambda: self.server.running, _web_chat_server=self.server,
            _web_application=None, restart_web_chat=restart, logger=logging.getLogger('web-test'))
        path = ROOT / 'xgent_app/sections/idle.py'
        node = next(n for n in ast.parse(path.read_text(encoding='utf-8')).body
                    if getattr(n, 'name', '') == 'reconcile_web_config_once')
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), ns)
        asyncio.run_coroutine_threadsafe(ns['reconcile_web_config_once'](), self.loop).result(5)

    def test_unchanged_password_keeps_existing_session_and_server(self):
        cookie = self.login()
        self.reconcile()
        self.assertEqual(0, self.restarts)
        self.assertEqual(200, self.request('/api/health', cookie=cookie)[0])

    def test_changed_password_revokes_cookie_stream_and_terminals(self):
        cookie = self.login()
        key = self.server._httpd.session_key
        old_server = self.server
        conn = http.client.HTTPConnection('127.0.0.1', self.server._httpd.server_address[1], timeout=3)
        self.addCleanup(conn.close)
        conn.request('GET', '/api/stream', headers={'Cookie': cookie})
        response = conn.getresponse()
        self.assertEqual(200, response.status)
        self.assertEqual(b'event: ready\n', response.fp.readline())
        response.fp.readline()
        response.fp.readline()
        self.password_hash = web_auth.hash_password('new-password')
        terminal_manager = SimpleNamespace(close_all=Mock())
        with patch('xgent_app.web_server.web_terminal.get_terminal_manager', return_value=terminal_manager):
            self.reconcile()
        terminal_manager.close_all.assert_called_once()
        self.assertTrue(old_server.shutdown_event.is_set())
        self.assertNotEqual(key, self.server._httpd.session_key)
        self.assertEqual(1, self.restarts)
        self.assertEqual(b'', response.read())  # 旧 SSE 已关闭，而非继续泄漏新帧。
        self.assertEqual(401, self.request('/api/health', cookie=cookie)[0])
        self.assertEqual(401, self.request('/api/login', {'password': 'old-password'})[0])
        self.assertEqual(200, self.request('/api/health', cookie=self.login('new-password'))[0])

    def test_stopped_server_rejects_old_cookie_on_existing_handler(self):
        cookie = self.login()
        handler = SimpleNamespace(server=self.server._httpd, headers={'Cookie': cookie})
        self.assertTrue(_Handler._is_authenticated(handler))
        self.server.stop()
        self.assertFalse(_Handler._is_authenticated(handler))

    def test_http_timeout_returns_504_and_cancels_write(self):
        cookie = self.login()
        cancelled = threading.Event()
        writes = []
        async def write(key, value):
            try:
                await asyncio.sleep(30)
                writes.append(value)
            finally:
                cancelled.set()
        self.server.config.write_setting = write
        original = _Handler._run_coro
        def short_wait(handler, coro, timeout=30):
            return original(handler, coro, timeout=0.02)
        with patch.object(_Handler, '_run_coro', short_wait):
            status, _, data = self.request('/api/config', {'key': 'global_depth', 'value': 42}, cookie)
        self.assertEqual(504, status)
        self.assertIn('可能已完成', data['error'])
        self.assertTrue(cancelled.wait(1))
        self.assertEqual([], writes)


if __name__ == '__main__':
    unittest.main()
