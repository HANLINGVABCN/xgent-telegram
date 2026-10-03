"""Offline security regression tests; no real outbound connections."""
import importlib.util
import io
import socket
import ssl
from pathlib import Path
from unittest.mock import Mock

import pytest

SERVER = Path(__file__).resolve().parents[1] / "skill-public/script/webdav-filemanager/server.py"


@pytest.fixture
def app():
    spec = importlib.util.spec_from_file_location("webdav_security_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ALLOW_PRIVATE_REMOTE = False
    return module


def address(ip, port=80):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))


def test_connection_uses_only_checked_addresses(app, monkeypatch):
    resolved, connected = [], []
    def dns(host, port, **kwargs):
        resolved.append(host)
        ip = "93.184.216.34" if len(resolved) == 1 else "127.0.0.1"
        return [address(ip, port)]
    class OfflineSocket:
        def __init__(self, *args): pass
        def settimeout(self, timeout): pass
        def connect(self, target):
            connected.append(target)
            raise OSError("offline fixture")
        def close(self): pass
    monkeypatch.setattr(app.socket, "getaddrinfo", dns)
    monkeypatch.setattr(app.socket, "socket", OfflineSocket)
    with pytest.raises(app.urllib.error.URLError, match="offline fixture"):
        app.open_remote_url("http://audit.invalid/file", {}, timeout=1)
    assert resolved == ["audit.invalid"]
    assert connected == [("93.184.216.34", 80)]


def test_https_keeps_hostname_verification_and_sni(app, monkeypatch):
    addresses = (address("93.184.216.34", 443),)
    sock = Mock()
    connector = Mock(return_value=sock)
    monkeypatch.setattr(app, "connect_remote_addresses", connector)
    context = ssl.create_default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    wrapped = Mock(return_value=sock)
    monkeypatch.setattr(context, "wrap_socket", wrapped)
    factory = app.pinned_connection_factory(app.http.client.HTTPSConnection, addresses)
    connection = factory("downloads.example:443", timeout=3, context=context)
    connection.connect()
    connector.assert_called_once_with(addresses, 3, None)
    assert wrapped.call_args.kwargs["server_hostname"] == "downloads.example"
    connection.close()


def test_redirect_is_validated_again(app, monkeypatch):
    def dns(host, port, **kwargs):
        ip = "127.0.0.1" if host == "private.invalid" else "93.184.216.34"
        return [address(ip, port)]
    monkeypatch.setattr(app.socket, "getaddrinfo", dns)
    opener = Mock()
    opener.open.side_effect = app._RedirectTo("http://private.invalid/file")
    monkeypatch.setattr(app.urllib.request, "build_opener", Mock(return_value=opener))
    with pytest.raises(ValueError, match="127.0.0.1"):
        app.open_remote_url("http://public.invalid/file", {})
    assert opener.open.call_count == 1


@pytest.mark.parametrize("url", ["http://example.com:0/x", "http://example.com:70000/x",
    "http://example.com:bad/x", "http://[broken/x", "http://user:pass@example.com/x"])
def test_invalid_remote_url_returns_error(app, url):
    assert app.validate_remote_url(url)[0] is False


@pytest.mark.parametrize("ips", [[], ["100.64.0.1"], ["127.0.0.1"], ["224.0.0.1"],
                                     ["93.184.216.34", "10.0.0.1"]])
def test_non_public_or_empty_dns_results_are_rejected(app, monkeypatch, ips):
    monkeypatch.setattr(app.socket, "getaddrinfo", lambda *a, **k: [address(ip) for ip in ips])
    assert app.validate_remote_url("http://example.invalid/x")[0] is False


def test_login_lock_expires_with_fresh_counter(app, monkeypatch):
    handler = object.__new__(app.FileManagerHandler)
    clock = [1000.0]
    monkeypatch.setattr(app.time, "time", lambda: clock[0])
    for _ in range(app.LOGIN_MAX_FAILS): handler._record_login_fail("test-peer")
    assert not handler._check_login_rate("test-peer")
    clock[0] += app.LOGIN_LOCKOUT_SECONDS + 1
    assert handler._check_login_rate("test-peer")
    handler._record_login_fail("test-peer")
    assert handler._check_login_rate("test-peer")
    assert app._login_attempts["test-peer"]["fails"] == 1


def test_login_tracking_is_bounded_and_expires(app, monkeypatch):
    handler = object.__new__(app.FileManagerHandler)
    monkeypatch.setattr(app, "MAX_LOGIN_TRACKED_IPS", 3)
    clock = [1000.0]
    monkeypatch.setattr(app.time, "time", lambda: clock[0])
    for ip in range(5): handler._record_login_fail(str(ip))
    assert len(app._login_attempts) == 3
    clock[0] += app.LOGIN_ATTEMPT_TTL + 1
    handler._record_login_fail("new")
    assert set(app._login_attempts) == {"new"}


def test_login_body_limit_is_checked_before_reading(app):
    handler = object.__new__(app.FileManagerHandler)
    handler.path = "/api/auth/login"
    handler.headers = {"Content-Length": str(app.MAX_AUTH_BODY_SIZE + 1)}
    handler.rfile = Mock()
    with pytest.raises(app.RequestBodyError) as error:
        handler.read_body()
    assert error.value.status == 413
    handler.rfile.read.assert_not_called()


