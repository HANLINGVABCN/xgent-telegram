from concurrent.futures import Future
import hashlib
import io
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from xgent_app.file_transfers import CHUNK_BYTES, FileTransfers, TransferError


def spec(size, **fields):
    return {'idempotency_key': 'stable-batch', 'conversation_id': 'global_memory',
            'mode': 'attachment', 'text': '大文件说明',
            'files': [{'filename': 'archive.zip', 'size': size}], **fields}


def until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('Timed out waiting for background file confirmation')


def test_interrupted_chunks_retry_from_actual_offset_and_survive_restart(tmp_path):
    store = FileTransfers(tmp_path)
    try:
        transfer = store.begin(spec(6)); identifier = transfer['transfer_id']
        assert store.append(identifier, 0, 0, io.BytesIO(b'abc'), 3)['offset'] == 3
        assert store.append(identifier, 0, 0, io.BytesIO(b'abc'), 3)['offset'] == 3
        with pytest.raises(TransferError, match='不一致'):
            store.append(identifier, 0, 0, io.BytesIO(b'bad'), 3)
        with pytest.raises(TransferError, match='中断'):
            store.append(identifier, 0, 3, io.BytesIO(b'd'), 3)
        assert store.status(identifier)['offsets'] == [3]
    finally:
        store.close()
    restarted = FileTransfers(tmp_path)
    try:
        assert restarted.begin(spec(6))['offsets'] == [3]
        restarted.append(identifier, 0, 3, io.BytesIO(b'def'), 3)
        assert restarted.file(identifier, 0).read_bytes() == b'abcdef'
        with pytest.raises(TransferError, match='相同上传标识'):
            restarted.begin(spec(6, text='other text'))
        with pytest.raises(TransferError):
            restarted.status('../outside')
    finally:
        restarted.close()


def test_finalize_returns_before_original_backup_and_accepted_retry_is_deduplicated(tmp_path):
    store = FileTransfers(tmp_path)
    receipt = Future(); submitted = []
    def submit(data, mode, cid):
        submitted.append((data, mode, cid))
        assert Path(data['files'][0]['path']).read_bytes() == b'abcdef'
        return receipt
    try:
        identifier = store.begin(spec(6))['transfer_id']
        store.append(identifier, 0, 0, io.BytesIO(b'abcdef'), 6)
        assert store.complete(identifier, submit)['state'] == 'processing'
        until(lambda: len(submitted) == 1)
        assert store.status(identifier)['state'] == 'processing'
        assert store.complete(identifier, submit)['state'] == 'processing'
        receipt.set_result({'ok': True, 'record_id': 123, 'message': {'content': '大文件说明'}})
        accepted = store.status(identifier)
        assert accepted['state'] == 'accepted' and accepted['record_id'] == 123
        assert not store.file(identifier, 0).exists()
        assert store.begin(spec(6))['state'] == 'accepted'
        assert store.complete(identifier, submit)['state'] == 'accepted'
        assert len(submitted) == 1
    finally:
        store.close()


def test_failed_save_retains_bytes_for_retry_and_does_not_claim_acceptance(tmp_path):
    store = FileTransfers(tmp_path)
    try:
        identifier = store.begin(spec(3))['transfer_id']
        store.append(identifier, 0, 0, io.BytesIO(b'abc'), 3)
        store.complete(identifier, lambda *args: {'ok': False, 'error': 'disk full'})
        until(lambda: store.status(identifier)['state'] == 'failed')
        state = store.status(identifier)
        assert state['error'] == 'disk full' and state['offsets'] == [3]
        store.complete(identifier, lambda *args: {'ok': True, 'record_id': 1})
        until(lambda: store.status(identifier)['state'] == 'accepted')
    finally:
        store.close()


def test_disk_full_when_persisting_receipt_is_visible_instead_of_polling_forever(tmp_path):
    store = FileTransfers(tmp_path)
    try:
        identifier = store.begin(spec(3))['transfer_id']
        store.append(identifier, 0, 0, io.BytesIO(b'abc'), 3)
        with patch.object(store, 'save', side_effect=OSError(28, 'No space left on device')):
            store.finish(identifier, {'ok': False, 'error': '服务器磁盘空间不足'})
        state = store.status(identifier)
        assert state['state'] == 'failed' and '磁盘空间不足' in state['error']
        assert state['offsets'] == [3]
        assert identifier not in store.active
    finally:
        store.close()


def test_real_http_large_file_chunks_and_nonblocking_confirmation(tmp_path):
    import asyncio
    import json
    import threading
    import urllib.request
    from xgent_app import web_auth
    from xgent_app.web_server import WebChatConfig, WebChatServer
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True); thread.start()
    receipt = Future(); submitted = []
    def submit(data, mode, outbox, **options):
        submitted.append((data, mode, options))
        data['_temporary'].cleanup()
        return receipt
    server = WebChatServer(WebChatConfig('127.0.0.1', 0, web_auth.hash_password('password'), '', 1, loop,
        lambda *a: None, lambda *a: None, lambda *a: None, lambda *a: None, lambda: None, lambda: False,
        submit_file_batch=submit, file_transfer_directory=str(tmp_path)))
    server.start(); base = 'http://127.0.0.1:' + str(server._httpd.server_address[1])
    cookie = ''
    def request(path, data=None, raw=None):
        payload = raw if raw is not None else json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(base+path, data=payload, headers={'Origin': base, 'Cookie': cookie,
            'Content-Type': 'application/octet-stream' if raw is not None else 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read()), response.headers
    try:
        _, headers = request('/api/login', {'password': 'password'})
        cookie = headers['Set-Cookie'].split(';')[0]
        size = 51 * 1024 * 1024
        transfer, _ = request('/api/file-transfers/init', spec(size))
        identifier = transfer['transfer_id']; digest = hashlib.sha256()
        for offset in range(0, size, CHUNK_BYTES):
            content = b'x' * min(CHUNK_BYTES, size-offset); digest.update(content)
            result, _ = request(f'/api/file-transfers/chunk/{identifier}?index=0&offset={offset}', raw=content)
            assert result['offset'] == offset+len(content)
        result, _ = request('/api/file-transfers/complete/'+identifier, {})
        assert result['state'] == 'processing'
        until(lambda: bool(submitted))
        state, _ = request('/api/file-transfers/status/'+identifier)
        assert state['state'] == 'processing'  # Original backup/association has not confirmed yet.
        data, mode, options = submitted[0]
        assert data['files'][0]['size'] == size and data['files'][0]['sha256'] == digest.hexdigest()
        assert options['conversation_id'] == 'global_memory' and mode == 'attachment'
        receipt.set_result({'ok': True, 'record_id': 1})
        state, _ = request('/api/file-transfers/status/'+identifier)
        assert state['state'] == 'accepted' and state['record_id'] == 1
        assert request('/api/file-transfers/init', spec(size))[0]['state'] == 'accepted'
        assert len(submitted) == 1
    finally:
        server.stop(); loop.call_soon_threadsafe(loop.stop); thread.join(5); loop.close()


def test_large_transfer_uses_real_conversation_submission_and_original_backup():
    from tests.test_thinking_params import run_in_app
    result = run_in_app(
        'import asyncio, tempfile\n'
        'from pathlib import Path\n'
        'from tests.file_transfer_request_probe import large_upload\n'
        'with tempfile.TemporaryDirectory() as directory:\n'
        '    print(json.dumps(asyncio.run(large_upload(bot, Path(directory)))))\n'
    )
    assert all(result.values()), result
