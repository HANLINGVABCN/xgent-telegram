"""Web Chat 的认证与会话。

安全模型对齐 skill/script/notes/server.py 与 webdav-filemanager/server.py：
PBKDF2 密码哈希、HMAC 签名 cookie + 服务端强制 TTL、登录限速。

本模块是纯函数集合，不依赖 Telegram、不依赖 sections 命名空间，可以直接
import 和单测。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse
from typing import Any, Dict, Optional, Tuple

# PBKDF2 轮数。与 notes/server.py 保持一致。
PBKDF2_ROUNDS = 200_000
PBKDF2_SALT_BYTES = 16

SESSION_COOKIE_NAME = "xgent_web_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600

# 登录限速：连续失败达到上限后锁定一段时间。
LOGIN_MAX_FAILS = 5
LOGIN_LOCKOUT_SECONDS = 60

# Telegram WebApp initData 的有效期。超时的 initData 一律拒绝，避免被重放。
INIT_DATA_MAX_AGE_SECONDS = 24 * 3600


# --- ☆ 密码 ☆ ---

def hash_password(password: str) -> str:
    """返回 pbkdf2_sha256$轮数$salt$hash 形式的可存储字符串。"""
    password = str(password or "")
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ROUNDS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """常量时间比对。stored 格式错误时返回 False 而不是抛异常。"""
    if not password or not stored:
        return False
    try:
        algorithm, rounds_text, salt_b64, digest_b64 = str(stored).split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(rounds_text)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, rounds)
    return hmac.compare_digest(actual, expected)


def mask_password_hash(stored: str) -> str:
    """给菜单显示用。哈希本身不敏感，但也没必要整串贴出来。"""
    if not stored:
        return "未设置"
    return "已设置（PBKDF2 哈希）"


# --- ☆ 会话 cookie ☆ ---

def _sign(secret_key: bytes, payload: str) -> str:
    mac = hmac.new(secret_key, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def make_session_cookie_value(secret_key: bytes, issued_at: Optional[float] = None) -> str:
    """签发 '签发时间.签名' 形式的会话值。"""
    issued = int(issued_at if issued_at is not None else time.time())
    payload = str(issued)
    return f"{payload}.{_sign(secret_key, payload)}"


def verify_session_cookie(secret_key: bytes, value: str,
                          ttl_seconds: int = SESSION_TTL_SECONDS,
                          now: Optional[float] = None) -> bool:
    """校验签名并强制服务端 TTL。

    TTL 必须在服务端判定：cookie 的 Max-Age 由浏览器执行，攻击者可以留着
    过期 cookie 一直重放。
    """
    if not value:
        return False
    payload, _, signature = str(value).partition(".")
    if not payload or not signature:
        return False
    if not hmac.compare_digest(signature, _sign(secret_key, payload)):
        return False
    try:
        issued = int(payload)
    except ValueError:
        return False
    current = now if now is not None else time.time()
    if current < issued - 60:
        # 签发时间在未来：时钟回拨或伪造，一律拒绝。
        return False
    return (current - issued) <= ttl_seconds


def build_set_cookie(value: str, secure: bool, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    """Secure 只在确认是 HTTPS 时加。

    直连 HTTP 时加 Secure 会让浏览器丢弃 cookie，表现为"登录成功但一直
    跳回登录页"。
    """
    flags = f"{SESSION_COOKIE_NAME}={value}; Path=/; Max-Age={ttl_seconds}; HttpOnly; SameSite=Lax"
    return flags + "; Secure" if secure else flags


def build_clear_cookie() -> str:
    return f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"


def parse_cookie_header(header: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for chunk in str(header or "").split(";"):
        name, sep, value = chunk.strip().partition("=")
        if sep and name:
            cookies[name] = value
    return cookies


# --- ☆ 登录限速 ☆ ---

class LoginRateLimiter:
    """按 IP 计的失败计数。

    刻意不读 X-Forwarded-For：该头由客户端提供，攻击者每次伪造一个新值就能
    让每个"IP"都拿到独立的失败计数，限速形同虚设。这里只用 TCP 对端地址。
    """

    def __init__(self, max_fails: int = LOGIN_MAX_FAILS,
                 lockout_seconds: int = LOGIN_LOCKOUT_SECONDS):
        self.max_fails = max_fails
        self.lockout_seconds = lockout_seconds
        self._state: Dict[str, Dict[str, float]] = {}
        self._lock = threading.Lock()

    def is_locked(self, ip: str, now: Optional[float] = None) -> bool:
        current = now if now is not None else time.time()
        with self._lock:
            entry = self._state.get(ip)
            if not entry:
                return False
            if entry.get("locked_until", 0) > current:
                return True
            if entry.get("locked_until", 0):
                # 锁定期已过，清零重新计数
                self._state.pop(ip, None)
            return False

    def record_failure(self, ip: str, now: Optional[float] = None) -> None:
        current = now if now is not None else time.time()
        with self._lock:
            entry = self._state.setdefault(ip, {"fails": 0, "locked_until": 0})
            entry["fails"] += 1
            if entry["fails"] >= self.max_fails:
                entry["locked_until"] = current + self.lockout_seconds
                entry["fails"] = 0

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._state.pop(ip, None)

    def retry_after(self, ip: str, now: Optional[float] = None) -> int:
        current = now if now is not None else time.time()
        with self._lock:
            entry = self._state.get(ip) or {}
            remaining = entry.get("locked_until", 0) - current
        return max(0, int(remaining))


# --- ☆ Telegram WebApp initData ☆ ---

def verify_telegram_init_data(init_data: str, bot_token: str,
                              max_age_seconds: int = INIT_DATA_MAX_AGE_SECONDS,
                              now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """校验 Telegram WebApp 的 initData 签名，通过则返回解析后的字段。

    算法见 Telegram 官方文档：secret_key = HMAC_SHA256("WebAppData", bot_token)，
    再用它对按 key 排序、以 \\n 连接的 "k=v" 串签名，与 hash 参数比对。

    校验失败返回 None。调用方还需要再比对 user.id 是否为授权用户。
    """
    if not init_data or not bot_token:
        return None

    # parse_qsl 保留原始顺序；用 keep_blank_values 避免丢掉空值字段。
    try:
        pairs = urllib.parse.parse_qsl(str(init_data), keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return None
    if not pairs:
        return None

    data = dict(pairs)
    received_hash = data.pop("hash", "")
    if not received_hash:
        return None

    check_string = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash.lower()):
        return None

    # auth_date 过期的 initData 一律拒绝，避免被长期重放。
    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        return None
    current = now if now is not None else time.time()
    if auth_date <= 0 or (current - auth_date) > max_age_seconds:
        return None

    parsed: Dict[str, Any] = dict(data)
    if "user" in data:
        try:
            parsed["user"] = json.loads(data["user"])
        except (json.JSONDecodeError, TypeError):
            return None
    return parsed


def init_data_user_id(parsed: Optional[Dict[str, Any]]) -> Optional[int]:
    if not parsed:
        return None
    user = parsed.get("user")
    if not isinstance(user, dict):
        return None
    try:
        return int(user.get("id"))
    except (TypeError, ValueError):
        return None


# --- ☆ 同源校验 ☆ ---

def is_same_origin(origin_header: str, referer_header: str, host_header: str) -> bool:
    """变更型请求的同源校验。

    这些接口全靠 Cookie 认证，而 SameSite=Lax 并不拦截顶层表单 POST，
    任意站点用一个 enctype="text/plain" 的表单就能在用户登录状态下发起请求。

    无 Origin 也无 Referer 时放行：那是 curl / 脚本客户端，浏览器不会自动
    给它们附带 Cookie，不构成 CSRF。
    """
    source = str(origin_header or "").strip() or str(referer_header or "").strip()
    if not source:
        return True
    host = str(host_header or "").strip()
    if not host:
        return False
    try:
        parsed = urllib.parse.urlparse(source)
    except ValueError:
        return False
    return bool(parsed.netloc) and parsed.netloc == host


def generate_session_secret() -> bytes:
    """每次启动生成新的签名密钥——重启即让所有旧会话失效。"""
    return secrets.token_bytes(32)


def is_https_request(headers: Dict[str, str]) -> bool:
    """只认反代明确声明的 HTTPS。"""
    def get(name: str) -> str:
        for key, value in (headers or {}).items():
            if str(key).lower() == name:
                return str(value or "").strip().lower()
        return ""

    return get("x-forwarded-proto") == "https" or get("x-forwarded-ssl") == "on"


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(str(left or ""), str(right or ""))


__all__ = [
    "PBKDF2_ROUNDS",
    "SESSION_COOKIE_NAME",
    "SESSION_TTL_SECONDS",
    "LOGIN_MAX_FAILS",
    "LOGIN_LOCKOUT_SECONDS",
    "INIT_DATA_MAX_AGE_SECONDS",
    "hash_password",
    "verify_password",
    "mask_password_hash",
    "make_session_cookie_value",
    "verify_session_cookie",
    "build_set_cookie",
    "build_clear_cookie",
    "parse_cookie_header",
    "LoginRateLimiter",
    "verify_telegram_init_data",
    "init_data_user_id",
    "is_same_origin",
    "generate_session_secret",
    "is_https_request",
    "constant_time_equals",
]
