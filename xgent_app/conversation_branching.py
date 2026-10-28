"""Conversation turns, content-addressed workspaces, branching and safe withdrawal.

Snapshots contain user-editable project state, never the database, secrets, logs or
runtime storage.  The database remains the authority for history and lineage; the
object directory only stores checksum-addressed bytes referenced by snapshot rows.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import html
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from .conversations import ConversationError

EFFECTS = frozenset({'read_only', 'local_reversible', 'external', 'unknown'})
_active_turn = contextvars.ContextVar('xgent_active_turn', default=None)
_STATE_TREES = ('workspace', 'memory', 'skill-private')
_EXCLUDED_PARTS = frozenset({
    '.git', 'xgent_storage', 'venv', '.venv', 'env', '__pycache__',
    '.pytest_cache', '.ruff_cache', '.mypy_cache', 'node_modules', 'build',
    'dist', 'logs', '.install-state',
})
_EXCLUDED_NAMES = frozenset({
    '.env', 'xgent_memory.db', 'bot_memory.db', 'backup-schedule.json',
    'data-location.json', 'path-map.json', '.xgent-version',
})
_NODEQUALITY_RUNTIME = re.compile(r'^\.nodequality\d')
TURN_SNAPSHOT_TIMEOUT = 10.0
_STAT_CACHE: dict[str, dict[str, dict[str, Any]]] = {}
_STAT_INDEX_FILE = '.stat-index.tmp'


class BranchingError(ConversationError):
    status = 409


def active_turn_id() -> str | None:
    return _active_turn.get()


@contextlib.contextmanager
def bind_turn(turn_id: str | None):
    token = _active_turn.set(turn_id)
    try:
        yield turn_id
    finally:
        _active_turn.reset(token)


def operation_effect(kind: str) -> str:
    if kind in {'read', 'grep', 'search', 'fetch', 'intel', 'ask'}:
        return 'read_only'
    if kind in {'edit', 'file', 'file_base64'}:
        return 'local_reversible'
    if kind in {'sendfile', 'media'}:
        return 'external'
    return 'unknown'


def conversation_root(scope, code_root: str | Path) -> Path:
    return Path(scope.workspace_path or code_root).resolve()


def _safe_relative(value: str) -> str:
    path = PurePosixPath(str(value).replace('\\', '/'))
    if path.is_absolute() or not path.parts or any(part in {'', '.', '..'} for part in path.parts):
        raise BranchingError('文件快照包含不安全路径')
    return path.as_posix()


def _excluded(relative: str) -> bool:
    path = PurePosixPath(relative)
    if any(part in _EXCLUDED_PARTS for part in path.parts):
        return True
    # NodeQuality extracts an entire guest filesystem below a timestamped
    # runtime directory and mounts proc inside it.  It is command output, not
    # editable project state; snapshotting it can take minutes or hit EINVAL.
    if any(_NODEQUALITY_RUNTIME.match(part) for part in path.parts):
        return True
    name = path.name
    return (name in _EXCLUDED_NAMES or name.endswith(('.db-wal', '.db-shm', '.pid', '.lock', '.tmp'))
            or re.search(r'\.log(?:$|\.)', name, re.IGNORECASE) is not None)


def _mountinfo_path(value: str) -> str:
    """Decode the octal escapes used by Linux ``/proc/*/mountinfo``."""
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match.group(1), 8)), value)


def _nested_mounts(root: Path) -> set[str]:
    """Return lexical paths of mounts nested below ``root``.

    Project tools sometimes mount ``proc`` or another filesystem inside a
    temporary working directory.  Those trees are runtime inputs, not project
    state, and reading their pseudo-files can fail with EINVAL or block.
    """
    if os.name != 'posix':
        return set()
    mounts: set[str] = set()
    try:
        lines = Path('/proc/self/mountinfo').read_text(
            encoding='utf-8', errors='surrogateescape').splitlines()
    except OSError:
        return mounts
    root = Path(os.path.abspath(root))
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        mounted = Path(os.path.abspath(_mountinfo_path(fields[4])))
        try:
            relative = mounted.relative_to(root)
        except ValueError:
            continue
        if relative.parts:
            mounts.add(relative.as_posix())
    return mounts


def _inside_mount(relative: str, mounts: set[str]) -> bool:
    path = PurePosixPath(relative)
    return any(path == PurePosixPath(mount) or PurePosixPath(mount) in path.parents
               for mount in mounts)


def _git_exclude(relative: str) -> str:
    escaped = re.sub(r'([\\*?\[\]])', r'\\\1', relative)
    return f'/{escaped}/'


def _check_deadline(deadline: float | None, should_cancel: Any = None) -> None:
    if callable(should_cancel) and should_cancel():
        raise BranchingError('文件快照已因任务取消或停止而中断')
    if deadline is not None and time.monotonic() >= deadline:
        raise BranchingError(
            f'文件快照超过 {TURN_SNAPSHOT_TIMEOUT:g} 秒安全上限，已跳过精确文件快照')


def _command_timeout(deadline: float | None, maximum: float = 30.0,
                     should_cancel: Any = None) -> float:
    _check_deadline(deadline, should_cancel)
    if deadline is None:
        return maximum
    return max(0.1, min(maximum, deadline - time.monotonic()))


def _git_paths(root: Path, mounts: set[str], deadline: float | None = None,
               should_cancel: Any = None) -> set[str] | None:
    try:
        tracked = subprocess.run(
            ['git', '-C', str(root), 'ls-files', '-c', '-z'], check=True,
            capture_output=True, timeout=_command_timeout(deadline, should_cancel=should_cancel),
        )
        untracked = subprocess.run(
            ['git', '-C', str(root), 'ls-files', '-o', '--exclude-standard',
             '--exclude=/.nodequality[0-9]*/',
             *(f'--exclude={_git_exclude(mount)}' for mount in sorted(mounts)), '-z'],
            check=True, capture_output=True, timeout=_command_timeout(deadline, should_cancel=should_cancel),
        )
    except subprocess.TimeoutExpired as exc:
        raise BranchingError(
            f'文件快照超过 {TURN_SNAPSHOT_TIMEOUT:g} 秒安全上限，已跳过精确文件快照') from exc
    except (OSError, subprocess.CalledProcessError):
        return None
    _check_deadline(deadline, should_cancel)
    return {
        _safe_relative(raw.decode('utf-8', errors='surrogateescape'))
        for raw in (tracked.stdout + untracked.stdout).split(b'\0') if raw
    }


def _walk_tree(root: Path, relative_root: str = '', mounts: set[str] | None = None,
               deadline: float | None = None, should_cancel: Any = None) -> set[str]:
    mounts = mounts or set()
    if relative_root and _inside_mount(relative_root, mounts):
        return set()
    base = root / relative_root if relative_root else root
    if not base.exists():
        return set()
    found: set[str] = set()
    for parent, directories, filenames in os.walk(base, followlinks=False):
        _check_deadline(deadline, should_cancel)
        parent_path = Path(parent)
        kept = []
        for name in directories:
            path = parent_path / name
            relative = path.relative_to(root).as_posix()
            if (path.is_symlink() or _excluded(relative)
                    or _inside_mount(relative, mounts) or os.path.ismount(path)):
                continue
            kept.append(name)
        directories[:] = kept
        for name in filenames:
            _check_deadline(deadline, should_cancel)
            path = parent_path / name
            relative = path.relative_to(root).as_posix()
            if not path.is_symlink() and not _excluded(relative):
                found.add(relative)
    return found


