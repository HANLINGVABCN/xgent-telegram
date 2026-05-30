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
import mimetypes
import base64
import hmac
import secrets
import uuid
import time
import threading
import zipfile
import tarfile
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
STATE_LOCK = threading.Lock()
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'filemanager_state.json')
AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'filemanager_auth.json')
MAX_TTL_SECONDS = 10 * 365 * 24 * 60 * 60
MAX_SPARSE_FILE_SIZE = 100 * 1024 * 1024 * 1024
MAX_TEXT_EDIT_SIZE = 10 * 1024 * 1024
WEBDAV_DIR_NAME = 'webdav'

# Login rate limiter: { ip: { 'fails': int, 'locked_until': float } }
_login_attempts = {}
_login_attempts_lock = threading.Lock()
LOGIN_MAX_FAILS = 5
LOGIN_LOCKOUT_SECONDS = 60

# Background cache for root directory size calculation
_root_size_cache = {'size': 0, 'lock': threading.Lock(), 'started': False}

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
    return {'shares': {}, 'tempFiles': {}}


def load_state():
    with STATE_LOCK:
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            data = _empty_state()
        data.setdefault('shares', {})
        data.setdefault('tempFiles', {})
        return data


def save_state(data):
    with STATE_LOCK:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)


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
        """Minimal logging - only log requests with status info."""
        ts = self.log_date_time_string()
        sys.stderr.write(f'[{ts}] {self.command} {self.path} - {fmt % args}\n')

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
        if auth.startswith('Basic '):
            try:
                decoded = base64.b64decode(auth[6:]).decode('utf-8')
                if self._safe_equal(decoded, AUTH_CRED):
                    return True
            except Exception:
                pass
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

    def _new_session_cookie(self):
        self._clear_expired_sessions()
        token = secrets.token_urlsafe(32)
        with SESSIONS_LOCK:
            SESSIONS[token] = time.time() + SESSION_MAX_AGE
        return f'{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_MAX_AGE}; HttpOnly; SameSite=Lax'

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
        return json.loads(self.read_body().decode('utf-8'))

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
        """Get client IP, respecting X-Forwarded-For for reverse proxies."""
        forwarded = self.headers.get('X-Forwarded-For', '')
        if forwarded:
            return forwarded.split(',')[0].strip()
        return self.client_address[0] if self.client_address else '0.0.0.0'

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
        }
        handler = routes.get(path)
        if handler:
            handler(qs)
        else:
            self.send_err(404, 'API not found')

    def _public_share_download(self, path, qs, head_only=False):
        cleanup_expired_temp_files()
        token = path.rsplit('/', 1)[-1]
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
        u = shutil.disk_usage(ROOT_DIR)
        root_size = _calculate_root_logical_size()
        root_allocated_size = _calculate_root_allocated_size()
        self.send_json({
            'total': u.total,
            'used': u.used,
            'free': u.free,
            'rootSize': root_size,
            'rootAllocatedSize': root_allocated_size,
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
        if info and info.get('isDir'):
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

    def _api_shares(self, qs):
        cleanup_expired_temp_files()
        state = load_state()
        items = []
        for token, meta in state.get('shares', {}).items():
            req_path = meta.get('path', '')
            full = safe_join(ROOT_DIR, req_path)
            exists = bool(full and os.path.isfile(full))
            size = os.path.getsize(full) if exists else 0
            expires_at = float(meta.get('expiresAt') or 0)
            items.append({
                'token': token,
                'path': req_path,
                'name': os.path.basename(req_path.rstrip('/')) or req_path,
                'size': size,
                'exists': exists,
                'createdAt': float(meta.get('createdAt') or 0),
                'expiresAt': expires_at,
                'remaining': max(0, int(expires_at - time.time())) if expires_at else 0,
                'url': self._share_url(token),
            })
        items.sort(key=lambda x: x['expiresAt'])
        self.send_json({'success': True, 'items': items})

    def _share_url(self, token):
        host = self.headers.get('Host') or ''
        proto = self.headers.get('X-Forwarded-Proto') or 'http'
        return f'{proto}://{host}/s/{token}' if host else f'/s/{token}'

    def _api_download(self, qs):
        cleanup_expired_temp_files()
        req_path = qs.get('path', [''])[0]
        full = safe_join(ROOT_DIR, req_path)
        if not full or not os.path.isfile(full):
            return self.send_err(404, '文件不存在')
        self._stream_file(full)

    def _stream_file(self, full, head_only=False):
        file_size = os.path.getsize(full)
        filename = os.path.basename(full)
        start = 0
        end = file_size - 1
        status = 200
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
                        self.send_response(416)
                        self.send_header('Content-Range', f'bytes */{file_size}')
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                    status = 206
                except ValueError:
                    start, end, status = 0, file_size - 1, 200
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header('Content-Type', guess_mime(full))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
        self.send_header('Content-Disposition', build_content_disposition(filename))
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
            '/api/mkdir': self._api_mkdir,
            '/api/create-text': self._api_create_text,
            '/api/create-speed-file': self._api_create_speed_file,
            '/api/share': self._api_share,
            '/api/share-delete': self._api_share_delete,
            '/api/save-text': self._api_save_text,
            '/api/delete': self._api_delete,
            '/api/rename': self._api_rename,
            '/api/copy': self._api_copy,
            '/api/move': self._api_move,
            '/api/archive': self._api_archive,
            '/api/extract': self._api_extract,
        }
        handler = routes.get(path)
        if handler:
            handler()
        else:
            self.send_err(404, 'API not found')

    def _sanitize_error(self, e):
        """Sanitize error message to avoid leaking internal paths."""
        msg = str(e)
        if ROOT_DIR and ROOT_DIR in msg:
            msg = msg.replace(ROOT_DIR, '<root>')
        return msg

    def _api_upload(self, qs):
        target_dir = qs.get('path', ['/'])[0]
        raw_filename = qs.get('name', [''])[0]
        if not raw_filename:
            return self.send_err(400, '缺少文件名')
        # Security: strip path components to prevent directory traversal
        filename = os.path.basename(raw_filename)
        if not filename or filename.startswith('.'):
            return self.send_err(400, '文件名无效')
        dir_path = safe_join(ROOT_DIR, target_dir)
        if not dir_path or not os.path.isdir(dir_path):
            return self.send_err(400, '目标目录不存在')
        file_path = os.path.join(dir_path, filename)
        # Verify final path is within ROOT_DIR
        if not os.path.realpath(file_path).startswith(os.path.realpath(ROOT_DIR) + os.sep) and \
           os.path.realpath(file_path) != os.path.realpath(ROOT_DIR):
            return self.send_err(403, '路径非法')
        content_length = int(self.headers.get('Content-Length', 0))
        try:
            with open(file_path, 'wb') as f:
                remaining = content_length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            self.send_ok('上传成功')
        except OSError as e:
            self.send_err(500, f'上传失败: {self._sanitize_error(e)}')

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
            'createdAt': time.time(),
            'expiresAt': expires_at,
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
        if token not in state.get('shares', {}):
            return self.send_err(404, '链接不存在或已过期')
        state['shares'].pop(token, None)
        save_state(state)
        self.send_ok('链接已删除')

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
        new_fp = os.path.join(os.path.dirname(fp), new_name)
        if not os.path.realpath(new_fp).startswith(os.path.realpath(ROOT_DIR)):
            return self.send_err(403, '路径非法')
        if os.path.exists(new_fp):
            return self.send_err(400, '已存在同名文件或文件夹')
        try:
            os.rename(fp, new_fp)
            self.send_ok('重命名成功')
        except OSError as e:
            self.send_err(500, f'重命名失败: {e}')

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
                base, ext = os.path.splitext(os.path.basename(sp))
                i = 1
                while os.path.exists(target):
                    target = os.path.join(dp, f'{base}_副本{i}{ext}')
                    i += 1
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

        overwrite = self.headers.get('Overwrite', 'T').upper() == 'T'
        existed = os.path.exists(dest_full)

        if existed and not overwrite:
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

        overwrite = self.headers.get('Overwrite', 'T').upper() == 'T'
        existed = os.path.exists(dest_full)

        if existed and not overwrite:
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
    """Compatibility worker: periodically calculate ROOT_DIR allocated size."""
    while True:
        total = _calculate_root_allocated_size()
        with _root_size_cache['lock']:
            _root_size_cache['size'] = total
        time.sleep(60)


def _ensure_root_size_worker():
    """Start the background root size worker if not already running."""
    with _root_size_cache['lock']:
        if _root_size_cache['started']:
            return
        _root_size_cache['started'] = True
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
    args = parser.parse_args()

    ROOT_DIR = os.path.realpath(args.root)
    if not os.path.isdir(ROOT_DIR):
        _print_safe(f'[ERROR] Root directory "{args.root}" does not exist')
        sys.exit(1)

    # Security: prefer env var over CLI arg; local auth file persists web password changes.
    AUTH_CRED = load_auth_cred() or os.environ.get('WEBDAV_AUTH') or args.auth
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
