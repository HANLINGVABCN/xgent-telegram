#!/usr/bin/env python3
import argparse
import base64
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import secrets
import socketserver
import time
import urllib.parse
from pathlib import Path


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>便签</title>
  <style>
    :root { color-scheme: light; --bg:#f6f7f9; --panel:#fff; --text:#1f2937; --muted:#6b7280; --line:#d8dee8; --brand:#2563eb; --danger:#dc2626; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
    button, input, textarea { font: inherit; }
    .login { min-height:100vh; display:grid; place-items:center; padding:24px; }
    .login form { width:min(360px,100%); background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:22px; box-shadow:0 10px 30px rgba(15,23,42,.08); }
    h1 { margin:0 0 18px; font-size:22px; }
    label { display:block; margin:12px 0 6px; color:var(--muted); }
    input, textarea { width:100%; border:1px solid var(--line); border-radius:6px; background:#fff; color:var(--text); outline:none; }
    input { height:38px; padding:0 10px; }
    textarea { min-height:360px; resize:vertical; padding:12px; }
    input:focus, textarea:focus { border-color:var(--brand); box-shadow:0 0 0 3px rgba(37,99,235,.12); }
    button { border:1px solid var(--line); background:#fff; color:var(--text); border-radius:6px; height:36px; padding:0 12px; cursor:pointer; }
    button.primary { background:var(--brand); color:#fff; border-color:var(--brand); }
    button.danger { color:var(--danger); }
    .app { min-height:100vh; display:grid; grid-template-columns:320px 1fr; }
    aside { background:var(--panel); border-right:1px solid var(--line); min-height:100vh; display:flex; flex-direction:column; }
    .top { padding:14px; border-bottom:1px solid var(--line); display:grid; gap:10px; }
    .row { display:flex; gap:8px; align-items:center; }
    .row input { flex:1; }
    .notes { overflow:auto; padding:8px; }
    .note { width:100%; height:auto; text-align:left; display:block; border:1px solid transparent; padding:10px; margin-bottom:6px; background:transparent; }
    .note:hover, .note.active { background:#eef4ff; border-color:#c7d7fe; }
    .note-title { font-weight:650; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .note-preview { color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px; }
    main { padding:18px; min-width:0; }
    .editor { max-width:980px; margin:0 auto; display:grid; gap:12px; }
    .toolbar { display:flex; justify-content:space-between; gap:10px; align-items:center; }
    .actions { display:flex; gap:8px; }
    .status { color:var(--muted); font-size:12px; min-height:18px; }
    .empty { height:70vh; display:grid; place-items:center; color:var(--muted); }
    @media (max-width: 760px) {
      .app { grid-template-columns:1fr; }
      aside { min-height:auto; border-right:0; border-bottom:1px solid var(--line); }
      .notes { max-height:38vh; }
      main { padding:12px; }
      textarea { min-height:300px; }
    }
  </style>
</head>
<body>
  <div id="root"></div>
  <script>
    const root = document.getElementById('root');
    let notes = [];
    let current = null;
    let query = '';

    async function api(path, options = {}) {
      const res = await fetch(path, {
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
        ...options
      });
      if (res.status === 401) { renderLogin(); throw new Error('unauthorized'); }
      const text = await res.text();
      const data = text ? JSON.parse(text) : {};
      if (!res.ok) throw new Error(data.error || '请求失败');
      return data;
    }

    function renderLogin(message = '') {
      root.innerHTML = `<div class="login"><form id="loginForm">
        <h1>便签登录</h1>
        <label>账号</label><input name="username" autocomplete="username" required>
        <label>密码</label><input name="password" type="password" autocomplete="current-password" required>
        <p class="status">${escapeHtml(message)}</p>
        <button class="primary" type="submit">登录</button>
      </form></div>`;
      document.getElementById('loginForm').onsubmit = async (e) => {
        e.preventDefault();
        const fd = new FormData(e.target);
        try {
          await api('/api/login', { method:'POST', body: JSON.stringify({ username: fd.get('username'), password: fd.get('password') }) });
          await loadNotes();
        } catch (err) {
          renderLogin(err.message);
        }
      };
    }

    function renderApp() {
      root.innerHTML = `<div class="app">
        <aside>
          <div class="top">
            <div class="row"><input id="search" placeholder="搜索便签" value="${escapeAttr(query)}"><button id="newBtn" class="primary">新建</button></div>
            <div class="row"><button id="logoutBtn">退出登录</button></div>
          </div>
          <div class="notes" id="notes"></div>
        </aside>
        <main id="main"></main>
      </div>`;
      document.getElementById('newBtn').onclick = createNote;
      document.getElementById('logoutBtn').onclick = async () => { await api('/api/logout', { method:'POST' }); renderLogin(); };
      document.getElementById('search').oninput = async (e) => { query = e.target.value; await loadNotes(false); };
      renderNotes();
      renderEditor();
    }

    function renderNotes() {
      const list = document.getElementById('notes');
      list.innerHTML = notes.map(n => `<button class="note ${current && current.id === n.id ? 'active' : ''}" data-id="${n.id}">
        <div class="note-title">${escapeHtml(n.title || '无标题')}</div>
        <div class="note-preview">${escapeHtml((n.content || '').replace(/\s+/g, ' ').slice(0, 80))}</div>
      </button>`).join('') || '<div class="empty">没有便签</div>';
      list.querySelectorAll('.note').forEach(btn => btn.onclick = () => openNote(btn.dataset.id));
    }

    function renderEditor() {
      const main = document.getElementById('main');
      if (!current) {
        main.innerHTML = '<div class="empty">选择或新建一条便签</div>';
        return;
      }
      main.innerHTML = `<div class="editor">
        <div class="toolbar">
          <div class="status" id="status">${formatTime(current.updated_at)}</div>
          <div class="actions"><button id="deleteBtn" class="danger">删除</button><button id="saveBtn" class="primary">保存</button></div>
        </div>
        <input id="title" placeholder="标题" value="${escapeAttr(current.title || '')}">
        <textarea id="content" placeholder="写点什么">${escapeHtml(current.content || '')}</textarea>
      </div>`;
      document.getElementById('saveBtn').onclick = saveCurrent;
      document.getElementById('deleteBtn').onclick = deleteCurrent;
    }

    async function loadNotes(keepCurrent = true) {
      const data = await api('/api/notes?q=' + encodeURIComponent(query));
      notes = data.notes || [];
      if (!keepCurrent || !current || !notes.some(n => n.id === current.id)) current = notes[0] || null;
      renderApp();
    }

    async function openNote(id) {
      current = await api('/api/notes/' + encodeURIComponent(id));
      renderApp();
    }

    async function createNote() {
      current = await api('/api/notes', { method:'POST', body: JSON.stringify({ title:'新便签', content:'' }) });
      query = '';
      await loadNotes(true);
      current = await api('/api/notes/' + encodeURIComponent(current.id));
      renderApp();
      document.getElementById('title').focus();
    }

    async function saveCurrent() {
      const title = document.getElementById('title').value;
      const content = document.getElementById('content').value;
      current = await api('/api/notes/' + encodeURIComponent(current.id), { method:'PUT', body: JSON.stringify({ title, content }) });
      document.getElementById('status').textContent = '已保存 ' + formatTime(current.updated_at);
      await loadNotes(true);
    }

    async function deleteCurrent() {
      if (!current || !confirm('删除这条便签？')) return;
      await api('/api/notes/' + encodeURIComponent(current.id), { method:'DELETE' });
      current = null;
      await loadNotes(false);
    }

    function escapeHtml(s) { return String(s ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
    function escapeAttr(s) { return escapeHtml(s).replace(/`/g, '&#96;'); }
    function formatTime(ts) { return ts ? new Date(ts * 1000).toLocaleString() : ''; }
    loadNotes().catch(() => renderLogin());
  </script>
</body>
</html>"""


class NotesStore:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.notes_file = self.data_dir / "notes.json"
        self.auth_file = self.data_dir / "auth.json"

    def load_notes(self):
        if not self.notes_file.exists():
            return []
        try:
            with self.notes_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def save_notes(self, notes):
        tmp = self.notes_file.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(notes, f, ensure_ascii=False, indent=2)
        tmp.replace(self.notes_file)

    def write_auth(self, username, password):
        salt = secrets.token_hex(16)
        password_hash = hash_password(password, salt)
        secret = secrets.token_hex(32)
        data = {"username": username, "salt": salt, "password_hash": password_hash, "secret": secret}
        with self.auth_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(self.auth_file, 0o600)

    def load_auth(self):
        with self.auth_file.open("r", encoding="utf-8") as f:
            return json.load(f)


def hash_password(password, salt):
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return base64.b64encode(digest).decode()


def make_cookie(username, secret):
    ts = str(int(time.time()))
    payload = f"{username}:{ts}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()


def verify_cookie(value, auth):
    try:
        raw = base64.urlsafe_b64decode(value.encode()).decode()
        username, ts, sig = raw.rsplit(":", 2)
        if username != auth["username"]:
            return False
        payload = f"{username}:{ts}"
        expected = hmac.new(auth["secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "SimpleNotes/1.0"

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self.send_html(INDEX_HTML)
            return
        if self.path.startswith("/api/notes"):
            if not self.require_auth():
                return
            self.handle_get_notes()
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/login":
            self.handle_login()
            return
        if self.path == "/api/logout":
            self.send_json({"ok": True}, headers={"Set-Cookie": "notes_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
            return
        if self.path == "/api/notes":
            if not self.require_auth():
                return
            self.handle_create_note()
            return
        self.send_error(404)

    def do_PUT(self):
        if self.path.startswith("/api/notes/"):
            if not self.require_auth():
                return
            self.handle_update_note()
            return
        self.send_error(404)

    def do_DELETE(self):
        if self.path.startswith("/api/notes/"):
            if not self.require_auth():
                return
            self.handle_delete_note()
            return
        self.send_error(404)

    def parse_json(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def note_id_from_path(self):
        return urllib.parse.unquote(self.path.split("/api/notes/", 1)[1].split("?", 1)[0])

    def require_auth(self):
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        value = cookie.get("notes_session")
        try:
            auth = self.server.store.load_auth()
        except Exception:
            self.send_json({"error": "auth is not configured"}, status=500)
            return False
        if value and verify_cookie(value.value, auth):
            return True
        self.send_json({"error": "unauthorized"}, status=401)
        return False

    def handle_login(self):
        data = self.parse_json()
        auth = self.server.store.load_auth()
        username = str(data.get("username") or "")
        password = str(data.get("password") or "")
        password_hash = hash_password(password, auth["salt"])
        if username != auth["username"] or not hmac.compare_digest(password_hash, auth["password_hash"]):
            self.send_json({"error": "账号或密码错误"}, status=403)
            return
        session = make_cookie(username, auth["secret"])
        self.send_json({"ok": True}, headers={"Set-Cookie": f"notes_session={session}; Path=/; HttpOnly; SameSite=Lax"})

    def handle_get_notes(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = parsed.path.rstrip("/").split("/")
        notes = self.server.store.load_notes()
        if len(parts) == 4 and parts[-1]:
            note_id = parts[-1]
            for note in notes:
                if note["id"] == note_id:
                    self.send_json(note)
                    return
            self.send_json({"error": "not found"}, status=404)
            return
        q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].strip().lower()
        if q:
            notes = [n for n in notes if q in (n.get("title", "") + "\n" + n.get("content", "")).lower()]
        notes.sort(key=lambda n: n.get("updated_at", 0), reverse=True)
        self.send_json({"notes": notes})

    def handle_create_note(self):
        data = self.parse_json()
        now = time.time()
        note = {
            "id": secrets.token_hex(8),
            "title": str(data.get("title") or "新便签"),
            "content": str(data.get("content") or ""),
            "created_at": now,
            "updated_at": now,
        }
        notes = self.server.store.load_notes()
        notes.append(note)
        self.server.store.save_notes(notes)
        self.send_json(note, status=201)

    def handle_update_note(self):
        note_id = self.note_id_from_path()
        data = self.parse_json()
        notes = self.server.store.load_notes()
        for note in notes:
            if note["id"] == note_id:
                note["title"] = str(data.get("title") or "").strip() or "无标题"
                note["content"] = str(data.get("content") or "")
                note["updated_at"] = time.time()
                self.server.store.save_notes(notes)
                self.send_json(note)
                return
        self.send_json({"error": "not found"}, status=404)

    def handle_delete_note(self):
        note_id = self.note_id_from_path()
        notes = self.server.store.load_notes()
        new_notes = [n for n in notes if n["id"] != note_id]
        if len(new_notes) == len(notes):
            self.send_json({"error": "not found"}, status=404)
            return
        self.server.store.save_notes(new_notes)
        self.send_json({"ok": True})

    def send_html(self, body):
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_json(self, data, status=200, headers=None):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description="Simple private notes web app")
    parser.add_argument("-H", "--host", default="0.0.0.0")
    parser.add_argument("-p", "--port", type=int, default=8899)
    parser.add_argument("-d", "--data-dir", default="./data")
    parser.add_argument("-u", "--username")
    parser.add_argument("-P", "--password")
    parser.add_argument("--init-auth", action="store_true")
    args = parser.parse_args()

    store = NotesStore(args.data_dir)
    if args.username and args.password:
        store.write_auth(args.username, args.password)
        if args.init_auth:
            return
    if not store.auth_file.exists():
        raise SystemExit("auth is not configured; run with --username and --password once")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.store = store
    print(f"Simple Notes listening on http://{args.host}:{args.port}/ data={store.data_dir}")
    server.serve_forever()


if __name__ == "__main__":
    main()