def managed_paths(root: str | Path, deadline: float | None = None,
                  should_cancel: Any = None) -> list[str]:
    root = Path(root).resolve()
    mounts = _nested_mounts(root)
    _check_deadline(deadline, should_cancel)
    paths = _git_paths(root, mounts, deadline, should_cancel)
    if paths is None:
        paths = _walk_tree(root, mounts=mounts, deadline=deadline, should_cancel=should_cancel)
    for tree in _STATE_TREES:
        paths.update(_walk_tree(root, tree, mounts, deadline, should_cancel))

    def regular(relative: str) -> bool:
        _check_deadline(deadline, should_cancel)
        try:
            return stat.S_ISREG((root / relative).lstat().st_mode)
        except OSError:
            return False

    return sorted(path for path in paths
                  if not _excluded(path) and not _inside_mount(path, mounts) and regular(path))


@contextlib.contextmanager
def _open_regular(path: Path):
    """Open one stable regular file without following a final symlink."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise BranchingError(f'无法读取快照文件：{path}') from exc
    if not stat.S_ISREG(before.st_mode):
        raise BranchingError(f'快照只接受普通文件：{path}')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BranchingError(f'无法读取快照文件：{path}') from exc
    handle = os.fdopen(descriptor, 'rb')
    try:
        opened = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino, before.st_mode) != (
                opened.st_dev, opened.st_ino, opened.st_mode):
            raise BranchingError(f'文件在生成快照前被替换：{path}')
        yield handle, opened
    finally:
        handle.close()


def _digest_file(path: Path, deadline: float | None = None,
                 should_cancel: Any = None) -> tuple[int, str, int]:
    digest = hashlib.sha256()
    size = 0
    with _open_regular(path) as (handle, before):
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            _check_deadline(deadline, should_cancel)
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise BranchingError(f'文件在生成快照时变化：{path}')
    return size, digest.hexdigest(), stat.S_IMODE(after.st_mode)


def _stat_signature(st: os.stat_result) -> list[int]:
    return [
        int(st.st_dev),
        int(st.st_ino),
        int(st.st_size),
        int(getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9))),
        int(getattr(st, 'st_ctime_ns', int(st.st_ctime * 1e9))),
        stat.S_IMODE(st.st_mode),
    ]


def _load_stat_index(objects: Path, root_key: str) -> dict[str, dict[str, Any]]:
    cache_key = f'{objects}::{root_key}'
    if cache_key in _STAT_CACHE:
        return _STAT_CACHE[cache_key]
    index_path = objects / _STAT_INDEX_FILE
    data: dict[str, dict[str, Any]] = {}
    if index_path.is_file():
        with contextlib.suppress(Exception):
            raw = json.loads(index_path.read_text(encoding='utf-8'))
            if isinstance(raw, dict) and isinstance(raw.get(root_key), dict):
                data = raw[root_key]
    _STAT_CACHE[cache_key] = data
    return data


def _save_stat_index(objects: Path, root_key: str, entries: dict[str, dict[str, Any]]) -> None:
    cache_key = f'{objects}::{root_key}'
    _STAT_CACHE[cache_key] = entries
    if not objects.is_dir():
        return
    index_path = objects / _STAT_INDEX_FILE
    all_roots: dict[str, Any] = {}
    if index_path.is_file():
        with contextlib.suppress(Exception):
            loaded = json.loads(index_path.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                all_roots = loaded
    all_roots[root_key] = entries
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=objects,
            prefix='.stat-index-', suffix='.tmp', delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(all_roots, handle, ensure_ascii=False)
            handle.flush()
        os.replace(temporary, index_path)
        temporary = None
    except OSError:
        pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _store_object(source: Path, sha256: str, objects: Path,
                  deadline: float | None = None, should_cancel: Any = None) -> Path:
    target = objects / sha256[:2] / sha256[2:]
    if target.is_file():
        _size, digest, _ = _digest_file(target, deadline, should_cancel)
        if digest != sha256:
            raise BranchingError('内容对象校验失败，拒绝覆盖')
        return target
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.object-', delete=False) as handle:
            temporary = Path(handle.name)
            with _open_regular(source) as (incoming, _opened):
                for chunk in iter(lambda: incoming.read(1024 * 1024), b''):
                    _check_deadline(deadline, should_cancel)
                    handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        _size, digest, _mode = _digest_file(temporary, deadline, should_cancel)
        if digest != sha256:
            raise BranchingError(f'文件在对象存储期间变化：{source}')
        os.chmod(temporary, 0o400)
        try:
            os.replace(temporary, target)
        except FileExistsError:
            pass
        temporary = None
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def capture_manifest(root: str | Path, objects: str | Path, *,
                     timeout: float | None = None, should_cancel: Any = None) -> dict[str, Any]:
    root, objects = Path(root).resolve(), Path(objects).resolve()
    if not root.is_dir():
        raise BranchingError(f'工作区不存在：{root}')
    deadline = time.monotonic() + timeout if timeout is not None else None
    root_key = str(root)
    previous_index = _load_stat_index(objects, root_key)
    next_index: dict[str, dict[str, Any]] = {}
    index_dirty = False
    entries, total = [], 0
    digest = hashlib.sha256()
    paths = (managed_paths(root, deadline) if should_cancel is None
             else managed_paths(root, deadline, should_cancel))
    for relative in paths:
        _check_deadline(deadline, should_cancel)
        path = root / relative
        cached = previous_index.get(relative)
        reused = False
        if isinstance(cached, dict):
            try:
                src_st = path.lstat()
                if stat.S_ISREG(src_st.st_mode) and cached.get('src_sig') == _stat_signature(src_st):
                    sha256 = str(cached['sha256'])
                    target = objects / sha256[:2] / sha256[2:]
                    obj_st = target.lstat()
                    if stat.S_ISREG(obj_st.st_mode) and cached.get('obj_sig') == _stat_signature(obj_st):
                        size = int(src_st.st_size)
                        mode = stat.S_IMODE(src_st.st_mode)
                        object_path = target
                        next_index[relative] = cached
                        reused = True
            except (OSError, KeyError, TypeError, ValueError):
                reused = False
        if not reused:
            size, sha256, mode = _digest_file(path, deadline, should_cancel)
            object_path = _store_object(path, sha256, objects, deadline, should_cancel)
            try:
                next_index[relative] = {
                    'src_sig': _stat_signature(path.lstat()),
                    'obj_sig': _stat_signature(Path(object_path).lstat()),
                    'sha256': sha256,
                }
                index_dirty = True
            except OSError:
                pass
        entry = {'path': relative, 'sha256': sha256, 'size': size, 'mode': mode,
                 'object_path': str(object_path)}
        entries.append(entry)
        total += size
        digest.update(relative.encode('utf-8', errors='surrogateescape'))
        digest.update(b'\0' + sha256.encode() + b'\0' + str(mode).encode() + b'\n')
    if index_dirty or set(next_index) != set(previous_index):
        _save_stat_index(objects, root_key, next_index)
    return {'root_digest': digest.hexdigest(), 'entries': entries,
            'file_count': len(entries), 'total_size': total}


async def save_snapshot(db, conversation_id: str, turn_id: str | None, phase: str,
                        manifest: dict[str, Any]) -> str:
    conn = await db._get_conn()
    cursor = await conn.execute(
        '''SELECT s.id FROM conversation_snapshots s
           WHERE s.conversation_id=? AND s.root_digest=? AND s.file_count=? AND s.total_size=?
           AND (SELECT COUNT(*) FROM snapshot_entries e WHERE e.snapshot_id=s.id)=?
           ORDER BY s.created_at DESC LIMIT 1''',
        (conversation_id, manifest['root_digest'], manifest['file_count'],
         manifest['total_size'], manifest['file_count']),
    )
    existing = await cursor.fetchone()
    await cursor.close()
    if existing is not None:
        return existing['id']
    snapshot_id = uuid.uuid4().hex
    now = time.time()
    async with db._transaction() as conn:
        await conn.execute('INSERT INTO conversation_snapshots VALUES(?,?,?,?,?,?,?,?)', (
            snapshot_id, conversation_id, turn_id, phase, manifest['root_digest'],
            manifest['file_count'], manifest['total_size'], now,
        ))
        for item in manifest['entries']:
            await conn.execute('INSERT OR IGNORE INTO artifact_objects VALUES(?,?,?,?,?)', (
                item['sha256'], item['size'], item['object_path'], 'workspace', now,
            ))
            await conn.execute('INSERT INTO snapshot_entries VALUES(?,?,?,?,?)', (
                snapshot_id, item['path'], item['sha256'], item['mode'], item['size'],
            ))
    return snapshot_id


async def snapshot_manifest(db, snapshot_id: str) -> dict[str, Any]:
    conn = await db._get_conn()
    cursor = await conn.execute('SELECT * FROM conversation_snapshots WHERE id=?', (snapshot_id,))
    snapshot = await cursor.fetchone()
    await cursor.close()
    if snapshot is None:
        raise BranchingError('文件快照不存在')
    cursor = await conn.execute('''SELECT e.path,e.object_sha256 AS sha256,e.mode,e.size,o.path AS object_path
        FROM snapshot_entries e JOIN artifact_objects o ON o.sha256=e.object_sha256
        WHERE e.snapshot_id=? ORDER BY e.path''', (snapshot_id,))
    entries = [dict(row) for row in await cursor.fetchall()]
    await cursor.close()
    return {**dict(snapshot), 'entries': entries}


def manifest_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item['path']: item for item in manifest.get('entries', [])}


def compare_manifests(base: dict[str, Any], current: dict[str, Any], changed: dict[str, Any]) -> dict[str, Any]:
    base_map, current_map, changed_map = map(manifest_map, (base, current, changed))
    paths = sorted(set(base_map) | set(current_map) | set(changed_map))
    apply, conflicts, unchanged = [], [], []
    for path in paths:
        before = base_map.get(path, {}).get('sha256')
        source = current_map.get(path, {}).get('sha256')
        branch = changed_map.get(path, {}).get('sha256')
        if branch == before or branch == source:
            unchanged.append(path)
        elif source == before:
            apply.append({'path': path, 'action': 'delete' if branch is None else ('create' if before is None else 'update'),
                          'entry': changed_map.get(path)})
        else:
            conflicts.append({'path': path, 'base': before, 'source': source, 'branch': branch})
    return {'apply': apply, 'conflicts': conflicts, 'unchanged': unchanged}


def changes_to(current: dict[str, Any], target: dict[str, Any]) -> list[dict[str, Any]]:
    current_map, target_map = manifest_map(current), manifest_map(target)
    changes = []
    for path in sorted(set(current_map) | set(target_map)):
        if current_map.get(path, {}).get('sha256') == target_map.get(path, {}).get('sha256'):
            continue
        changes.append({'path': path, 'action': 'delete' if path not in target_map else
                        ('create' if path not in current_map else 'update'), 'entry': target_map.get(path)})
    return changes


def _copy_object(entry: dict[str, Any], target: Path) -> None:
    source = Path(entry['object_path']).resolve()
    size, digest, _ = _digest_file(source)
    if size != entry['size'] or digest != entry['sha256']:
        raise BranchingError('文件对象缺失或损坏：' + entry['path'])
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.branch-', delete=False) as handle:
            temporary = Path(handle.name)
            with source.open('rb') as incoming:
                shutil.copyfileobj(incoming, handle, 1024 * 1024)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, entry['mode'] or 0o600)
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply_changes(root: str | Path, changes: list[dict[str, Any]]) -> None:
    """Apply a pre-validated set with rollback; no partial file result escapes."""
    root = Path(root).resolve()
    staging = Path(tempfile.mkdtemp(prefix='conversation-apply-', dir=root.parent))
    backups = staging / 'backups'
    written: list[tuple[Path, Path | None]] = []
    try:
        for change in changes:
            relative = _safe_relative(change['path'])
            target = (root / relative).resolve()
            if target == root or not target.is_relative_to(root):
                raise BranchingError('目标文件越过工作区边界')
            backup = None
            if target.exists():
                if not target.is_file() or target.is_symlink():
                    raise BranchingError('目标不是可恢复的普通文件：' + relative)
                backup = backups / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
            written.append((target, backup))
            if change['action'] == 'delete':
                target.unlink(missing_ok=True)
            else:
                _copy_object(change['entry'], target)
        # Empty directories are harmless and preserve .git worktree metadata.
    except BaseException:
        for target, backup in reversed(written):
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, target)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def materialize_snapshot(manifest: dict[str, Any], target: str | Path) -> None:
    target = Path(target).resolve()
    desired = manifest_map(manifest)
    current = set(managed_paths(target)) if target.exists() else set()
    changes = [{'path': path, 'action': 'delete'} for path in sorted(current - set(desired))]
    changes.extend({'path': path, 'action': 'create', 'entry': entry}
                   for path, entry in desired.items())
    apply_changes(target, changes)


def create_git_worktree(code_root: str | Path, target: str | Path) -> None:
    code_root, target = Path(code_root).resolve(), Path(target).resolve()
    if target.exists() and any(target.iterdir()):
        raise BranchingError('分支工作区已存在且不为空')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        subprocess.run(['git', '-C', str(code_root), 'worktree', 'add', '--detach', str(target), 'HEAD'],
                       check=True, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        # Non-git installations still receive an isolated materialized directory.
        if (code_root / '.git').exists():
            detail = getattr(exc, 'stderr', '') or str(exc)
            raise BranchingError('创建 Git 分支工作区失败：' + detail.strip()) from exc
        target.mkdir(parents=True, exist_ok=True, mode=0o700)


def _repair_one_workspace(code_root: Path, expected: Path) -> None:
    if not expected.exists() or (expected / '.git').exists():
        return
    if not (code_root / '.git').exists():
        return
    saved = expected.with_name('.restore-' + expected.name + '-' + uuid.uuid4().hex)
    os.replace(expected, saved)
    try:
        create_git_worktree(code_root, expected)
        for parent, directories, filenames in os.walk(saved):
            relative_parent = Path(parent).relative_to(saved)
            directories[:] = [name for name in directories if name != '.git']
            for filename in filenames:
                if filename == '.git':
                    continue
                source = Path(parent) / filename
                target = expected / relative_parent / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    except BaseException:
        with contextlib.suppress(Exception):
            subprocess.run(['git', '-C', str(code_root), 'worktree', 'remove', '--force', str(expected)],
                           check=False, capture_output=True, timeout=60)
        shutil.rmtree(expected, ignore_errors=True)
        os.replace(saved, expected)
        raise
    else:
        shutil.rmtree(saved, ignore_errors=True)


async def repair_restored_workspaces(db, code_root: str | Path) -> None:
    """Recreate portable Git metadata after a data-only backup restore."""
    code_root = Path(code_root).resolve()
    workspaces = code_root / 'xgent_storage' / 'conversation_workspaces'
    objects = code_root / 'xgent_storage' / 'conversation_objects'
    conn = await db._get_conn()
    cursor = await conn.execute('SELECT sha256,path FROM artifact_objects')
    artifact_rows = [dict(row) for row in await cursor.fetchall()]
    await cursor.close()
    async with db._write() as write:
        for item in artifact_rows:
            expected_object = (objects / item['sha256'][:2] / item['sha256'][2:]).resolve()
            if item['path'] != str(expected_object) and expected_object.is_file():
                await write.execute('UPDATE artifact_objects SET path=? WHERE sha256=?',
                                    (str(expected_object), item['sha256']))
    cursor = await conn.execute("SELECT id,workspace_path FROM chat_sessions WHERE workspace_path IS NOT NULL AND workspace_path<>''")
    sessions = [dict(row) for row in await cursor.fetchall()]
    await cursor.close()
    for session in sessions:
        if not re.fullmatch(r'[0-9a-f]{32}', session['id']):
            raise BranchingError('分支对话标识无效，未修复工作区')
        expected = (workspaces / session['id']).resolve()
        if expected.parent != workspaces.resolve():
            raise BranchingError('分支工作区越过数据目录')
        await asyncio.to_thread(_repair_one_workspace, code_root, expected)
        if session['workspace_path'] != str(expected):
            async with db._write() as write:
                await write.execute('UPDATE chat_sessions SET workspace_path=? WHERE id=?',
                                    (str(expected), session['id']))


async def begin_turn(db, scope, code_root: str | Path | None = None,
                     objects: str | Path | None = None) -> str:
    """Record a turn boundary only.

    Turns no longer capture file snapshots: withdrawal and branching operate on
    conversation history, never on disk files, so a chat turn performs zero
    filesystem scanning before or after the model runs.
    """
    turn_id = uuid.uuid4().hex
    async with db._transaction() as conn:
        await conn.execute('''INSERT INTO conversation_turns
            (id,conversation_id,run_id,status,started_at,before_snapshot_id,snapshot_complete,error)
            VALUES(?,?,?,'running',?,NULL,0,NULL)''',
            (turn_id, scope.conversation_id, scope.run_id, time.time()))
    return turn_id


async def finish_turn(db, scope, turn_id: str, code_root: str | Path | None = None,
                      objects: str | Path | None = None,
                      status: str = 'completed', error: str | None = None) -> None:
    conn = await db._get_conn()
    cursor = await conn.execute(
        '''SELECT
            EXISTS(SELECT 1 FROM global_messages WHERE turn_id=?) AS has_messages,
            EXISTS(SELECT 1 FROM turn_operations WHERE turn_id=?) AS has_operations''',
        (turn_id, turn_id),
    )
    activity = await cursor.fetchone()
    await cursor.close()
    if not activity or not (activity['has_messages'] or activity['has_operations']):
        # A queued message may be cancelled before anything is handed to the
        # model; record the empty turn and release the slot immediately.
        async with db._transaction() as write:
            await write.execute('''UPDATE conversation_turns SET status='empty',finished_at=?,
                snapshot_complete=0,error=COALESCE(error,?) WHERE id=?''',
                (time.time(), '待发消息在接入前已取消或失效', turn_id))
        return
    async with db._transaction() as write:
        cursor = await write.execute(
            "SELECT MIN(id) first_id,MAX(CASE WHEN msg_type='ai_reply' THEN id END) final_id "
            "FROM global_messages WHERE turn_id=?", (turn_id,))
        row = await cursor.fetchone()
        await cursor.close()
        await write.execute('''UPDATE conversation_turns SET status=?,finished_at=?,
            user_record_id=?,final_record_id=?,snapshot_complete=0,
            error=COALESCE(?,error) WHERE id=?''', (
            status, time.time(), row['first_id'], row['final_id'], error, turn_id,
        ))


async def record_operation(db, kind: str, effect: str | None = None, details: dict | None = None) -> None:
    turn_id = active_turn_id()
    if not turn_id:
        return
    effect = effect or operation_effect(kind)
    if effect not in EFFECTS:
        effect = 'unknown'
    async with db._transaction() as conn:
        cursor = await conn.execute('SELECT COALESCE(MAX(sequence),0)+1 n FROM turn_operations WHERE turn_id=?', (turn_id,))
        sequence = int((await cursor.fetchone())['n'])
        await conn.execute('INSERT INTO turn_operations(turn_id,sequence,kind,effect,details,created_at) VALUES(?,?,?,?,?,?)', (
            turn_id, sequence, kind, effect,
            json.dumps(details or {}, ensure_ascii=False, sort_keys=True), time.time(),
        ))


def _metadata(value: Any) -> dict[str, Any]:
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return {}
    return dict(data) if isinstance(data, dict) else {}


async def freeze_attachment_metadata(db, metadata: dict[str, Any], uploads: Path,
                                     objects: Path) -> dict[str, Any]:
    result = json.loads(json.dumps(metadata, ensure_ascii=False))
    now = time.time()
    path_map = {}
    for key in ('attachments', 'server_uploads'):
        refs = result.get(key)
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict) or ref.get('storage', 'uploads') != 'uploads':
                continue
            from .attachments import resolve_upload_path
            old_reference_path = str(ref['path'])
            path = resolve_upload_path(old_reference_path, uploads)
            source = path
            expected = ref.get('sha256')
            if isinstance(expected, str) and re.fullmatch(r'[0-9a-f]{64}', expected):
                try:
                    from .attachment_versions import AttachmentVersions
                    archived = AttachmentVersions(uploads, ref).directory / (expected + '.bin')
                    if archived.is_file() and (await asyncio.to_thread(_digest_file, archived))[1] == expected:
                        source = archived
                except (OSError, ValueError):
                    pass
            size, digest, _mode = await asyncio.to_thread(_digest_file, source)
            if source == path and isinstance(expected, str) and expected != digest:
                ref['historical_version_unavailable'] = True
            object_path = await asyncio.to_thread(_store_object, source, digest, objects)
            immutable = uploads / 'objects' / digest[:2] / digest[2:]
            if not immutable.exists():
                immutable.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                await asyncio.to_thread(_copy_object, {
                    'path': ref.get('name') or path.name, 'object_path': str(object_path),
                    'size': size, 'sha256': digest, 'mode': 0o400,
                }, immutable)
            ref.update(path=immutable.relative_to(uploads).as_posix(), size=size, sha256=digest,
                       immutable_object=True)
            path_map[str(path)] = str(immutable)
            path_map[old_reference_path] = str(immutable)
            ref.pop('versions', None)
            identity = hashlib.sha256(ref['path'].encode('utf-8')).hexdigest()
            ref['id'] = identity
            async with db._transaction() as conn:
                await conn.execute('INSERT OR IGNORE INTO artifact_objects VALUES(?,?,?,?,?)',
                                   (digest, size, str(object_path), 'attachment', now))
    display_refs = result.get('display_media')
    if isinstance(display_refs, list):
        for ref in display_refs:
            if not isinstance(ref, dict) or not isinstance(ref.get('path'), str):
                continue
            path = Path(ref['path'])
            if not path.is_absolute() or not path.is_file() or path.is_symlink():
                continue
            size, digest, _mode = await asyncio.to_thread(_digest_file, path)
            object_path = await asyncio.to_thread(_store_object, path, digest, objects)
            suffix = path.suffix.lower() if re.fullmatch(r'\.[a-zA-Z0-9]{1,12}', path.suffix) else '.bin'
            view_path = objects / 'views' / (digest + suffix)
            if not view_path.exists():
                view_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    os.link(object_path, view_path)
                except OSError:
                    shutil.copy2(object_path, view_path)
                os.chmod(view_path, 0o400)
            path_map[str(path)] = str(view_path)
            ref.update(path=str(view_path), size=size, sha256=digest, immutable_object=True)
            async with db._transaction() as conn:
                await conn.execute('INSERT OR IGNORE INTO artifact_objects VALUES(?,?,?,?,?)',
                                   (digest, size, str(object_path), 'output', now))
    if path_map:
        result['_branch_path_map'] = path_map
    return result


def _safe_attachment_name(value: str) -> str:
    name = re.sub(r'[^\w.()\-\u4e00-\u9fff]+', '_', Path(value).name, flags=re.UNICODE).strip('._')
    return (name or 'attachment.bin')[:120]


async def localize_branch_metadata(metadata: dict[str, Any], branch_id: str,
                                   uploads: Path, objects: Path) -> dict[str, Any]:
    """Create per-branch writable attachment files from shared immutable bytes."""
    result = json.loads(json.dumps(metadata, ensure_ascii=False))
    path_map = dict(result.get('_branch_path_map') or {})
    branch_root = (uploads / 'branches' / branch_id).resolve()
    if branch_root.parent != (uploads / 'branches').resolve():
        raise BranchingError('分支附件目录无效')
    for key in ('attachments', 'server_uploads'):
        refs = result.get(key)
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            storage = ref.get('storage', 'uploads')
            root = uploads if storage == 'uploads' else uploads.parent / 'generated_media' if storage == 'generated_media' else None
            if root is None or not isinstance(ref.get('path'), str):
                continue
            from .attachments import resolve_upload_path
            source = resolve_upload_path(ref['path'], root)
            size, digest, _mode = await asyncio.to_thread(_digest_file, source)
            object_path = await asyncio.to_thread(_store_object, source, digest, objects)
            filename = f"{digest}_{_safe_attachment_name(str(ref.get('name') or source.name))}"
            target = branch_root / filename
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                await asyncio.to_thread(_copy_object, {
                    'path': filename, 'object_path': str(object_path), 'size': size,
                    'sha256': digest, 'mode': 0o600,
                }, target)
            old_absolute = str(source)
            old_relative = str(ref['path'])
            relative = target.relative_to(uploads).as_posix()
            ref.update(path=relative, size=size, sha256=digest, id=hashlib.sha256(relative.encode()).hexdigest(),
                       storage='uploads', immutable_object=False, branch_working_copy=True)
            ref.pop('versions', None)
            for old, mapped in list(path_map.items()):
                if mapped in {old_absolute, str(object_path), old_relative}:
                    path_map[old] = str(target)
            path_map[old_absolute] = str(target)
            path_map[old_relative] = str(target)
    if path_map:
        result['_branch_path_map'] = path_map
    return result


async def freeze_output_paths(db, content: str, storage: Path, objects: Path) -> str:
    command_root = (storage / 'command_outputs').resolve()
    values = set(re.findall(r'data-output-path=["\']([^"\']+)["\']', str(content or '')))
    result = str(content or '')
    for encoded in values:
        raw = html.unescape(encoded)
        path = Path(raw)
        try:
            path = path.resolve(strict=True)
        except (OSError, ValueError):
            continue
        if not path.is_file() or path.is_symlink() or not path.is_relative_to(command_root):
            continue
        size, digest, _mode = await asyncio.to_thread(_digest_file, path)
        object_path = await asyncio.to_thread(_store_object, path, digest, objects)
        view_path = objects / 'views' / (digest + '.txt')
        if not view_path.exists():
            view_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.link(object_path, view_path)
            except OSError:
                shutil.copy2(object_path, view_path)
            os.chmod(view_path, 0o400)
        async with db._transaction() as conn:
            await conn.execute('INSERT OR IGNORE INTO artifact_objects VALUES(?,?,?,?,?)',
                               (digest, size, str(object_path), 'output', time.time()))
        result = result.replace(raw, str(view_path)).replace(encoded, html.escape(str(view_path), quote=True))
    return result


class ConversationBranchService:
    def __init__(self, db, *, code_root: str | Path, storage_root: str | Path):
        self.db = db
        self.code_root = Path(code_root).resolve()
        self.storage = Path(storage_root).resolve()
        self.objects = self.storage / 'conversation_objects'
        self.workspaces = self.storage / 'conversation_workspaces'
        self.uploads = self.storage / 'uploads'
        self.objects.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspaces.mkdir(parents=True, exist_ok=True, mode=0o700)

    async def _session(self, conversation_id: str):
        session = await self.db.get_session(conversation_id)
        if not session or session.get('deleting'):
            raise BranchingError('对话不存在')
        return session

    def _root(self, session: dict) -> Path:
        return Path(session.get('workspace_path') or self.code_root).resolve()

    async def _record(self, conversation_id: str, record_id: int):
        conn = await self.db._get_conn()
        cursor = await conn.execute('SELECT * FROM global_messages WHERE id=? AND session_id=? AND withdrawn=0',
                                    (record_id, conversation_id))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise BranchingError('目标消息不存在或已撤回')
        return dict(row)

    async def _turn(self, turn_id: str | None):
        if not turn_id:
            return None
        conn = await self.db._get_conn()
        row = None
        for _ in range(40):
            cursor = await conn.execute('SELECT * FROM conversation_turns WHERE id=?', (turn_id,))
            row = await cursor.fetchone()
            await cursor.close()
            if not row or row['status'] != 'running':
                return dict(row) if row else None
            await asyncio.sleep(0.05)
        return dict(row) if row else None

    async def _anchor(self, conversation_id: str, record_id: int, mode: str):
        record = await self._record(conversation_id, record_id)
        turn = await self._turn(record.get('turn_id'))
        if mode == 'user':
            if record['role'] != 'user' or record['msg_type'] not in {'user_text', 'user_file', 'user_photo', 'command'}:
                raise BranchingError('只能从用户消息编辑并创建分支')
            if turn and turn.get('user_record_id') != record_id:
                raise BranchingError('这条消息属于同一批次的后续输入，请从该回合第一条用户消息创建分支')
            snapshot_id = turn.get('before_snapshot_id') if turn and turn.get('snapshot_complete') else None
            boundary = (await self._turn_bounds(turn['id']))[0] - 1 if turn else record_id - 1
        elif mode == 'assistant':
            if record['msg_type'] != 'ai_reply' or record['role'] != 'assistant':
                raise BranchingError('只能从完整 AI 回答创建分支')
            if turn and turn.get('final_record_id') != record_id:
                raise BranchingError('这不是该回合的最终 AI 回答')
            snapshot_id = turn.get('after_snapshot_id') if turn and turn.get('snapshot_complete') else None
            boundary = (await self._turn_bounds(turn['id']))[1] if turn else record_id
            conn = await self.db._get_conn()
            cursor = await conn.execute(
                'SELECT id,msg_type FROM global_messages WHERE session_id=? AND id>? AND withdrawn=0 ORDER BY id LIMIT 1',
                (conversation_id, boundary),
            )
            next_row = await cursor.fetchone()
            await cursor.close()
            if next_row and next_row['msg_type'] == 'token_usage':
                boundary = int(next_row['id'])
        else:
            raise BranchingError('未知分支方式')
        return record, turn, snapshot_id, boundary

    async def _turn_bounds(self, turn_id: str) -> tuple[int, int]:
        conn = await self.db._get_conn()
        cursor = await conn.execute(
            "SELECT MIN(id) a,MAX(id) b FROM global_messages WHERE turn_id=? AND msg_type<>'command'",
            (turn_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or row['a'] is None:
            raise BranchingError('回合记录不完整')
        return int(row['a']), int(row['b'])

    async def branch_preview(self, conversation_id: str, record_id: int, mode: str) -> dict:
        session = await self._session(conversation_id)
        record, turn, snapshot_id, boundary = await self._anchor(conversation_id, record_id, mode)
        return {
            'conversation_id': conversation_id, 'conversation_name': session.get('name') or '新对话',
            'record_id': record_id, 'mode': mode, 'boundary_record_id': boundary,
            'turn_id': turn.get('id') if turn else None, 'exact_files': False,
            'snapshot_id': None,
            'warning': '',
            'draft_text': record['content'] if mode == 'user' else '',
            'has_attachments': bool(_metadata(record.get('metadata')).get('attachments')),
        }

    async def _request(self, request_id: str | None, operation: str, conversation_id: str):
        if not request_id:
            return None
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', request_id):
            raise BranchingError('无效幂等请求标识')
        conn = await self.db._get_conn()
        cursor = await conn.execute('SELECT * FROM conversation_mutation_requests WHERE request_id=?', (request_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None
        if row['operation'] != operation or row['conversation_id'] != conversation_id:
            raise BranchingError('请求标识已用于其他操作')
        return json.loads(row['response'])

    async def _save_request(self, request_id: str | None, operation: str, conversation_id: str, response: dict):
        if not request_id:
            return
        async with self.db._transaction() as conn:
            await conn.execute('INSERT INTO conversation_mutation_requests VALUES(?,?,?,?,?)', (
                request_id, operation, conversation_id,
                json.dumps(response, ensure_ascii=False), time.time(),
            ))

    async def create_branch(self, conversation_id: str, record_id: int, mode: str,
                            *, request_id: str | None = None) -> dict:
        previous = await self._request(request_id, 'branch', conversation_id)
        if previous:
            return {**previous, 'duplicate': True}
        source = await self._session(conversation_id)
        record, turn, _snapshot_id, boundary = await self._anchor(conversation_id, record_id, mode)
        # Branches copy conversation history only.  No file snapshot, no Git
        # worktree and no workspace copy: the branch keeps working on the same
        # project directory as normal chats.
        branch_id = uuid.uuid4().hex
        now = time.time()
        base_raw = (source.get('name') or '新对话').strip() or '新对话'
        name = f'{base_raw[:75]} · 分支'
        generation = None
        read_conn = await self.db._get_conn()
        cursor = await read_conn.execute(
            'SELECT * FROM global_messages WHERE session_id=? AND id<=? AND withdrawn=0 ORDER BY id',
            (conversation_id, boundary),
        )
        source_rows = [dict(row) for row in await cursor.fetchall()]
        await cursor.close()
        for row in source_rows:
            row['_branch_metadata'] = await freeze_attachment_metadata(
                self.db, _metadata(row.get('metadata')), self.uploads, self.objects,
            )
            row['_branch_metadata'] = await localize_branch_metadata(
                row['_branch_metadata'], branch_id, self.uploads, self.objects,
            )
            row['_branch_content'] = row['content']
            for old_path, object_path in row['_branch_metadata'].pop('_branch_path_map', {}).items():
                row['_branch_content'] = row['_branch_content'].replace(old_path, object_path)
            row['_branch_content'] = await freeze_output_paths(
                self.db, row['_branch_content'], self.storage, self.objects,
            )
        draft_metadata = (await freeze_attachment_metadata(
            self.db, _metadata(record.get('metadata')), self.uploads, self.objects,
        ) if mode == 'user' else None)
        if draft_metadata:
            draft_metadata = await localize_branch_metadata(
                draft_metadata, branch_id, self.uploads, self.objects,
            )
            draft_metadata.pop('_branch_path_map', None)
        async with self.db._transaction() as conn:
            name_cursor = await conn.execute('SELECT name FROM chat_sessions WHERE deleting=0 AND name IS NOT NULL')
            existing_names = {row['name'] for row in await name_cursor.fetchall()}
            await name_cursor.close()
            suffix = ' · 分支'
            name = f'{base_raw[:80 - len(suffix)]}{suffix}'
            branch_index = 2
            while name in existing_names:
                suffix = f' · 分支 {branch_index}'
                name = f'{base_raw[:80 - len(suffix)]}{suffix}'
                branch_index += 1
            generation = await self.db._allocate_conversation_generation(conn)
            await conn.execute('''INSERT INTO chat_sessions
                (id,name,model,created_at,last_active,archived,generation,agent_iteration,
                 context_start_record_id,context_epoch,deleting,title_auto_pending,group_id,pinned,
                 parent_conversation_id,parent_turn_id,workspace_path,branch_mode,
                 branch_base_snapshot_id,branch_created_at)
                VALUES(?,?,?,?,?,0,?,0,0,0,0,0,?,0,?,?,?,?,?,?)''', (
                branch_id, name, source.get('model'), now, now, generation,
                source.get('group_id') or 'ungrouped', conversation_id,
                turn.get('id') if turn else None, None,
                'context', None, now,
            ))
            cursor = await conn.execute('SELECT * FROM conversation_turns WHERE conversation_id=? AND '
                'EXISTS(SELECT 1 FROM global_messages m WHERE m.turn_id=conversation_turns.id AND m.id<=?) '
                'ORDER BY started_at,id', (conversation_id, boundary))
            copied_turns = [dict(row) for row in await cursor.fetchall()]
            turn_map: dict[str, str] = {}
            for old in copied_turns:
                new_turn = uuid.uuid4().hex
                turn_map[old['id']] = new_turn
                await conn.execute('''INSERT INTO conversation_turns
                    (id,conversation_id,status,started_at,finished_at,before_snapshot_id,
                     after_snapshot_id,user_record_id,final_record_id,snapshot_complete,error)
                    VALUES(?,?,'copied',?,?,?,?,NULL,NULL,?,?)''', (
                    new_turn, branch_id, old['started_at'], old['finished_at'],
                    old['before_snapshot_id'], old['after_snapshot_id'],
                    old['snapshot_complete'], old['error'],
                ))
                op_cursor = await conn.execute('SELECT * FROM turn_operations WHERE turn_id=? ORDER BY sequence', (old['id'],))
                for op in await op_cursor.fetchall():
                    await conn.execute('INSERT INTO turn_operations(turn_id,sequence,kind,effect,details,created_at) VALUES(?,?,?,?,?,?)',
                        (new_turn, op['sequence'], op['kind'], op['effect'], op['details'], op['created_at']))
            record_map: dict[int, int] = {}
            for row in source_rows:
                metadata = row['_branch_metadata']
                cursor = await conn.execute('''INSERT INTO global_messages
                    (chat_id,user_id,msg_type,role,content,timestamp,session_id,metadata,turn_id,withdrawn,source_record_id)
                    VALUES(?,?,?,?,?,?,?,?,?,0,?)''', (
                    row['chat_id'], row['user_id'], row['msg_type'], row['role'], row['_branch_content'],
                    row['timestamp'], branch_id,
                    json.dumps(metadata, ensure_ascii=False) if metadata else None,
                    turn_map.get(row.get('turn_id')), row['id'],
                ))
                record_map[row['id']] = cursor.lastrowid
            for old in copied_turns:
                new_turn = turn_map[old['id']]
                await conn.execute('UPDATE conversation_turns SET user_record_id=?,final_record_id=? WHERE id=?', (
                    record_map.get(old.get('user_record_id')), record_map.get(old.get('final_record_id')), new_turn,
                ))
            if mode == 'user':
                await conn.execute('INSERT INTO conversation_branch_drafts VALUES(?,?,?,?,?)', (
                    branch_id, record_id, record['content'],
                    json.dumps(draft_metadata, ensure_ascii=False) if draft_metadata else None, now,
                ))
            await conn.execute("INSERT INTO config(key,value) VALUES('current_chat_id',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(branch_id),))
            cursor = await conn.execute("SELECT value FROM config WHERE key='conversation_revision'")
            revision_row = await cursor.fetchone()
            revision = int(json.loads(revision_row['value'])) if revision_row else 0
            await conn.execute("INSERT INTO config(key,value) VALUES('conversation_revision',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(revision + 1),))
        self.db._config_cache.pop('current_chat_id', None)
        self.db._config_cache.pop('conversation_revision', None)
        response = {'ok': True, 'conversation_id': branch_id, 'name': name,
                    'branch_mode': 'context',
                    'parent_conversation_id': conversation_id,
                    'parent_turn_id': turn.get('id') if turn else None,
                    'draft': {'text': record['content'], 'has_attachments': bool(_metadata(record.get('metadata')).get('attachments'))}
                    if mode == 'user' else None}
        await self._save_request(request_id, 'branch', conversation_id, response)
        return response

    async def lineage(self, conversation_id: str) -> dict:
        session = await self._session(conversation_id)
        parent = await self.db.get_session(session.get('parent_conversation_id')) if session.get('parent_conversation_id') else None
        conn = await self.db._get_conn()
        cursor = await conn.execute('SELECT id,name,branch_mode,parent_turn_id FROM chat_sessions '
                                    'WHERE parent_conversation_id=? AND deleting=0 ORDER BY created_at,id', (conversation_id,))
        children = [dict(row) for row in await cursor.fetchall()]
        await cursor.close()
        cursor = await conn.execute('SELECT * FROM conversation_branch_drafts WHERE conversation_id=?', (conversation_id,))
        draft = await cursor.fetchone()
        await cursor.close()
        return {'conversation_id': conversation_id, 'branch_mode': session.get('branch_mode'),
                'parent': {'id': parent['id'], 'name': parent['name'], 'turn_id': session.get('parent_turn_id')}
                if parent else None, 'children': children,
                'draft': {'text': draft['text'], 'metadata': _metadata(draft['metadata'])} if draft else None}

    async def apply_preview(self, conversation_id: str) -> dict:
        branch = await self._session(conversation_id)
        parent_id = branch.get('parent_conversation_id')
        if not parent_id or not branch.get('branch_base_snapshot_id'):
            raise BranchingError('当前对话不是可应用的分支')
        parent = await self._session(parent_id)
        base = await snapshot_manifest(self.db, branch['branch_base_snapshot_id'])
        source_capture = await asyncio.to_thread(capture_manifest, self._root(parent), self.objects)
        branch_capture = await asyncio.to_thread(capture_manifest, self._root(branch), self.objects)
        comparison = compare_manifests(base, source_capture, branch_capture)
        return {'conversation_id': conversation_id, 'parent_conversation_id': parent_id,
                'base_snapshot_id': base['id'], 'source_digest': source_capture['root_digest'],
                'branch_digest': branch_capture['root_digest'], 'changes': comparison['apply'],
                'conflicts': comparison['conflicts'], 'can_apply': not comparison['conflicts']}

    async def apply(self, conversation_id: str, data: dict) -> dict:
        previous = await self._request(data.get('request_id'), 'apply', conversation_id)
        if previous:
            return {**previous, 'duplicate': True}
        preview = await self.apply_preview(conversation_id)
        if (data.get('confirm') is not True or data.get('base_snapshot_id') != preview['base_snapshot_id']
                or data.get('source_digest') != preview['source_digest']
                or data.get('branch_digest') != preview['branch_digest']):
            raise BranchingError('文件状态已变化，请重新预览后确认')
        if preview['conflicts']:
            raise BranchingError('来源与分支存在文件冲突，未修改任何文件')
        branch, parent = await self._session(conversation_id), await self._session(preview['parent_conversation_id'])
        before_manifest = await asyncio.to_thread(capture_manifest, self._root(parent), self.objects)
        before_branch = await asyncio.to_thread(capture_manifest, self._root(branch), self.objects)
        before_id = await save_snapshot(self.db, parent['id'], None, 'before_branch_apply', before_manifest)
        applied_files = synced_branch = False
        try:
            await asyncio.to_thread(apply_changes, self._root(parent), preview['changes'])
            applied_files = True
            after_manifest = await asyncio.to_thread(capture_manifest, self._root(parent), self.objects)
            after_id = await save_snapshot(self.db, parent['id'], None, 'after_branch_apply', after_manifest)
            await asyncio.to_thread(
                apply_changes, self._root(branch), changes_to(before_branch, after_manifest),
            )
            synced_branch = True
            async with self.db._transaction() as conn:
                content = f"分支「{branch.get('name') or conversation_id}」的 {len(preview['changes'])} 项文件改动已应用。"
                await conn.execute('''INSERT INTO global_messages
                    (chat_id,user_id,msg_type,role,content,timestamp,session_id,metadata,withdrawn)
                    VALUES(0,0,'system_op','system',?,?,?,?,0)''', (
                    content, time.time(), parent['id'], json.dumps({'ui_audit': False, 'branch_apply': {
                        'branch_id': conversation_id, 'before_snapshot_id': before_id,
                        'after_snapshot_id': after_id}}, ensure_ascii=False),
                ))
                await conn.execute('UPDATE chat_sessions SET branch_base_snapshot_id=? WHERE id=?',
                                   (after_id, conversation_id))
        except BaseException:
            if synced_branch:
                with contextlib.suppress(Exception):
                    current_branch = await asyncio.to_thread(capture_manifest, self._root(branch), self.objects)
                    await asyncio.to_thread(apply_changes, self._root(branch), changes_to(current_branch, before_branch))
            if applied_files:
                with contextlib.suppress(Exception):
                    current = await asyncio.to_thread(capture_manifest, self._root(parent), self.objects)
                    await asyncio.to_thread(apply_changes, self._root(parent), changes_to(current, before_manifest))
            raise
        response = {'ok': True, 'applied': len(preview['changes']), 'before_snapshot_id': before_id,
                    'after_snapshot_id': after_id, 'parent_conversation_id': parent['id']}
        await self._save_request(data.get('request_id'), 'apply', conversation_id, response)
        return response

    async def withdraw_preview(self, conversation_id: str, record_id: int) -> dict:
        """Preview withdrawing a user message and everything after it.

        Withdrawal only removes conversation history and model context; it never
        scans or restores disk files.  Recorded side effects (commands, file
        writes, outbound files) are reported as warnings for the confirmation
        dialog but never block the withdrawal.  Only queued input, an open ask
        form or a turn that is still running prevent it.
        """
        record = await self._record(conversation_id, record_id)
        if record['role'] != 'user' or record['msg_type'] not in {'user_text', 'user_file', 'user_photo', 'command'}:
            raise BranchingError('撤回只能从用户消息开始')
        turn = await self._turn(record.get('turn_id'))
        if turn and turn.get('user_record_id') != record_id:
            raise BranchingError('这条消息属于同一回合，请从该回合第一条用户消息撤回')
        conn = await self.db._get_conn()
        cursor = await conn.execute("SELECT COUNT(*) n FROM message_queue WHERE conversation_id=? AND status IN ('pending','paused','claimed')",
                                    (conversation_id,))
        queued = int((await cursor.fetchone())['n'])
        await cursor.close()
        cursor = await conn.execute("SELECT state FROM message_queue_controls WHERE conversation_id=?", (conversation_id,))
        control = await cursor.fetchone()
        await cursor.close()
        waiting = bool(queued or control and control['state'] == 'ask')
        cursor = await conn.execute(
            "SELECT id FROM conversation_turns WHERE conversation_id=? AND status<>'withdrawn' "
            "AND (id=? OR EXISTS(SELECT 1 FROM global_messages m WHERE m.turn_id=conversation_turns.id "
            "AND m.session_id=? AND m.withdrawn=0 AND m.id>=?)) ORDER BY started_at,id",
            (conversation_id, turn['id'] if turn else '', conversation_id, record_id),
        )
        ids = [row['id'] for row in await cursor.fetchall()]
        await cursor.close()
        cursor = await conn.execute(
            "SELECT COUNT(*) n FROM conversation_turns WHERE conversation_id=? AND status='running'",
            (conversation_id,))
        running = bool(int((await cursor.fetchone())['n']))
        await cursor.close()
        effects: dict[str, int] = {}
        kinds: dict[str, set[str]] = {}
        if ids:
            placeholders = ','.join('?' for _ in ids)
            cursor = await conn.execute(
                f'SELECT kind,effect,COUNT(*) n FROM turn_operations WHERE turn_id IN ({placeholders}) GROUP BY kind,effect',
                ids)
            for row in await cursor.fetchall():
                effects[row['effect']] = effects.get(row['effect'], 0) + int(row['n'])
                kinds.setdefault(row['effect'], set()).add(row['kind'])
            await cursor.close()
        warnings = withdraw_warnings(effects, kinds)
        placeholders = ','.join('?' for _ in ids)
        cursor = await conn.execute(
            'SELECT COUNT(*) n FROM global_messages WHERE session_id=? AND withdrawn=0 AND (id>=?'
            + (f' OR turn_id IN ({placeholders}))' if ids else ')'),
            (conversation_id, record_id, *ids),
        )
        count = int((await cursor.fetchone())['n'])
        await cursor.close()
        cursor = await conn.execute(
            'SELECT COALESCE(MAX(id),0) last_id,COUNT(*) n FROM global_messages WHERE session_id=? AND withdrawn=0',
            (conversation_id,))
        state = await cursor.fetchone()
        await cursor.close()
        # Optimistic concurrency token: the confirmed withdrawal must match the
        # conversation the user previewed (no new messages in between).
        current_digest = hashlib.sha256(
            f"{state['last_id']}:{state['n']}:{record_id}:{','.join(ids)}".encode()).hexdigest()
        reason = ('仍有待发消息或表单等待处理，请先完成、取回或取消后再撤回。' if waiting else
                  '当前对话仍在执行，请等待回答结束或停止后再撤回。' if running else '')
        return {'record_id': record_id, 'turn_id': turn['id'] if turn else f'record:{record_id}',
                'turn_ids': ids, 'message_count': count, 'effects': effects,
                'operation_kinds': {key: sorted(value) for key, value in kinds.items()},
                'warnings': warnings, 'warning': '\n'.join(warnings),
                'can_withdraw': not waiting and not running, 'reason': reason, 'queued': queued,
                'restore_snapshot_id': None, 'current_digest': current_digest,
                'latest_snapshot_id': None}

    async def withdraw(self, conversation_id: str, record_id: int, data: dict) -> dict:
        previous = await self._request(data.get('request_id'), 'withdraw', conversation_id)
        if previous:
            return {**previous, 'duplicate': True}
        preview = await self.withdraw_preview(conversation_id, record_id)
        if not preview['can_withdraw']:
            raise BranchingError(preview['reason'])
        if (data.get('confirm') is not True or data.get('turn_id') != preview['turn_id']
                or data.get('current_digest') != preview['current_digest']):
            raise BranchingError('对话已变化，请重新预览后确认')
        record = await self._record(conversation_id, record_id)
        conn = await self.db._get_conn()
        ids = preview['turn_ids']
        if ids:
            placeholders = ','.join('?' for _ in ids)
            cursor = await conn.execute(
                f'SELECT metadata FROM global_messages WHERE session_id=? AND withdrawn=0 AND (id>=? OR turn_id IN ({placeholders}))',
                (conversation_id, record_id, *ids),
            )
        else:
            cursor = await conn.execute(
                'SELECT metadata FROM global_messages WHERE session_id=? AND withdrawn=0 AND id>=?',
                (conversation_id, record_id),
            )
        native_ids, logical_ids = set(), set()
        for row in await cursor.fetchall():
            metadata = _metadata(row['metadata'])
            if type(metadata.get('source_message_id')) is int:
                native_ids.add(metadata['source_message_id'])
            live = metadata.get('web_live')
            if isinstance(live, dict):
                logical_ids.update(mid for mid in live.get('message_ids', []) if type(mid) is int)
        await cursor.close()
        async with self.db._transaction() as conn:
            await conn.execute('UPDATE global_messages SET withdrawn=1 WHERE session_id=? AND id>=?',
                               (conversation_id, record_id))
            await conn.execute('UPDATE ui_messages SET withdrawn=1 WHERE conversation_id=? AND turn_id IS NULL AND timestamp>=?',
                               (conversation_id, record['timestamp']))
            if ids:
                placeholders = ','.join('?' for _ in ids)
                await conn.execute(f'UPDATE global_messages SET withdrawn=1 WHERE session_id=? AND turn_id IN ({placeholders})',
                                   (conversation_id, *ids))
                await conn.execute(f'UPDATE ui_messages SET withdrawn=1 WHERE conversation_id=? AND turn_id IN ({placeholders})',
                                   (conversation_id, *ids))
                await conn.execute(f"UPDATE conversation_turns SET status='withdrawn' WHERE id IN ({placeholders})", ids)
            await conn.execute('DELETE FROM chat_messages WHERE session_id=?', (conversation_id,))
            cursor = await conn.execute('SELECT * FROM global_messages WHERE session_id=? AND withdrawn=0 ORDER BY id', (conversation_id,))
            model_rows = self.db.conversation_records_to_messages(await cursor.fetchall())
            now = time.time()
            for message in model_rows:
                if isinstance(message.get('content'), str):
                    await conn.execute('INSERT INTO chat_messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',
                                       (conversation_id, message['role'], message['content'], now))
            await conn.execute('UPDATE chat_sessions SET last_active=? WHERE id=?', (now, conversation_id))
        response = {'ok': True, 'withdrawn_turns': len(ids), 'withdrawn_messages': preview['message_count'],
                    'restored_files': 0, 'conversation_id': conversation_id,
                    'warnings': preview['warnings'],
                    'telegram_native_message_ids': sorted(native_ids),
                    'telegram_logical_message_ids': sorted(logical_ids)}
        await self._save_request(data.get('request_id'), 'withdraw', conversation_id, response)
        return response


_EFFECT_WARNINGS = {
    'unknown': ('包含命令执行', '已经执行的命令及其对系统、文件或进程造成的影响不会撤销，撤回只删除对话记录和 AI 上下文。'),
    'local_reversible': ('包含文件写入', '磁盘上已经写入或修改的文件不会回滚，撤回只删除对话记录和 AI 上下文。'),
    'external': ('包含外部发送', '已经发送出去的文件或媒体不会收回（Telegram 端会尽力删除对应消息）。'),
}


def withdraw_warnings(effects: dict[str, int], kinds: dict[str, set[str]]) -> list[str]:
    """Human-readable side-effect notes for the withdrawal confirmation."""
    notes = []
    for effect in ('unknown', 'local_reversible', 'external'):
        if not effects.get(effect):
            continue
        title, detail = _EFFECT_WARNINGS[effect]
        names = '、'.join(sorted(kinds.get(effect, ()))) or effect
        notes.append(f'{title}（{names} × {effects[effect]}）：{detail}')
    return notes


async def consume_branch_draft(db, conversation_id: str) -> dict | None:
    async with db._transaction() as conn:
        cursor = await conn.execute('SELECT * FROM conversation_branch_drafts WHERE conversation_id=?', (conversation_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        await conn.execute('DELETE FROM conversation_branch_drafts WHERE conversation_id=?', (conversation_id,))
        result = dict(row)
        result['metadata'] = _metadata(result.get('metadata'))
        return result
