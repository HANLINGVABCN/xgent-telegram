#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WebDAV 文件管理器 - 零依赖 Python 文件管理服务器
用法: python3 server.py [--port PORT] [--root DIR] [--auth USER:PASS]

功能:
  - 网页文件管理器 (上传/下载/复制/移动/删除/重命名)
  - WebDAV 协议支持 (可用系统文件管理器挂载)
  - 右键压缩/解压 (zip 压缩，zip/tar 系列解压)
  - 磁盘容量显示
  - 网页登录认证，WebDAV Basic Auth 认证
  - 零外部依赖，仅使用 Python 标准库
"""

import os
import sys
import json
import shutil
import argparse
import urllib.parse
import urllib.request
import mimetypes
import base64
import hmac
import secrets
import uuid
import time
import socket
import ipaddress
import threading
import zipfile
import tarfile
import re
from http import cookies
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from datetime import datetime, timezone
from email.utils import formatdate

# ============================================================
# Global Config
# ============================================================
ROOT_DIR = ''
AUTH_CRED = None  # 'user:pass' or None
SESSION_COOKIE = 'fm_session'
SESSION_MAX_AGE = 7 * 24 * 60 * 60
MAX_SESSIONS = 1000
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
STATE_LOCK = threading.RLock()
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'filemanager_state.json')
AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'filemanager_auth.json')
MAX_TTL_SECONDS = 10 * 365 * 24 * 60 * 60
MAX_SPARSE_FILE_SIZE = 100 * 1024 * 1024 * 1024
MAX_TEXT_EDIT_SIZE = 10 * 1024 * 1024
# 同一 IP 连续请求间隔若超过此值，视为一次新的下载会话。
# 用于把多线程/Range 分块下载收敛成"1 次下载"，而非每个分片算 1 次。
SHARE_SESSION_GAP = 30 * 60
WEBDAV_DIR_NAME = 'webdav'
DOWNLOAD_DIR_NAME = 'download'
REMOTE_TASK_SAMPLE_LIMIT = 240
# 远程下载的单文件上限，避免一条链接把磁盘刷爆。
MAX_REMOTE_DOWNLOAD_SIZE = int(os.environ.get('WEBDAV_MAX_REMOTE_DOWNLOAD', 20 * 1024 * 1024 * 1024))
# 允许远程下载访问内网地址（默认关闭）。自建内网源站时可显式打开。
ALLOW_PRIVATE_REMOTE = os.environ.get('WEBDAV_ALLOW_PRIVATE_REMOTE', '').lower() in ('1', 'true', 'yes')
REMOTE_TASKS = {}
REMOTE_TASKS_LOCK = threading.Lock()
UPLOAD_BATCH_DIRS = {}
UPLOAD_BATCH_UPDATED = {}
UPLOAD_BATCH_DIRS_LOCK = threading.Lock()
UPLOAD_BATCH_TTL_SECONDS = 2 * 60 * 60
# In-memory metadata for active chunked uploads (taskId -> {partPath, finalPath, size, ...})
UPLOAD_TASKS = {}
UPLOAD_TASKS_LOCK = threading.Lock()

# Login rate limiter: { ip: { 'fails': int, 'locked_until': float } }
_login_attempts = {}
_login_attempts_lock = threading.Lock()
LOGIN_MAX_FAILS = 5
LOGIN_LOCKOUT_SECONDS = 60
# 可信反向代理的 IP 列表（逗号分隔）。为空时一律忽略 X-Forwarded-For，
# 只用真实对端地址做限速，否则伪造该头即可绕过登录锁定。
TRUSTED_PROXIES = {
    ip.strip() for ip in os.environ.get('WEBDAV_TRUSTED_PROXIES', '').split(',') if ip.strip()
}

# Background cache for root directory size calculation
_root_size_cache = {
    'size': 0,
    'logical': 0,
    'allocated': 0,
    'updated': 0,
    'lock': threading.Lock(),
    'started': False,
}

# ============================================================
# Utility Functions
# ============================================================

def safe_join(root, req_path):
    """Safely resolve req_path within root, preventing directory traversal."""
    root = os.path.realpath(root)
    req_path = req_path.replace('\\', '/').strip('/')
    if not req_path:
        return root
    target = os.path.realpath(os.path.join(root, req_path))
    if target == root or target.startswith(root + os.sep):
        return target
    return None


def format_size(n):
    """Format bytes to human-readable size string."""
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f'{n:.1f} {u}'
        n /= 1024
    return f'{n:.1f} PB'


def rfc1123_date(ts):
    """Format timestamp as RFC 1123 date string."""
    return formatdate(timeval=ts, localtime=False, usegmt=True)


def iso8601_date(ts):
    """Format timestamp as ISO 8601 date string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def guess_mime(path):
    """Guess MIME type from file path."""
    return mimetypes.guess_type(path)[0] or 'application/octet-stream'


def get_file_info(root, rel_path, full_path):
    """Build file information dictionary."""
    try:
        st = os.stat(full_path)
    except OSError:
        return None
    is_dir = os.path.isdir(full_path)
    return {
        'name': os.path.basename(full_path) or '/',
        'path': rel_path.replace('\\', '/'),
        'isDir': is_dir,
        'size': 0 if is_dir else st.st_size,
        'modified': st.st_mtime,
        'modifiedStr': datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
    }


def _empty_state():
    return {'shares': {}, 'shareTrash': {}, 'tempFiles': {}, 'tasks': {}, 'pinned': [], 'taskTrash': {}}


def load_state():
    with STATE_LOCK:
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            data = _empty_state()
        data.setdefault('shares', {})
        data.setdefault('shareTrash', {})
        data.setdefault('tempFiles', {})
        data.setdefault('tasks', {})
        data.setdefault('pinned', [])
        data.setdefault('taskTrash', {})
        return data


def save_state(data):
    with STATE_LOCK:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)


# ---- Task persistence helpers ----
# Persisted task snapshot fields (shared by remote/upload/download kinds):
# id, kind, name, status, size, loaded, error, url, path, createdAt,
# startedAt, finishedAt, lastBps, maxBps, samples
TASK_SNAPSHOT_FIELDS = (
    'id', 'kind', 'name', 'status', 'size', 'loaded', 'error', 'url', 'path',
    'createdAt', 'startedAt', 'finishedAt', 'lastBps', 'maxBps', 'samples',
)


def task_snapshot(task):
    """Build a JSON-serializable snapshot from a task dict (live or remote)."""
    snap = {'kind': task.get('kind') or 'remote'}
    for key in TASK_SNAPSHOT_FIELDS:
        if key in task:
            snap[key] = task[key]
    # samples can be large; cap to REMOTE_TASK_SAMPLE_LIMIT for storage
    samples = snap.get('samples') or []
    if len(samples) > REMOTE_TASK_SAMPLE_LIMIT:
        snap['samples'] = samples[-REMOTE_TASK_SAMPLE_LIMIT:]
    return snap


def save_task(task):
    """Persist a single task snapshot into state['tasks'][id] (atomic).

    Uses STATE_LOCK across the entire read-check-write cycle to prevent
    race conditions with concurrent delete operations. If the task has
    already been soft-deleted (present in state['taskTrash']), skip writing.
    """
    if not task or not task.get('id'):
        return
    snap = task_snapshot(task)
    task_id = snap['id']
    with STATE_LOCK:
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
        except Exception:
            state = {}
        state.setdefault('tasks', {})
        state.setdefault('taskTrash', {})
        # 如果任务已在回收站，不再写回（防止 worker 线程复活已删除的任务）
        if task_id in state.get('taskTrash', {}):
            return
        state['tasks'][task_id] = snap
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)


def delete_persisted_task(task_id):
    """Remove a task from the persistent state."""
    if not task_id:
        return
    state = load_state()
    if state.get('tasks', {}).pop(task_id, None) is not None:
        save_state(state)


def load_persisted_tasks():
    """Return all persisted task snapshots as a dict id -> snapshot."""
    return load_state().get('tasks', {}) or {}


def load_auth_cred():
    try:
        with open(AUTH_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        auth = str(data.get('auth') or '')
        return auth or None
    except Exception:
        return None


def save_auth_cred(auth):
    tmp = AUTH_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'auth': auth}, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, AUTH_FILE)


def valid_login_username(username):
    if not username or ':' in username:
        return False
    return not any(ord(ch) < 32 or ord(ch) == 127 for ch in username)


def clamp_ttl(value, default=24 * 60 * 60):
    try:
        ttl = int(value)
    except (TypeError, ValueError):
        ttl = default
    return max(60, min(ttl, MAX_TTL_SECONDS))


def valid_leaf_name(name):
    if not name:
        return False
    return not (name.startswith('.') or '/' in name or '\\' in name or '..' in name)


def safe_upload_relpath(value):
    rel = str(value or '').replace('\\', '/').strip('/')
    if not rel:
        return ''
    parts = []
    for part in rel.split('/'):
        if not part or part in ('.', '..') or '\x00' in part:
            return ''
        if part.startswith('.') or '/' in part or '\\' in part or '..' in part:
            return ''
        parts.append(part)
    return '/'.join(parts)


def cleanup_upload_batches(now=None):
    now = now or time.time()
    with UPLOAD_BATCH_DIRS_LOCK:
        expired = [
            batch_id for batch_id, updated in UPLOAD_BATCH_UPDATED.items()
            if now - float(updated or 0) > UPLOAD_BATCH_TTL_SECONDS
        ]
        for batch_id in expired:
            UPLOAD_BATCH_UPDATED.pop(batch_id, None)
            UPLOAD_BATCH_DIRS.pop(batch_id, None)


def normalize_extension(ext):
    ext = str(ext or '').strip().lstrip('.')
    if not ext:
        return ''
    safe = ''.join(ch for ch in ext if ch.isalnum() or ch in ('-', '_'))
    return safe[:32]


def make_unique_path(parent, name):
    base, ext = os.path.splitext(name)
    candidate = os.path.join(parent, name)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(parent, f'{base}_{i}{ext}')
        i += 1
    return candidate


def cleanup_expired_temp_files():
    state = load_state()
    changed = False
    now = time.time()
    for rel_path, meta in list(state.get('tempFiles', {}).items()):
        expires_at = float(meta.get('expiresAt') or 0)
        if expires_at and expires_at <= now:
            full = safe_join(ROOT_DIR, rel_path)
            if full and os.path.isfile(full):
                try:
                    os.remove(full)
                except OSError:
                    pass
            state['tempFiles'].pop(rel_path, None)
            changed = True
    for token, meta in list(state.get('shares', {}).items()):
        expires_at = float(meta.get('expiresAt') or 0)
        if expires_at and expires_at <= now:
            state['shares'].pop(token, None)
            changed = True
    if changed:
        save_state(state)


def build_content_disposition(filename):
    enc_name = urllib.parse.quote(filename)
    fallback = ''.join(ch if 32 <= ord(ch) < 127 and ch not in '\\";' else '_' for ch in filename)
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{enc_name}'


def clean_forwarded_value(value):
    value = str(value or '').strip().strip('"')
    value = value.replace('\r', '').replace('\n', '')
    return value


def forwarded_header_params(header):
    first = str(header or '').split(',', 1)[0]
    params = {}
    for part in first.split(';'):
        key, sep, value = part.partition('=')
        if sep:
            params[key.strip().lower()] = clean_forwarded_value(value)
    return params


def normalize_external_host(host, port=''):
    host = clean_forwarded_value(str(host or '').split(',', 1)[0])
    port = clean_forwarded_value(str(port or '').split(',', 1)[0])
    if not host:
        return ''
    if port and ':' not in host and port.isdigit():
        host = f'{host}:{port}'
    return host


def normalize_external_proto(proto):
    proto = clean_forwarded_value(str(proto or '').split(',', 1)[0]).lower()
    if proto in ('http', 'https'):
        return proto
    return ''


def read_text_file(full):
    with open(full, 'rb') as f:
        raw = f.read(MAX_TEXT_EDIT_SIZE + 1)
    if len(raw) > MAX_TEXT_EDIT_SIZE:
        raise ValueError(f'文本编辑最大支持 {format_size(MAX_TEXT_EDIT_SIZE)}')
    return raw.decode('utf-8-sig')


def calculate_logical_size(path):
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))]
            for name in filenames:
                fp = os.path.join(dirpath, name)
                if os.path.islink(fp):
                    continue
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def root_relative_path(full):
    root_real = os.path.realpath(ROOT_DIR)
    full_real = os.path.realpath(full)
    if full_real == root_real:
        return '/'
    return '/' + os.path.relpath(full_real, root_real).replace('\\', '/')


def ensure_webdav_storage():
    base = safe_join(ROOT_DIR, WEBDAV_DIR_NAME) if ROOT_DIR else None
    if not base:
        return None
    os.makedirs(base, exist_ok=True)
    return base


def ensure_download_storage():
    base = safe_join(ROOT_DIR, DOWNLOAD_DIR_NAME) if ROOT_DIR else None
    if not base:
        return None
    os.makedirs(base, exist_ok=True)
    return base


def safe_download_filename(name, fallback='download.bin'):
    name = urllib.parse.unquote(str(name or '')).replace('\\', '/').split('/')[-1].strip()
    name = ''.join('_' if ord(ch) < 32 or ch in '<>:"|?*' else ch for ch in name)
    name = name.strip(' .')
    if not name or name in ('.', '..') or name.startswith('.'):
        name = fallback
    return name[:180] or fallback


def filename_from_content_disposition(header):
    header = str(header or '')
    m = re.search(r"filename\*=([^']*)''([^;]+)", header, re.I)
    if m:
        try:
            return urllib.parse.unquote(m.group(2), encoding=m.group(1) or 'utf-8')
        except LookupError:
            return urllib.parse.unquote(m.group(2))
    m = re.search(r'filename="?([^";]+)"?', header, re.I)
    return m.group(1).strip() if m else ''


def remote_url_filename(url):
    path = urllib.parse.urlparse(url).path
    return safe_download_filename(os.path.basename(path), 'remote-download.bin')


def remote_task_snapshot(task):
    public = {'kind': 'remote'}
    for key in (
        'id', 'url', 'name', 'status', 'size', 'loaded', 'createdAt', 'startedAt',
        'finishedAt', 'path', 'error', 'lastBps', 'maxBps', 'samples'
    ):
        public[key] = task.get(key)
    return public


def remote_task_speed_update(task, now=None, force=False):
    now = now or time.time()
    last_t = float(task.get('lastT') or now)
    if not force and now - last_t < 0.25:
        return
    loaded = int(task.get('loaded') or 0)
    last_bytes = int(task.get('lastBytes') or 0)
    dt = max(0.001, now - last_t)
    cur = max(0.0, (loaded - last_bytes) / dt)
    started = float(task.get('startedAt') or now)
    elapsed = max(0.001, now - started)
    avg = loaded / elapsed
    task['lastBps'] = cur
    task['maxBps'] = max(float(task.get('maxBps') or 0), cur)
    samples = task.setdefault('samples', [])
    samples.append({'t': elapsed, 'bps': cur, 'bytes': loaded, 'avg': avg})
    if len(samples) > REMOTE_TASK_SAMPLE_LIMIT:
        del samples[:-REMOTE_TASK_SAMPLE_LIMIT]
    task['lastT'] = now
    task['lastBytes'] = loaded


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用自动跳转，把重定向目标交回给调用方逐跳校验。

    默认的 urlopen 会自动跟随 302，攻击者只要用一个公网地址跳到
    169.254.169.254（云元数据）或 127.0.0.1，任何只检查初始 URL 的
    校验都会被绕过。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _RedirectTo(newurl)


