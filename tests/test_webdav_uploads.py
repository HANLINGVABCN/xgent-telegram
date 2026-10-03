"""Functional upload regressions; all storage is isolated in pytest temporary roots."""
import importlib.util
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "skill-public/script/webdav-filemanager/server.py"


@pytest.fixture
def app(tmp_path):
    spec = importlib.util.spec_from_file_location("webdav_upload_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    storage = tmp_path / "files"
    storage.mkdir()
    module.ROOT_DIR = str(storage)
    module.STATE_FILE = str(tmp_path / "state.json")
    module.AUTH_FILE = str(tmp_path / "auth.json")
    return module


def request(app, method, data=None, query=None, body=b"", declared=None):
    handler = object.__new__(app.FileManagerHandler)
    handler.headers = {"Content-Length": str(len(body) if declared is None else declared)}
    handler.rfile = io.BytesIO(body)
    handler.close_connection = False
    handler.read_json = lambda: data or {}
    handler.send_json = lambda payload, status=200, headers=None: setattr(handler, "response", (status, payload))
    if query is None:
        getattr(handler, method)()
    else:
        getattr(handler, method)(query)
    return handler.response


def init(app, name="sample.bin", size=4, **kwargs):
    return request(app, "_api_upload_init", {"path": "/", "name": name, "size": size, **kwargs})


def chunk(app, tid, body, offset=0, declared=None):
    return request(app, "_api_upload_chunk", query={"taskId": [tid], "offset": [str(offset)]},
                   body=body, declared=declared)


def complete(app, tid):
    return request(app, "_api_upload_complete", {"taskId": tid})


def test_empty_file_completes(app):
    status, data = init(app, size=0)
    assert status == 200
    assert complete(app, data["taskId"])[0] == 200
    assert (Path(app.ROOT_DIR) / "sample.bin").read_bytes() == b""


def test_partial_file_cannot_be_marked_complete(app):
    _, data = init(app, size=4)
    tid = data["taskId"]
    assert chunk(app, tid, b"ab")[0] == 200
    assert complete(app, tid)[0] == 409
    assert not (Path(app.ROOT_DIR) / "sample.bin").exists()
    assert app.load_persisted_tasks()[tid]["status"] != "done"
    assert chunk(app, tid, b"cd", offset=2)[0] == 200
    assert complete(app, tid)[0] == 200
    assert (Path(app.ROOT_DIR) / "sample.bin").read_bytes() == b"abcd"


def test_complete_never_overwrites_a_new_conflicting_file(app):
    _, data = init(app)
    tid = data["taskId"]
    chunk(app, tid, b"abcd")
    target = Path(app.ROOT_DIR) / "sample.bin"
    target.write_bytes(b"existing-user-data")
    assert complete(app, tid)[0] == 409
    assert target.read_bytes() == b"existing-user-data"
    assert target.with_name("sample.bin.part").read_bytes() == b"abcd"


def test_zero_byte_upload_does_not_overwrite_existing_file(app):
    target = Path(app.ROOT_DIR) / "sample.bin"
    target.write_bytes(b"existing")
    assert init(app, size=0)[0] == 409
    assert target.read_bytes() == b"existing"


def test_chunk_cannot_exceed_declared_file_size(app):
    _, data = init(app, size=2)
    assert chunk(app, data["taskId"], b"abcd")[0] == 400
    part = Path(app.ROOT_DIR) / "sample.bin.part"
    assert part.read_bytes() == b""


def test_short_chunk_reports_error_and_can_resume(app):
    _, data = init(app)
    tid = data["taskId"]
    assert chunk(app, tid, b"ab", declared=4)[0] == 400
    assert init(app)[1]["offset"] == 2
    assert chunk(app, tid, b"cd", offset=2)[0] == 200
    assert complete(app, tid)[0] == 200


def test_complete_is_idempotent_after_response_lost(app):
    _, data = init(app)
    tid = data["taskId"]
    chunk(app, tid, b"abcd")
    assert complete(app, tid)[0] == 200
    app.UPLOAD_TASKS.clear()  # also works after service restart
    assert complete(app, tid)[0] == 200
    assert (Path(app.ROOT_DIR) / "sample.bin").read_bytes() == b"abcd"


def test_cancel_after_restart_removes_partial_data(app):
    _, data = init(app)
    tid = data["taskId"]
    chunk(app, tid, b"ab")
    app.UPLOAD_TASKS.clear()
    assert request(app, "_api_upload_cancel", {"taskId": tid, "action": "cancel"})[0] == 200
    assert not (Path(app.ROOT_DIR) / "sample.bin.part").exists()
    assert tid not in app.load_persisted_tasks()


def test_folder_resume_after_restart_keeps_task_and_offset(app):
    _, data = init(app, relpath="folder/sample.bin", batch="batch-1")
    tid = data["taskId"]
    chunk(app, tid, b"ab")
    app.UPLOAD_TASKS.clear()
    app.UPLOAD_BATCH_DIRS.clear()
    app.UPLOAD_BATCH_UPDATED.clear()
    status, data = init(app, relpath="folder/sample.bin", batch="batch-1")
    assert status == 200
    assert data["taskId"] == tid
    assert data["offset"] == 2


def test_unrelated_existing_folder_still_conflicts(app):
    (Path(app.ROOT_DIR) / "folder").mkdir()
    assert init(app, relpath="folder/sample.bin", batch="batch-1")[0] == 409


def test_unowned_part_file_is_not_used_as_upload_data(app):
    target = Path(app.ROOT_DIR) / "sample.bin.part"
    target.write_bytes(b"unrelated")
    assert init(app)[0] == 409
    assert target.read_bytes() == b"unrelated"


def test_resume_with_different_file_size_is_rejected(app):
    _, data = init(app, size=4)
    chunk(app, data["taskId"], b"ab")
    assert init(app, size=8)[0] == 409
    assert app.load_persisted_tasks()[data["taskId"]]["size"] == 4


def test_short_ordinary_upload_is_not_published(app):
    status, _ = request(app, "_api_upload", query={"path": ["/"], "name": ["plain.bin"]},
                        body=b"ab", declared=4)
    assert status == 400
    assert not (Path(app.ROOT_DIR) / "plain.bin").exists()


def test_normal_ordinary_upload(app):
    status, _ = request(app, "_api_upload", query={"path": ["/"], "name": ["plain.bin"]}, body=b"abcd")
    assert status == 200
    assert (Path(app.ROOT_DIR) / "plain.bin").read_bytes() == b"abcd"


@pytest.mark.parametrize("size", [-1, "invalid"])
def test_invalid_upload_size_is_a_client_error(app, size):
    assert init(app, size=size)[0] == 400


def test_duplicate_chunks_cannot_append_twice(app):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    _, data = init(app)
    barrier = threading.Barrier(2)
    def send():
        barrier.wait(timeout=3)
        return chunk(app, data["taskId"], b"abcd")[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(send) for _ in range(2)]
        statuses = sorted(job.result(timeout=5) for job in jobs)
    assert statuses == [200, 409]
    assert (Path(app.ROOT_DIR) / "sample.bin.part").read_bytes() == b"abcd"
    assert complete(app, data["taskId"])[0] == 200


def test_completed_task_rejects_late_chunks(app):
    _, data = init(app)
    tid = data["taskId"]
    chunk(app, tid, b"abcd")
    complete(app, tid)
    assert chunk(app, tid, b"abcd")[0] == 409
    assert not (Path(app.ROOT_DIR) / "sample.bin.part").exists()


def test_no_hardlink_filesystem_uses_exclusive_copy(app, monkeypatch):
    import errno
    def unsupported(*args):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")
    monkeypatch.setattr(app.os, "link", unsupported)
    _, data = init(app)
    chunk(app, data["taskId"], b"abcd")
    assert complete(app, data["taskId"])[0] == 200
    assert (Path(app.ROOT_DIR) / "sample.bin").read_bytes() == b"abcd"
    assert not (Path(app.ROOT_DIR) / "sample.bin.part").exists()


def test_copy_fallback_still_does_not_overwrite(app, monkeypatch):
    import errno
    def unsupported(*args):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")
    monkeypatch.setattr(app.os, "link", unsupported)
    _, data = init(app)
    chunk(app, data["taskId"], b"abcd")
    target = Path(app.ROOT_DIR) / "sample.bin"
    target.write_bytes(b"keep")
    assert complete(app, data["taskId"])[0] == 409
    assert target.read_bytes() == b"keep"


def test_failed_copy_keeps_part_and_does_not_publish_partial_file(app, monkeypatch):
    import errno
    def unsupported(*args):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")
    def broken_copy(src, out, size):
        out.write(b"a")
        raise OSError(errno.ENOSPC, "disk full")
    monkeypatch.setattr(app.os, "link", unsupported)
    monkeypatch.setattr(app.shutil, "copyfileobj", broken_copy)
    _, data = init(app)
    chunk(app, data["taskId"], b"abcd")
    assert complete(app, data["taskId"])[0] == 500
    assert not (Path(app.ROOT_DIR) / "sample.bin").exists()
    assert (Path(app.ROOT_DIR) / "sample.bin.part").read_bytes() == b"abcd"


def test_cancel_failure_keeps_record_for_retry(app, monkeypatch):
    _, data = init(app)
    tid = data["taskId"]
    chunk(app, tid, b"ab")
    app.UPLOAD_TASKS.clear()
    original_remove = app.os.remove
    def refuse_part(path, *args, **kwargs):
        if str(path).endswith("sample.bin.part"):
            raise PermissionError("busy file")
        return original_remove(path, *args, **kwargs)
    monkeypatch.setattr(app.os, "remove", refuse_part)
    assert request(app, "_api_upload_cancel", {"taskId": tid, "action": "cancel"})[0] == 500
    assert tid in app.load_persisted_tasks()
    assert (Path(app.ROOT_DIR) / "sample.bin.part").read_bytes() == b"ab"


def test_http_upload_pause_restart_resume_download(app):
    import base64
    import http.client
    import json
    import threading
    app.AUTH_CRED = "test:temporary-password"
    auth = "Basic " + base64.b64encode(app.AUTH_CRED.encode()).decode()
    server = app.ThreadedHTTPServer(("127.0.0.1", 0), app.FileManagerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def call(path, body=None, binary=False):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        headers = {"Authorization": auth}
        if body is not None and not binary:
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            connection.request("POST" if body is not None else "GET", path, body, headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, payload
        finally:
            connection.close()
    try:
        status, raw = call("/api/upload-init", {"path":"/", "name":"http.bin", "size":4})
        assert status == 200
        tid = json.loads(raw)["taskId"]
        assert call(f"/api/upload-chunk?taskId={tid}&offset=0", b"ab", True)[0] == 200
        assert call("/api/upload-cancel", {"taskId":tid, "action":"pause"})[0] == 200
        app.UPLOAD_TASKS.clear()
        status, raw = call("/api/upload-init", {"path":"/", "name":"http.bin", "size":4})
        assert status == 200 and json.loads(raw)["offset"] == 2
        assert call(f"/api/upload-chunk?taskId={tid}&offset=2", b"cd", True)[0] == 200
        assert call("/api/upload-complete", {"taskId":tid})[0] == 200
        assert call("/api/download?path=/http.bin") == (200, b"abcd")
        status, raw = call("/api/tasks")
        record = next(t for t in json.loads(raw)["items"] if t["id"] == tid)
        assert status == 200 and record["status"] == "done" and record["loaded"] == 4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
