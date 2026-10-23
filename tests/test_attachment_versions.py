import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from xgent_app.attachments import create_attachment_reference, prepare_attachment_context, AttachmentContextError
from xgent_app.attachment_versions import AttachmentVersions, VersionError, save_original_version
from tests.test_attachments import image_bytes


@pytest.fixture
def upload(tmp_path):
    root = tmp_path / 'uploads'
    root.mkdir()
    path = root / 'working.txt'
    data = b'original  contents\n'
    path.write_bytes(data)
    ref = create_attachment_reference({'abs_path': str(path)}, root, path.name, data)
    save_original_version(ref, data, root)
    return root, path, ref, AttachmentVersions(root, ref)


def test_original_independent_copy_dedup_and_latest_ten(upload):
    root, path, ref, store = upload
    original = ref['sha256']
    snapshot = store.directory / (original + '.bin')
    assert not snapshot.samefile(path)
    for i in range(15):
        data = f'version {i}'.encode()
        path.write_bytes(data)
        assert store.read_current()[0] == data
    listed = store.describe()
    assert len(listed['versions']) == 11
    assert listed['seed'] == original and listed['seed_kind'] == 'original'
    assert snapshot.read_bytes() == b'original  contents\n'
    assert len(list(store.directory.glob('*.bin'))) == 11
    path.write_bytes(b'version 10')
    store.read_current()
    assert len(store.describe()['versions']) == 11
    assert store.describe()['current'] == hashlib.sha256(b'version 10').hexdigest()
    index = (store.directory / 'index.json').read_bytes()
    store.read_current()
    assert (store.directory / 'index.json').read_bytes() == index


def test_legacy_baseline_same_size_edits_and_restart(upload):
    root, path, ref, _ = upload
    legacy = root / 'old.txt'
    legacy.write_bytes(b'BEFORE')
    reference = create_attachment_reference({'abs_path': str(legacy)}, root, legacy.name, b'BEFORE')
    legacy.write_bytes(b'AFTER!')
    store = AttachmentVersions(root, reference)
    assert store.read_current()[0] == b'AFTER!'
    assert store.describe()['seed_kind'] == 'baseline'
    assert store.describe()['seed'] != reference['sha256']
    legacy.write_bytes('new UTF16 中文'.encode('utf-16'))
    restarted = AttachmentVersions(root, reference)
    restarted.read_current()
    assert restarted.describe()['versions'][0]['encoding'] == 'utf-16'


def test_context_is_current_only_even_when_snapshots_exist(upload):
    root, path, ref, store = upload
    path.write_bytes(b'CURRENT-LATEST')
    record = {'id': 1, 'role': 'user', 'msg_type': 'user_file', 'content': 'unchanged message', 'metadata': {'attachments': [ref]}}
    parts, updates, errors = prepare_attachment_context([record], root)
    assert not errors and 'CURRENT-LATEST' in str(parts) and 'original  contents' not in str(parts)
    assert 'SHA-256' in str(parts)
    assert record['metadata']['attachments'][0]['sha256'] == ref['sha256']
    assert updates[0][2]['attachments'][0]['versions']['count'] == 2
    path.unlink()
    parts, _, errors = prepare_attachment_context([record], root)
    assert not errors and '附件暂不可用' in str(parts)
    assert 'original  contents' not in str(parts)


def test_snapshot_failure_keeps_readable_context_but_refuses_new_original(upload):
    root, path, ref, store = upload
    path.write_bytes(b'latest')
    before = (store.directory / 'index.json').read_bytes()
    with patch.object(AttachmentVersions, '_write', side_effect=OSError('disk full')):
        data, summary, warning = store.read_current()
        assert data == b'latest' and summary is None and '未能留存版本' in warning
        with pytest.raises(AttachmentContextError, match='备份失败'):
            save_original_version(ref, b'latest', root)
    assert (store.directory / 'index.json').read_bytes() == before


def test_restore_missing_and_restore_retains_pre_restore_bytes(upload):
    root, path, ref, store = upload
    path.write_bytes(b'latest to back up')
    token = store.describe()['current_file']['fingerprint']
    ticket = store.prepare_restore(ref['sha256'], token)
    try:
        ticket.apply()
        assert path.read_bytes() == b'original  contents\n'
    finally:
        ticket.finish(True)
    assert store.read_version(hashlib.sha256(b'latest to back up').hexdigest())[0] == b'latest to back up'
    path.unlink()
    ticket = store.prepare_restore(ref['sha256'], 'missing')
    try:
        ticket.apply()
    finally:
        ticket.finish(True)
    assert path.read_bytes() == b'original  contents\n'


def test_restore_conflicts_snapshot_corruption_and_unreadable_current(upload):
    root, path, ref, store = upload
    token = store.describe()['current_file']['fingerprint']
    path.write_bytes(b'new external write')
    with pytest.raises(VersionError, match='改变'):
        store.prepare_restore(ref['sha256'], token)
    (store.directory / (ref['sha256'] + '.bin')).write_bytes(b'corrupt')
    with pytest.raises(VersionError, match='校验失败'):
        store.read_version(ref['sha256'])
    assert path.read_bytes() == b'new external write'


