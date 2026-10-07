"""网页终端的 pty 会话管理。

独立于 sections 命名空间，可被 web_server 直接 import 和单测。

安全模型
--------
终端 = 任意命令执行，比聊天危险得多。本模块只负责会话生命周期，**不做认证**——
会话创建前必须由调用方（web_server 的 _require_auth）完成认证。原因：认证逻辑
（密码 / Telegram initData + authorized_user_id 比对）属于 web 层，pty 层不应
重复实现，否则两套规则容易漂移。

防护措施（在本模块内）：
- 会话数上限 MAX_SESSIONS：防遗忘的会话堆积。
- 空闲超时 IDLE_TIMEOUT：长期无输入输出的会话自动关闭，缩小被劫持后的暴露面。
- session_id 用 secrets.token_urlsafe(32)：不可猜测，URL/-body 里携带也安全。
- pty 仅 posix：Windows 等非 posix 平台 open() 直接抛 RuntimeError，不会静默
  跑一个无隔离的 subprocess。
- 审计：open / close / resize / 空闲超时 记 INFO 日志（session_id 取前 8 位 +
  pid）。input 是字节流，逐条记录无意义且可能含敏感内容，不记。

线程模型
--------
每个 PTY 只有一个后台读取器，持续写入最多 2 MiB 的带序号缓冲。
每条 SSE 连接有独立游标并广播读取，不竞争 master_fd；断线可补发保留窗口内输出。
窗口之外的输出明确报告 gap，不伪装成完整终端恢复。关闭会话回收读取线程和子进程。
"""

from __future__ import annotations

import contextlib
from collections import deque
import logging
import os
import secrets
import select
import signal
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 单实例（单用户 bot）下，3 个并发终端足够：手机 + 电脑 + 一个备用。
# 超过则拒绝新建，避免资源泄漏型 DoS。
MAX_SESSIONS = 3

# 30 分钟无任何输入输出即视为遗忘，自动关闭。终端不像聊天会主动结束，必须有
# 兜底回收，否则一个忘了关的会话会一直占着 pty + 进程。
IDLE_TIMEOUT_SECONDS = 30 * 60

# session_id 在日志里只显示前 8 位，够排查又不致整串泄露到日志文件。
_LOG_SID_LEN = 8


class TerminalSession:
    """一个独立的 pty + shell 会话。"""

    __slots__ = (
        "id", "pid", "master_fd", "cols", "rows",
        "created_at", "last_activity", "closed", "condition", "chunks", "sequence",
        "buffer_bytes", "reader", "legacy_cursor", "write_lock", "reaped",
    )

    def __init__(self, session_id: str, pid: int, master_fd: int,
                 cols: int, rows: int):
        self.id = session_id
        self.pid = pid
        self.master_fd = master_fd
        self.cols = cols
        self.rows = rows
        self.created_at = time.time()
        self.last_activity = time.time()
        self.closed = False
        self.condition = threading.Condition()
        self.chunks = deque()
        self.sequence = 0
        self.buffer_bytes = 0
        self.reader = None
        self.legacy_cursor = 0
        self.write_lock = threading.Lock()
        self.reaped = False


