"""Web Chat 的 HTTP 服务。

零外部依赖，用 stdlib http.server，与 skill/script/ 下两个服务器的做法一致
（那里也是刻意零依赖，方便 Agent 直接部署）。

线程模型：
  - PTB 的 asyncio 事件循环跑对话核心
  - ThreadingHTTPServer 的工作线程处理 HTTP 请求
  - 两边通过 WebOutbox（线程安全队列）和 run_coroutine_threadsafe 通信

安全基线逐条对齐 skill/script/notes/server.py 与 webdav-filemanager/server.py：
未设密码拒绝启动、默认只绑 127.0.0.1、认证前就限制请求体大小、同源 CSRF
校验、限速不信任 X-Forwarded-For、不发 CORS 头。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import http.server
import json
import logging
import mimetypes
import os
import socketserver
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from xgent_app import web_auth
from xgent_app import web_terminal
from xgent_app.web_bridge import MEDIA_TOKEN_REGISTRY, WebOutbox, build_web_conversation_objects

logger = logging.getLogger(__name__)

# 认证前就要挡住的请求体上限。parse_json 在 /api/login 里跑在认证之前，
# 不限大小的话一个未授权请求就能让工作线程吃满内存。
MAX_REQUEST_BODY = 1 * 1024 * 1024

# 文件上传请求体上限的两档基线，对齐 agent_sendfile.py 的发送侧阈值：
#   官方 api.telegram.org —— send_document 上限 50MB
#   本地 Bot API server  —— send_document 上限 2GB
# 实际生效值由 WebChatConfig.upload_body_limit 携带（idle.py 据 API_BASE_URL 选档），
# 这里只是默认值。读体前先校验，避免大文件把工作线程内存打爆。
MAX_UPLOAD_BODY_OFFICIAL = 50 * 1024 * 1024
MAX_UPLOAD_BODY_LOCAL_API = 2 * 1024 * 1024 * 1024

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")
VENDOR_DIR = os.path.join(STATIC_DIR, "vendor")
# 允许直接访问的静态资源白名单：路径 -> (磁盘文件名, Content-Type)。
# 只列出这几个固定文件，不做通用目录遍历——避免 /vendor/../xxx 这类路径
# 逃逸到 webui 之外读取任意文件。这几个文件本地自带，纯 Web 模式下不再需要
# 联网拉取 telegram.org / cdn.jsdelivr.net（这两个域名在部分网络环境下访问
# 缓慢甚至超时，之前把它们当阻塞 <script> 直接写在 <head> 里，会让整个页面
# 卡在资源加载上迟迟不能渲染——这就是"访问网页端慢死"的根因）。
VENDOR_ASSETS: Dict[str, Any] = {
    "/vendor/telegram-web-app.js": ("telegram-web-app.js", "application/javascript; charset=utf-8"),
    "/vendor/xterm.min.js": ("xterm.min.js", "application/javascript; charset=utf-8"),
    "/vendor/xterm.min.css": ("xterm.min.css", "text/css; charset=utf-8"),
    "/vendor/addon-fit.min.js": ("addon-fit.min.js", "application/javascript; charset=utf-8"),
}

# SSE 心跳间隔。低于常见反代的 60s 空闲超时。
SSE_HEARTBEAT_SECONDS = 25.0


class WebChatConfig:
    """服务运行期需要的一切，由 sections 侧组装后传进来。

    用回调而不是直接 import sections 里的函数：sections 靠共享命名空间加载，
    本模块要保持可独立 import 和单测。
    """

    def __init__(
        self,
        host: str,
        port: int,
        password_hash: str,
        bot_token: str,
        authorized_user_id: int,
        loop: asyncio.AbstractEventLoop,
        submit_message: Callable[[str, WebOutbox], Any],
        read_history: Callable[[int], Any],
        read_settings: Callable[[], Any],
        write_setting: Callable[[str, Any], Any],
        request_stop: Callable[[], None],
        is_busy: Callable[[], bool],
        submit_callback: Optional[Callable[[str, int, WebOutbox], Any]] = None,
        submit_command: Optional[Callable[[str, WebOutbox], Any]] = None,
        submit_upload: Optional[Callable[[str, bytes, str, WebOutbox], Any]] = None,
        upload_body_limit: int = MAX_UPLOAD_BODY_OFFICIAL,
        is_terminal_enabled: Optional[Callable[[], bool]] = None,
        is_web_enabled: Optional[Callable[[], bool]] = None,
        media_allowed_roots: Optional[List[str]] = None,
    ):
        self.host = host
        self.port = port
        self.password_hash = password_hash
        self.bot_token = bot_token
        self.authorized_user_id = authorized_user_id
        self.loop = loop
        self.submit_message = submit_message
        # 网页按钮 / 命令路由。可选，保留向后兼容（旧测试构造 WebChatConfig 时不传）。
        self.submit_callback = submit_callback or (lambda *a: None)
        self.submit_command = submit_command or (lambda *a: None)
        # 网页文件上传。可选：旧构造路径不传时回退为占位（没人会调到，因为
        # /api/upload 路由只在 idle 注入了真实回放时才注册语义）。
        self.submit_upload = submit_upload or (lambda *a: None)
        # 上传请求体上限。由 idle.py 按 API_BASE_URL 选档传入，web_server 本身
        # 不 import sections，避免破坏「可独立 import」约定。
        self.upload_body_limit = int(upload_body_limit)
        # 终端开关。默认关闭——终端是任意命令执行，必须显式开启。
        self.is_terminal_enabled = is_terminal_enabled or (lambda: False)
        self.is_web_enabled = is_web_enabled or (lambda: True)
        # /api/media/resolve 允许换 token 的根目录白名单（xgent_storage、
        # agent workspace 这类"本应用自己的数据目录"）。由 sections 侧组装
        # 传入：web_server 不 import sections，保持可独立单测的约定。
        self.media_allowed_roots = [str(r) for r in (media_allowed_roots or [])]
        self.read_history = read_history
        self.read_settings = read_settings
        self.write_setting = write_setting
        self.request_stop = request_stop
        self.is_busy = is_busy


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "XGentWeb/1.0"
    protocol_version = "HTTP/1.1"

    # --- 基础设施 ---

    @property
    def config(self) -> WebChatConfig:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        # 默认实现直接往 stderr 写，会污染 bot 日志。降级到 debug 且不记路径
        # （路径里可能带查询参数）。
        logger.debug("web %s", fmt % args)

    def _client_ip(self) -> str:
        # 只用 TCP 对端地址。X-Forwarded-For 由客户端提供，信它等于让攻击者
        # 每次换一个值就能绕开限速。
        return self.client_address[0] if self.client_address else "unknown"

    def _headers_dict(self) -> Dict[str, str]:
        return {key: value for key, value in self.headers.items()}

    def _send_json(self, data: Any, status: int = 200,
                   extra_headers: Optional[Dict[str, str]] = None) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(raw)
        if status != 200 and getattr(self, "command", "") == "POST":
            with contextlib.suppress(Exception):
                length = int(self.headers.get("Content-Length") or 0)
                if 0 < length <= MAX_REQUEST_BODY:
                    self.rfile.read(length)

    def _send_html(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        # WebApp 需要能被 Telegram 内嵌，所以不发 X-Frame-Options: DENY。
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def _read_json(self) -> Optional[Dict[str, Any]]:
        """读请求体。超限或格式错误时直接回错误响应并返回 None。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._send_json({"error": "bad content-length"}, status=400)
            return None
        if length < 0 or length > MAX_REQUEST_BODY:
            self._send_json({"error": "请求体过大"}, status=413)
            return None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "invalid json"}, status=400)
            return None
        return data if isinstance(data, dict) else {}

    def _parse_multipart_boundary(self) -> Optional[bytes]:
        """从 Content-Type 头里取 multipart/form-data 的 boundary。

        格式：multipart/form-data; boundary=----xxx。boundary 在帧里会前后各
        多两个横杠（--boundary），头里给的是不含那两个横杠的原始值。
        """
        ctype = self.headers.get("Content-Type", "") or ""
        if "multipart/form-data" not in ctype:
            return None
        # 直接 split 而不用 cgi/email（cgi 在 3.13 已移除，且本项目零依赖）。
        for part in ctype.split(";"):
            part = part.strip()
            if part.lower().startswith("boundary="):
                # 头里的 boundary 可能带引号。
                value = part[len("boundary="):].strip().strip('"')
                if value:
                    return value.encode("utf-8")
        return None

    def _read_multipart(self) -> Optional[Dict[str, Any]]:
        """读 multipart/form-data 上传体。返回 {file, filename, text} 或 None。

        只认两个字段：名为 file 的二进制部分（提 filename/字节），名为 text
        的附言（可空）。超限或格式错直接回错误响应。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._send_json({"error": "bad content-length"}, status=400)
            return None
        if length <= 0:
            self._send_json({"error": "空请求体"}, status=400)
            return None
        if length > self.config.upload_body_limit:
            self._send_json({"error": "文件过大"}, status=413)
            return None

        boundary = self._parse_multipart_boundary()
        if not boundary:
            self._send_json({"error": "缺少 boundary"}, status=400)
            return None

        # 一次性读完整请求体（上限 50MB，工作线程内存可控）。流式解析也能写，
        # 但 stdlib BaseHTTPRequestHandler 拿不到可靠的可读流式接口，且 50MB
        # 一次性读对本服务的并发量足够。
        body = self.rfile.read(length)

        result: Dict[str, Any] = {"file": None, "filename": None, "text": ""}
        delimiter = b"--" + boundary
        # 按 --boundary 切块。第一个块是前置 CRLF（空），最后两个是 -- 和 结束。
        segments = body.split(delimiter)
        for seg in segments:
            # 结束边界之后的内容是尾部 "--\r\n"，跳过。
            if seg in (b"", b"--", b"--\r\n", b"\r\n"):
                continue
            # 每块前面有 \r\n，后面也有 \r\n。剥掉。
            if seg.startswith(b"\r\n"):
                seg = seg[2:]
            if seg.endswith(b"\r\n"):
                seg = seg[:-2]
            # 块 = 头部CRLF +正文。头与正文用空行 CRLF CRLF 分隔。
            header_end = seg.find(b"\r\n\r\n")
            if header_end < 0:
                continue
            header_bytes = seg[:header_end].decode("utf-8", errors="replace")
            content = seg[header_end + 4:]

            # 解析 Content-Disposition：name="file"; filename="xxx.txt"
            name = None
            filename = None
            for line in header_bytes.split("\r\n"):
                low = line.lower()
                if low.startswith("content-disposition:"):
                    for field in line.split(";"):
                        field = field.strip()
                        if field.startswith("name="):
                            name = field[len("name="):].strip().strip('"')
                        elif field.startswith("filename="):
                            filename = field[len("filename="):].strip().strip('"')
                elif low.startswith("content-type:"):
                    # 不用，文件类型靠 ArtifactManager 的 mimetypes 推断。
                    pass

            if name == "file":
                if not content:
                    self._send_json({"error": "空文件"}, status=400)
                    return None
                result["file"] = content
                result["filename"] = filename or "upload"
            elif name == "text":
                result["text"] = content.decode("utf-8", errors="replace").strip()

        if result["file"] is None:
            self._send_json({"error": "缺少文件"}, status=400)
            return None
        return result

    # --- 认证 ---

    def _is_authenticated(self) -> bool:
        cookies = web_auth.parse_cookie_header(self.headers.get("Cookie", ""))
        value = cookies.get(web_auth.SESSION_COOKIE_NAME, "")
        return web_auth.verify_session_cookie(self.server.session_key, value)  # type: ignore[attr-defined]

    def _require_auth(self) -> bool:
        if self._is_authenticated():
            return True
        self._send_json({"error": "未登录"}, status=401)
        return False

    def _require_same_origin(self) -> bool:
        """所有变更型请求都要过。理由见 web_auth.is_same_origin 的注释。"""
        if web_auth.is_same_origin(
            self.headers.get("Origin", ""),
            self.headers.get("Referer", ""),
            self.headers.get("Host", ""),
        ):
            return True
        self._send_json({"error": "跨站请求被拒绝"}, status=403)
        return False

    def _run_coro(self, coro: Any, timeout: float = 30.0) -> Any:
        """把协程丢回 PTB 的事件循环执行并等结果。"""
        future = asyncio.run_coroutine_threadsafe(coro, self.config.loop)
        return future.result(timeout=timeout)

    # --- 路由 ---

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/":
                self._serve_index()
            elif path == "/terminal":
                self._serve_terminal()
            elif path in VENDOR_ASSETS:
                self._serve_vendor_asset(path)
            elif path.startswith("/api/media/"):
                self._handle_media_download(path[len("/api/media/"):])
            elif path == "/api/session":
                self._send_json({"authenticated": self._is_authenticated()})
            elif path == "/api/history":
                self._handle_history()
            elif path == "/api/config":
                self._handle_read_config()
            elif path == "/api/stream":
                self._handle_stream()
            elif path == "/api/term/output":
                self._handle_term_output()
            else:
                self._send_json({"error": "not found"}, status=404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logger.exception("web GET %s 失败", path)
            with contextlib.suppress(Exception):
                self._send_json({"error": "internal error"}, status=500)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            if not self._require_same_origin():
                return
            if path == "/api/login":
                self._handle_login()
            elif path == "/api/logout":
                self._handle_logout()
            elif path == "/api/chat":
                self._handle_chat()
            elif path == "/api/upload":
                self._handle_upload()
            elif path == "/api/callback":
                self._handle_callback()
            elif path == "/api/command":
                self._handle_command()
            elif path == "/api/stop":
                self._handle_stop()
            elif path == "/api/media/resolve":
                self._handle_media_resolve()
            elif path == "/api/config":
                self._handle_write_config()
            elif path == "/api/term/open":
                self._handle_term_open()
            elif path == "/api/term/input":
                self._handle_term_input()
            elif path == "/api/term/resize":
                self._handle_term_resize()
            elif path == "/api/term/close":
                self._handle_term_close()
            else:
                self._send_json({"error": "not found"}, status=404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logger.exception("web POST %s 失败", path)
            with contextlib.suppress(Exception):
                self._send_json({"error": "internal error"}, status=500)

    # --- 处理器 ---

    def _serve_index(self) -> None:
        if not self.config.is_web_enabled():
            term_on = self.config.is_terminal_enabled()
            if term_on:
                body = (
                    '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
                    '<title>XGent</title><style>'
                    ':root{color-scheme:dark}'
                    '*{box-sizing:border-box;margin:0;padding:0}'
                    'body{height:100%;display:flex;align-items:center;justify-content:center;'
                    'background:#17212b;color:#e9edf0;font-family:-apple-system,"Segoe UI",'
                    '"PingFang SC","Microsoft YaHei",sans-serif;padding:20px}'
                    '.card{background:#1c2733;border:1px solid #2b3a47;border-radius:16px;'
                    'padding:40px 32px;width:min(380px,92vw);text-align:center}'
                    '.icon{font-size:48px;margin-bottom:16px}'
                    '.card h1{font-size:20px;font-weight:600;margin-bottom:8px}'
                    '.card .sub{color:#7d8e9e;font-size:13px;line-height:1.6;margin-bottom:24px}'
                    '.btn{display:block;width:100%;padding:14px;border:none;border-radius:10px;'
                    'background:#5288c1;color:#fff;font-size:15px;font-weight:600;'
                    'cursor:pointer;text-decoration:none;transition:background .15s}'
                    '.btn:hover{background:#3a6a9e}'
                    '</style></head><body><div class="card">'
                    '<div class="icon">\U0001f5a5\ufe0f</div>'
                    '<h1>XGent 终端</h1>'
                    '<p class="sub">Web Chat 未开启。<br>终端已就绪。</p>'
                    '<a class="btn" href="/terminal">打开终端</a>'
                    '</div></body></html>'
                )
            else:
                body = (
                    '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
                    '<title>XGent</title><style>'
                    ':root{color-scheme:dark}'
                    '*{box-sizing:border-box;margin:0;padding:0}'
                    'body{height:100%;display:flex;align-items:center;justify-content:center;'
                    'background:#17212b;color:#e9edf0;font-family:-apple-system,"Segoe UI",'
                    '"PingFang SC","Microsoft YaHei",sans-serif;padding:20px}'
                    '.card{background:#1c2733;border:1px solid #2b3a47;border-radius:16px;'
                    'padding:40px 32px;width:min(380px,92vw);text-align:center}'
                    '.icon{font-size:48px;margin-bottom:16px}'
                    '.card h1{font-size:20px;font-weight:600;margin-bottom:8px}'
                    '.card .sub{color:#7d8e9e;font-size:13px;line-height:1.6;margin-bottom:24px}'
                    '</style></head><body><div class="card">'
                    '<div class="icon">\U0001f510</div>'
                    '<h1>Web 服务未开启</h1>'
                    '<p class="sub">请在 Telegram /start \u2192 \U0001f310 Web 里开启 Web Chat 或终端。</p>'
                    '</div></body></html>'
                )
            self._send_html(body.encode('utf-8'))
            return
        index_path = os.path.join(STATIC_DIR, "index.html")
        try:
            with open(index_path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._send_html(b"<h1>webui/index.html missing</h1>", status=500)
            return
        self._send_html(body)

    def _handle_login(self) -> None:
        ip = self._client_ip()
        limiter = self.server.limiter  # type: ignore[attr-defined]
        if limiter.is_locked(ip):
            self._send_json(
                {"error": f"尝试次数过多，请 {limiter.retry_after(ip)} 秒后再试"},
                status=429,
                extra_headers={"Retry-After": str(limiter.retry_after(ip))},
            )
            return

        data = self._read_json()
        if data is None:
            return

        authenticated = False

        # 路径一：Telegram WebApp 免密登录。initData 由 Telegram 用 bot token
        # 签名，校验通过再比对 user.id 等价于 check_authorized_user_middleware。
        init_data = str(data.get("init_data") or "")
        if init_data:
            parsed = web_auth.verify_telegram_init_data(init_data, self.config.bot_token)
            user_id = web_auth.init_data_user_id(parsed)
            if user_id is not None and user_id == self.config.authorized_user_id:
                authenticated = True
            else:
                logger.warning("web 登录：initData 校验失败或用户不匹配 (ip=%s)", ip)

        # 路径二：密码。
        if not authenticated:
            password = str(data.get("password") or "")
            if password and web_auth.verify_password(password, self.config.password_hash):
                authenticated = True

        if not authenticated:
            limiter.record_failure(ip)
            self._send_json({"error": "认证失败"}, status=401)
            return

        limiter.record_success(ip)
        cookie = web_auth.build_set_cookie(
            web_auth.make_session_cookie_value(self.server.session_key),  # type: ignore[attr-defined]
            secure=web_auth.is_https_request(self._headers_dict()),
        )
        self._send_json({"ok": True}, extra_headers={"Set-Cookie": cookie})

    def _handle_logout(self) -> None:
        self._send_json({"ok": True}, extra_headers={"Set-Cookie": web_auth.build_clear_cookie()})

    def _handle_history(self) -> None:
        if not self._require_auth():
            return
        if not self._require_web_enabled():
            return
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            limit_val = int((query.get("limit") or ["0"])[0])
        except ValueError:
            limit_val = 0
        limit = limit_val if limit_val > 0 else 1000000
        messages = self._run_coro(self.config.read_history(limit))
        self._send_json({"messages": messages})

    def _handle_read_config(self) -> None:
        if not self._require_auth():
            return
        self._send_json(self._run_coro(self.config.read_settings()))

    def _handle_write_config(self) -> None:
        if not self._require_auth():
            return
        data = self._read_json()
        if data is None:
            return
        key = str(data.get("key") or "")
        if not key:
            self._send_json({"error": "缺少 key"}, status=400)
            return
        try:
            result = self._run_coro(self.config.write_setting(key, data.get("value")))
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        self._send_json({"ok": True, "settings": result})

    def _handle_chat(self) -> None:
        if not self._require_auth():
            return
        if not self._require_web_enabled():
            return
        data = self._read_json()
        if data is None:
            return
        text = str(data.get("text") or "").strip()
        if not text:
            self._send_json({"error": "消息不能为空"}, status=400)
            return

        # 全局对话锁被占用时直接拒绝，语义与 Telegram 侧的"仍在处理"一致。
        if self.config.is_busy():
            self._send_json({"error": "系统仍在处理上一个请求，请稍后再发送"}, status=409)
            return

        outbox = self.server.outbox  # type: ignore[attr-defined]
        # submit_message 只负责把任务丢进事件循环，不等对话跑完——一轮 Agent
        # 对话可能跑好几分钟，HTTP 请求不能挂在那里。
        self.config.submit_message(text, outbox)
        self._send_json({"ok": True})

    def _handle_upload(self) -> None:
        """网页文件上传。校验与 /api/chat 一致，只是改读 multipart。

        结果同样经 SSE 推回：文件存盘→同步到 Telegram→进对话核心，HTTP 请求
        本身立即返回 ok，不等对话跑完。
        """
        if not self._require_auth():
            return
        if not self._require_web_enabled():
            return
        data = self._read_multipart()
        if data is None:
            return

        # 全局对话锁被占用时直接拒绝，语义与 /api/chat 的「仍在处理」一致。
        if self.config.is_busy():
            self._send_json({"error": "系统仍在处理上一个请求，请稍后再发送"}, status=409)
            return

        outbox = self.server.outbox  # type: ignore[attr-defined]
        filename = str(data.get("filename") or "upload")
        content = data["file"]   # _read_multipart 已保证非 None
        caption = str(data.get("text") or "")
        self.config.submit_upload(filename, content, caption, outbox)
        self._send_json({"ok": True})

    def _handle_stop(self) -> None:
        if not self._require_auth():
            return
        if not self._require_web_enabled():
            return
        self.config.request_stop()
        self._send_json({"ok": True})

    def _handle_callback(self) -> None:
        """网页内联按钮点击。复用 Telegram 的回调路由，结果经 SSE 推回。"""
        if not self._require_auth():
            return
        if not self._require_web_enabled():
            return
        data = self._read_json()
        if data is None:
            return
        callback_data = str(data.get("callback_data") or "")
        if not callback_data:
            self._send_json({"error": "缺少 callback_data"}, status=400)
            return
        try:
            message_id = int(data.get("message_id") or 0)
        except (TypeError, ValueError):
            message_id = 0

        outbox = self.server.outbox  # type: ignore[attr-defined]
        self.config.submit_callback(callback_data, message_id, outbox)
        self._send_json({"ok": True})

    def _handle_command(self) -> None:
        """网页 /命令。复用 Telegram 的 cmd_* 处理函数，结果经 SSE 推回。"""
        if not self._require_auth():
            return
        if not self._require_web_enabled():
            return
        data = self._read_json()
        if data is None:
            return
        command = str(data.get("command") or "").strip()
        if not command:
            self._send_json({"error": "命令不能为空"}, status=400)
            return
        if not command.startswith("/"):
            command = "/" + command

        outbox = self.server.outbox  # type: ignore[attr-defined]
        self.config.submit_command(command, outbox)
        self._send_json({"ok": True})

    def _handle_stream(self) -> None:
        if not self._require_auth():
            return
        if not self._require_web_enabled():
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        # 反代缓冲会让 SSE 完全失效（帧攒着不发）。
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        outbox = self.server.outbox  # type: ignore[attr-defined]
        stop_event = self.server.shutdown_event  # type: ignore[attr-defined]
        try:
            # 每条连接一个独立订阅：帧是广播给所有连接的，不是被谁抢走一份。
            # 退出 with 时自动摘除，死连接不会继续占着广播位。
            with outbox.subscribe() as stream:
                while not stop_event.is_set():
                    frame = stream.get(timeout=SSE_HEARTBEAT_SECONDS)
                    if frame is None:
                        # 超时或队列关闭：发注释行保活，顺便探测连接是否还在。
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    payload = json.dumps(frame, ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            # 浏览器关页面就是这条路径，属正常。
            pass

    # --- 终端 ---

    def _serve_terminal(self) -> None:
        # 页面本身不要求认证，与 / (index) 一致：前端 terminal.html 自己走登录
        # 流程（initData 免密或密码）。终端 API 层强制认证 + 开关校验。
        term_path = os.path.join(STATIC_DIR, "terminal.html")
        try:
            with open(term_path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._send_html(b"<h1>terminal.html missing</h1>", status=500)
            return
        self._send_html(body)

    def _handle_media_resolve(self) -> None:
        """按服务器路径换一个下载 token（POST /api/media/resolve）。

        为什么需要它：媒体帧里的 download_url 只在当前进程生命周期内有效，
        而且刷新后 /api/history 只回文本——但每条媒体操作入库的文本都带着
        服务器绝对路径（"已保存到 x""已发送服务器文件给用户: x"）。前端刷新
        时从历史文本里挖出路径，来这里换一个**新** token，媒体卡片因此跨
        刷新、跨进程重启都能恢复。

        信任边界：不是任意路径读取。只接受 media_allowed_roots（本应用自己
        的数据目录：xgent_storage、agent workspace）之内的**已存在文件**，
        且要求登录 + 同源，与 /api/media/<token> 的口子等宽。
        """
        if not self._require_auth():
            return
        if not self._require_web_enabled():
            return
        data = self._read_json()
        if data is None:
            return
        raw_path = str(data.get("path") or "").strip()
        if not raw_path:
            self._send_json({"error": "缺少 path"}, status=400)
            return

        try:
            real = os.path.realpath(os.path.abspath(raw_path))
        except (OSError, ValueError):
            self._send_json({"error": "invalid path"}, status=400)
            return

        allowed = False
        for root in self.config.media_allowed_roots:
            try:
                root_real = os.path.realpath(os.path.abspath(root))
                if os.path.commonpath([real, root_real]) == root_real:
                    allowed = True
                    break
            except (OSError, ValueError):
                continue
        if not allowed:
            self._send_json({"error": "路径不在允许的目录内"}, status=403)
            return
        if not os.path.isfile(real):
            self._send_json({"error": "文件不存在"}, status=404)
            return

        filename = os.path.basename(real)
        mime_type, _ = mimetypes.guess_type(filename)
        mime_type = mime_type or "application/octet-stream"
        token = MEDIA_TOKEN_REGISTRY.register(real, filename)
        try:
            size = os.path.getsize(real)
        except OSError:
            size = 0
        self._send_json({
            "url": f"/api/media/{token}",
            "filename": filename,
            "mime_type": mime_type,
            "size": size,
        })

    def _handle_media_download(self, token: str) -> None:
        """AI 生成/发送的图片与文件的下载出口。见 web_bridge.py 的
        MediaTokenRegistry：send_photo/send_document 拿到本地路径时会注册
        一个 token，SSE 帧里带上 /api/media/<token>，前端据此渲染 <img> 或
        下载链接。

        要求登录（和其它 /api/* 一致）——这不是公开静态资源，是用户自己的
        聊天内容。token 本身只在当前进程内存里有效，重启后全部失效。
        """
        if not self._require_auth():
            return
        entry = MEDIA_TOKEN_REGISTRY.resolve(token)
        if entry is None:
            self._send_json({"error": "not found"}, status=404)
            return
        abs_path, filename, mime_type = entry
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            self._send_json({"error": "file missing"}, status=404)
            return

        # inline 让 <video>/<audio> 标签能直接播放、<img> 能直接显示；
        # 其余类型保持 attachment，浏览器落盘而不是尝试内联执行。
        inline_mime = mime_type.startswith(("image/", "video/", "audio/", "text/"))
        disposition = "inline" if inline_mime else "attachment"
        # RFC 5987 编码文件名，兼容非 ASCII 文件名（中文文件名很常见）。
        quoted_name = urllib.parse.quote(filename)
        self.send_response(200)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{quoted_name}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        try:
            # 流式分块读写：视频等大文件不能一次性 read() 进内存，否则一个
            # 大文件下载就能把工作线程内存打爆（和 _read_multipart 的 50MB
            # 量级完全不是一个数量级，这里必须流式处理）。
            with open(abs_path, "rb") as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 浏览器中途取消下载，或文件在两次 stat/read 之间被删除，都属正常。
            pass

    def _serve_vendor_asset(self, path: str) -> None:
        """本地自带的第三方静态资源（xterm.js 等），不要求认证——与 index.html
        / terminal.html 同级别的公开静态文件，浏览器渲染页面时就要加载它们，
        这时候还没走完登录流程。真正的敏感操作都在需要认证的 /api/* 路由上。
        """
        filename, content_type = VENDOR_ASSETS[path]
        asset_path = os.path.join(VENDOR_DIR, filename)
        try:
            with open(asset_path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._send_json({"error": "asset missing"}, status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        # 静态资源内容按文件名固定，可以放心长期缓存；改版本号（文件名或加
        # query）时浏览器会当新资源处理，不用担心缓存吃到旧内容。
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def _require_terminal_enabled(self) -> bool:
        if self.config.is_terminal_enabled():
            return True
        self._send_json({"error": "终端功能未开启"}, status=403)
        return False

    def _require_web_enabled(self) -> bool:
        if self.config.is_web_enabled():
            return True
        self._send_json({"error": "Web Chat 未开启"}, status=403)
        return False

    def _handle_term_open(self) -> None:
        """创建一个独立终端会话。"""
        if not self._require_auth():
            return
        if not self._require_terminal_enabled():
            return
        if not web_terminal.is_terminal_supported():
            self._send_json({"error": "服务器平台不支持终端"}, status=503)
            return
        data = self._read_json() or {}
        try:
            cols = int(data.get("cols", 80) or 80)
            rows = int(data.get("rows", 24) or 24)
        except (TypeError, ValueError):
            cols, rows = 80, 24
        try:
            session = web_terminal.get_terminal_manager().open(cols=cols, rows=rows)
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=409)
            return
        self._send_json({"session_id": session.id, "pid": session.pid})

    def _handle_term_input(self) -> None:
        """写入终端输入。data 是 base64 编码的字节。"""
        if not self._require_auth():
            return
        if not self._require_terminal_enabled():
            return
        data = self._read_json() or {}
        session_id = str(data.get("session_id") or "")
        if not session_id:
            self._send_json({"error": "缺少 session_id"}, status=400)
            return
        try:
            payload = base64.b64decode(str(data.get("data") or ""))
        except (ValueError, TypeError):
            self._send_json({"error": "bad data"}, status=400)
            return
        if not web_terminal.get_terminal_manager().write(session_id, payload):
            self._send_json({"error": "会话不存在或已关闭"}, status=404)
            return
        self._send_json({"ok": True})

    def _handle_term_resize(self) -> None:
        if not self._require_auth():
            return
        if not self._require_terminal_enabled():
            return
        data = self._read_json() or {}
        session_id = str(data.get("session_id") or "")
        try:
            cols = int(data.get("cols", 80) or 80)
            rows = int(data.get("rows", 24) or 24)
        except (TypeError, ValueError):
            cols, rows = 80, 24
        web_terminal.get_terminal_manager().resize(session_id, cols, rows)
        self._send_json({"ok": True})

    def _handle_term_close(self) -> None:
        if not self._require_auth():
            return
        if not self._require_terminal_enabled():
            return
        data = self._read_json() or {}
        session_id = str(data.get("session_id") or "")
        web_terminal.get_terminal_manager().close(session_id)
        self._send_json({"ok": True})

    def _handle_term_output(self) -> None:
        """终端输出 SSE。每个连接独占一个会话的读取。"""
        if not self._require_auth():
            return
        if not self._require_terminal_enabled():
            return
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        session_id = (query.get("session_id") or [""])[0]
        manager = web_terminal.get_terminal_manager()
        if not session_id or manager.get(session_id) is None:
            self._send_json({"error": "会话不存在"}, status=404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        stop_event = self.server.shutdown_event  # type: ignore[attr-defined]
        try:
            while not stop_event.is_set():
                chunk = manager.read(session_id, timeout=SSE_HEARTBEAT_SECONDS)
                if chunk is None:
                    # 会话结束（子进程退出 / EOF）。发 close 事件并回收。
                    self.wfile.write(b"event: close\ndata: \n\n")
                    self.wfile.flush()
                    manager.close(session_id)
                    break
                if chunk == b"":
                    # select 超时，发注释行保活并探测连接。
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(
                    {"data": base64.b64encode(chunk).decode("ascii")},
                    ensure_ascii=False,
                )
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            # 浏览器关页面属正常。
            pass


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    # SO_REUSEADDR 在两个平台语义不同：
    #   Linux —— 只影响 TIME_WAIT，不允许两个进程同时监听同一端口。重启 bot 时
    #            避免 "Address already in use"，是想要的行为。
    #   Windows —— 允许直接抢占正在监听的端口，端口冲突不会报错，另一个进程还能
    #            把连接劫持走。
    # HTTPServer 默认把它设成 1，所以必须在 Windows 上显式关掉。
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request: Any, client_address: Any) -> None:
        """浏览器关页面 / 断开 SSE 是常态，不该在 bot 日志里刷整页 traceback。

        socketserver 默认把异常打到 stderr；SSE 连接被客户端中断时异常发生在
        handle_one_request 读下一行请求那一步，在 handler 的 try/except 之外，
        所以只能在这里兜。真正的异常仍然记 debug 级别，不是直接吞掉。
        """
        logger.debug("web 连接异常 %s", client_address, exc_info=True)


class WebChatServer:
    """生命周期封装。start() 由 setup_bot_commands 调，stop() 由 on_shutdown 调。"""

    def __init__(self, config: WebChatConfig):
        self.config = config
        self._httpd: Optional[_ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.outbox = WebOutbox()
        self.shutdown_event = threading.Event()

    def start(self) -> None:
        if self._httpd is not None:
            return
        # 未设密码拒绝启动。对齐 notes/server.py 的 SystemExit：一个能执行
        # shell 命令的控制台，绝不能无密码监听。
        if not self.config.password_hash:
            raise RuntimeError("未设置访问密码，拒绝启动 Web 服务")

        httpd = _ThreadingHTTPServer((self.config.host, self.config.port), _Handler)
        httpd.config = self.config          # type: ignore[attr-defined]
        httpd.session_key = web_auth.generate_session_secret()  # type: ignore[attr-defined]
        httpd.limiter = web_auth.LoginRateLimiter()             # type: ignore[attr-defined]
        httpd.outbox = self.outbox          # type: ignore[attr-defined]
        httpd.shutdown_event = self.shutdown_event              # type: ignore[attr-defined]

        thread = threading.Thread(target=httpd.serve_forever, name="xgent-web", daemon=True)
        thread.start()
        self._httpd = httpd
        self._thread = thread
        logger.info("Web Chat 已启动: http://%s:%s", self.config.host, self.config.port)

    def stop(self) -> None:
        self.shutdown_event.set()
        self.outbox.close()
        # 停服时回收所有终端会话，避免 pty + 子进程残留。
        with contextlib.suppress(Exception):
            web_terminal.get_terminal_manager().close_all()
        if self._httpd is not None:
            with contextlib.suppress(Exception):
                self._httpd.shutdown()
            with contextlib.suppress(Exception):
                self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Web Chat 已停止")

    @property
    def running(self) -> bool:
        return self._httpd is not None


__all__ = ["WebChatConfig", "WebChatServer", "MAX_REQUEST_BODY", "STATIC_DIR"]