def test_restore_apply_failure_and_rollback_do_not_overwrite_concurrent_change(upload):
    _, path, ref, store = upload
    path.write_bytes(b'before restore')
    ticket = store.prepare_restore(ref['sha256'], store.describe()['current_file']['fingerprint'])
    ticket.apply()
    ticket.finish(False)
    assert path.read_bytes() == b'before restore'
    ticket = store.prepare_restore(ref['sha256'], store.describe()['current_file']['fingerprint'])
    ticket.apply()
    path.write_bytes(b'external after restore')
    with pytest.raises(VersionError, match='后续修改'):
        ticket.finish(False)
    assert path.read_bytes() == b'external after restore'


def test_version_metadata_tracks_images_and_encoding(upload):
    _, path, ref, store = upload
    path.write_bytes(image_bytes('PNG', size=(31, 29)))
    store.read_current()
    first = store.describe()['versions'][0]
    assert (first['mime_type'], first['width'], first['height']) == ('image/png', 31, 29)
    path.write_bytes(image_bytes('JPEG', size=(41, 33)))
    store.read_current()
    newest = store.describe()['versions'][0]
    assert newest['mime_type'] == 'image/jpeg'
    assert store.read_version(first['id'])[1]['width'] == 31


def test_boundary_and_symlink_refused(upload, tmp_path):
    root, path, ref, store = upload
    with pytest.raises(AttachmentContextError):
        AttachmentVersions(root, {**ref, 'path': '../outside.txt'})
    with pytest.raises(AttachmentContextError):
        AttachmentVersions(root, {**ref, 'id': '0' * 64})
    with pytest.raises(VersionError):
        store.read_version('../index.json')
    target = tmp_path / 'outside.txt'
    target.write_bytes(b'secret')
    try:
        (store.directory / 'trap').symlink_to(target)
    except OSError:
        pytest.skip('symlinks unavailable')
    with pytest.raises(VersionError):
        store._safe(store.directory / 'trap')


def test_failed_index_publish_does_not_prune_old_versions_or_publish_summary(upload):
    _, path, ref, store = upload
    for i in range(10):
        path.write_bytes(f'old {i}'.encode())
        store.read_current()
    before = (store.directory / 'index.json').read_bytes()
    blobs = set(store.directory.glob('*.bin'))
    write = store._write
    def fail_index(target, data, *args):
        if target.name == 'index.json':
            raise OSError('index publication failed')
        return write(target, data, *args)
    path.write_bytes(b'new pending version')
    with patch.object(store, '_write', fail_index):
        data, summary, warning = store.read_current()
    assert data == b'new pending version' and summary is None and warning
    assert (store.directory / 'index.json').read_bytes() == before
    assert all(blob.exists() for blob in blobs)


def test_parallel_readers_publish_one_version_and_keep_index_valid(upload):
    from concurrent.futures import ThreadPoolExecutor
    root, path, ref, store = upload
    path.write_bytes(b'shared changed content')
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: AttachmentVersions(root, ref).read_current(), range(12)))
    assert all(not item[2] for item in results)
    assert len(store.describe()['versions']) == 2
    assert len(list(store.directory.glob('*.bin'))) == 2


def test_restore_changed_between_prepare_and_replace_never_overwrites(upload):
    _, path, ref, store = upload
    path.write_bytes(b'before')
    ticket = store.prepare_restore(ref['sha256'], store.describe()['current_file']['fingerprint'])
    path.write_bytes(b'concurrent writer')
    try:
        with pytest.raises(VersionError, match='并发修改'):
            ticket.apply()
    finally:
        ticket.finish(False)
    assert path.read_bytes() == b'concurrent writer'


def test_writer_immediately_after_rename_is_not_adopted_for_rollback(upload):
    _, path, ref, store = upload
    path.write_bytes(b'before restore')
    ticket = store.prepare_restore(ref['sha256'], store.describe()['current_file']['fingerprint'])
    import os
    replace = os.replace
    def concurrent_replace(source, target):
        replace(source, target)
        if Path(target) == path:
            path.write_bytes(b'external writer just after rename')
    with patch('xgent_app.attachment_versions.os.replace', concurrent_replace):
        with pytest.raises(VersionError, match='再次改变'):
            ticket.apply()
    with pytest.raises(VersionError, match='后续修改'):
        ticket.finish(False)
    assert path.read_bytes() == b'external writer just after rename'
    assert store.read_version(hashlib.sha256(b'before restore').hexdigest())[0] == b'before restore'


def test_corrupted_typed_index_is_not_overwritten(upload):
    _, path, ref, store = upload
    index_path = store.directory / 'index.json'
    index = json.loads(index_path.read_text(encoding='utf-8'))
    index['entries'][0]['encoding'] = None
    index_path.write_text(json.dumps(index), encoding='utf-8')
    before = index_path.read_bytes()
    path.write_bytes(b'current still available')
    data, summary, warning = store.read_current()
    assert data == b'current still available' and summary is None and warning
    assert index_path.read_bytes() == before
    with pytest.raises(VersionError, match='索引损坏'):
        store.read_version(ref['sha256'])