class TerminalManager:
    """管理所有终端会话的单例。线程安全。"""

    def __init__(
        self,
        max_sessions: int = MAX_SESSIONS,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
    ):
        self.max_sessions = max_sessions
        self.idle_timeout = idle_timeout
        self._sessions: Dict[str, TerminalSession] = {}
        self._lock = threading.Lock()

    # --- 生命周期 ---

    def open(self, cols: int = 80, rows: int = 24,
             shell: Optional[str] = None) -> TerminalSession:
        """新建一个终端会话。非 posix 或达上限时抛 RuntimeError。

        cols/rows 是初始窗口尺寸，会通过 TIOCSWINSZ 设到 pty，让 vim/top 这类
        全屏程序一开始就有正确的布局。
        """
        if os.name != "posix":
            raise RuntimeError("终端仅支持 Linux/Unix（pty 不可用）")

        # 每次开新的顺手清掉过期的，避免清理逻辑要单独调度。
        self.cleanup_idle()

        cols = max(1, min(int(cols or 80), 500))
        rows = max(1, min(int(rows or 24), 200))

        with self._lock:
            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError(
                    f"已达并发终端上限（{self.max_sessions}），请先关闭已有终端"
                )
            # posix-only 模块在函数内 import，保证 Windows 也能 import 本文件。
            import pty  # type: ignore

            shell = shell or os.environ.get("SHELL") or "/bin/bash"
            env = dict(os.environ)
            env["TERM"] = "xterm-256color"

            # pty.fork() 已替我们完成 setsid + 设置 controlling tty，子进程里
            # 直接 exec 即可。
            pid, master_fd = pty.fork()
            if pid == 0:
                # 子进程：exec 失败必须 _exit，否则会带着父进程的代码继续跑。
                try:
                    os.execvpe(shell, [shell], env)
                except Exception:  # noqa: BLE001
                    os._exit(127)

            session_id = secrets.token_urlsafe(32)
            session = TerminalSession(session_id, pid, master_fd, cols, rows)
            self._sessions[session_id] = session

        self._ioctl_winsize(master_fd, rows, cols)
        os.set_blocking(master_fd, False)
        session.reader = threading.Thread(target=self._pump, args=(session,), daemon=True,
                                          name="xgent-pty-reader")
        session.reader.start()
        logger.info(
            "终端开启 sid=%s pid=%s shell=%s %sx%s",
            session_id[:_LOG_SID_LEN], pid, shell, cols, rows,
        )
        return session

    def get(self, session_id: str) -> Optional[TerminalSession]:
        return self._sessions.get(session_id) if session_id else None

    def close(self, session_id: str) -> bool:
        """主动关闭一个会话。不存在返回 False。"""
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        self._terminate(session)
        logger.info("终端关闭 sid=%s pid=%s", session_id[:_LOG_SID_LEN], session.pid)
        return True

    def close_all(self) -> None:
        """服务停止时调用，回收所有 pty。"""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            self._terminate(session)
            logger.info("终端关闭(停服) sid=%s pid=%s",
                        session.id[:_LOG_SID_LEN], session.pid)

    def cleanup_idle(self) -> int:
        """关闭超过空闲超时的会话，返回关闭数量。"""
        now = time.time()
        expired = []
        with self._lock:
            for sid, session in self._sessions.items():
                if session.closed or now - session.last_activity > self.idle_timeout:
                    expired.append(sid)
        for sid in expired:
            self.close(sid)
            logger.info("终端空闲超时自动关闭 sid=%s", sid[:_LOG_SID_LEN])
        return len(expired)

    # --- 数据通道 ---

    def list_sessions(self):
        self.cleanup_idle()
        with self._lock:
            return [{"id": s.id, "pid": s.pid, "cols": s.cols, "rows": s.rows,
                     "created_at": s.created_at, "last_activity": s.last_activity,
                     "closed": s.closed, "sequence": s.sequence}
                    for s in self._sessions.values()]

    def _append(self, session, data):
        with session.condition:
            session.sequence += 1
            session.chunks.append((session.sequence, data))
            session.buffer_bytes += len(data)
            while session.buffer_bytes > 2 * 1024 * 1024:
                _, dropped = session.chunks.popleft()
                session.buffer_bytes -= len(dropped)
            session.last_activity = time.time()
            session.condition.notify_all()

    def _pump(self, session):
        try:
            while not session.closed:
                if time.time() - session.last_activity > self.idle_timeout:
                    break
                readable, _, _ = select.select([session.master_fd], [], [], .5)
                if not readable: continue
                with session.write_lock:
                    if session.closed: break
                    try: data = os.read(session.master_fd, 65536)
                    except BlockingIOError: continue
                if not data: break
                self._append(session, data)
        except (OSError, ValueError):
            pass
        finally:
            self._terminate(session)

    def read_frames(self, session_id, after=0, timeout=1.0):
        session = self.get(session_id)
        if session is None: return {"closed": True, "frames": [], "gap": False}
        with session.condition:
            session.condition.wait_for(lambda: session.closed or session.sequence > after, timeout)
            oldest = session.chunks[0][0] if session.chunks else session.sequence + 1
            return {"closed": session.closed, "gap": after < oldest - 1 or after > session.sequence,
                    "frames": [(seq, data) for seq, data in session.chunks if seq > after][:16],
                    "sequence": session.sequence}

    def read(self, session_id: str, timeout: float = 1.0) -> Optional[bytes]:
        # Compatibility for callers without cursors; HTTP uses independent read_frames cursors.
        session = self.get(session_id)
        if session is None: return None
        batch = self.read_frames(session_id, session.legacy_cursor, timeout)
        if batch["frames"]:
            session.legacy_cursor = batch["frames"][-1][0]
            return b"".join(data for _, data in batch["frames"])
        return None if batch["closed"] else b""

    def write(self, session_id: str, data: bytes) -> bool:
        session = self.get(session_id)
        if session is None or session.closed: return False
        try:
            deadline = time.monotonic() + 3
            with session.write_lock:
                view = memoryview(data)
                while view:
                    if session.closed or time.monotonic() >= deadline: return False
                    try:
                        written = os.write(session.master_fd, view[:4096])
                    except BlockingIOError:
                        select.select([], [session.master_fd], [], .05)
                        continue
                    if written <= 0: return False
                    view = view[written:]
                session.last_activity = time.time()
            return True
        except OSError:
            return False

    def resize(self, session_id: str, cols: int, rows: int) -> bool:
        """调整终端窗口大小。"""
        session = self._sessions.get(session_id) if session_id else None
        if session is None or session.closed:
            return False
        cols = max(1, min(int(cols or 80), 500))
        rows = max(1, min(int(rows or 24), 200))
        self._ioctl_winsize(session.master_fd, rows, cols)
        session.cols, session.rows = cols, rows
        logger.info("终端 resize sid=%s %sx%s", session_id[:_LOG_SID_LEN], cols, rows)
        return True

    # --- 内部 ---

    def _mark_closed(self, session: TerminalSession) -> None:
        """读/写检测到 EOF 时标记关闭，但进程回收交给 close/close_all/cleanup。

        不在这里 pop：SSE 线程读到 EOF 后会结束，但 session 对象仍需被 close()
        正式回收 pid（避免僵尸进程）。标记 closed 让后续 read/write 快速返回。
        """
        session.closed = True

    def _terminate(self, session: TerminalSession) -> None:
        with session.condition:
            session.closed = True
            session.condition.notify_all()
            already_reaping = session.reaped
            session.reaped = True
        if already_reaping:
            if session.reader and session.reader is not threading.current_thread():
                session.reader.join(timeout=4)
            return
        with session.write_lock:
            with contextlib.suppress(OSError): os.close(session.master_fd)
        with contextlib.suppress(OSError): os.killpg(session.pid, signal.SIGHUP)
        # Nonblocking wait in a bounded loop, then force termination and reap once.
        try:
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                pid, _ = os.waitpid(session.pid, os.WNOHANG)
                if pid: break
                time.sleep(.02)
            else:
                with contextlib.suppress(OSError): os.killpg(session.pid, signal.SIGKILL)
                os.waitpid(session.pid, 0)
        except (ChildProcessError, OSError): pass
        if session.reader and session.reader is not threading.current_thread():
            session.reader.join(timeout=2)

    def _ioctl_winsize(self, fd: int, rows: int, cols: int) -> None:
        import fcntl  # type: ignore
        import struct  # noqa: F401  (与 termios 同组，posix-only)
        import termios  # type: ignore
        with contextlib.suppress(OSError):
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    @property
    def session_count(self) -> int:
        return len(self._sessions)


# 进程级单例。WebChatServer 持有它，停服时 close_all。
_manager: Optional[TerminalManager] = None
_manager_lock = threading.Lock()


def get_terminal_manager() -> TerminalManager:
    """获取全局 TerminalManager 单例。"""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = TerminalManager()
    return _manager


def is_terminal_supported() -> bool:
    """当前平台是否支持终端（pty）。"""
    return os.name == "posix"


__all__ = [
    "TerminalSession",
    "TerminalManager",
    "MAX_SESSIONS",
    "IDLE_TIMEOUT_SECONDS",
    "get_terminal_manager",
    "is_terminal_supported",
]
