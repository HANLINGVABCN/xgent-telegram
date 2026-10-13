"""Functional tests for File Trash and Rename Copy/Move in WebDAV file manager."""
import importlib.util
import io
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "skill-public/script/webdav-filemanager/server.py"


@pytest.fixture
def app(tmp_path):
    spec = importlib.util.spec_from_file_location("webdav_trash_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    storage = tmp_path / "files"
    storage.mkdir()
    module.ROOT_DIR = str(storage)
    module.STATE_FILE = str(tmp_path / "state.json")
    module.AUTH_FILE = str(tmp_path / "auth.json")
    return module


def request(app, method, data=None, query=None):
    handler = object.__new__(app.FileManagerHandler)
    handler.headers = {}
    handler.close_connection = False
    handler.read_json = lambda: data or {}
    handler.send_json = lambda payload, status=200, headers=None: setattr(handler, "response", (status, payload))
    handler.send_ok = lambda msg="ok": setattr(handler, "response", (200, {"success": True, "message": msg}))
    handler.send_err = lambda code, msg: setattr(handler, "response", (code, {"success": False, "error": msg}))
    if query is None:
        getattr(handler, method)()
    else:
        getattr(handler, method)(query)
    return getattr(handler, "response", (500, {}))


def test_delete_moves_file_to_trash(app):
    f = Path(app.ROOT_DIR) / "test.txt"
    f.write_text("hello trash", encoding="utf-8")
    status, data = request(app, "_api_delete", {"paths": ["/test.txt"]})
    assert status == 200
    assert not f.exists()
    trash_dir = Path(app.ROOT_DIR) / ".trash"
    assert trash_dir.exists()
    assert len(list(trash_dir.glob("*_test.txt"))) == 1

    # _api_list should not show .trash
    status, list_data = request(app, "_api_list", query={"path": ["/"]})
    assert status == 200
    assert all(item["name"] != ".trash" for item in list_data["items"])


def test_file_trash_list_and_restore(app):
    f = Path(app.ROOT_DIR) / "doc.txt"
    f.write_text("important doc", encoding="utf-8")
    request(app, "_api_delete", {"paths": ["/doc.txt"]})

    status, res = request(app, "_api_file_trash_list")
    assert status == 200
    assert len(res["items"]) == 1
    item = res["items"][0]
    assert item["name"] == "doc.txt"
    assert item["originalPath"] == "/doc.txt"

    # Restore
    status, restore_res = request(app, "_api_file_trash_restore", {"ids": [item["id"]]})
    assert status == 200
    assert f.exists()
    assert f.read_text(encoding="utf-8") == "important doc"


def test_file_trash_restore_with_conflict_renames(app):
    f = Path(app.ROOT_DIR) / "doc.txt"
    f.write_text("original content", encoding="utf-8")
    request(app, "_api_delete", {"paths": ["/doc.txt"]})

    # Create a new doc.txt in the same place
    f.write_text("new content", encoding="utf-8")

    status, res = request(app, "_api_file_trash_list")
    item = res["items"][0]

    # Restore should auto-rename with (1)
    status, _ = request(app, "_api_file_trash_restore", {"ids": [item["id"]]})
    assert status == 200
    assert f.read_text(encoding="utf-8") == "new content"
    renamed = Path(app.ROOT_DIR) / "doc (1).txt"
    assert renamed.exists()
    assert renamed.read_text(encoding="utf-8") == "original content"


def test_file_trash_permanent_delete_and_clear(app):
    f1 = Path(app.ROOT_DIR) / "f1.txt"
    f2 = Path(app.ROOT_DIR) / "f2.txt"
    f1.write_text("f1", encoding="utf-8")
    f2.write_text("f2", encoding="utf-8")
    request(app, "_api_delete", {"paths": ["/f1.txt", "/f2.txt"]})

    status, res = request(app, "_api_file_trash_list")
    assert len(res["items"]) == 2

    # Delete one permanently
    tid1 = res["items"][0]["id"]
    status, _ = request(app, "_api_file_trash_delete", {"ids": [tid1]})
    assert status == 200

    status, res = request(app, "_api_file_trash_list")
    assert len(res["items"]) == 1

    # Clear trash
    status, _ = request(app, "_api_file_trash_clear")
    assert status == 200

    status, res = request(app, "_api_file_trash_list")
    assert len(res["items"]) == 0


def test_copy_and_move_with_renames(app):
    f = Path(app.ROOT_DIR) / "sample.txt"
    f.write_text("sample copy", encoding="utf-8")

    # Copy with rename
    status, data = request(app, "_api_copy", {
        "sources": ["/sample.txt"],
        "dest": "/",
        "renames": {"/sample.txt": "sample (1).txt"}
    })
    assert status == 200
    copied = Path(app.ROOT_DIR) / "sample (1).txt"
    assert copied.exists()
    assert copied.read_text(encoding="utf-8") == "sample copy"
    assert f.exists()

    # Move with rename
    status, data = request(app, "_api_move", {
        "sources": ["/sample (1).txt"],
        "dest": "/",
        "renames": {"/sample (1).txt": "sample (2).txt"}
    })
    assert status == 200
    assert not copied.exists()
    moved = Path(app.ROOT_DIR) / "sample (2).txt"
    assert moved.exists()
    assert moved.read_text(encoding="utf-8") == "sample copy"
