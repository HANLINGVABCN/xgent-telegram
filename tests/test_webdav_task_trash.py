"""Persistent task deletion must survive purge, stale workers and refresh."""
from pathlib import Path

from tests.test_webdav_uploads import app, request


def seed(app, task_id='remote-test'):
    root = Path(app.ROOT_DIR)
    target = root / 'download' / 'kept.bin'
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b'keep finished file')
    task = {'id': task_id, 'kind': 'remote', 'name': 'kept.bin', 'path': '/download/kept.bin',
            'url': 'https://example.invalid/file', 'status': 'done', 'size': 18, 'loaded': 18,
            'createdAt': 1, 'tempPath': str(target) + '.part'}
    app.save_task(task)
    app.REMOTE_TASKS[task_id] = task
    return dict(task), target


def delete(app, task, trash=True):
    return request(app, '_api_tasks_delete', {'items': [task], 'trash': trash})


def assert_gone(app, task_id):
    assert task_id not in app.load_persisted_tasks()
    assert task_id not in app.load_state().get('taskTrash', {})
    status, data = request(app, '_api_tasks', query={})
    assert status == 200 and all(t['id'] != task_id for t in data['items'])


def test_delete_purge_then_late_worker_cannot_resurrect(app):
    task, target = seed(app)
    assert delete(app, task)[0] == 200
    assert task['id'] in app.load_state()['taskTrash']
    assert delete(app, task, trash=False)[0] == 200
    app.save_task(task)  # old worker finishes after the trash was emptied
    assert_gone(app, task['id'])
    assert target.read_bytes() == b'keep finished file'


def test_hard_delete_handles_legacy_frontend_only_trash(app):
    task, target = seed(app)
    assert delete(app, task, trash=False)[0] == 200
    assert task['id'] not in app.REMOTE_TASKS
    assert_gone(app, task['id'])
    assert target.exists()


def test_late_soft_delete_cannot_recreate_purged_trash(app):
    task, _ = seed(app)
    delete(app, task)
    delete(app, task, trash=False)
    delete(app, task)  # stale request from another tab
    assert_gone(app, task['id'])


def test_stale_whole_state_save_cannot_undo_task_delete(app):
    task, _ = seed(app)
    old_state = app.load_state()
    delete(app, task)
    old_state['pinned'] = ['/example']
    app.save_state(old_state)
    assert task['id'] not in app.load_persisted_tasks()
    assert task['id'] in app.load_state()['taskTrash']


def test_stale_state_after_purge_does_not_restore_task(app):
    task, _ = seed(app)
    old_state = app.load_state()
    delete(app, task)
    delete(app, task, trash=False)
    app.save_state(old_state)
    assert_gone(app, task['id'])


def test_restore_is_persisted_and_preserves_finished_file(app):
    task, target = seed(app)
    delete(app, task)
    status, _ = request(app, '_api_tasks_restore', {'id': task['id']})
    assert status == 200
    assert task['id'] in app.load_persisted_tasks()
    assert task['id'] not in app.load_state()['taskTrash']
    assert target.exists()


def test_new_task_with_same_path_is_not_deleted_by_old_id(app):
    old, _ = seed(app)
    delete(app, old)
    delete(app, old, trash=False)
    new = dict(old, id='remote-new')
    app.save_task(new)
    delete(app, old, trash=False)
    assert 'remote-new' in app.load_persisted_tasks()


def test_running_worker_finishing_after_purge_does_not_republish(app, monkeypatch):
    import threading
    task, target = seed(app)
    app.REMOTE_TASKS[task['id']]['status'] = 'queued'
    started, release = threading.Event(), threading.Event()
    class Response:
        status = 200
        headers = {'Content-Length': '4'}
        def read(self, size):
            started.set()
            assert release.wait(5)
            return b'abcd'
        def close(self): pass
    monkeypatch.setattr(app, 'open_remote_url', lambda *args, **kwargs: Response())
    thread = threading.Thread(target=app.remote_download_worker, args=(task['id'],), daemon=True)
    thread.start()
    try:
        assert started.wait(3)
        delete(app, task)
        delete(app, task, trash=False)
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert_gone(app, task['id'])
    assert target.read_bytes() == b'keep finished file'
    assert not Path(str(target) + '.part').exists()


def test_permanent_deletion_survives_module_restart(app):
    import importlib.util
    task, _ = seed(app)
    delete(app, task)
    delete(app, task, trash=False)
    spec = importlib.util.spec_from_file_location('restarted_webdav', app.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    fresh.STATE_FILE, fresh.ROOT_DIR = app.STATE_FILE, app.ROOT_DIR
    fresh.save_task(task)
    assert_gone(fresh, task['id'])
