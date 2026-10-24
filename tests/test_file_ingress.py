"""File reception is independent of Telegram limits and bounded in memory."""
import hashlib
import io
from pathlib import Path

import pytest

from xgent_app.file_ingress import FileTransportCapabilities, read_multipart


def multipart(files, fields=None, boundary='test-boundary'):
    parts = []
    for name, value in (fields or {}).items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode() + value.encode() + b'\r\n')
    for filename, content in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + content + b'\r\n')
    return b''.join(parts) + f'--{boundary}--\r\n'.encode()


class BoundedReader(io.BytesIO):
    def read(self, size=-1):
        assert 0 <= size <= 65536
        return super().read(size)


def test_multipart_multiple_files_empty_unicode_and_boundary_like_bytes(tmp_path):
    content = b'A' * 65521 + b'\r\n--test-boundaryX\r\n--test-boundary--X' + b'Z' * 150000
    files = [('archive.zip', content), ('空文件.txt', b''), ('quote;name.bin', b'abc')]
    body = multipart(files, {'text': '完整说明\n', 'conversation_id': 'abc'})
    parsed = read_multipart(BoundedReader(body), len(body), 'multipart/form-data; boundary="test-boundary"', tmp_path)
    try:
        assert parsed['text'] == '完整说明\n'
        assert [f['filename'] for f in parsed['files']] == [f[0] for f in files]
        for item, (_, expected) in zip(parsed['files'], files):
            assert Path(item['path']).read_bytes() == expected
            assert item['sha256'] == hashlib.sha256(expected).hexdigest()
    finally:
        parsed['_temporary'].cleanup()
    assert not list(tmp_path.iterdir())


def test_truncated_upload_cleans_all_partial_files(tmp_path):
    body = multipart([('one', b'abc'), ('two', b'def')])[:-15]
    with pytest.raises(ValueError):
        read_multipart(BoundedReader(body), len(body) + 20, 'multipart/form-data; boundary=test-boundary', tmp_path)
    assert not list(tmp_path.iterdir())


def test_no_file_count_cap(tmp_path):
    body = multipart([(str(i), b'content') for i in range(101)])
    parsed = read_multipart(BoundedReader(body), len(body), 'multipart/form-data; boundary=test-boundary', tmp_path)
    try:
        assert len(parsed['files']) == 101
    finally:
        parsed['_temporary'].cleanup()


def test_over_50mb_upload_has_bounded_memory(tmp_path):
    import tracemalloc
    header = b'--large\r\nContent-Disposition: form-data; name="file"; filename="big.bin"\r\n\r\n'
    tail = b'\r\n--large--\r\n'
    size = 51 * 1024 * 1024
    class LargeStream:
        def __init__(self):
            self.offset = 0
        def read(self, count):
            assert count <= 65536
            start = self.offset; self.offset += count
            output = bytearray()
            if start < len(header):
                output.extend(header[start:min(len(header), start+count)])
            body_start = max(start, len(header))
            body_end = min(start+count, len(header)+size)
            if body_end > body_start:
                output.extend(b'x' * (body_end-body_start))
            if start+count > len(header)+size:
                output.extend(tail[max(0, start-len(header)-size):start+count-len(header)-size])
            return bytes(output)
    tracemalloc.start()
    parsed = read_multipart(LargeStream(), len(header)+size+len(tail), 'multipart/form-data; boundary=large', tmp_path)
    try:
        _, peak = tracemalloc.get_traced_memory()
        assert parsed['files'][0]['size'] == size
        assert Path(parsed['files'][0]['path']).stat().st_size == size
        assert peak < 3 * 1024 * 1024
    finally:
        tracemalloc.stop()
        parsed['_temporary'].cleanup()


def test_transport_modes_do_not_assume_custom_proxy_is_local(monkeypatch):
    monkeypatch.delenv('TELEGRAM_API_MODE', raising=False)
    assert FileTransportCapabilities.configured('https://proxy.example').mode == 'cloud'
    assert FileTransportCapabilities.configured('http://127.0.0.1:8081').mode == 'local_bot_api'
    assert FileTransportCapabilities.configured('https://private.example', 'local_bot_api').download_limit is None


def test_web_and_telegram_file_workflows():
    from tests.test_thinking_params import run_in_app
    result = run_in_app(
        "import asyncio, tempfile\n"
        "from pathlib import Path\n"
        "from tests.file_ingress_probe import workflows\n"
        "with tempfile.TemporaryDirectory() as directory:\n"
        "    print(json.dumps(asyncio.run(workflows(bot, Path(directory)))))\n"
    )
    assert all(result.values()), result


def test_real_http_batch_routes_ignore_telegram_limits(tmp_path):
    import asyncio
    import json
    import threading
    import urllib.request
    from concurrent.futures import Future
    from xgent_app import web_auth
    from xgent_app.web_server import WebChatConfig, WebChatServer
    received = []
    def submit(data, mode, outbox, **options):
        received.append((mode, [Path(f['path']).read_bytes() for f in data['files']]))
        data['_temporary'].cleanup()
        receipt = Future(); receipt.set_result({'ok': True})
        return receipt
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True); thread.start()
    config = WebChatConfig('127.0.0.1', 0, web_auth.hash_password('password'), '', 1, loop,
        lambda *a: None, lambda *a: None, lambda *a: None, lambda *a: None,
        lambda: None, lambda: False, submit_file_batch=submit, upload_body_limit=1,
        upload_directory=str(tmp_path))
    server = WebChatServer(config); server.start()
    base = 'http://127.0.0.1:' + str(server._httpd.server_address[1])
    try:
        login = urllib.request.Request(base+'/api/login', data=json.dumps({'password':'password'}).encode(),
            headers={'Content-Type':'application/json', 'Origin':base})
        with urllib.request.urlopen(login) as response:
            cookie = response.headers['Set-Cookie'].split(';')[0]
        for endpoint in ('/api/attachments', '/api/server-uploads', '/api/upload'):
            body = multipart([(str(i), b'x'*1000) for i in range(11)])
            request = urllib.request.Request(base+endpoint, data=body, headers={
                'Content-Type':'multipart/form-data; boundary=test-boundary', 'Origin':base, 'Cookie':cookie})
            with urllib.request.urlopen(request) as response:
                assert json.loads(response.read())['ok']
        assert [mode for mode, _ in received] == ['attachment', 'server_upload', 'attachment']
        assert all(len(files)==11 for _, files in received)
        assert not list(tmp_path.iterdir())
    finally:
        server.stop(); loop.call_soon_threadsafe(loop.stop); thread.join(5); loop.close()
