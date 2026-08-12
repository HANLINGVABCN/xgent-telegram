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
每个终端输出 SSE 连接占用一个 WebChatServer 工作线程，在该线程里 select+
read master_fd 并推帧。输入是独立的 POST 请求写 master_fd。两者通过 session_id
关联，共享同一个 master_fd（一个写者 + 一个读者，无需额外锁）。
"""

from __future__ import annotations

import contextlib
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
        "created_at", "last_activity", "closed",
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
                if now - session.last_activity > self.idle_timeout:
                    expired.append(sid)
        for sid in expired:
            self.close(sid)
            logger.info("终端空闲超时自动关闭 sid=%s", sid[:_LOG_SID_LEN])
        return len(expired)

    # --- 数据通道 ---

    def read(self, session_id: str, timeout: float = 1.0) -> Optional[bytes]:
        """读取终端输出。

        返回值约定：
          - bytes：实际读到的输出
          - b""：select 超时，无数据（调用方发 SSE 心跳）
          - None：会话不存在 / 已关闭 / 子进程已退出（调用方结束 SSE）
        """
        session = self._sessions.get(session_id) if session_id else None
        if session is None or session.closed:
            return None
        try:
            readable, _, _ = select.select([session.master_fd], [], [], timeout)
        except (OSError, ValueError):
            # master_fd 已关闭等，视为会话结束。
            self._mark_closed(session)
            return None
        if not readable:
            return b""
        try:
            data = os.read(session.master_fd, 65536)
        except OSError:
            # Linux 上 slave 全部关闭后，master 端 read 会抛 EIO 而非返回 b''。
            self._mark_closed(session)
            return None
        if not data:
            # EOF：子进程关闭了写端。
            self._mark_closed(session)
            return None
        session.last_activity = time.time()
        return data

    def write(self, session_id: str, data: bytes) -> bool:
        """写入终端输入。成功返回 True。"""
        session = self._sessions.get(session_id) if session_id else None
        if session is None or session.closed:
            return False
        try:
            os.write(session.master_fd, data)
        except OSError:
            self._mark_closed(session)
            return False
        session.last_activity = time.time()
        return True

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
        session.closed = True
        with contextlib.suppress(OSError):
            os.close(session.master_fd)
        # SIGHUP 是终端关闭的标准信号，shell 会自己退出。
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(session.pid, signal.SIGHUP)
        # 非阻塞回收，避免工作线程卡在 waitpid 上。
        with contextlib.suppress(ChildProcessError):
            os.waitpid(session.pid, os.WNOHANG)

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
