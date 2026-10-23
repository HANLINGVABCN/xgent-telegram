"""Independent byte snapshots of user uploads, not tool-context archives.

The index records versions actually observed, never an imagined edit history.
All filesystem mutations are serialized across processes for one attachment.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from .conversations import ExecutionFileLock

HEX = re.compile(r'[0-9a-f]{64}')
RECENT_VERSIONS = 10
_upload_receipt = ContextVar('upload_receipt', default=None)


@contextmanager
def upload_receipt_scope(receipt):
    token = _upload_receipt.set(receipt)
    try:
        yield
    finally:
        _upload_receipt.reset(token)


def confirm_upload_received():
    receipt = _upload_receipt.get()
    if receipt is not None and not receipt.done():
        receipt.set_result({'ok': True})


class VersionError(ValueError):
    status = 409


def attachment_identity(reference, upload_root):
    from .attachments import resolve_upload_path, AttachmentContextError
    if reference.get('storage', 'uploads') != 'uploads':
        raise AttachmentContextError('只有用户上传附件支持版本管理')
    path = resolve_upload_path(reference['path'], upload_root)
    relative = path.relative_to(Path(upload_root).resolve()).as_posix()
    identity = hashlib.sha256(relative.encode('utf-8')).hexdigest()
    if reference.get('id') not in (None, identity):
        raise AttachmentContextError('附件身份与路径不一致')
    return identity, path


def inspect_version(data, reference):
    from .attachments import inspect_payload, AttachmentContextError
    try:
        if reference.get('kind') == 'binary' and hashlib.sha256(data).hexdigest() == reference.get('sha256'):
            return {'kind': 'binary', 'mime_type': reference['mime_type']}
        return inspect_payload(data, reference.get('name') or 'attachment')
    except AttachmentContextError as exc:
        if reference.get('kind') == 'binary':
            import filetype
            detected = filetype.guess(data)
            if detected and not detected.mime.startswith('image/'):
                return {'kind': 'binary', 'mime_type': detected.mime}
        # Retain damaged/unsupported bytes too, so restoring another version can
        # first back up exactly what is currently on disk without pretending it parsed.
        return {'kind': 'invalid', 'mime_type': 'application/octet-stream', 'error': str(exc)}


def _signature(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _token(value):
    return _digest(json.dumps(value, sort_keys=True).encode('utf-8'))


class AttachmentVersions:
    def __init__(self, upload_root, reference):
        self.upload_root = Path(upload_root).resolve()
        self.reference = dict(reference)
        self.identity, self.path = attachment_identity(reference, self.upload_root)
        self.root = self.upload_root.parent / 'attachment_versions'
        self.directory = self.root / self.identity

    def _safe(self, path):
        path = Path(path)
        if not path.is_relative_to(self.root) or path.resolve() != path:
            raise VersionError('版本存储路径无效或包含符号链接')
        if path.exists() and path.is_symlink():
            raise VersionError('版本存储不接受符号链接')
        return path

    @contextmanager
    def locked(self):
        self._safe(self.root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._safe(self.directory)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        lock = ExecutionFileLock(str(self.directory / 'versions'))
        self._safe(Path(lock.path))
        deadline = time.monotonic() + 5
        while not lock.acquire():
            if time.monotonic() >= deadline:
                raise VersionError('附件版本正在被其他操作使用，请稍后重试')
            time.sleep(.02)
        try:
            self._safe(self.directory)
            yield
        finally:
            lock.release()

    def _read_index(self):
        path = self._safe(self.directory / 'index.json')
        if not path.exists():
            return {'version': 1, 'attachment_id': self.identity, 'seed': None, 'seed_kind': None,
                    'current': None, 'entries': []}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if (type(data['version']) is not int or data['version'] != 1 or data['attachment_id'] != self.identity
                    or data['seed_kind'] not in {'original', 'baseline'}
                    or not HEX.fullmatch(data['seed']) or not HEX.fullmatch(data['current'])
                    or not isinstance(data['entries'], list) or not 1 <= len(data['entries']) <= RECENT_VERSIONS + 1):
                raise ValueError()
            seen = set()
            for item in data['entries']:
                if (not HEX.fullmatch(item['id']) or item['id'] in seen
                        or type(item['size']) is not int or item['size'] < 0
                        or item['kind'] not in {'text', 'image', 'binary', 'invalid'}
                        or not re.fullmatch(r'[a-zA-Z0-9.+_-]+/[a-zA-Z0-9.+_-]+', item['mime_type'])
                        or any(type(item[key]) not in (int, float) or not math.isfinite(item[key]) or item[key] < 0
                               for key in ('saved_at', 'observed_at'))):
                    raise ValueError()
                if item['kind'] == 'text' and (not isinstance(item.get('encoding'), str) or not item['encoding']):
                    raise ValueError()
                if item['kind'] == 'image' and (not item['mime_type'].startswith('image/')
                        or any(type(item.get(key)) is not int or item[key] <= 0 for key in ('width', 'height'))):
                    raise ValueError()
                seen.add(item['id'])
            if not {data['current'], data['seed']}.issubset(seen):
                raise ValueError()
            return data
        except (KeyError, TypeError, ValueError) as exc:
            raise VersionError('附件版本索引损坏，未覆盖现有存档') from exc

    def _write(self, path, data, mode=0o600):
        self._safe(path)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.version-', suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            self._safe(path)
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _read_snapshot(self, index, version):
        if not isinstance(version, str) or not HEX.fullmatch(version):
            raise VersionError('无效的版本标识')
        entry = next((item for item in index['entries'] if item['id'] == version), None)
        if entry is None:
            raise VersionError('版本已不存在，请刷新版本列表')
        from .attachments import _read_stable_upload, AttachmentUnavailable
        try:
            data = _read_stable_upload(self._safe(self.directory / (version + '.bin')))
        except AttachmentUnavailable as exc:
            raise VersionError('版本副本缺失或不可读取') from exc
        if len(data) != entry['size'] or _digest(data) != version:
            raise VersionError('版本副本校验失败，拒绝预览或恢复')
        return data, entry

    def _observe(self, data, info, *, original=False, prune=True):
        index = self._read_index()
        version = _digest(data)
        existing = next((item for item in index['entries'] if item['id'] == version), None)
        if existing:
            self._read_snapshot(index, version)
        else:
            path = self._safe(self.directory / (version + '.bin'))
            self._write(path, data)
        now = time.time()
        if original and index['seed'] and (index['seed_kind'] != 'original' or index['seed'] != version):
            raise VersionError('附件已有版本历史，不能重新指定上传原件')
        if index['current'] == version:
            return self.summary(index)
        if not index['seed']:
            index.update(seed=version, seed_kind='original' if original else 'baseline')
        entry = {**info, 'id': version, 'size': len(data), 'saved_at': existing['saved_at'] if existing else now, 'observed_at': now}
        index['entries'] = [item for item in index['entries'] if item['id'] != version] + [entry]
        seed = next(item for item in index['entries'] if item['id'] == index['seed'])
        recent = [item for item in index['entries'] if item['id'] != index['seed']][-RECENT_VERSIONS:]
        index['entries'] = [seed, *recent]
        index['current'] = version
        self._write(self.directory / 'index.json', json.dumps(index, ensure_ascii=False, indent=2).encode('utf-8'))
        if prune:
            retained = {item['id'] for item in index['entries']}
            candidates = {path.stem for path in self.directory.glob('*.bin') if HEX.fullmatch(path.stem)}
            self._prune(candidates - retained)
        return self.summary(index)

    def _prune(self, candidates):
        for version in candidates:
            try:
                self._safe(self.directory / (version + '.bin')).unlink(missing_ok=True)
            except (OSError, VersionError):
                # The new index is already safe; excess old bytes are preferable
                # to losing a usable snapshot because cleanup failed.
                import logging
                logging.getLogger(__name__).warning('附件版本清理失败: %s', self.identity)

    @staticmethod
    def summary(index):
        return {'version': 1, 'current': index['current'], 'seed': index['seed'],
                'seed_kind': index['seed_kind'], 'count': len(index['entries'])}

    def original(self, data):
        with self.locked():
            return self._observe(data, inspect_version(data, self.reference), original=True)

    def read_current(self):
        """Read once under the version lock. Archiving failures do not omit bytes."""
        from .attachments import _read_stable_upload
        data = None
        try:
            with self.locked():
                data = _read_stable_upload(self.path)
                _, again = attachment_identity(self.reference, self.upload_root)
                if again != self.path:
                    raise VersionError('附件路径在读取期间改变')
                summary = self._observe(data, inspect_version(data, self.reference))
                return data, summary, None
        except (OSError, VersionError):
            if data is None:
                data = _read_stable_upload(self.path)
            return data, None, '未能留存版本；不能将本次当前文件内容视为已备份。'

    def _current(self):
        from .attachments import _read_stable_upload, AttachmentUnavailable
        # Restore never follows symlinks even if their destinations are in-root.
        raw_path = Path(self.reference['path'])
        if not raw_path.is_absolute():
            raw_path = self.upload_root / raw_path
        if raw_path != self.path or raw_path.resolve() != raw_path:
            raise VersionError('附件路径包含符号链接或已变化，不能恢复')
        try:
            before = self.path.stat()
            data = _read_stable_upload(self.path)
            after = self.path.stat()
            if _signature(before) != _signature(after):
                raise VersionError('文件正在写入，请稍后重新打开')
            info = inspect_version(data, self.reference)
            token = _token([_digest(data), _signature(after)])
            return data, {'state': 'available' if info['kind'] != 'invalid' else 'damaged',
                          'fingerprint': token, 'id': _digest(data), 'size': len(data), **info}, after
        except FileNotFoundError:
            return None, {'state': 'missing', 'fingerprint': 'missing', 'error': '当前文件缺失'}, None
        except (OSError, AttachmentUnavailable) as exc:
            raise VersionError('当前文件不可读或不稳定，未覆盖原路径') from exc

    def describe(self):
        with self.locked():
            index = self._read_index()
            try:
                data, current, _ = self._current()
                if data is not None:
                    try:
                        self._observe(data, inspect_version(data, self.reference))
                        index = self._read_index()
                    except (VersionError, OSError):
                        current['warning'] = '未能留存版本'
            except VersionError as exc:
                current = {'state': 'unreadable', 'error': str(exc)}
            return {**self.summary(index), 'current_file': current,
                    'versions': [dict(item) for item in reversed(index['entries'])]}

    def read_version(self, version):
        with self.locked():
            return self._read_snapshot(self._read_index(), version)

    def prepare_restore(self, version, fingerprint):
        guard = self.locked()
        guard.__enter__()
        try:
            index = self._read_index()
            data, entry = self._read_snapshot(index, version)
            before, current, file_stat = self._current()
            if not isinstance(fingerprint, str) or current['fingerprint'] != fingerprint:
                raise VersionError('当前文件已改变，请刷新版本列表后再确认恢复')
            # Back up readable bytes even if invalid; no changes before this succeeds.
            if before is not None:
                self._observe(before, inspect_version(before, self.reference), prune=False)
            return RestoreTicket(self, guard, index, data, entry, before, current, file_stat)
        except BaseException:
            guard.__exit__(None, None, None)
            raise


class RestoreTicket:
    def __init__(self, store, guard, index, data, entry, before, current, file_stat):
        self.store, self.guard, self.index = store, guard, index
        self.data, self.entry, self.before, self.current, self.file_stat = data, entry, before, current, file_stat
        self.applied = False
        self.applied_token = None

    def _replace_upload(self, data):
        path = self.store.path
        self.store._current()  # Check in-root, no symlink before staging replacement.
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.resolve() != path.parent:
            raise VersionError('附件目录已改变，未恢复')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.restore-', suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IMODE(self.file_stat.st_mode) if self.file_stat else 0o600)
            staged = temporary.stat()
            _, current, _ = self.store._current()
            expected = self.applied_token if self.applied else self.current['fingerprint']
            if current['fingerprint'] != expected:
                raise VersionError('文件在恢复期间改变，未覆盖并发修改')
            os.replace(temporary, path)
            temporary = None
            # The renamed file keeps the staged inode/size/mtime. Do not adopt
            # an external writer's post-rename bytes as our rollback token.
            return _token([_digest(data), _signature(staged)])
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def apply(self):
        self.applied_token = self._replace_upload(self.data)
        self.applied = True
        if self.store._current()[1]['fingerprint'] != self.applied_token:
            raise VersionError('文件在原子替换后再次改变，未覆盖后续修改，请刷新核对')
        return self.store._observe(self.data, inspect_version(self.data, self.store.reference), prune=False)

    def finish(self, success):
        try:
            if not success and self.applied:
                _, current, _ = self.store._current()
                if current['fingerprint'] != self.applied_token:
                    raise VersionError('恢复记录提交失败，且文件再次变化；已保留版本，请刷新核对，未覆盖后续修改')
                if self.before is None:
                    self.store.path.unlink()
                else:
                    self._replace_upload(self.before)
                # Keep the pre-restore observation when rollback succeeded.
                if self.before is not None:
                    self.store._observe(self.before, inspect_version(self.before, self.store.reference), prune=False)
            index = self.store._read_index()
            retained = {item['id'] for item in index['entries']}
            candidates = {path.stem for path in self.store.directory.glob('*.bin') if HEX.fullmatch(path.stem)}
            self.store._prune(candidates - retained)
        finally:
            self.guard.__exit__(None, None, None)


def save_original_version(reference, data, upload_root):
    from .attachments import AttachmentContextError
    try:
        reference['versions'] = AttachmentVersions(upload_root, reference).original(data)
    except (OSError, VersionError) as exc:
        raise AttachmentContextError('上传原件版本备份失败，尚未确认接收，请重试。') from exc
    return reference