@pytest.mark.parametrize("body", [b"[]", b"null", b"bad", b'"text"', b"123"])
def test_json_body_must_be_an_object(app, body):
    handler = object.__new__(app.FileManagerHandler)
    handler.path = "/api/auth/login"
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    with pytest.raises(app.RequestBodyError) as error:
        handler.read_json()
    assert error.value.status == 400


def test_request_framing_rejects_duplicates_and_stream_encoding(app):
    from email.message import Message
    handler = object.__new__(app.FileManagerHandler)
    handler.headers = Message()
    handler.headers['Content-Length'] = '0'
    handler.headers['Content-Length'] = '5'
    with pytest.raises(app.RequestBodyError) as error:
        handler.request_body_length()
    assert error.value.status == 400
    handler.headers = {'Transfer-Encoding': 'chunked'}
    with pytest.raises(app.RequestBodyError):
        handler.request_body_length()


def test_login_route_preserves_body_limit_error(app):
    handler = object.__new__(app.FileManagerHandler)
    app.AUTH_CRED = 'test:password'
    handler.path = '/api/auth/login'
    handler.client_address = ('127.0.0.1', 1234)
    handler.headers = {'Content-Length': str(app.MAX_AUTH_BODY_SIZE + 1)}
    handler.rfile = Mock()
    handler.send_err = Mock()
    handler._route('POST')
    assert handler.send_err.call_args.args[0] == 413
    handler.rfile.read.assert_not_called()
    assert handler.close_connection


def test_request_timeout_is_applied_to_accepted_socket(app, monkeypatch):
    server = object.__new__(app.ThreadedHTTPServer)
    server.request_timeout = 2.5
    request = Mock()
    monkeypatch.setattr(app.HTTPServer, 'get_request', lambda self: (request, ('127.0.0.1', 1)))
    assert server.get_request()[0] is request
    request.settimeout.assert_called_once_with(2.5)


def test_worker_cap_rejects_excess_and_recovers_after_completion(app, monkeypatch):
    import threading
    server = object.__new__(app.ThreadedHTTPServer)
    server._request_slots = threading.BoundedSemaphore(1)
    server.shutdown_request = Mock()
    dispatch = Mock()
    monkeypatch.setattr(app.ThreadingMixIn, 'process_request', dispatch)
    monkeypatch.setattr(app.ThreadingMixIn, 'process_request_thread', lambda *args: None)
    first, excess, later = Mock(), Mock(), Mock()
    peer = ('127.0.0.1', 1234)
    server.process_request(first, peer)
    server.process_request(excess, peer)
    assert dispatch.call_count == 1
    import json
    response = excess.sendall.call_args.args[0]
    assert response.startswith(b'HTTP/1.1 503')
    assert json.loads(response.split(b'\r\n\r\n', 1)[1])['error']
    server.shutdown_request.assert_called_once_with(excess)
    server.process_request_thread(first, peer)
    server.process_request(later, peer)
    assert dispatch.call_count == 2
    server.process_request_thread(later, peer)


def test_worker_start_failure_releases_slot(app, monkeypatch):
    import threading
    server = object.__new__(app.ThreadedHTTPServer)
    server._request_slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(app.ThreadingMixIn, 'process_request', Mock(side_effect=RuntimeError('thread failure')))
    with pytest.raises(RuntimeError):
        server.process_request(Mock(), ('127.0.0.1', 1234))
    assert server._request_slots.acquire(blocking=False)


def test_outbound_opener_does_not_use_implicit_proxy(app, monkeypatch):
    monkeypatch.setattr(app.socket, 'getaddrinfo', lambda *a, **k: [address('93.184.216.34')])
    opener = Mock()
    build = Mock(return_value=opener)
    monkeypatch.setattr(app.urllib.request, 'build_opener', build)
    app.open_remote_url('http://public.invalid/file', {})
    proxies = [handler for handler in build.call_args.args if isinstance(handler, app.urllib.request.ProxyHandler)]
    assert len(proxies) == 1 and proxies[0].proxies == {}


def test_encoded_auth_route_keeps_small_body_limit(app):
    handler = object.__new__(app.FileManagerHandler)
    handler.path = '/api/%61uth/login'
    handler.headers = {'Content-Length': str(app.MAX_AUTH_BODY_SIZE + 1)}
    handler.rfile = Mock()
    with pytest.raises(app.RequestBodyError) as error:
        handler.read_body()
    assert error.value.status == 413
    handler.rfile.read.assert_not_called()


def test_normal_remote_download_preserves_host_and_handles_redirect(app, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen_hosts = []
    class FixtureHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen_hosts.append(self.headers['Host'])
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/file')
                self.end_headers()
                return
            body = b'local-test-file'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args): pass
    server = HTTPServer(('127.0.0.1', 0), FixtureHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    app.ALLOW_PRIVATE_REMOTE = True
    monkeypatch.setattr(app.urllib.request, 'getproxies', Mock(side_effect=AssertionError('implicit proxy')))
    try:
        with app.open_remote_url(f'http://127.0.0.1:{server.server_port}/redirect', {}, timeout=2) as response:
            assert response.read() == b'local-test-file'
        assert seen_hosts == [f'127.0.0.1:{server.server_port}'] * 2
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