class _RedirectTo(Exception):
    def __init__(self, url):
        super().__init__(url)
        self.url = url


def validate_remote_url(url):
    """校验远程下载目标。返回 (ok, 错误文案)。

    解析出真实 IP 后拒绝环回/私网/链路本地/保留地址，防止把这个下载器
    当成打内网和云元数据服务的跳板。
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return False, '请输入有效的 http/https 链接'
    if ALLOW_PRIVATE_REMOTE:
        return True, ''
    host = parsed.hostname
    if not host:
        return False, '链接缺少主机名'
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == 'https' else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError:
        return False, f'无法解析主机: {host}'
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False, f'无法识别的地址: {addr}'
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False, (
                f'拒绝访问内网/保留地址 ({addr})。'
                '确需下载内网资源时，请设置环境变量 WEBDAV_ALLOW_PRIVATE_REMOTE=1 后重启。'
            )
    return True, ''


def open_remote_url(url, headers, timeout=30, max_redirects=5):
    """打开远程 URL，逐跳校验重定向目标，防止跳转绕过 SSRF 检查。"""
    opener = urllib.request.build_opener(_NoRedirect)
    current = url
    for _ in range(max_redirects + 1):
        ok, err = validate_remote_url(current)
        if not ok:
            raise ValueError(err)
        req = urllib.request.Request(current, headers=headers)
        try:
            return opener.open(req, timeout=timeout)
        except _RedirectTo as redirect:
            current = urllib.parse.urljoin(current, redirect.url)
    raise ValueError('重定向次数过多')


def remote_download_worker(task_id):
    temp_path = ''
    last_save = 0.0
    resp = None
    try:
        with REMOTE_TASKS_LOCK:
            task = REMOTE_TASKS.get(task_id)
            if not task:
                return
            task['status'] = 'downloading'
            if not task.get('startedAt'):
                task['startedAt'] = time.time()
            task['lastT'] = task['startedAt']
            task['lastBytes'] = int(task.get('loaded') or 0)
            url = task['url']
            temp_path = task.get('tempPath') or ''

        download_dir = ensure_download_storage()
        if not download_dir:
            raise OSError('下载目录不可用')

        # Determine resume offset from any pre-existing .part file
        existing = 0
        if temp_path and os.path.exists(temp_path):
            try:
                existing = os.path.getsize(temp_path)
            except OSError:
                existing = 0

        # Single request with Range if we have existing bytes
        headers = {'User-Agent': 'WebDAV-FileManager/1.0'}
        if existing > 0:
            headers['Range'] = f'bytes={existing}-'
        resp = open_remote_url(url, headers, timeout=30)
        status = getattr(resp, 'status', None) or resp.getcode()
        is_resume = (status == 206)
        content_total = int(resp.headers.get('Content-Length') or 0)

        # Resolve filename + target path from this request (first run only)
        if not temp_path:
            header_name = filename_from_content_disposition(resp.headers.get('Content-Disposition'))
            filename = safe_download_filename(header_name or remote_url_filename(url), 'remote-download.bin')
            target = make_unique_path(download_dir, filename)
            temp_path = target + '.part'
            rel = root_relative_path(target)
            with REMOTE_TASKS_LOCK:
                t2 = REMOTE_TASKS.get(task_id)
                if t2:
                    t2['name'] = os.path.basename(target)
                    t2['path'] = rel
                    t2['tempPath'] = temp_path
        else:
            target = temp_path[:-5] if temp_path.endswith('.part') else temp_path + '.final'

        if is_resume and existing > 0:
            with REMOTE_TASKS_LOCK:
                t2 = REMOTE_TASKS.get(task_id)
                if t2:
                    t2['loaded'] = existing
                    t2['lastBytes'] = existing
                    t2['size'] = (existing + content_total) if content_total else t2.get('size') or 0
        else:
            # Fresh start (server ignored Range or no existing bytes)
            existing = 0
            with REMOTE_TASKS_LOCK:
                t2 = REMOTE_TASKS.get(task_id)
                if t2:
                    t2['loaded'] = 0
                    t2['lastBytes'] = 0
                    t2['size'] = content_total or t2.get('size') or 0

        mode = 'ab' if (is_resume and existing > 0) else 'wb'
        if mode == 'wb' and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

        save_task(merge_remote_task_for_persist(REMOTE_TASKS.get(task_id)))

        # 断点续传时把已有的部分算进总量，上限针对最终文件大小。
        written_total = existing if mode == 'ab' else 0

        with open(temp_path, mode) as out:
            while True:
                with REMOTE_TASKS_LOCK:
                    task = REMOTE_TASKS.get(task_id)
                    if not task:
                        return
                    if task.get('cancel'):
                        if task.get('pauseRequested'):
                            raise InterruptedError('paused')
                        raise InterruptedError('已取消')
                chunk = resp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                written_total += len(chunk)
                if written_total > MAX_REMOTE_DOWNLOAD_SIZE:
                    raise ValueError(
                        f'远程文件超过上限 {format_size(MAX_REMOTE_DOWNLOAD_SIZE)}，已中止下载'
                    )
                with REMOTE_TASKS_LOCK:
                    task = REMOTE_TASKS.get(task_id)
                    if task:
                        task['loaded'] = int(task.get('loaded') or 0) + len(chunk)
                        remote_task_speed_update(task)
                # Throttled persistence (~ every 1s)
                now = time.time()
                if now - last_save > 1.0:
                    last_save = now
                    try:
                        save_task(merge_remote_task_for_persist(REMOTE_TASKS.get(task_id)))
                    except Exception:
                        pass

        os.replace(temp_path, target)
        with REMOTE_TASKS_LOCK:
            task = REMOTE_TASKS.get(task_id)
            if task:
                task['loaded'] = os.path.getsize(target)
                task['size'] = task['loaded'] if not task.get('size') else task['size']
                task['status'] = 'done'
                task['finishedAt'] = time.time()
                task['lastBps'] = 0
                task['error'] = ''
                remote_task_speed_update(task, task['finishedAt'], force=True)
        save_task(merge_remote_task_for_persist(REMOTE_TASKS.get(task_id)))
    except InterruptedError as e:
        msg = str(e)
        is_pause = (msg == 'paused')
        # Keep .part file for both pause and cancel-with-resume; only remove on hard cancel
        with REMOTE_TASKS_LOCK:
            task = REMOTE_TASKS.get(task_id)
            if task:
                if is_pause:
                    task['status'] = 'paused'
                    task['pauseRequested'] = False
                    task['lastBps'] = 0
                    task['error'] = ''
                else:
                    # Hard cancel: remove .part
                    if temp_path and os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except OSError:
                            pass
                    task['status'] = 'canceled'
                    task['lastBps'] = 0
                    task['error'] = msg
                task['finishedAt'] = time.time()
        save_task(merge_remote_task_for_persist(REMOTE_TASKS.get(task_id)))
    except Exception as e:
        # Keep .part for resume on transient errors
        with REMOTE_TASKS_LOCK:
            task = REMOTE_TASKS.get(task_id)
            if task:
                task['status'] = 'error'
                task['finishedAt'] = time.time()
                task['lastBps'] = 0
                task['error'] = str(e)
        save_task(merge_remote_task_for_persist(REMOTE_TASKS.get(task_id)))
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


def merge_remote_task_for_persist(task):
    """Convert a live remote task (with private fields) into a persistable dict."""
    if not task:
        return None
    return task_snapshot(task)


def start_remote_worker(task_id):
    """Launch the download worker thread for an existing task (resume/start)."""
    t = threading.Thread(target=remote_download_worker, args=(task_id,), daemon=True)
    t.start()


def resume_persisted_remote_tasks():
    """On server boot, re-launch workers for unfinished remote tasks."""
    try:
        tasks = load_persisted_tasks()
    except Exception:
        return
    for task_id, snap in list(tasks.items()):
        if not snap or snap.get('kind') != 'remote':
            continue
        status = snap.get('status')
        if status not in ('downloading', 'queued', 'paused'):
            continue
        # Rebuild in-memory task from snapshot
        task = {
            'id': snap.get('id') or task_id,
            'url': snap.get('url') or '',
            'name': snap.get('name') or 'remote-download',
            'status': 'queued',
            'size': int(snap.get('size') or 0),
            'loaded': int(snap.get('loaded') or 0),
            'createdAt': float(snap.get('createdAt') or time.time()),
            'startedAt': float(snap.get('startedAt') or 0),
            'finishedAt': 0,
            'path': snap.get('path') or '/' + DOWNLOAD_DIR_NAME,
            'error': '',
            'lastBps': 0,
            'maxBps': float(snap.get('maxBps') or 0),
            'samples': [],
            'lastT': 0,
            'lastBytes': 0,
            'cancel': False,
            'pauseRequested': False,
            'kind': 'remote',
        }
        # Recover tempPath from path
        path_rel = snap.get('path') or ''
        if path_rel and path_rel != '/' + DOWNLOAD_DIR_NAME:
            base = safe_join(ROOT_DIR, path_rel)
            if base:
                task['tempPath'] = base + '.part'
        if not task['url']:
            # Cannot resume without a URL; mark as error
            task['status'] = 'error'
            task['error'] = '重启后缺少 URL，无法继续'
        with REMOTE_TASKS_LOCK:
            REMOTE_TASKS[task_id] = task
        if task['status'] == 'queued':
            start_remote_worker(task_id)


def archive_member_path(name):
    raw = str(name or '').replace('\\', '/')
    if not raw or raw.startswith('/') or raw.startswith('~/'):
        return '' if not raw else None
    if len(raw) >= 2 and raw[1] == ':':
        return None
    parts = []
    for part in raw.split('/'):
        if not part or part == '.':
            continue
        if part == '..' or ':' in part:
            return None
        parts.append(part)
    return '/'.join(parts)


def supported_archive_name(name):
    lower = str(name or '').lower()
    return (
        lower.endswith('.zip') or
        lower.endswith('.tar') or
        lower.endswith('.tar.gz') or
        lower.endswith('.tgz') or
        lower.endswith('.tar.bz2') or
        lower.endswith('.tbz2') or
        lower.endswith('.tar.xz') or
        lower.endswith('.txz')
    )


def create_zip_archive(source_paths, target):
    root_real = os.path.realpath(ROOT_DIR)
    target_real = os.path.realpath(target)
    dirs_seen = set()
    file_count = 0

    def add_dir(zf, arcname):
        arcname = arcname.replace('\\', '/').strip('/')
        if not arcname:
            return
        if not arcname.endswith('/'):
            arcname += '/'
        if arcname in dirs_seen:
            return
        dirs_seen.add(arcname)
        zf.writestr(arcname, b'')

    def can_include(path):
        if os.path.islink(path):
            return False
        real = os.path.realpath(path)
        if real == target_real:
            return False
        return real == root_real or real.startswith(root_real + os.sep)

    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for source in source_paths:
            source = os.path.realpath(source)
            base = os.path.basename(source.rstrip(os.sep)) or 'root'
            if os.path.isdir(source):
                add_dir(zf, base)
                for dirpath, dirnames, filenames in os.walk(source):
                    dirnames[:] = [
                        d for d in dirnames
                        if can_include(os.path.join(dirpath, d))
                    ]
                    rel_dir = os.path.relpath(dirpath, source)
                    arc_dir = base if rel_dir == '.' else f'{base}/{rel_dir.replace(os.sep, "/")}'
                    add_dir(zf, arc_dir)
                    for filename in filenames:
                        fp = os.path.join(dirpath, filename)
                        if not can_include(fp):
                            continue
                        rel_file = filename if rel_dir == '.' else f'{rel_dir.replace(os.sep, "/")}/{filename}'
                        zf.write(fp, f'{base}/{rel_file}'.replace('\\', '/'))
                        file_count += 1
            elif can_include(source):
                zf.write(source, base)
                file_count += 1
    return file_count


def extract_zip_archive(archive, dest):
    count = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            rel = archive_member_path(info.filename)
            if rel is None:
                raise ValueError('压缩包包含非法路径')
            if not rel:
                continue
            target = safe_join(dest, rel)
            if not target:
                raise ValueError('压缩包包含非法路径')
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            count += 1
    return count


def extract_tar_archive(archive, dest):
    count = 0
    with tarfile.open(archive, 'r:*') as tf:
        for member in tf.getmembers():
            rel = archive_member_path(member.name)
            if rel is None:
                raise ValueError('压缩包包含非法路径')
            if not rel:
                continue
            target = safe_join(dest, rel)
            if not target:
                raise ValueError('压缩包包含非法路径')
            if member.isdir():
                os.makedirs(target, exist_ok=True)
            elif member.issym() or member.islnk():
                continue  # 跳过符号链接，防止路径穿越
            elif member.isfile():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src, open(target, 'wb') as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                count += 1
    return count


def extract_archive(archive, dest):
    lower = archive.lower()
    if lower.endswith('.zip'):
        return extract_zip_archive(archive, dest)
    if supported_archive_name(lower):
        return extract_tar_archive(archive, dest)
    raise ValueError('仅支持解压 zip、tar、tar.gz、tgz、tar.bz2、tbz2、tar.xz、txz')


# ============================================================
# WebDAV XML Helpers
# ============================================================
ET.register_namespace('D', 'DAV:')
DAV = 'DAV:'


def dav_tag(name):
    return f'{{{DAV}}}{name}'


def build_propfind_response(entries):
    """Build a PROPFIND 207 Multi-Status response XML."""
    ms = ET.Element(dav_tag('multistatus'))
    for entry in entries:
        resp = ET.SubElement(ms, dav_tag('response'))
        href_el = ET.SubElement(resp, dav_tag('href'))
        href_el.text = entry['href']

        ps = ET.SubElement(resp, dav_tag('propstat'))
        prop = ET.SubElement(ps, dav_tag('prop'))

        # resourcetype
        rt = ET.SubElement(prop, dav_tag('resourcetype'))
        if entry['is_dir']:
            ET.SubElement(rt, dav_tag('collection'))

        # displayname
        dn = ET.SubElement(prop, dav_tag('displayname'))
        dn.text = entry['name']

        # getlastmodified
        glm = ET.SubElement(prop, dav_tag('getlastmodified'))
        glm.text = entry['lastmodified']

        # creationdate
        cd = ET.SubElement(prop, dav_tag('creationdate'))
        cd.text = entry['creationdate']

        if not entry['is_dir']:
            # getcontentlength
            gcl = ET.SubElement(prop, dav_tag('getcontentlength'))
            gcl.text = str(entry['size'])
            # getcontenttype
            gct = ET.SubElement(prop, dav_tag('getcontenttype'))
            gct.text = entry['contenttype']

        # getetag
        etag_el = ET.SubElement(prop, dav_tag('getetag'))
        etag_el.text = entry.get('etag', '"0"')

        status = ET.SubElement(ps, dav_tag('status'))
        status.text = 'HTTP/1.1 200 OK'

    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(ms, encoding='utf-8')


def make_dav_entry(href, full_path, name):
    """Create a WebDAV entry dict from a filesystem path."""
    try:
        st = os.stat(full_path)
    except OSError:
        return None
    is_dir = os.path.isdir(full_path)
    etag = f'"{int(st.st_mtime)}-{st.st_size}"'
    return {
        'href': href,
        'name': name,
        'is_dir': is_dir,
        'size': 0 if is_dir else st.st_size,
        'lastmodified': rfc1123_date(st.st_mtime),
        'creationdate': iso8601_date(getattr(st, 'st_birthtime', st.st_ctime)),
        'contenttype': 'httpd/unix-directory' if is_dir else guess_mime(full_path),
        'etag': etag,
    }


# ============================================================
# HTTP Request Handler
# ============================================================
class FileManagerHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'WebDAV-FileManager/1.0'

    def log_message(self, fmt, *args):
        """Minimal logging - only log requests with status info.

        路径里含机密文件名和分享 token（/s/<token>），日志会进 systemd journal，
        所以只记录路径前缀和查询串的键名，不记具体值。
        """
        ts = self.log_date_time_string()
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            # /s/<token> 这类分享链接只保留前缀
            if path.startswith('/s/'):
                path = '/s/<token>'
            elif len(path) > 80:
                path = path[:80] + '...'
            if parsed.query:
                keys = sorted({
                    kv.split('=', 1)[0] for kv in parsed.query.split('&') if kv
                })
                path = f'{path}?{"&".join(keys)}=<redacted>'
        except Exception:
            path = '<unparsable>'
        sys.stderr.write(f'[{ts}] {self.command} {path} - {fmt % args}\n')

    # ----------------------------------------------------------
    # Authentication
    # ----------------------------------------------------------
    def _auth_parts(self):
        if not AUTH_CRED:
            return None, None
        user, sep, password = AUTH_CRED.partition(':')
        if not sep:
            return AUTH_CRED, ''
        return user, password

    def _safe_equal(self, a, b):
        return hmac.compare_digest(a.encode('utf-8'), b.encode('utf-8'))

    def _basic_auth_valid(self):
        if not AUTH_CRED:
            return True
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Basic '):
            return False
        # Basic 认证此前完全不限速：限速只作用于 /api/auth/login，
        # 直接对 /dav/ 或 /api/list 打 Basic 头可以无限次爆破口令，
        # 连伪造 X-Forwarded-For 都不需要。
        client_ip = self._get_client_ip()
        if not self._check_login_rate(client_ip):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode('utf-8')
            if self._safe_equal(decoded, AUTH_CRED):
                self._clear_login_fails(client_ip)
                return True
        except Exception:
            pass
        self._record_login_fail(client_ip)
        return False

    def _session_token(self):
        raw_cookie = self.headers.get('Cookie', '')
        if not raw_cookie:
            return None
        jar = cookies.SimpleCookie()
        try:
            jar.load(raw_cookie)
        except cookies.CookieError:
            return None
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _clear_expired_sessions(self):
        now = time.time()
        with SESSIONS_LOCK:
            for token, expires_at in list(SESSIONS.items()):
                if expires_at <= now:
                    SESSIONS.pop(token, None)
            # Enforce session count cap: evict oldest if over limit
            if len(SESSIONS) > MAX_SESSIONS:
                sorted_tokens = sorted(SESSIONS.items(), key=lambda x: x[1])
                excess = len(SESSIONS) - MAX_SESSIONS
                for token, _ in sorted_tokens[:excess]:
                    SESSIONS.pop(token, None)

    def _session_valid(self):
        if not AUTH_CRED:
            return True
        token = self._session_token()
        if not token:
            return False
        with SESSIONS_LOCK:
            expires_at = SESSIONS.get(token)
            if not expires_at:
                return False
            if expires_at <= time.time():
                SESSIONS.pop(token, None)
                return False
            return True

    def _request_proto(self):
        """Detect the effective external scheme (http/https), honoring reverse-proxy headers.

        The backend only listens on plain HTTP, so a real HTTPS deployment terminates
        TLS at a reverse proxy that forwards X-Forwarded-Proto / Forwarded. Direct HTTP
        access reports 'http' and must NOT receive a Secure cookie, or the browser drops
        it and the web login loops forever.
        """
        forwarded = forwarded_header_params(self.headers.get('Forwarded', ''))
        proto = (
            normalize_external_proto(forwarded.get('proto'))
            or normalize_external_proto(self.headers.get('X-Forwarded-Proto'))
            or normalize_external_proto(self.headers.get('X-Url-Scheme'))
        )
        if not proto:
            ssl_hint = clean_forwarded_value(self.headers.get('X-Forwarded-Ssl')).lower()
            proto = 'https' if ssl_hint == 'on' else 'http'
        return proto

    def _new_session_cookie(self):
        self._clear_expired_sessions()
        token = secrets.token_urlsafe(32)
        with SESSIONS_LOCK:
            SESSIONS[token] = time.time() + SESSION_MAX_AGE
        secure = '; Secure' if self._request_proto() == 'https' else ''
        return f'{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_MAX_AGE}; HttpOnly{secure}; SameSite=Lax'

    def _expired_session_cookie(self):
        token = self._session_token()
        if token:
            with SESSIONS_LOCK:
                SESSIONS.pop(token, None)
        return f'{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax'

    def check_web_auth(self):
        if not AUTH_CRED or self._session_valid() or self._basic_auth_valid():
            return True
        self.send_err(401, '未登录或登录已过期')
        return False

    def check_webdav_auth(self):
        if self._basic_auth_valid():
            return True
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="FileManager"')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return False

    # ----------------------------------------------------------
    # Response Helpers
    # ----------------------------------------------------------
    def send_json(self, obj, status=200, headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(data))
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def send_ok(self, msg='OK'):
        self.send_json({'success': True, 'message': msg})

    def send_err(self, status, msg):
        self.send_json({'error': msg}, status)

    def send_empty(self, status):
        self.send_response(status)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def read_body(self):
        n = int(self.headers.get('Content-Length', 0))
        return self.rfile.read(n) if n > 0 else b''

    def read_json(self):
        try:
            return json.loads(self.read_body().decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self.send_error(400, f'Invalid JSON: {e}')

    # ----------------------------------------------------------
    # Routing
    # ----------------------------------------------------------
    def _route(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if path.startswith('/api/auth/'):
            handler = getattr(self, f'_api_auth_{method}', None)
            if handler:
                handler(path, qs)
            else:
                self.send_err(405, 'Method Not Allowed')
        elif method in ('GET', 'HEAD') and path.startswith('/s/'):
            self._public_share_download(path, qs, head_only=(method == 'HEAD'))
        elif method == 'GET' and path in ('/', '/index.html'):
            self._serve_frontend()
        elif path == '/dav' or path.startswith('/dav/'):
            if not self.check_webdav_auth():
                return
            handler = getattr(self, f'_webdav_{method}', None)
            if handler:
                handler(path)
            else:
                self.send_err(405, 'Method Not Allowed')
        elif path.startswith('/api/'):
            if not self.check_web_auth():
                return
            handler = getattr(self, f'_api_{method}', None)
            if handler:
                handler(path, qs)
            else:
                self.send_err(405, 'Method Not Allowed')
        else:
            self.send_err(404, 'Not Found')

    def do_GET(self):      self._route('GET')
    def do_HEAD(self):     self._route('HEAD')
    def do_POST(self):     self._route('POST')
    def do_PUT(self):      self._route('PUT')
    def do_DELETE(self):   self._route('DELETE')
    def do_PROPFIND(self): self._route('PROPFIND')
    def do_PROPPATCH(self):self._route('PROPPATCH')
    def do_MKCOL(self):    self._route('MKCOL')
    def do_COPY(self):     self._route('COPY')
    def do_MOVE(self):     self._route('MOVE')
    def do_LOCK(self):     self._route('LOCK')
    def do_UNLOCK(self):   self._route('UNLOCK')

    def do_OPTIONS(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        is_dav = path == '/dav' or path.startswith('/dav/')
        if is_dav and not self.check_webdav_auth():
            return
        self.send_response(200)
        self.send_header('Allow', 'OPTIONS,GET,HEAD,POST,PUT,DELETE,PROPFIND,PROPPATCH,MKCOL,COPY,MOVE,LOCK,UNLOCK')
        self.send_header('DAV', '1, 2')
        self.send_header('MS-Author-Via', 'DAV')
        # Only set permissive CORS on WebDAV paths (needed by WebDAV clients)
        if is_dav:
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'OPTIONS,GET,HEAD,POST,PUT,DELETE,PROPFIND,PROPPATCH,MKCOL,COPY,MOVE')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, Depth, Destination, Overwrite')
        self.send_header('Content-Length', '0')
        self.end_headers()

    # ----------------------------------------------------------
    # Serve Frontend
    # ----------------------------------------------------------
    def _serve_frontend(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        html_path = os.path.join(script_dir, 'index.html')
        if not os.path.isfile(html_path):
            self.send_err(404, 'index.html not found')
            return
        with open(html_path, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(data))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    # ==========================================================
    # Web Login API
    # ==========================================================
    def _api_auth_GET(self, path, qs):
        if path != '/api/auth/status':
            return self.send_err(404, 'API not found')
        self._clear_expired_sessions()
        username, _ = self._auth_parts()
        self.send_json({
            'authRequired': bool(AUTH_CRED),
            'authenticated': self._session_valid(),
            'username': username or '',
        })

    def _api_auth_POST(self, path, qs):
        if path in ('/api/auth/change-account', '/api/auth/change-password'):
            return self._api_auth_change_account()
        if path == '/api/auth/login':
            return self._api_auth_login()
        if path == '/api/auth/logout':
            return self._api_auth_logout()
        self.send_err(404, 'API not found')

    def _get_client_ip(self):
        """取客户端 IP 用于限速。

        只有显式配置了可信代理时才认 X-Forwarded-For：这个头由客户端提供，
        攻击者每次换一个伪造值就能让每个"IP"都拿到独立的失败计数，
        5 次/60 秒的锁定形同虚设。
        """
        peer = self.client_address[0] if self.client_address else '0.0.0.0'
        if not TRUSTED_PROXIES:
            return peer
        if peer not in TRUSTED_PROXIES:
            # 直连来源不可信，忽略它声称的转发链
            return peer
        forwarded = self.headers.get('X-Forwarded-For', '')
        if forwarded:
            # 取最右一个非可信跳，即可信代理实际看到的对端
            hops = [h.strip() for h in forwarded.split(',') if h.strip()]
            for hop in reversed(hops):
                if hop not in TRUSTED_PROXIES:
                    return hop
        return peer

    def _check_login_rate(self, ip):
        """Return True if login is allowed, False if rate-limited."""
        now = time.time()
        with _login_attempts_lock:
            rec = _login_attempts.get(ip)
            if not rec:
                return True
            if rec['locked_until'] > now:
                return False
            if rec['fails'] >= LOGIN_MAX_FAILS:
                rec['locked_until'] = now + LOGIN_LOCKOUT_SECONDS
                return False
            return True

    def _record_login_fail(self, ip):
        now = time.time()
        with _login_attempts_lock:
            rec = _login_attempts.setdefault(ip, {'fails': 0, 'locked_until': 0})
            rec['fails'] += 1
            if rec['fails'] >= LOGIN_MAX_FAILS:
                rec['locked_until'] = now + LOGIN_LOCKOUT_SECONDS

    def _clear_login_fails(self, ip):
        with _login_attempts_lock:
            _login_attempts.pop(ip, None)

    def _api_auth_login(self):
        if not AUTH_CRED:
            return self.send_json({'success': True, 'authenticated': True})
        client_ip = self._get_client_ip()
        if not self._check_login_rate(client_ip):
            return self.send_err(429, '登录尝试过于频繁，请稍后再试')
        try:
            data = self.read_json()
        except Exception:
            return self.send_err(400, '请求格式不正确')
        username = str(data.get('username', ''))
        password = str(data.get('password', ''))
        expected_user, expected_password = self._auth_parts()
        if (self._safe_equal(username, expected_user) and
                self._safe_equal(password, expected_password)):
            self._clear_login_fails(client_ip)
            self.send_json(
                {'success': True, 'authenticated': True},
                headers={'Set-Cookie': self._new_session_cookie()},
            )
        else:
            self._record_login_fail(client_ip)
            self.send_err(401, '账号或密码错误')

    def _api_auth_logout(self):
        self.send_json(
            {'success': True, 'authenticated': False},
            headers={'Set-Cookie': self._expired_session_cookie()},
        )

    def _api_auth_change_account(self):
        global AUTH_CRED
        if not AUTH_CRED:
            return self.send_err(400, '当前未启用登录认证')
        if not self._session_valid():
            return self.send_err(401, '登录已过期，请重新登录')
        try:
            data = self.read_json()
        except Exception:
            return self.send_err(400, '请求格式不正确')
        old_password = str(data.get('oldPassword', ''))
        username, expected_password = self._auth_parts()
        if not self._safe_equal(old_password, expected_password):
            return self.send_err(401, '当前密码错误')

        new_username = str(data.get('newUsername', data.get('username', username))).strip()
        if not valid_login_username(new_username):
            return self.send_err(400, '登录账号不能为空，且不能包含冒号或控制字符')

        new_password = str(data.get('newPassword', ''))
        password = expected_password
        if new_password:
            if len(new_password) < 6:
                return self.send_err(400, '新密码至少 6 位')
            password = new_password

        AUTH_CRED = f'{new_username}:{password}'
        try:
            save_auth_cred(AUTH_CRED)
        except OSError as e:
            return self.send_err(500, f'保存账号设置失败: {self._sanitize_error(e)}')
        with SESSIONS_LOCK:
            SESSIONS.clear()
        self.send_json(
            {'success': True, 'authenticated': False, 'username': new_username},
            headers={'Set-Cookie': self._expired_session_cookie()},
        )

    # ==========================================================
    # REST API - GET
    # ==========================================================
    def _api_GET(self, path, qs):
        routes = {
            '/api/list': self._api_list,
            '/api/disk': self._api_disk,
            '/api/download': self._api_download,
            '/api/fileinfo': self._api_fileinfo,
            '/api/text': self._api_text_get,
            '/api/shares': self._api_shares,
            '/api/share-detail': self._api_share_detail,
            '/api/remote-downloads': self._api_remote_downloads,
            '/api/tasks': self._api_tasks,
            '/api/tasks-trash': self._api_tasks_trash,
            '/api/pinned': self._api_pinned,
        }
        handler = routes.get(path)
        if handler:
            handler(qs)
        else:
            self.send_err(404, 'API not found')

    def _client_ip(self):
        forwarded = self.headers.get('X-Forwarded-For', '')
        if forwarded:
            return forwarded.split(',', 1)[0].strip() or self.client_address[0]
        real_ip = self.headers.get('X-Real-IP', '').strip()
        return real_ip or self.client_address[0]

    def _record_share_access(self, state, token, meta, full, length):
        now = time.time()
        ip = self._client_ip()
        ua = self.headers.get('User-Agent', '')[:240]
        file_size = os.path.getsize(full) if full and os.path.isfile(full) else 0
        stats = meta.setdefault('accessStats', {})
        item = stats.setdefault(ip, {
            'ip': ip,
            'count': 0,
            'bytes': 0,
            'firstAt': now,
            'lastAt': 0,
            'lastCountedAt': 0,
            'sessionBytes': 0,
            'userAgent': '',
        })
        # 会话判定：同一 IP 距上次请求超过阈值，或换了一个新文件，算一次新下载。
        gap = now - float(item.get('lastCountedAt') or 0)
        new_session = gap > SHARE_SESSION_GAP or item.get('fileSize') != file_size
        item['count'] = int(item.get('count') or 0) + (1 if new_session else 0)
        item['bytes'] = int(item.get('bytes') or 0) + int(length or 0)
        item['lastAt'] = now
        item['lastCountedAt'] = now
        item['fileSize'] = file_size
        item['userAgent'] = ua
        if new_session:
            item['sessionBytes'] = int(length or 0)
        else:
            item['sessionBytes'] = int(item.get('sessionBytes') or 0) + int(length or 0)
        # 整体次数同样按会话计；流量按实际传输字节累加。
        if new_session:
            meta['downloadCount'] = int(meta.get('downloadCount') or 0) + 1
        meta['downloadBytes'] = int(meta.get('downloadBytes') or 0) + int(length or 0)
        meta['lastAccessAt'] = now
        save_state(state)

    def _share_token_from_path(self, path):
        tail = path[len('/s/'):].strip('/') if path.startswith('/s/') else ''
        return tail.split('/', 1)[0] if tail else ''

    def _public_share_download(self, path, qs, head_only=False):
        cleanup_expired_temp_files()
        token = self._share_token_from_path(path)
        if not token:
            return self.send_err(404, '链接不存在或已过期')
        state = load_state()
        meta = state.get('shares', {}).get(token)
        if not meta:
            return self.send_err(404, '链接不存在或已过期')
        if float(meta.get('expiresAt') or 0) <= time.time():
            state['shares'].pop(token, None)
            save_state(state)
            return self.send_err(404, '链接已过期')
        full = safe_join(ROOT_DIR, meta.get('path', ''))
        if not full or not os.path.isfile(full):
            return self.send_err(404, '文件不存在')
        # 先解析 Range 得到本次实际要传输的字节数，用于精确统计流量；416 不计入。
        length = 0
        if not head_only:
            resolved = self._resolve_range(os.path.getsize(full))
            if resolved is not None:
                start, end, _ = resolved
                length = max(0, end - start + 1)
                self._record_share_access(state, token, meta, full, length)
        self._stream_file(full, head_only=head_only)

    def _api_list(self, qs):
        cleanup_expired_temp_files()
        req_path = qs.get('path', ['/'])[0]
        full = safe_join(ROOT_DIR, req_path)
        if not full or not os.path.isdir(full):
            return self.send_err(400, '目录不存在')
        items = []
        try:
            for name in os.listdir(full):
                fp = os.path.join(full, name)
                rp = (req_path.rstrip('/') + '/' + name) if req_path != '/' else '/' + name
                info = get_file_info(ROOT_DIR, rp, fp)
                if info:
                    items.append(info)
        except PermissionError:
            return self.send_err(403, '无权限访问此目录')
        self.send_json({'path': req_path, 'items': items})

    def _api_disk(self, qs=None):
        cleanup_expired_temp_files()
        _ensure_root_size_worker()
        u = shutil.disk_usage(ROOT_DIR)
        # ?refresh=1 triggers a synchronous recompute so the caller (e.g. after
        # delete/upload/move) sees up-to-date numbers immediately instead of the
        # up-to-60s-old background cache value.
        refresh = False
        if qs:
            refresh = (qs.get('refresh', [''])[0] in ('1', 'true', 'yes'))
        if refresh:
            logical = _calculate_root_logical_size()
            allocated = _calculate_root_allocated_size()
            with _root_size_cache['lock']:
                _root_size_cache['logical'] = logical
                _root_size_cache['allocated'] = allocated
                _root_size_cache['size'] = allocated
                _root_size_cache['updated'] = time.time()
        with _root_size_cache['lock']:
            root_size = int(_root_size_cache.get('logical') or _root_size_cache.get('size') or 0)
            root_allocated_size = int(_root_size_cache.get('allocated') or _root_size_cache.get('size') or 0)
            root_updated = float(_root_size_cache.get('updated') or 0)
        self.send_json({
            'total': u.total,
            'used': u.used,
            'free': u.free,
            'rootSize': root_size,
            'rootAllocatedSize': root_allocated_size,
            'rootSizeUpdatedAt': root_updated,
            'rootPath': ROOT_DIR,
            'totalStr': format_size(u.total),
            'usedStr': format_size(u.used),
            'freeStr': format_size(u.free),
            'rootSizeStr': format_size(root_size),
            'rootAllocatedSizeStr': format_size(root_allocated_size),
            'percent': round(u.used / u.total * 100, 1) if u.total else 0,
        })

    def _api_fileinfo(self, qs):
        cleanup_expired_temp_files()
        req_path = qs.get('path', [''])[0]
        full = safe_join(ROOT_DIR, req_path)
        if not full or not os.path.exists(full):
            return self.send_err(404, '项目不存在')
        info = get_file_info(ROOT_DIR, req_path, full)
        if info:
            # Expose the absolute path on the server filesystem (VPS real path)
            info['realPath'] = full
            if info.get('isDir'):
                info['size'] = calculate_logical_size(full)
                info['sizeStr'] = format_size(info['size'])
        self.send_json(info)

    def _api_text_get(self, qs):
        req_path = qs.get('path', [''])[0]
        full = safe_join(ROOT_DIR, req_path)
        if not full or not os.path.isfile(full):
            return self.send_err(404, '文件不存在')
        try:
            content = read_text_file(full)
        except UnicodeDecodeError:
            return self.send_err(400, '文件不是 UTF-8 文本，不能在网页内编辑')
        except ValueError as e:
            return self.send_err(400, str(e))
        except OSError as e:
            return self.send_err(500, f'读取失败: {self._sanitize_error(e)}')
        info = get_file_info(ROOT_DIR, req_path, full)
        self.send_json({'success': True, 'file': info, 'content': content})

    def _share_item(self, token, meta, trashed=False):
        req_path = meta.get('path', '')
        full = safe_join(ROOT_DIR, req_path)
        exists = bool(full and os.path.isfile(full))
        size = os.path.getsize(full) if exists else int(meta.get('size') or 0)
        expires_at = float(meta.get('expiresAt') or 0)
        stats = meta.get('accessStats') or {}
        unique_ips = len(stats)
        downloads = int(meta.get('downloadCount') or sum(int(v.get('count') or 0) for v in stats.values()))
        name = os.path.basename(req_path.rstrip('/')) or req_path
        return {
            'token': token,
            'path': req_path,
            'name': name,
            'size': size,
            'exists': exists,
            'createdAt': float(meta.get('createdAt') or 0),
            'expiresAt': expires_at,
            'remaining': max(0, int(expires_at - time.time())) if expires_at and not trashed else 0,
            'deletedAt': float(meta.get('deletedAt') or 0),
            'lastAccessAt': float(meta.get('lastAccessAt') or 0),
            'downloadCount': downloads,
            'downloadBytes': int(meta.get('downloadBytes') or 0),
            'uniqueIpCount': unique_ips,
            'url': self._share_url(token),
            'trashed': trashed,
        }

    def _share_detail_payload(self, token, meta, trashed=False):
        item = self._share_item(token, meta, trashed)
        stats = []
        total_count = 0
        total_bytes = 0
        for row in (meta.get('accessStats') or {}).values():
            total_count += int(row.get('count') or 0)
            total_bytes += int(row.get('bytes') or 0)
        for ip, row in (meta.get('accessStats') or {}).items():
            count = int(row.get('count') or 0)
            byte_count = int(row.get('bytes') or 0)
            stats.append({
                'ip': ip,
                'count': count,
                'bytes': byte_count,
                'avgBytes': int(byte_count / count) if count else 0,
                'countPercent': round(count / total_count * 100, 1) if total_count else 0,
                'bytePercent': round(byte_count / total_bytes * 100, 1) if total_bytes else 0,
                'firstAt': float(row.get('firstAt') or 0),
                'lastAt': float(row.get('lastAt') or 0),
                'userAgent': row.get('userAgent') or '',
            })
        stats.sort(key=lambda x: x['lastAt'], reverse=True)
        item['ipStats'] = stats
        return item

    def _api_shares(self, qs):
        cleanup_expired_temp_files()
        state = load_state()
        items = []
        for token, meta in state.get('shares', {}).items():
            req_path = meta.get('path', '')
            full = safe_join(ROOT_DIR, req_path)
            exists = bool(full and os.path.isfile(full))
            if exists and not meta.get('size'):
                meta['size'] = os.path.getsize(full)
            items.append(self._share_item(token, meta, False))
        trash = [self._share_item(token, meta, True) for token, meta in state.get('shareTrash', {}).items()]
        items.sort(key=lambda x: x['expiresAt'] or 0)
        trash.sort(key=lambda x: x['deletedAt'] or 0, reverse=True)
        self.send_json({'success': True, 'items': items, 'trash': trash})

    def _api_share_detail(self, qs):
        token = str(qs.get('token', [''])[0]).strip()
        in_trash = str(qs.get('trash', ['0'])[0]).lower() in ('1', 'true', 'yes')
        if not token:
            return self.send_err(400, '缺少链接 token')
        state = load_state()
        source = state.get('shareTrash' if in_trash else 'shares', {})
        meta = source.get(token)
        if not meta:
            return self.send_err(404, '链接不存在')
        self.send_json({'success': True, 'item': self._share_detail_payload(token, meta, in_trash)})

    def _api_remote_downloads(self, qs=None):
        with REMOTE_TASKS_LOCK:
            items = [remote_task_snapshot(task) for task in REMOTE_TASKS.values()]
        items.sort(key=lambda x: x.get('createdAt') or 0, reverse=True)
        self.send_json({'success': True, 'items': items})

    def _api_tasks(self, qs=None):
        """Unified task list: merge live remote tasks + persisted upload/download records.

        Live remote tasks take precedence over stale persisted snapshots.
        Tasks present in taskTrash are always excluded (prevents resurrected tasks
        from appearing after a soft-delete).
        Returns {success, items} sorted by createdAt desc.
        """
        state = load_state()
        persisted = state.get('tasks', {}) or {}
        trash_ids = set(state.get('taskTrash', {}).keys())
        items_by_id = {}
        # Start from persisted snapshots (covers upload/download history + remote)
        for task_id, snap in persisted.items():
            if task_id in trash_ids:
                continue
            snap = dict(snap or {})
            snap.setdefault('id', task_id)
            snap.setdefault('kind', 'remote')
            items_by_id[task_id] = snap
        # Live remote tasks override their persisted counterparts (fresher progress)
        with REMOTE_TASKS_LOCK:
            for task_id, task in REMOTE_TASKS.items():
                if task_id in trash_ids:
                    continue
                items_by_id[task_id] = remote_task_snapshot(task)
        items = list(items_by_id.values())
        items.sort(key=lambda x: x.get('createdAt') or 0, reverse=True)
        self.send_json({'success': True, 'items': items})

    def _external_origin(self):
        forwarded = forwarded_header_params(self.headers.get('Forwarded', ''))
        proto = self._request_proto()
        host = normalize_external_host(
            forwarded.get('host') or self.headers.get('X-Forwarded-Host') or self.headers.get('Host'),
            self.headers.get('X-Forwarded-Port'),
        )
        return f'{proto}://{host}' if host else ''

    def _share_url(self, token, filename=None):
        suffix = ''
        if filename:
            suffix = '/' + urllib.parse.quote(filename, safe='')
        origin = self._external_origin()
        return f'{origin}/s/{token}{suffix}' if origin else f'/s/{token}{suffix}'

    def _api_download(self, qs):
        cleanup_expired_temp_files()
        req_path = qs.get('path', [''])[0]
        full = safe_join(ROOT_DIR, req_path)
        if not full or not os.path.isfile(full):
            return self.send_err(404, '文件不存在')
        self._stream_file(full)

    def _resolve_range(self, file_size):
        """Parse the Range header for a single byte range.

        Returns (start, end, status):
          - (0, file_size-1, 200) when there is no usable Range header;
          - (start, end, 206) for a valid Range request.
        Returns None when the range is unsatisfiable (caller should send 416).
        """
        start, end, status = 0, file_size - 1, 200
        range_header = self.headers.get('Range', '')
        if range_header.startswith('bytes='):
            spec = range_header[6:].split(',', 1)[0].strip()
            if '-' in spec:
                left, right = spec.split('-', 1)
                try:
                    if left == '':
                        suffix = max(0, int(right))
                        start = max(0, file_size - suffix)
                    else:
                        start = int(left)
                        if right:
                            end = min(file_size - 1, int(right))
                    if start > end or start >= file_size:
                        return None
                    status = 206
                except ValueError:
                    start, end, status = 0, file_size - 1, 200
        return start, end, status

    def _stream_file(self, full, head_only=False):
        file_size = os.path.getsize(full)
        filename = os.path.basename(full)
        resolved = self._resolve_range(file_size)
        if resolved is None:
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{file_size}')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        start, end, status = resolved
        length = max(0, end - start + 1)
        st = os.stat(full)
        self.send_response(status)
        self.send_header('Content-Type', guess_mime(full))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
        self.send_header('Content-Disposition', build_content_disposition(filename))
        self.send_header('Content-Transfer-Encoding', 'binary')
        self.send_header('Last-Modified', rfc1123_date(st.st_mtime))
        self.send_header('ETag', f'"{int(st.st_mtime)}-{file_size}"')
        self.send_header('Cache-Control', 'private, no-transform')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if head_only:
            return
        with open(full, 'rb') as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    # ==========================================================
    # REST API - POST
    # ==========================================================
    def _api_POST(self, path, qs):
        routes = {
            '/api/upload': lambda: self._api_upload(qs),
            '/api/upload-init': self._api_upload_init,
            '/api/upload-chunk': lambda: self._api_upload_chunk(qs),
            '/api/upload-complete': self._api_upload_complete,
            '/api/upload-cancel': self._api_upload_cancel,
            '/api/mkdir': self._api_mkdir,
            '/api/create-text': self._api_create_text,
            '/api/create-speed-file': self._api_create_speed_file,
            '/api/share': self._api_share,
            '/api/share-delete': self._api_share_delete,
            '/api/share-restore': self._api_share_restore,
            '/api/share-purge': self._api_share_purge,
            '/api/share-trash-clear': self._api_share_trash_clear,
            '/api/remote-download': self._api_remote_download_start,
            '/api/remote-download-cancel': self._api_remote_download_cancel,
            '/api/remote-download-resume': self._api_remote_download_resume,
            '/api/remote-download-delete': self._api_remote_download_delete,
            '/api/tasks-delete': self._api_tasks_delete,
            '/api/save-text': self._api_save_text,
            '/api/delete': self._api_delete,
            '/api/rename': self._api_rename,
            '/api/copy': self._api_copy,
            '/api/move': self._api_move,
            '/api/archive': self._api_archive,
            '/api/extract': self._api_extract,
            '/api/tasks-restore': self._api_tasks_restore,
            '/api/pinned': self._api_pinned,
        }
        handler = routes.get(path)
        if handler:
            if not self._check_csrf():
                return
            handler()
        else:
            self.send_err(404, 'API not found')

    def _check_csrf(self):
        """同源校验，防跨站请求伪造。

        这些接口全部靠 Cookie 认证，而 SameSite=Lax 并不拦截顶层表单 POST，
        且处理器只调 read_json() 从不校验 Content-Type——任意站点用一个
        enctype="text/plain" 的表单就能在用户登录状态下删掉整个根目录。
        """
        origin = (self.headers.get('Origin') or '').strip()
        referer = (self.headers.get('Referer') or '').strip()
        source = origin or referer
        if not source:
            # 浏览器发起的跨站请求一定带 Origin；两个头都没有时通常是
            # curl/脚本客户端，此时 Cookie 也不会被自动附带，放行。
            return True
        try:
            parsed = urllib.parse.urlparse(source)
        except ValueError:
            self.send_err(403, '来源校验失败')
            return False
        host_header = (self.headers.get('Host') or '').strip()
        if parsed.netloc and host_header and parsed.netloc == host_header:
            return True
        self.send_err(403, '跨站请求被拒绝')
        return False

    def _sanitize_error(self, e):
        """Sanitize error message to avoid leaking internal paths."""
        msg = str(e)
        if ROOT_DIR and ROOT_DIR in msg:
            msg = msg.replace(ROOT_DIR, '<root>')
        return msg

    def _api_upload(self, qs):
        cleanup_upload_batches()
        target_dir = qs.get('path', ['/'])[0]
        raw_filename = qs.get('name', [''])[0]
        raw_relpath = qs.get('relpath', [''])[0]
        batch_id = str(qs.get('batch', [''])[0]).strip()
        if batch_id:
            with UPLOAD_BATCH_DIRS_LOCK:
                UPLOAD_BATCH_UPDATED[batch_id] = time.time()
        if not raw_filename:
            return self.send_err(400, '缺少文件名')
        relpath = safe_upload_relpath(raw_relpath)
        if raw_relpath and not relpath:
            return self.send_err(400, '相对路径无效')
        if relpath:
            filename = os.path.basename(relpath)
        else:
            filename = os.path.basename(raw_filename)
            if not valid_leaf_name(filename):
                return self.send_err(400, '文件名无效')
            relpath = filename
        if not filename or filename.startswith('.'):
            return self.send_err(400, '文件名无效')
        dir_path = safe_join(ROOT_DIR, target_dir)
        if not dir_path or not os.path.isdir(dir_path):
            return self.send_err(400, '目标目录不存在')
        file_path = os.path.realpath(os.path.join(dir_path, *relpath.split('/')))
        # Verify final path is within ROOT_DIR
        root_real = os.path.realpath(ROOT_DIR)
        if not (file_path == root_real or file_path.startswith(root_real + os.sep)):
            return self.send_err(403, '路径非法')
        rel_parts = relpath.split('/')
        top_name = rel_parts[0]
        top_path = os.path.realpath(os.path.join(dir_path, top_name))
        if not (top_path == root_real or top_path.startswith(root_real + os.sep)):
            return self.send_err(403, '路径非法')
        with UPLOAD_BATCH_DIRS_LOCK:
            batch_dirs = UPLOAD_BATCH_DIRS.setdefault(batch_id, set()) if batch_id else set()
            top_created_by_batch = top_path in batch_dirs
        if os.path.exists(top_path) and not top_created_by_batch:
            conflict_type = 'folder' if os.path.isdir(top_path) else 'file'
            return self.send_json({
                'error': '当前目录已存在同名文件或文件夹，请重命名后再上传',
                'conflict': conflict_type,
                'name': top_name,
            }, 409)
        content_length = int(self.headers.get('Content-Length', 0))
        try:
            parent_dirs = rel_parts[:-1]
            current_dir = os.path.realpath(dir_path)
            for part in parent_dirs:
                current_dir = os.path.realpath(os.path.join(current_dir, part))
                if not (current_dir == root_real or current_dir.startswith(root_real + os.sep)):
                    return self.send_err(403, '路径非法')
                if os.path.exists(current_dir):
                    if not os.path.isdir(current_dir):
                        return self.send_err(400, '目标路径已有同名文件，无法创建文件夹')
                else:
                    os.mkdir(current_dir)
                    if batch_id:
                        with UPLOAD_BATCH_DIRS_LOCK:
                            UPLOAD_BATCH_DIRS.setdefault(batch_id, set()).add(current_dir)
                            UPLOAD_BATCH_UPDATED[batch_id] = time.time()
            with open(file_path, 'xb') as f:
                remaining = content_length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            self.send_ok('上传成功')
        except (FileExistsError, IsADirectoryError):
            if os.path.exists(top_path) and not top_created_by_batch:
                conflict_type = 'folder' if os.path.isdir(top_path) else 'file'
                return self.send_json({
                    'error': '当前目录已存在同名文件或文件夹，请重命名后再上传',
                    'conflict': conflict_type,
                    'name': top_name,
                }, 409)
            return self.send_err(400, '上传失败: 目标路径已存在同名项目')
        except OSError as e:
            self.send_err(500, f'上传失败: {self._sanitize_error(e)}')

    # ---- Upload (resumable chunked) ----
    def _api_upload_init(self):
        """Initialize a resumable upload. Returns {taskId, offset}.
        Body: { path, name, relpath, size, batch }
        - If the final target already exists -> 409 conflict.
        - If a .part file exists, offset = its size (resume point).
        """
        cleanup_upload_batches()
        data = self.read_json()
        target_dir = data.get('path', '/')
        raw_filename = str(data.get('name', '')).strip()
        raw_relpath = str(data.get('relpath', '') or '')
        size = int(data.get('size', 0) or 0)
        batch_id = str(data.get('batch', '') or '').strip()
        if batch_id:
            with UPLOAD_BATCH_DIRS_LOCK:
                UPLOAD_BATCH_UPDATED[batch_id] = time.time()
        if not raw_filename:
            return self.send_err(400, '缺少文件名')
        relpath = safe_upload_relpath(raw_relpath) if raw_relpath else raw_filename
        if raw_relpath and not relpath:
            return self.send_err(400, '相对路径无效')
        if not relpath:
            relpath = raw_filename
        filename = os.path.basename(relpath) if '/' in relpath else relpath
        if not filename or not valid_leaf_name(filename):
            # valid_leaf_name rejects dotfiles; allow normal names only
            if not filename or filename.startswith('.') or '/' in filename or '\\' in filename or '..' in filename:
                return self.send_err(400, '文件名无效')
        dir_path = safe_join(ROOT_DIR, target_dir)
        if not dir_path or not os.path.isdir(dir_path):
            return self.send_err(400, '目标目录不存在')
        file_path = os.path.realpath(os.path.join(dir_path, *relpath.split('/')))
        root_real = os.path.realpath(ROOT_DIR)
        if not (file_path == root_real or file_path.startswith(root_real + os.sep)):
            return self.send_err(403, '路径非法')
        # Conflict check: if final file already exists (and not part of current batch), 409
        rel_parts = relpath.split('/')
        top_name = rel_parts[0]
        top_path = os.path.realpath(os.path.join(dir_path, top_name))
        if not (top_path == root_real or top_path.startswith(root_real + os.sep)):
            return self.send_err(403, '路径非法')
        with UPLOAD_BATCH_DIRS_LOCK:
            batch_dirs = UPLOAD_BATCH_DIRS.setdefault(batch_id, set()) if batch_id else set()
            top_created_by_batch = top_path in batch_dirs
        if os.path.exists(top_path) and not top_created_by_batch:
            conflict_type = 'folder' if os.path.isdir(top_path) else 'file'
            return self.send_json({
                'error': '当前目录已存在同名文件或文件夹，请重命名后再上传',
                'conflict': conflict_type,
                'name': top_name,
            }, 409)
        # Ensure parent dirs exist
        try:
            parent_dirs = rel_parts[:-1]
            current_dir = os.path.realpath(dir_path)
            for part in parent_dirs:
                current_dir = os.path.realpath(os.path.join(current_dir, part))
                if not (current_dir == root_real or current_dir.startswith(root_real + os.sep)):
                    return self.send_err(403, '路径非法')
                if os.path.exists(current_dir):
                    if not os.path.isdir(current_dir):
                        return self.send_err(400, '目标路径已有同名文件，无法创建文件夹')
                else:
                    os.mkdir(current_dir)
                    if batch_id:
                        with UPLOAD_BATCH_DIRS_LOCK:
                            UPLOAD_BATCH_DIRS.setdefault(batch_id, set()).add(current_dir)
                            UPLOAD_BATCH_UPDATED[batch_id] = time.time()
        except OSError as e:
            return self.send_err(500, f'创建目录失败: {self._sanitize_error(e)}')
        # Determine resume offset from existing .part
        part_path = file_path + '.part'
        offset = 0
        if os.path.exists(part_path):
            try:
                offset = os.path.getsize(part_path)
            except OSError:
                offset = 0
        # Reuse an existing unfinished upload task for the same target path (avoids
        # leaving zombie 'paused' records each time the user pauses & resumes).
        rel_path = root_relative_path(file_path)
        existing_task_id = None
        for tid_key, snap in load_persisted_tasks().items():
            if (snap.get('kind') == 'upload'
                    and snap.get('path') == rel_path
                    and snap.get('status') in ('queued', 'downloading', 'paused', 'error')):
                existing_task_id = tid_key
                break
        if existing_task_id:
            task_id = existing_task_id
        else:
            task_id = 'up:' + secrets.token_urlsafe(12)
        now = time.time()
        task = {
            'id': task_id,
            'kind': 'upload',
            'name': os.path.basename(file_path),
            'status': 'paused' if offset > 0 else 'queued',
            'size': size,
            'loaded': offset,
            'error': '',
            'url': '',
            'path': rel_path,
            'createdAt': now,
            'startedAt': 0,
            'finishedAt': 0,
            'lastBps': 0,
            'maxBps': 0,
            'samples': [],
        }
        # Persist internal helper fields (not exposed in snapshot but kept in memory)
        save_task(task)
        # Keep an in-memory map for chunk handlers to find paths quickly
        UPLOAD_TASKS[task_id] = {
            'partPath': part_path,
            'finalPath': file_path,
            'size': size,
            'createdAt': now,
            'batchId': batch_id,
        }
        self.send_json({'success': True, 'taskId': task_id, 'offset': offset})

    def _api_upload_chunk(self, qs):
        """Append a binary chunk to the .part file.
        Query: taskId, offset, total
        Body: raw bytes to append.
        """
        task_id = str(qs.get('taskId', [''])[0]).strip()
        try:
            offset = int(qs.get('offset', ['0'])[0])
        except ValueError:
            offset = 0
        if not task_id:
            return self.send_err(400, '缺少 taskId')
        info = UPLOAD_TASKS.get(task_id)
        if not info:
            # Reconstruct from persisted state if possible
            snap = load_persisted_tasks().get(task_id)
            if not snap or snap.get('kind') != 'upload':
                return self.send_err(404, '上传任务不存在（可能已重启服务）')
            full = safe_join(ROOT_DIR, snap.get('path', ''))
            if not full:
                return self.send_err(404, '上传任务路径无效')
            info = {
                'partPath': full + '.part',
                'finalPath': full,
                'size': int(snap.get('size') or 0),
                'createdAt': float(snap.get('createdAt') or time.time()),
                'batchId': '',
            }
            UPLOAD_TASKS[task_id] = info
        part_path = info['partPath']
        # Verify offset matches current .part size (prevents race/corruption)
        try:
            current = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        except OSError:
            current = 0
        if current != offset:
            # Drain the request body so the connection can be reused (keep-alive)
            try:
                cl = int(self.headers.get('Content-Length', 0))
                while cl > 0:
                    buf = self.rfile.read(min(cl, 65536))
                    if not buf:
                        break
                    cl -= len(buf)
            except OSError:
                pass
            return self.send_json({
                'success': False,
                'error': f'偏移不一致（期望 {offset}，实际 {current}）',
                'offset': current,
            }, 409)
        content_length = int(self.headers.get('Content-Length', 0))
        try:
            with open(part_path, 'ab') as f:
                remaining = content_length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            new_size = os.path.getsize(part_path)
        except OSError as e:
            return self.send_err(500, f'写入失败: {self._sanitize_error(e)}')
        # Update persisted progress
        snap = load_persisted_tasks().get(task_id)
        if snap is None:
            snap = {
                'id': task_id, 'kind': 'upload', 'name': os.path.basename(info['finalPath']),
                'status': 'downloading', 'size': info.get('size', 0), 'loaded': new_size,
                'error': '', 'url': '', 'path': root_relative_path(info['finalPath']),
                'createdAt': info.get('createdAt', time.time()), 'startedAt': 0,
                'finishedAt': 0, 'lastBps': 0, 'maxBps': 0, 'samples': [],
            }
        snap['loaded'] = new_size
        snap['status'] = 'downloading'
        save_task(snap)
        self.send_json({'success': True, 'offset': new_size, 'loaded': new_size})

    def _api_upload_complete(self):
        """Finalize a chunked upload: rename .part -> final path."""
        data = self.read_json()
        task_id = str(data.get('taskId', '')).strip()
        if not task_id:
            return self.send_err(400, '缺少 taskId')
        info = UPLOAD_TASKS.get(task_id)
        snap = load_persisted_tasks().get(task_id)
        if not info:
            if not snap or snap.get('kind') != 'upload':
                return self.send_err(404, '上传任务不存在')
            full = safe_join(ROOT_DIR, snap.get('path', ''))
            if not full:
                return self.send_err(404, '上传任务路径无效')
            info = {'partPath': full + '.part', 'finalPath': full}
        part_path = info['partPath']
        final_path = info['finalPath']
        if not os.path.exists(part_path):
            return self.send_err(400, '未找到上传分片数据（.part 缺失）')
        try:
            os.replace(part_path, final_path)
        except OSError as e:
            return self.send_err(500, f'完成上传失败: {self._sanitize_error(e)}')
        if snap:
            snap['status'] = 'done'
            snap['loaded'] = snap.get('size') or os.path.getsize(final_path)
            snap['finishedAt'] = time.time()
            snap['error'] = ''
            save_task(snap)
        UPLOAD_TASKS.pop(task_id, None)
        self.send_ok('上传完成')

    def _api_upload_cancel(self):
        """Pause or cancel a chunked upload.
        Body: { taskId, action } action in {'pause','cancel'}.
        pause: keep .part; cancel: delete .part and task record.
        """
        data = self.read_json()
        task_id = str(data.get('taskId', '')).strip()
        action = str(data.get('action', 'pause')).strip().lower()
        info = UPLOAD_TASKS.get(task_id)
        snap = load_persisted_tasks().get(task_id)
        part_path = info.get('partPath') if info else None
        if action == 'cancel':
            if part_path and os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass
            UPLOAD_TASKS.pop(task_id, None)
            delete_persisted_task(task_id)
            self.send_ok('上传已取消并删除')
            return
        # pause
        if snap:
            snap['status'] = 'paused'
            save_task(snap)
        self.send_ok('上传已暂停')

    def _api_tasks_delete(self):
        """Delete one or more task records.

        Body: {
            ids?: ['x', ...] | 'x',          # ids to delete
            id?:  'x',                        # singular alias of ids
            items?: [{ id, kind, path, ...snap_fields }, ...],  # rich delete
            trash?: bool                      # default true
        }
        - trash=true (default): soft-delete — move snapshot from state['tasks']
          into state['taskTrash'] so it no longer shows in the main list but is
          recoverable. Stops live task + removes .part/.temp artifacts.
        - trash=false: hard-delete — permanently remove from state['taskTrash'].

        Matching strategy per requested item (so soft-delete actually lands even
        when the client's local id differs from the server-persisted id, e.g.
        upload tasks where the client id is 'up:timestamp:rand' but the server
        id is 'up:token' from /api/upload-init, or download tasks the server
        never persists at all):
            1. exact id  ->  state['tasks'] / REMOTE_TASKS / state['taskTrash']
            2. (kind, path) fallback  ->  state['tasks'] (upload/download history)
            3. no server record  ->  persist the client-supplied snapshot
               straight into taskTrash (so the trash UI still shows an entry)
        """
        data = self.read_json()
        trash = data.get('trash', True)

        # Build a normalized request list. Each entry is a dict with at least
        # {id}, optionally {kind, path} and extra snapshot fields.
        req_items = []
        raw_items = data.get('items')
        if isinstance(raw_items, list):
            for it in raw_items:
                if not isinstance(it, dict):
                    continue
                rid = str(it.get('id') or it.get('serverTaskId') or it.get('remoteId') or '').strip()
                if rid:
                    it = dict(it)
                    it['id'] = rid
                    req_items.append(it)
        ids = data.get('ids') or []
        if isinstance(ids, str):
            ids = [ids]
        for tid in ids or []:
            tid = str(tid or '').strip()
            if tid:
                req_items.append({'id': tid})
        single = str(data.get('id', '') or '').strip()
        if single and not any(it.get('id') == single for it in req_items):
            req_items.append({'id': single})
        if not req_items:
            return self.send_err(400, '缺少任务 id')

        # Deduplicate by id (keep the richest entry — the one carrying kind/path)
        seen = {}
        for it in req_items:
            rid = it.get('id')
            prev = seen.get(rid)
            if prev is None or (not prev.get('path') and it.get('path')):
                seen[rid] = it
        req_items = list(seen.values())

        if not trash:
            # Hard-delete from trash only.
            with STATE_LOCK:
                state = load_state()
                trash_map = state.get('taskTrash', {})
                changed = 0
                for it in req_items:
                    rid = it['id']
                    # exact id
                    if trash_map.pop(rid, None) is not None:
                        changed += 1
                        continue
                    # (kind, path) fallback inside trash
                    kind = it.get('kind')
                    path = it.get('path')
                    if kind and path:
                        for tid, s in list(trash_map.items()):
                            if s.get('kind') == kind and s.get('path') == path:
                                trash_map.pop(tid, None)
                                changed += 1
                                break
                if changed:
                    state['taskTrash'] = trash_map
                    save_state(state)
            self.send_ok(f'已彻底删除 {changed} 个任务')
            return

        # Soft-delete: stop live task, move snapshot into trash.
        # 先在 REMOTE_TASKS_LOCK 内拍取所有 live task 快照（避免 STATE_LOCK → REMOTE_TASKS_LOCK 死锁）
        live_tasks = {}
        live_snaps = {}
        with REMOTE_TASKS_LOCK:
            for it in req_items:
                rid = it['id']
                live_task = REMOTE_TASKS.get(rid)
                if live_task:
                    live_tasks[rid] = live_task
                    live_snaps[rid] = task_snapshot(live_task)
        # 然后在 STATE_LOCK 内完成 load-modify-save，防止 worker 的 save_task 在中间复活任务
        temp_paths = []  # (task_id, kind, path) for post-lock .part/.temp cleanup
        with STATE_LOCK:
            state = load_state()
            tasks_map = state.get('tasks', {})
            trash_map = state.setdefault('taskTrash', {})
            moved = 0
            for it in req_items:
                rid = it['id']
                # 1. exact id match in persisted tasks
                snap = tasks_map.get(rid)
                matched_id = rid if snap is not None else None
                # 2. (kind, path) fallback across persisted tasks
                if snap is None:
                    kind = it.get('kind')
                    path = it.get('path')
                    if kind and path:
                        for tid, s in tasks_map.items():
                            sk = s.get('kind') or 'remote'
                            sp = s.get('path') or ''
                            if sk == kind and sp and sp == path:
                                snap = s
                                matched_id = tid
                                break
                # 3. live snap (remote) fallback
                if snap is None and rid in live_snaps:
                    snap = live_snaps[rid]
                    matched_id = rid
                # 4. nothing on the server — adopt the client snapshot verbatim
                #    (download tasks the server never persisted; upload tasks
                #    whose server id we can't resolve) so the trash has an entry.
                if snap is None:
                    snap = {}
                    for k in TASK_SNAPSHOT_FIELDS:
                        if k in it and it[k] not in (None, ''):
                            snap[k] = it[k]
                    matched_id = rid
                snap = dict(snap)
                # 合并 live 中的实时信息，确保回收站条目有足够数据
                snap.setdefault('id', matched_id or rid)
                snap.setdefault('kind', it.get('kind') or 'remote')
                live_task = live_tasks.get(rid)
                if live_task:
                    for key in ('name', 'status', 'size', 'url', 'path', 'error', 'loaded',
                                'createdAt', 'startedAt', 'finishedAt', 'lastBps', 'maxBps'):
                        if not snap.get(key) and live_task.get(key):
                            snap[key] = live_task[key]
                # 用客户端传来的字段补齐（download/未持久化 upload 任务的主要信息来源）
                for key in ('name', 'kind', 'path', 'url', 'size', 'loaded', 'status', 'error',
                            'createdAt', 'lastBps', 'maxBps'):
                    if not snap.get(key) and it.get(key) not in (None, '', 0):
                        snap[key] = it[key]
                # 确保基本字段有默认值，避免回收站显示空白条目
                snap.setdefault('name', it.get('name') or '')
                snap.setdefault('status', it.get('status') or '')
                snap.setdefault('size', int(it.get('size') or 0) or 0)
                snap.setdefault('url', it.get('url') or '')
                snap.setdefault('path', it.get('path') or '')
                snap.setdefault('error', it.get('error') or '')
                snap.setdefault('loaded', int(it.get('loaded') or 0) or 0)
                snap.setdefault('createdAt', it.get('createdAt') or 0)
                snap['deletedAt'] = time.time()
                final_id = matched_id or rid
                trash_map[final_id] = snap
                if matched_id and tasks_map.pop(matched_id, None) is not None:
                    moved += 1
                temp_paths.append((final_id, snap.get('kind'), snap.get('path', '')))
            state['tasks'] = tasks_map
            state['taskTrash'] = trash_map
            save_state(state)
        # 然后再停止 live task（在 STATE_LOCK 外，避免嵌套锁导致死锁）
        for final_id, kind, path in temp_paths:
            rid_candidates = {final_id}
            # also try the original request id (may differ from matched server id)
            for it in req_items:
                if it.get('kind') == kind and it.get('path') == path and it.get('path'):
                    rid_candidates.add(it['id'])
            for rid in rid_candidates:
                live_task = live_tasks.get(rid)
                with REMOTE_TASKS_LOCK:
                    if live_task and REMOTE_TASKS.get(rid) is live_task:
                        live_task['cancel'] = True
                        live_task['pauseRequested'] = False
                        if live_task.get('status') == 'downloading':
                            live_task['status'] = 'canceled'
                            live_task['finishedAt'] = time.time()
                        temp_path = live_task.get('tempPath') or ''
                        REMOTE_TASKS.pop(rid, None)
                    else:
                        temp_path = ''
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                UPLOAD_TASKS.pop(rid, None)
            # Remove upload .part if any
            if kind == 'upload' and path:
                full = safe_join(ROOT_DIR, path)
                if full and os.path.exists(full + '.part'):
                    try:
                        os.remove(full + '.part')
                    except OSError:
                        pass
        self.send_ok(f'已删除 {len(req_items)} 个任务')

    def _api_tasks_restore(self):
        """Restore a soft-deleted task back into the active list.
        Body: { id } -> restores state['taskTrash'][id] into state['tasks'].
        The task is never auto-resumed; remote tasks come back as 'paused'.
        """
        data = self.read_json()
        task_id = str(data.get('id', '') or '').strip()
        if not task_id:
            return self.send_err(400, '缺少任务 id')
        state = load_state()
        trash_map = state.get('taskTrash', {})
        snap = trash_map.get(task_id)
        if snap is None:
            return self.send_err(404, '回收站中无此任务')
        snap = dict(snap)
        snap.pop('deletedAt', None)
        # Force non-terminal remote tasks to 'paused' so they don't auto-resume.
        if snap.get('kind', 'remote') == 'remote' and snap.get('status') not in ('done', 'canceled', 'error'):
            snap['status'] = 'paused'
        state.setdefault('tasks', {})[task_id] = snap
        trash_map.pop(task_id, None)
        state['taskTrash'] = trash_map
        save_state(state)
        self.send_json({'success': True, 'task': snap})

    def _api_tasks_trash(self, qs=None):
        """Return soft-deleted task snapshots, newest first.
        Filters out entries with no meaningful content to avoid showing
        blank ♻️ rows in the trash UI.
        """
        trash_map = load_state().get('taskTrash', {})
        items = []
        for tid, s in trash_map.items():
            snap = dict(s or {})
            snap.setdefault('id', tid)
            # 跳过完全没有内容的空条目（只有 id 和 deletedAt，没有任何实际信息）
            has_content = snap.get('name') or snap.get('url') or snap.get('path') or snap.get('status')
            if not has_content:
                continue
            snap.setdefault('kind', 'remote')
            snap.setdefault('name', '未命名任务')
            items.append(snap)
        items.sort(key=lambda x: x.get('deletedAt') or x.get('createdAt') or 0, reverse=True)
        self.send_json({'success': True, 'items': items})

    def _api_mkdir(self):
        data = self.read_json()
        parent = data.get('path', '/')
        name = data.get('name', '').strip()
        if not name:
            return self.send_err(400, '请输入文件夹名称')
        # Security: reject path separators and traversal sequences in name
        if not valid_leaf_name(name):
            return self.send_err(400, '文件夹名称不能包含路径分隔符或 "..". ')
        pp = safe_join(ROOT_DIR, parent)
        if not pp:
            return self.send_err(400, '路径非法')
        np = os.path.join(pp, name)
        # Double-check final path stays within ROOT_DIR
        if not os.path.realpath(np).startswith(os.path.realpath(ROOT_DIR) + os.sep):
            return self.send_err(403, '路径非法')
        if os.path.exists(np):
            return self.send_err(400, '已存在同名文件或文件夹')
        try:
            os.makedirs(np)
            self.send_ok('创建成功')
        except OSError as e:
            self.send_err(500, f'创建失败: {self._sanitize_error(e)}')

    def _api_create_text(self):
        data = self.read_json()
        parent = data.get('path', '/')
        name = str(data.get('name', '')).strip()
        content = str(data.get('content', ''))
        ext = normalize_extension(data.get('ext', 'txt'))
        if ext and not name.lower().endswith('.' + ext.lower()):
            name = f'{name}.{ext}'
        if not valid_leaf_name(name):
            return self.send_err(400, '文件名无效')
        pp = safe_join(ROOT_DIR, parent)
        if not pp or not os.path.isdir(pp):
            return self.send_err(400, '目标目录不存在')
        target = make_unique_path(pp, name)
        try:
            with open(target, 'w', encoding='utf-8', newline='') as f:
                f.write(content)
            self.send_json({'success': True, 'message': '文本文件创建成功', 'path': '/' + os.path.relpath(target, ROOT_DIR).replace('\\', '/')})
        except OSError as e:
            self.send_err(500, f'创建失败: {self._sanitize_error(e)}')

    def _api_create_speed_file(self):
        cleanup_expired_temp_files()
        data = self.read_json()
        parent = data.get('path', '/')
        name = str(data.get('name', '')).strip() or 'speedtest'
        ext = normalize_extension(data.get('ext', 'bin'))
        size = int(data.get('size', 0) or 0)
        ttl = clamp_ttl(data.get('ttl', 24 * 60 * 60))
        if ext and not name.lower().endswith('.' + ext.lower()):
            name = f'{name}.{ext}'
        if not valid_leaf_name(name):
            return self.send_err(400, '文件名无效')
        if size <= 0 or size > MAX_SPARSE_FILE_SIZE:
            return self.send_err(400, f'文件大小必须在 1 B 到 {format_size(MAX_SPARSE_FILE_SIZE)} 之间')
        pp = safe_join(ROOT_DIR, parent)
        if not pp or not os.path.isdir(pp):
            return self.send_err(400, '目标目录不存在')
        target = make_unique_path(pp, name)
        try:
            with open(target, 'wb') as f:
                f.truncate(size)
            rel = '/' + os.path.relpath(target, ROOT_DIR).replace('\\', '/')
            state = load_state()
            state.setdefault('tempFiles', {})[rel] = {
                'createdAt': time.time(),
                'expiresAt': time.time() + ttl,
                'size': size,
            }
            save_state(state)
            self.send_json({'success': True, 'message': '测速文件创建成功', 'path': rel, 'expiresAt': state['tempFiles'][rel]['expiresAt']})
        except OSError as e:
            self.send_err(500, f'创建失败: {self._sanitize_error(e)}')

    def _api_share(self):
        cleanup_expired_temp_files()
        data = self.read_json()
        req_path = data.get('path', '')
        ttl = clamp_ttl(data.get('ttl', 24 * 60 * 60))
        full = safe_join(ROOT_DIR, req_path)
        if not full or not os.path.isfile(full):
            return self.send_err(404, '文件不存在')
        token = secrets.token_urlsafe(24)
        expires_at = time.time() + ttl
        state = load_state()
        state.setdefault('shares', {})[token] = {
            'path': req_path,
            'size': os.path.getsize(full),
            'createdAt': time.time(),
            'expiresAt': expires_at,
            'downloadCount': 0,
            'downloadBytes': 0,
            'accessStats': {},
        }
        save_state(state)
        self.send_json({
            'success': True,
            'url': self._share_url(token),
            'token': token,
            'path': req_path,
            'name': os.path.basename(full),
            'size': os.path.getsize(full),
            'expiresAt': expires_at,
            'remaining': max(0, int(expires_at - time.time())),
        })

    def _api_share_delete(self):
        cleanup_expired_temp_files()
        data = self.read_json()
        token = str(data.get('token', '')).strip()
        if not token:
            return self.send_err(400, '缺少链接 token')
        state = load_state()
        meta = state.get('shares', {}).pop(token, None)
        if not meta:
            return self.send_err(404, '链接不存在或已过期')
        meta['deletedAt'] = time.time()
        state.setdefault('shareTrash', {})[token] = meta
        save_state(state)
        self.send_ok('链接已移动到回收站')

    def _api_share_restore(self):
        data = self.read_json()
        token = str(data.get('token', '')).strip()
        if not token:
            return self.send_err(400, '缺少链接 token')
        state = load_state()
        meta = state.get('shareTrash', {}).pop(token, None)
        if not meta:
            return self.send_err(404, '回收站里没有这个链接')
        meta.pop('deletedAt', None)
        state.setdefault('shares', {})[token] = meta
        save_state(state)
        self.send_ok('链接已还原')

    def _api_share_purge(self):
        data = self.read_json()
        token = str(data.get('token', '')).strip()
        if not token:
            return self.send_err(400, '缺少链接 token')
        state = load_state()
        if token not in state.get('shareTrash', {}):
            return self.send_err(404, '回收站里没有这个链接')
        state['shareTrash'].pop(token, None)
        save_state(state)
        self.send_ok('链接已彻底删除')

    def _api_share_trash_clear(self):
        state = load_state()
        state['shareTrash'] = {}
        save_state(state)
        self.send_ok('回收站已清空')

    def _api_remote_download_start(self):
        data = self.read_json()
        url = str(data.get('url', '')).strip()
        ok, err = validate_remote_url(url)
        if not ok:
            return self.send_err(400, err)
        task_id = secrets.token_urlsafe(16)
        now = time.time()
        task = {
            'id': task_id,
            'kind': 'remote',
            'url': url,
            'name': remote_url_filename(url),
            'status': 'queued',
            'size': 0,
            'loaded': 0,
            'createdAt': now,
            'startedAt': 0,
            'finishedAt': 0,
            'path': '/' + DOWNLOAD_DIR_NAME,
            'error': '',
            'lastBps': 0,
            'maxBps': 0,
            'samples': [],
            'lastT': 0,
            'lastBytes': 0,
            'cancel': False,
            'pauseRequested': False,
        }
        with REMOTE_TASKS_LOCK:
            REMOTE_TASKS[task_id] = task
        save_task(task)
        start_remote_worker(task_id)
        self.send_json({'success': True, 'task': remote_task_snapshot(task)})

    def _api_remote_download_cancel(self):
        """Pause or cancel a remote task.
        Body: { id, action } where action is 'pause' (default) or 'cancel'.
        - pause: stop worker, KEEP .part file, status -> paused
        - cancel: stop worker, DELETE .part file, status -> canceled
        """
        data = self.read_json()
        task_id = str(data.get('id', '')).strip()
        action = str(data.get('action', 'pause')).strip().lower()
        with REMOTE_TASKS_LOCK:
            task = REMOTE_TASKS.get(task_id)
            if not task:
                # 任务可能在 worker 退出后不在 REMOTE_TASKS 中，但持久化中有记录
                snap = load_persisted_tasks().get(task_id)
                if not snap:
                    return self.send_err(404, '任务不存在')
                if snap.get('status') in ('done', 'error', 'canceled'):
                    return self.send_json({'success': True, 'message': '任务已结束'})
                # 直接更新持久化状态
                if action == 'cancel':
                    snap = dict(snap)
                    snap['status'] = 'canceled'
                    snap['finishedAt'] = time.time()
                    save_task(snap)
                    # 删除 .part 文件
                    path_rel = snap.get('path') or ''
                    if path_rel and path_rel != '/' + DOWNLOAD_DIR_NAME:
                        full = safe_join(ROOT_DIR, path_rel)
                        if full and os.path.exists(full + '.part'):
                            try:
                                os.remove(full + '.part')
                            except OSError:
                                pass
                    self.send_ok('任务已取消')
                else:
                    snap = dict(snap)
                    snap['status'] = 'paused'
                    save_task(snap)
                    self.send_ok('任务已暂停')
                return
            if task.get('status') in ('done', 'error', 'canceled'):
                return self.send_json({'success': True, 'message': '任务已结束'})
            task['cancel'] = True
            task['pauseRequested'] = (action != 'cancel')
            if action == 'cancel':
                task['status'] = 'canceled'
                task['finishedAt'] = time.time()
            else:
                task['status'] = 'paused'
        # 使用 lock 内保存的 task 引用来持久化，而不是重新从 REMOTE_TASKS 获取
        # （worker 可能在此期间将任务从 REMOTE_TASKS 中移除）
        save_task(merge_remote_task_for_persist(task))
        self.send_ok('任务已暂停' if action != 'cancel' else '任务已取消')

    def _api_remote_download_resume(self):
        """Resume a paused/queued/error remote download with HTTP Range."""
        data = self.read_json()
        task_id = str(data.get('id', '')).strip()
        # 检查任务是否已被软删除（在回收站中）
        if task_id in load_state().get('taskTrash', {}):
            return self.send_err(410, '任务已被删除，请先从回收站恢复')
        with REMOTE_TASKS_LOCK:
            task = REMOTE_TASKS.get(task_id)
            if not task:
                # 任务可能在 worker 退出后从 REMOTE_TASKS 中移除了，尝试从持久化恢复
                snap = load_persisted_tasks().get(task_id)
                if not snap:
                    return self.send_err(404, '任务不存在')
                if snap.get('status') == 'done':
                    return self.send_err(400, '任务已完成，无需继续')
                # 从快照重建内存任务
                task = {
                    'id': snap.get('id') or task_id,
                    'url': snap.get('url') or '',
                    'name': snap.get('name') or 'remote-download',
                    'status': 'queued',
                    'size': int(snap.get('size') or 0),
                    'loaded': int(snap.get('loaded') or 0),
                    'createdAt': float(snap.get('createdAt') or time.time()),
                    'startedAt': float(snap.get('startedAt') or 0),
                    'finishedAt': 0,
                    'path': snap.get('path') or '/' + DOWNLOAD_DIR_NAME,
                    'error': '',
                    'lastBps': 0,
                    'maxBps': float(snap.get('maxBps') or 0),
                    'samples': [],
                    'lastT': 0,
                    'lastBytes': 0,
                    'cancel': False,
                    'pauseRequested': False,
                    'kind': 'remote',
                }
                path_rel = snap.get('path') or ''
                if path_rel and path_rel != '/' + DOWNLOAD_DIR_NAME:
                    base = safe_join(ROOT_DIR, path_rel)
                    if base:
                        task['tempPath'] = base + '.part'
                if not task['url']:
                    return self.send_err(400, '缺少 URL，无法恢复')
                REMOTE_TASKS[task_id] = task
            else:
                if task.get('status') in ('downloading',):
                    return self.send_json({'success': True, 'task': remote_task_snapshot(task), 'message': '任务正在下载'})
                if task.get('status') == 'done':
                    return self.send_err(400, '任务已完成，无需继续')
                # Reset cancel flags
                task['cancel'] = False
                task['pauseRequested'] = False
                task['status'] = 'queued'
                task['error'] = ''
        # 使用 lock 内保存的 task 引用，而不是重新从 REMOTE_TASKS 获取
        save_task(merge_remote_task_for_persist(task))
        start_remote_worker(task_id)
        self.send_json({'success': True, 'task': remote_task_snapshot(task)})

    def _api_remote_download_delete(self):
        data = self.read_json()
        task_id = str(data.get('id', '')).strip()
        temp_path = ''
        with REMOTE_TASKS_LOCK:
            task = REMOTE_TASKS.get(task_id)
            if not task:
                # Still try to remove from persistent store
                pass
            else:
                if task.get('status') == 'downloading':
                    task['cancel'] = True
                    task['pauseRequested'] = False
                    task['status'] = 'canceled'
                    task['finishedAt'] = time.time()
                temp_path = task.get('tempPath') or ''
                REMOTE_TASKS.pop(task_id, None)
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        delete_persisted_task(task_id)
        # 也从 taskTrash 中移除（如果存在）
        with STATE_LOCK:
            state = load_state()
            if state.get('taskTrash', {}).pop(task_id, None) is not None:
                save_state(state)
        self.send_ok('任务已删除')

    def _api_save_text(self):
        data = self.read_json()
        req_path = data.get('path', '')
        content = str(data.get('content', ''))
        encoded = content.encode('utf-8')
        if len(encoded) > MAX_TEXT_EDIT_SIZE:
            return self.send_err(400, f'文本编辑最大支持 {format_size(MAX_TEXT_EDIT_SIZE)}')
        full = safe_join(ROOT_DIR, req_path)
        if not full or not os.path.isfile(full):
            return self.send_err(404, '文件不存在')
        try:
            # Refuse obvious binary files before overwriting.
            read_text_file(full)
            with open(full, 'w', encoding='utf-8', newline='') as f:
                f.write(content)
        except UnicodeDecodeError:
            return self.send_err(400, '文件不是 UTF-8 文本，不能在网页内编辑')
        except ValueError as e:
            return self.send_err(400, str(e))
        except OSError as e:
            return self.send_err(500, f'保存失败: {self._sanitize_error(e)}')
        self.send_json({'success': True, 'message': '文本已保存', 'file': get_file_info(ROOT_DIR, req_path, full)})

    def _api_delete(self):
        data = self.read_json()
        paths = data.get('paths', [])
        if not paths:
            return self.send_err(400, '请选择要删除的文件')
        errs = []
        for p in paths:
            fp = safe_join(ROOT_DIR, p)
            if not fp:
                errs.append(f'{p}: 路径非法')
                continue
            if not os.path.exists(fp):
                errs.append(f'{p}: 文件不存在')
                continue
            try:
                if os.path.isdir(fp):
                    shutil.rmtree(fp)
                else:
                    os.remove(fp)
            except OSError as e:
                errs.append(f'{p}: {e}')
        if errs:
            self.send_json({'success': False, 'errors': errs}, 207)
        else:
            self.send_ok('删除成功')

    def _api_rename(self):
        data = self.read_json()
        path = data.get('path', '')
        new_name = data.get('newName', '').strip()
        if not path or not new_name:
            return self.send_err(400, '缺少参数')
        if '/' in new_name or '\\' in new_name:
            return self.send_err(400, '名称不能包含路径分隔符')
        fp = safe_join(ROOT_DIR, path)
        if not fp or not os.path.exists(fp):
            return self.send_err(404, '文件不存在')
        root_real = os.path.realpath(ROOT_DIR)
        # 不能重命名根目录本身：safe_join(ROOT_DIR, '/') 会返回 ROOT_DIR，
        # 于是 path='/' 能把整个根目录改名成兄弟目录，服务直接瘫痪。
        if os.path.realpath(fp) == root_real:
            return self.send_err(403, '不能重命名根目录')
        new_fp = os.path.join(os.path.dirname(fp), new_name)
        # 必须用 == root 或 root + os.sep 判断：裸 startswith 会让
        # /data/root_evil 匹配上 /data/root。
        new_real = os.path.realpath(new_fp)
        if not (new_real == root_real or new_real.startswith(root_real + os.sep)):
            return self.send_err(403, '路径非法')
        if os.path.exists(new_fp):
            return self.send_err(400, '已存在同名文件或文件夹')
        try:
            os.rename(fp, new_fp)
            self.send_ok('重命名成功')
        except OSError as e:
            self.send_err(500, f'重命名失败: {e}')

    def _api_pinned(self, qs=None):
        """GET /api/pinned -> {'pinned': [...]}
        POST /api/pinned {path, action:'add'|'remove'} -> {'success':True,'pinned':[...]}
        Only top-level folders directly under ROOT_DIR may be pinned.
        """
        if qs is None:
            # POST branch (route dispatch calls handler() with no args)
            data = self.read_json() or {}
            path = (data.get('path') or '').strip()
            action = (data.get('action') or 'add').strip()
            if not path or action not in ('add', 'remove'):
                return self.send_err(400, '缺少参数')
            # normalize: must be a single-segment path under root
            normalized = '/' + path.replace('\\', '/').strip('/')
            if not normalized or '/' in normalized.strip('/'):
                return self.send_err(400, '只能置顶根目录下的文件夹')
            fp = safe_join(ROOT_DIR, normalized)
            if not fp or not os.path.isdir(fp):
                return self.send_err(404, '文件夹不存在')
            state = load_state()
            pinned = list(state.get('pinned') or [])
            # prune stale pins (folder deleted/renamed) opportunistically
            pinned = [p for p in pinned if os.path.isdir(safe_join(ROOT_DIR, p))]
            if action == 'add':
                if normalized not in pinned:
                    pinned.append(normalized)
            else:
                pinned = [p for p in pinned if p != normalized]
            state['pinned'] = pinned
            save_state(state)
            self.send_json({'success': True, 'pinned': pinned})
            return
        # GET branch
        state = load_state()
        pinned = list(state.get('pinned') or [])
        pinned = [p for p in pinned if os.path.isdir(safe_join(ROOT_DIR, p))]
        self.send_json({'pinned': pinned})

    def _api_copy(self):
        data = self.read_json()
        sources = data.get('sources', [])
        dest = data.get('dest', '')
        if not sources or not dest:
            return self.send_err(400, '缺少参数')
        dp = safe_join(ROOT_DIR, dest)
        if not dp or not os.path.isdir(dp):
            return self.send_err(400, '目标目录不存在')
        errs = []
        for s in sources:
            sp = safe_join(ROOT_DIR, s)
            if not sp or not os.path.exists(sp):
                errs.append(f'{s}: 文件不存在')
                continue
            target = os.path.join(dp, os.path.basename(sp))
            if os.path.exists(target):
                errs.append(f'{s}: 目标目录已存在同名文件或文件夹')
                continue
            try:
                if os.path.isdir(sp):
                    shutil.copytree(sp, target)
                else:
                    shutil.copy2(sp, target)
            except OSError as e:
                errs.append(f'{s}: {e}')
        if errs:
            self.send_json({'success': False, 'errors': errs}, 207)
        else:
            self.send_ok('复制成功')

    def _api_move(self):
        data = self.read_json()
        sources = data.get('sources', [])
        dest = data.get('dest', '')
        if not sources or not dest:
            return self.send_err(400, '缺少参数')
        dp = safe_join(ROOT_DIR, dest)
        if not dp or not os.path.isdir(dp):
            return self.send_err(400, '目标目录不存在')
        root_real = os.path.realpath(ROOT_DIR)
        errs = []
        for s in sources:
            sp = safe_join(ROOT_DIR, s)
            if not sp or not os.path.exists(sp):
                errs.append(f'{s}: 文件不存在')
                continue
            target = os.path.join(dp, os.path.basename(sp))
            # Security: verify target stays within ROOT_DIR (handles symlinks)
            target_real = os.path.realpath(target)
            if not (target_real == root_real or target_real.startswith(root_real + os.sep)):
                errs.append(f'{s}: 目标路径非法')
                continue
            if os.path.exists(target):
                errs.append(f'{s}: 目标目录已存在同名文件或文件夹')
                continue
            try:
                shutil.move(sp, target)
            except OSError as e:
                errs.append(f'{s}: {self._sanitize_error(e)}')
        if errs:
            self.send_json({'success': False, 'errors': errs}, 207)
        else:
            self.send_ok('移动成功')

    def _api_archive(self):
        data = self.read_json()
        sources = data.get('sources', [])
        dest = data.get('dest', '/')
        name = str(data.get('name', '')).strip() or 'archive.zip'
        if not sources:
            return self.send_err(400, '请选择要压缩的文件')
        if not name.lower().endswith('.zip'):
            name += '.zip'
        if not valid_leaf_name(name):
            return self.send_err(400, '压缩包名称无效')

        dp = safe_join(ROOT_DIR, dest)
        if not dp or not os.path.isdir(dp):
            return self.send_err(400, '压缩位置不存在')

        source_paths = []
        errs = []
        root_real = os.path.realpath(ROOT_DIR)
        for s in sources:
            sp = safe_join(ROOT_DIR, s)
            if not sp or not os.path.exists(sp):
                errs.append(f'{s}: 文件不存在')
                continue
            if os.path.realpath(sp) == root_real:
                errs.append(f'{s}: 不能压缩根目录本身')
                continue
            source_paths.append(sp)
        if errs:
            return self.send_json({'success': False, 'errors': errs}, 207)

        target = make_unique_path(dp, name)
        try:
            count = create_zip_archive(source_paths, target)
            self.send_json({
                'success': True,
                'message': f'压缩完成，共 {count} 个文件',
                'path': root_relative_path(target),
                'dest': root_relative_path(dp),
                'name': os.path.basename(target),
                'count': count,
            })
        except (OSError, zipfile.BadZipFile, ValueError) as e:
            if os.path.exists(target):
                try:
                    os.remove(target)
                except OSError:
                    pass
            self.send_err(500, f'压缩失败: {self._sanitize_error(e)}')

    def _api_extract(self):
        data = self.read_json()
        req_path = data.get('path', '')
        dest = data.get('dest', '/')
        if not req_path:
            return self.send_err(400, '缺少压缩包路径')

        archive = safe_join(ROOT_DIR, req_path)
        if not archive or not os.path.isfile(archive):
            return self.send_err(404, '压缩包不存在')
        if not supported_archive_name(archive):
            return self.send_err(400, '仅支持解压 zip、tar、tar.gz、tgz、tar.bz2、tbz2、tar.xz、txz')

        dp = safe_join(ROOT_DIR, dest)
        if not dp or not os.path.isdir(dp):
            return self.send_err(400, '解压位置不存在')

        try:
            count = extract_archive(archive, dp)
            self.send_json({
                'success': True,
                'message': f'解压完成，共 {count} 个文件',
                'dest': root_relative_path(dp),
                'count': count,
            })
        except (OSError, zipfile.BadZipFile, tarfile.TarError, ValueError) as e:
            self.send_err(500, f'解压失败: {self._sanitize_error(e)}')

    # ==========================================================
    # WebDAV - Path Resolution
    # ==========================================================
    def _dav_resolve(self, url_path):
        """Resolve /dav/... URL to the dedicated ROOT_DIR/webdav storage."""
        try:
            dav_root = ensure_webdav_storage()
        except OSError:
            return None, '/'
        if not dav_root:
            return None, '/'
        rel = url_path[4:] if url_path.startswith('/dav') else url_path
        rel = rel.replace('\\', '/').strip('/')
        if not rel:
            return os.path.realpath(dav_root), '/'
        full = safe_join(dav_root, rel)
        return full, '/' + rel

    def _dav_href(self, rel_path, is_dir=False):
        """Build a properly encoded WebDAV href."""
        parts = rel_path.strip('/').split('/')
        encoded = '/'.join(urllib.parse.quote(p, safe='') for p in parts if p)
        href = '/dav/' + encoded if encoded else '/dav/'
        if is_dir and not href.endswith('/'):
            href += '/'
        return href

    # ==========================================================
    # WebDAV - PROPFIND
    # ==========================================================
    def _webdav_PROPFIND(self, path):
        self.read_body()  # Read and ignore request body
        full, rel = self._dav_resolve(path)
        if not full or not os.path.exists(full):
            return self.send_empty(404)

        depth = self.headers.get('Depth', '1')
        entries = []

        # Add the resource itself
        is_dir = os.path.isdir(full)
        href = self._dav_href(rel, is_dir)
        entry = make_dav_entry(href, full, os.path.basename(full) or '根目录')
        if entry:
            entries.append(entry)

        # If directory and depth > 0, add children
        if is_dir and depth != '0':
            try:
                for name in sorted(os.listdir(full)):
                    child_full = os.path.join(full, name)
                    child_rel = rel.rstrip('/') + '/' + name
                    child_is_dir = os.path.isdir(child_full)
                    child_href = self._dav_href(child_rel, child_is_dir)
                    child_entry = make_dav_entry(child_href, child_full, name)
                    if child_entry:
                        entries.append(child_entry)
            except PermissionError:
                pass

        xml_body = build_propfind_response(entries)
        self.send_response(207)
        self.send_header('Content-Type', 'application/xml; charset=utf-8')
        self.send_header('Content-Length', len(xml_body))
        self.end_headers()
        self.wfile.write(xml_body)

    # ==========================================================
    # WebDAV - PROPPATCH (stub)
    # ==========================================================
    def _webdav_PROPPATCH(self, path):
        self.read_body()
        full, rel = self._dav_resolve(path)
        if not full or not os.path.exists(full):
            return self.send_empty(404)
        href = self._dav_href(rel, os.path.isdir(full))
        safe_href = xml_escape(href)
        xml = f'''<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>{safe_href}</D:href>
    <D:propstat>
      <D:prop/>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>'''
        body = xml.encode('utf-8')
        self.send_response(207)
        self.send_header('Content-Type', 'application/xml; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    # ==========================================================
    # WebDAV - GET / HEAD
    # ==========================================================
    def _webdav_GET(self, path):
        full, rel = self._dav_resolve(path)
        if not full or not os.path.exists(full):
            return self.send_empty(404)

        if os.path.isdir(full):
            body = b'<!DOCTYPE html><html><body><h1>Directory</h1></body></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            size = os.path.getsize(full)
            self.send_response(200)
            self.send_header('Content-Type', guess_mime(full))
            self.send_header('Content-Length', size)
            st = os.stat(full)
            self.send_header('ETag', f'"{int(st.st_mtime)}-{size}"')
            self.send_header('Last-Modified', rfc1123_date(st.st_mtime))
            self.end_headers()
            with open(full, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break

    def _webdav_HEAD(self, path):
        full, rel = self._dav_resolve(path)
        if not full or not os.path.exists(full):
            return self.send_empty(404)
        if os.path.isdir(full):
            self.send_response(200)
            self.send_header('Content-Type', 'httpd/unix-directory')
            self.send_header('Content-Length', '0')
        else:
            size = os.path.getsize(full)
            self.send_response(200)
            self.send_header('Content-Type', guess_mime(full))
            self.send_header('Content-Length', size)
            st = os.stat(full)
            self.send_header('ETag', f'"{int(st.st_mtime)}-{size}"')
            self.send_header('Last-Modified', rfc1123_date(st.st_mtime))
        self.end_headers()

    # ==========================================================
    # WebDAV - PUT
    # ==========================================================
    def _webdav_PUT(self, path):
        full, rel = self._dav_resolve(path)
        if not full:
            return self.send_empty(403)
        parent = os.path.dirname(full)
        if not os.path.isdir(parent):
            return self.send_empty(409)
        existed = os.path.exists(full)
        content_length = int(self.headers.get('Content-Length', 0))
        try:
            with open(full, 'wb') as f:
                remaining = content_length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            self.send_empty(204 if existed else 201)
        except OSError:
            self.send_empty(403)

    # ==========================================================
    # WebDAV - DELETE
    # ==========================================================
    def _webdav_DELETE(self, path):
        full, rel = self._dav_resolve(path)
        if not full or not os.path.exists(full):
            return self.send_empty(404)
        if rel == '/':
            return self.send_empty(403)
        try:
            if os.path.isdir(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
            self.send_empty(204)
        except OSError:
            self.send_empty(403)

    # ==========================================================
    # WebDAV - MKCOL
    # ==========================================================
    def _webdav_MKCOL(self, path):
        full, rel = self._dav_resolve(path)
        if not full:
            return self.send_empty(403)
        if os.path.exists(full):
            return self.send_empty(405)
        parent = os.path.dirname(full)
        if not os.path.isdir(parent):
            return self.send_empty(409)
        try:
            os.makedirs(full)
            self.send_empty(201)
        except OSError:
            self.send_empty(403)

    # ==========================================================
    # WebDAV - COPY
    # ==========================================================
    def _webdav_COPY(self, path):
        full, rel = self._dav_resolve(path)
        if not full or not os.path.exists(full):
            return self.send_empty(404)

        dest_header = self.headers.get('Destination', '')
        if not dest_header:
            return self.send_empty(400)

        dest_parsed = urllib.parse.urlparse(dest_header)
        dest_url_path = urllib.parse.unquote(dest_parsed.path)
        dest_full, _ = self._dav_resolve(dest_url_path)
        if not dest_full:
            return self.send_empty(403)

        overwrite_header = self.headers.get('Overwrite')
        # RFC 4918 规定缺省为 T，保持兼容；但"没写这个头"和"明确要求覆盖"
        # 不该等价于可以递归删掉一整棵非空目录——客户端一个畸形请求就能
        # 无提示地毁掉整个目录树，且没有回收站。
        overwrite = (overwrite_header or 'T').upper() == 'T'
        existed = os.path.exists(dest_full)

        if existed and not overwrite:
            return self.send_empty(412)

        if (existed and overwrite_header is None
                and os.path.isdir(dest_full) and os.listdir(dest_full)):
            # 目标是非空目录且客户端未显式声明 Overwrite：拒绝，要求显式表态
            return self.send_empty(412)

        try:
            if existed:
                if os.path.isdir(dest_full):
                    shutil.rmtree(dest_full)
                else:
                    os.remove(dest_full)
            if os.path.isdir(full):
                shutil.copytree(full, dest_full)
            else:
                os.makedirs(os.path.dirname(dest_full), exist_ok=True)
                shutil.copy2(full, dest_full)
            self.send_empty(204 if existed else 201)
        except OSError:
            self.send_empty(403)

    # ==========================================================
    # WebDAV - MOVE
    # ==========================================================
    def _webdav_MOVE(self, path):
        full, rel = self._dav_resolve(path)
        if not full or not os.path.exists(full):
            return self.send_empty(404)
        if rel == '/':
            return self.send_empty(403)

        dest_header = self.headers.get('Destination', '')
        if not dest_header:
            return self.send_empty(400)

        dest_parsed = urllib.parse.urlparse(dest_header)
        dest_url_path = urllib.parse.unquote(dest_parsed.path)
        dest_full, _ = self._dav_resolve(dest_url_path)
        if not dest_full:
            return self.send_empty(403)

        overwrite_header = self.headers.get('Overwrite')
        # RFC 4918 规定缺省为 T，保持兼容；但"没写这个头"和"明确要求覆盖"
        # 不该等价于可以递归删掉一整棵非空目录——客户端一个畸形请求就能
        # 无提示地毁掉整个目录树，且没有回收站。
        overwrite = (overwrite_header or 'T').upper() == 'T'
        existed = os.path.exists(dest_full)

        if existed and not overwrite:
            return self.send_empty(412)

        if (existed and overwrite_header is None
                and os.path.isdir(dest_full) and os.listdir(dest_full)):
            # 目标是非空目录且客户端未显式声明 Overwrite：拒绝，要求显式表态
            return self.send_empty(412)

        try:
            if existed:
                if os.path.isdir(dest_full):
                    shutil.rmtree(dest_full)
                else:
                    os.remove(dest_full)
            shutil.move(full, dest_full)
            self.send_empty(204 if existed else 201)
        except OSError:
            self.send_empty(403)

    # ==========================================================
    # WebDAV - LOCK (compatibility stub)
    # ==========================================================
    def _webdav_LOCK(self, path):
        self.read_body()
        token = str(uuid.uuid4())
        xml = f'''<?xml version="1.0" encoding="utf-8"?>
<D:prop xmlns:D="DAV:">
  <D:lockdiscovery>
    <D:activelock>
      <D:locktype><D:write/></D:locktype>
      <D:lockscope><D:exclusive/></D:lockscope>
      <D:depth>infinity</D:depth>
      <D:timeout>Second-3600</D:timeout>
      <D:locktoken>
        <D:href>opaquelocktoken:{token}</D:href>
      </D:locktoken>
    </D:activelock>
  </D:lockdiscovery>
</D:prop>'''
        body = xml.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/xml; charset=utf-8')
        self.send_header('Lock-Token', f'<opaquelocktoken:{token}>')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    # ==========================================================
    # WebDAV - UNLOCK (compatibility stub)
    # ==========================================================
    def _webdav_UNLOCK(self, path):
        self.send_empty(204)


# ============================================================
# Root Size Calculator
# ============================================================
def _allocated_size(path):
    st = os.stat(path)
    blocks = getattr(st, 'st_blocks', None)
    if blocks is not None:
        return int(blocks) * 512
    return st.st_size


def _calculate_root_logical_size():
    """Calculate the apparent file sizes in ROOT_DIR."""
    return calculate_logical_size(ROOT_DIR)


def _calculate_root_allocated_size():
    """Calculate real disk blocks used by ROOT_DIR, not sparse logical size."""
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(ROOT_DIR):
            for name in filenames:
                fp = os.path.join(dirpath, name)
                try:
                    total += _allocated_size(fp)
                except OSError:
                    pass
            for name in dirnames:
                dp = os.path.join(dirpath, name)
                try:
                    total += _allocated_size(dp)
                except OSError:
                    pass
    except OSError:
        pass
    try:
        total += _allocated_size(ROOT_DIR)
    except OSError:
        pass
    return total


def _calc_root_size():
    """Periodically calculate ROOT_DIR size without blocking API requests."""
    while True:
        logical = _calculate_root_logical_size()
        allocated = _calculate_root_allocated_size()
        with _root_size_cache['lock']:
            _root_size_cache['logical'] = logical
            _root_size_cache['allocated'] = allocated
            _root_size_cache['size'] = allocated
            _root_size_cache['updated'] = time.time()
        time.sleep(60)


def _ensure_root_size_worker():
    """Start the background root size worker if not already running.

    Computes the real size synchronously once before returning, so the first
    /api/disk request (and thus the first page load) sees the correct value
    instead of the stale 0 the cache is initialized with.
    """
    with _root_size_cache['lock']:
        if _root_size_cache['started']:
            return
        _root_size_cache['started'] = True
        # Synchronous first computation: avoids showing 0 / a wrong number on
        # the very first page load (the worker loop sleeps up to 60s otherwise).
        try:
            logical = _calculate_root_logical_size()
            allocated = _calculate_root_allocated_size()
            _root_size_cache['logical'] = logical
            _root_size_cache['allocated'] = allocated
            _root_size_cache['size'] = allocated
            _root_size_cache['updated'] = time.time()
        except OSError:
            pass
    t = threading.Thread(target=_calc_root_size, daemon=True)
    t.start()


# ============================================================
# Threaded HTTP Server
# ============================================================
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ============================================================
# Main Entry Point
# ============================================================
def main():
    global ROOT_DIR, AUTH_CRED

    parser = argparse.ArgumentParser(
        description='WebDAV 文件管理器 - 零依赖 Python 文件管理服务器',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
使用示例:
  python3 server.py                             # 使用默认设置启动
  python3 server.py -p 8080 -r /data/files      # 指定端口和根目录
  python3 server.py -a admin:123456              # 启用认证
  python3 server.py -H 127.0.0.1 -p 9000        # 仅本地访问
  WEBDAV_AUTH=admin:123456 python3 server.py     # 通过环境变量传入凭据(更安全)
        ''')
    parser.add_argument('-p', '--port', type=int, default=8080,
                        help='监听端口 (默认: 8080)')
    parser.add_argument('-r', '--root', default='.',
                        help='文件根目录 (默认: 当前目录)')
    parser.add_argument('-a', '--auth', default=None,
                        help='认证信息，格式: 用户名:密码')
    parser.add_argument('-H', '--host', default='0.0.0.0',
                        help='监听地址 (默认: 0.0.0.0)')
    parser.add_argument('--allow-no-auth', action='store_true',
                        help='允许在没有配置认证时启动（危险：任何能访问端口的人都有完整权限）')
    args = parser.parse_args()

    ROOT_DIR = os.path.realpath(args.root)
    if not os.path.isdir(ROOT_DIR):
        _print_safe(f'[ERROR] Root directory "{args.root}" does not exist')
        sys.exit(1)

    # Security: prefer env var over CLI arg; local auth file persists web password changes.
    AUTH_CRED = load_auth_cred() or os.environ.get('WEBDAV_AUTH') or args.auth
    if not AUTH_CRED:
        # 无认证时所有鉴权检查都直接放行（完整的上传/下载/删除/分享能力），
        # 而默认监听 0.0.0.0，等于把整块磁盘交给能访问该端口的任何人。
        # 明确要求显式表态，而不是静默地不设防启动。
        if args.allow_no_auth:
            _print_safe(
                '[WARN] 已按 --allow-no-auth 在无认证模式下启动。'
                '任何能访问该端口的人都拥有完整文件管理权限。'
            )
        else:
            _print_safe(
                '[ERROR] 未配置认证信息，拒绝启动。\n'
                '        请任选其一：\n'
                '          -a 用户名:密码\n'
                '          环境变量 WEBDAV_AUTH=用户名:密码\n'
                '        确实需要无认证（例如仅绑定 127.0.0.1 供本机使用）时，\n'
                '        请显式加上 --allow-no-auth。'
            )
            sys.exit(1)
    if AUTH_CRED and not os.path.isfile(AUTH_FILE):
        try:
            save_auth_cred(AUTH_CRED)
        except OSError:
            pass

    server = ThreadedHTTPServer((args.host, args.port), FileManagerHandler)

    auth_user = (AUTH_CRED or '').split(':', 1)[0]
    auth_info = f'ON ({auth_user})' if AUTH_CRED else 'OFF'
    banner = f'''
+---------------------------------------------------+
|          WebDAV File Manager Started               |
+---------------------------------------------------+
|                                                    |
|  Web UI:   http://{args.host}:{args.port}/
|  WebDAV:   http://{args.host}:{args.port}/dav/
|  Root Dir: {ROOT_DIR}
|  Auth:     {auth_info}
|                                                    |
+---------------------------------------------------+
'''
    _print_safe(banner)

    # Resume unfinished remote download tasks from persistent state
    try:
        resume_persisted_remote_tasks()
    except Exception as e:
        _print_safe(f'[WARN] resume remote tasks failed: {e}')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _print_safe('\nShutting down server...')
        server.shutdown()
        _print_safe('Server stopped.')


def _print_safe(text):
    """Print text safely, handling encoding issues on Windows."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode('utf-8', errors='replace').decode('ascii', errors='replace'))


if __name__ == '__main__':
    main()
