# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

import asyncio
import codecs
import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from telegram.ext import Application

from xgent_app.text_utils import clip_middle_text
from xgent_app.agent_status import AgentTurnOrigin

# 以下这些符号需要从共享命名空间获取（由 xgent_server.py 注入）
# logger, BotMemoryDB, AgentExecutor, AgentCommandBlacklist, COMMAND_OUTPUT_DIR,
# to_display_path, BLACKLIST_BLOCKED_NOTICE, GlobalRecorder, MessageType,
# process_conversation

logger = logging.getLogger(__name__)
def strip_terminal_control_sequences(text: str) -> str:
    cleaned = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text or '')
    cleaned = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', cleaned)
    return cleaned


def looks_like_interactive_prompt(text: str) -> bool:
    if not text:
        return False
    sample = strip_terminal_control_sequences(text[-3000:])
    lines = [line.rstrip() for line in sample.replace('\r', '\n').splitlines() if line.strip()]
    if not lines:
        stripped = sample.strip()
        if not stripped:
            return False
        lines = [stripped]

    recent = lines[-12:]
    tail = "\n".join(recent)
    tail_lower = tail.lower()
    prompt_patterns = (
        r'[\(\[]\s*y(?:es)?\s*/\s*n(?:o)?\s*[\)\]]',
        r'\byes\s*/\s*no\b',
        r'\bare\s+you\s+sure\b',
        r'\bdo\s+you\s+want\b',
        r'\bcontinue\?',
        r'\bconfirm(?:ation)?\b',
        r'\bpress\s+(?:any\s+)?key\b',
        r'\bpassword\s*[:：]?\s*$',
        r'\bpassphrase\s*[:：]?\s*$',
        r'\busername\s*[:：]?\s*$',
        r'\blogin\s*[:：]?\s*$',
        r'请输入',
        r'请选择',
        r'是否',
        r'确认',
        r'按任意键',
    )
    if any(re.search(pattern, tail_lower, re.IGNORECASE | re.MULTILINE) for pattern in prompt_patterns):
        return True

    menu_item_count = sum(
        1 for line in recent
        if re.match(r'^\s*(?:\d+|[a-zA-Z])[\).\、:：]\s+\S+', line)
    )
    if menu_item_count >= 2 and re.search(r'(select|choice|choose|输入|选择|选项|序号|菜单)', tail_lower):
        return True

    last = recent[-1].strip()
    lower_last = last.lower()
    if re.search(r'(^|\s)(?:[\w.-]+@[\w.-]+(?::[^\r\n]*)?|bash-[\d.]+|sh|zsh|fish|root)(?:[#>$])\s*$', last):
        return True
    repl_prompt_patterns = (
        r'^\s*(?:>>>|\.\.\.|>)\s*$',
        r'^\s*In\s+\[\d+\]:\s*$',
        r'^\s*(?:mysql|sqlite|redis|psql|node)(?:\s*\([^)]*\))?>\s*$',
    )
    if any(re.search(pattern, last, re.IGNORECASE) for pattern in repl_prompt_patterns):
        return True
    if re.search(r'(?:password|passphrase|username|login)\s*[:：]\s*$', lower_last):
        return True
    if re.search(
        r'(?:enter|input|select|choose|choice|confirm|continue|press|type|'
        r'请输入|输入|选择|选项|确认|继续|按).{0,80}(?:[?:：>]|$)\s*$',
        lower_last,
    ):
        return True
    if last.endswith('?') and len(last) <= 180:
        return True
    return False


def looks_like_long_running_command(command: str) -> bool:
    cmd = (command or '').lower()
    patterns = (
        r'(^|[\s;&|])tail\b(?=[^;&|]*?(?:\s|=)(?:-f|--follow(?:=[^\s;&|]+)?)(?:\s|=|$))',
        r'(^|[\s;&|])journalctl\b(?=[^;&|]*?(?:\s|=)(?:-f|--follow)(?:\s|=|$))',
        r'(^|[\s;&|])docker\s+logs\b(?=[^;&|]*?(?:\s|=)(?:-f|--follow)(?:\s|=|$|[a-z]))',
        r'(^|[\s;&|])kubectl\s+logs\b(?=[^;&|]*?(?:\s|=)(?:-f|--follow)(?:\s|=|$|[a-z]))',
        r'(^|[\s;&|])(pm2)\s+logs\b',
        r'(^|[\s;&|])(watch|top|htop|btop|less|more|man|vim|vi|nano|ssh|tmux|screen)(\s|$)',
        r'(^|[\s;&|])(bash|sh|zsh|fish)(\s+(-i|--login|-l))*\s*$',
        r'(^|[\s;&|])(python|python3|node|ipython)\s*$',
        r'(^|[\s;&|])(mysql|psql|sqlite3|redis-cli)(\s|$)',
        r'(^|[\s;&|])ping\b(?![^;&|]*\s-(?:c|n)\s*\d)',
        r'(^|[\s;&|])sleep\s+(?:infinity|inf)\b',
        r'(^|[\s;&|])yes\b',
        r'\bwhile\s+true\b',
        r'\bfor\s+;;\s+do\b',
        r'(^|[\s;&|])python(?:3)?\s+-m\s+http\.server\b',
        r'(^|[\s;&|])(flask|streamlit)\s+run\b',
        r'(^|[\s;&|])(celery|rq)\s+worker\b',
        r'(^|[\s;&|])(vite|next|nuxt|astro)\b.*\b(dev|start|preview)\b',
        r'(^|[\s;&|])(cmd|cmd\.exe)\b.*\s/[qdkc]*k\b',
        r'(^|[\s;&|])(powershell|powershell\.exe|pwsh|pwsh\.exe)\b.*\s-(noexit|noe)\b',
        r'(^|[\s;&|])(python|python3|node|npm|pnpm|yarn|bun|uvicorn|gunicorn)\b.*\b(serve|server|dev|start|runserver)\b',
    )
    return any(re.search(pattern, cmd) for pattern in patterns)


def save_command_output(command: str, output: str) -> Dict[str, Any]:
    now = datetime.now()
    dated_dir = os.path.join(COMMAND_OUTPUT_DIR, now.strftime("%Y-%m-%d"))
    os.makedirs(dated_dir, exist_ok=True)
    name = f"{now.strftime('%H%M%S')}_{uuid.uuid4().hex[:8]}.txt"
    path = os.path.join(dated_dir, name)
    content = (
        f"Command:\n{command}\n\n"
        f"Captured at: {now.isoformat(timespec='seconds')}\n\n"
        "Output:\n"
        f"{output}"
    )
    with open(path, 'w', encoding='utf-8', errors='replace') as f:
        f.write(content)
    return {
        'path': to_display_path(path),
        'bytes': len(content.encode('utf-8', errors='replace')),
    }


async def save_command_output_async(command: str, output: str) -> Dict[str, Any]:
    """把命令输出落盘丢到线程里。

    输出可能有好几 MB，在事件循环上同步写会阻塞整个 bot，而这条路径还持有
    全局对话锁。写失败也不能让整条命令的结果丢掉——命令本身已经跑完了，
    存档失败只是少一个文件。
    """
    try:
        return await asyncio.to_thread(save_command_output, command, output)
    except Exception as e:
        logger.error(f"命令输出存档失败: {e}")
        return {'path': None, 'bytes': 0}


async def terminate_async_process(process: Any):
    if process is None or process.returncode is not None:
        return
    try:
        if os.name != 'nt':
            import signal

            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            try:
                killer = await asyncio.create_subprocess_exec(
                    'taskkill', '/PID', str(process.pid), '/T', '/F',
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.communicate()
            except Exception:
                process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            if os.name != 'nt':
                import signal

                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
            await process.wait()
    except ProcessLookupError:
        pass
    except Exception as e:
        logger.warning(f"终止命令进程失败: {e}")


class AgentShellSession:
    """一个可持续读写的 shell 进程；Linux/macOS 使用 PTY，Windows 使用管道降级。"""

    MAX_BUFFER = 50000

    def __init__(self, session_id: str, command: str):
        self.session_id = session_id
        self.command = command
        self.started_at = time.time()
        self.started_monotonic = time.monotonic()
        self.last_output_at = self.started_at
        self.last_output_monotonic = self.started_monotonic
        self.first_output_at: Optional[float] = None
        self.first_output_monotonic: Optional[float] = None
        self.lock = threading.RLock()
        self.input_lock = threading.Lock()
        self.output = ""
        self.output_start_offset = 0
        self.read_index = 0
        self.output_chunks = 0
        self.output_events: Deque[Tuple[float, int]] = deque(maxlen=300)
        self.process: Optional[subprocess.Popen] = None
        self.controller_fd: Optional[int] = None
        self.reader_thread: Optional[threading.Thread] = None
        self.pty_enabled = os.name != 'nt'

    def start(self):
        env = {**os.environ, 'LANG': 'en_US.UTF-8', 'TERM': os.environ.get('TERM') or 'xterm-256color'}
        if self.pty_enabled:
            import pty

            controller_fd, terminal_fd = pty.openpty()
            self.controller_fd = controller_fd
            self.process = subprocess.Popen(
                self.command,
                shell=True,
                executable='/bin/bash',
                stdin=terminal_fd,
                stdout=terminal_fd,
                stderr=terminal_fd,
                cwd=AgentExecutor.WORK_DIR,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
            os.close(terminal_fd)
            self.reader_thread = threading.Thread(target=self._read_pty_loop, daemon=True)
        else:
            self.process = subprocess.Popen(
                self.command,
                shell=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=AgentExecutor.WORK_DIR,
                env=env,
            )
            self.reader_thread = threading.Thread(target=self._read_pipe_loop, daemon=True)
        self.reader_thread.start()

    def _append_output(self, text: str):
        if not text:
            return
        now_wall = time.time()
        now_monotonic = time.monotonic()
        with self.lock:
            if self.first_output_at is None:
                self.first_output_at = now_wall
            if self.first_output_monotonic is None:
                self.first_output_monotonic = now_monotonic
            self.output_chunks += 1
            self.output_events.append((now_monotonic, len(text)))
            self.output += text
            self.last_output_at = now_wall
            self.last_output_monotonic = now_monotonic
            overflow = len(self.output) - self.MAX_BUFFER
            if overflow > 0:
                self.output = self.output[overflow:]
                self.output_start_offset += overflow
                self.read_index = max(0, self.read_index - overflow)

    def _read_pty_loop(self):
        if self.controller_fd is None:
            raise RuntimeError("PTY 读取线程启动时 controller_fd 为空")
        while True:
            try:
                chunk = os.read(self.controller_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            self._append_output(chunk.decode('utf-8', errors='replace'))

    def _read_pipe_loop(self):
        if not self.process or not self.process.stdout:
            return
        fd = self.process.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            self._append_output(chunk.decode('utf-8', errors='replace'))

    def is_running(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def return_code(self) -> Optional[int]:
        if not self.process:
            return None
        return self.process.poll()

    def write_bytes(self, payload: bytes):
        if not payload:
            return
        with self.input_lock:
            if self.pty_enabled:
                if self.controller_fd is None:
                    raise RuntimeError("shell session has no PTY")
                view = memoryview(payload)
                offset = 0
                while offset < len(view):
                    written = os.write(self.controller_fd, view[offset:])
                    if written <= 0:
                        raise RuntimeError("shell session PTY write returned no bytes")
                    offset += written
                return
            if not self.process or not self.process.stdin:
                raise RuntimeError("shell session stdin is closed")
            self.process.stdin.write(payload)
            self.process.stdin.flush()

    def read_delta(self, max_chars: int = 4000) -> str:
        with self.lock:
            delta = self.output[self.read_index:]
            self.read_index = len(self.output)
        if len(delta) > max_chars:
            # read_index 已经推进过整段，被裁掉的部分再也读不回来了。
            # 以前这里直接返回尾部 max_chars，中间内容静默消失，模型据此
            # 推理时完全不知道自己少看了东西（滚过去的报错就此隐形）。
            # 和 read_snapshot 保持一致：保留首尾并标注省略量。
            return clip_middle_text(delta, max_chars, "shell 增量输出")
        return delta

    def read_recent(self, max_chars: int = 4000) -> str:
        with self.lock:
            return self.output[-max_chars:]

    def read_from_offset(self, offset: int) -> Tuple[str, int]:
        """按绝对字符偏移读取新增输出，不修改 shellread 使用的 read_index。"""
        with self.lock:
            absolute_start = self.output_start_offset
            absolute_end = absolute_start + len(self.output)
            relative_start = max(0, min(len(self.output), int(offset) - absolute_start))
            return self.output[relative_start:], absolute_end

    def read_snapshot(self, max_chars: int = 4000) -> str:
        with self.lock:
            snapshot = self.output
            self.read_index = len(self.output)
        return clip_middle_text(snapshot, max_chars, "shell 输出")

    def output_activity(self) -> Tuple[int, float, int, float]:
        with self.lock:
            output_chars = len(self.output)
            idle_seconds = max(0.0, time.monotonic() - self.last_output_monotonic)
            output_chunks = self.output_chunks
            if self.first_output_monotonic is None:
                active_seconds = 0.0
            else:
                active_seconds = max(0.0, time.monotonic() - self.first_output_monotonic)
        return output_chars, idle_seconds, output_chunks, active_seconds

    def output_activity_snapshot(self, recent_window_seconds: float = 15.0) -> Dict[str, Any]:
        now = time.monotonic()
        with self.lock:
            output_chars = len(self.output)
            idle_seconds = max(0.0, now - self.last_output_monotonic)
            output_chunks = self.output_chunks
            if self.first_output_monotonic is None:
                active_seconds = 0.0
            else:
                active_seconds = max(0.0, now - self.first_output_monotonic)
            events = list(self.output_events)

        window = max(0.5, float(recent_window_seconds or 0.5))
        recent_events = [(ts, size) for ts, size in events if now - ts <= window]
        recent_chunks = len(recent_events)
        recent_chars = sum(size for _, size in recent_events)
        recent_span = 0.0
        if recent_chunks >= 2:
            recent_span = max(0.0, recent_events[-1][0] - recent_events[0][0])
        return {
            'output_chars': output_chars,
            'output_idle_seconds': idle_seconds,
            'output_chunks': output_chunks,
            'output_active_seconds': active_seconds,
            'recent_output_window_seconds': window,
            'recent_output_chunks': recent_chunks,
            'recent_output_chars': recent_chars,
            'recent_output_span_seconds': recent_span,
        }

    def wait_reader_drain(self, timeout: float = 0.5):
        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=timeout)

    def terminate(self):
        if not self.process:
            return
        if self.process.poll() is not None:
            return
        try:
            if self.pty_enabled:
                import signal

                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if self.pty_enabled:
                    import signal

                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                else:
                    self.process.kill()
        except Exception as e:
            logger.warning(f"关闭 shell 会话失败 {self.session_id}: {e}")
        finally:
            if self.controller_fd is not None:
                try:
                    os.close(self.controller_fd)
                except OSError:
                    pass
                self.controller_fd = None


class AgentShellSessionManager:
    """Codex/Claude Code 风格的可等待、可交互 shell 会话层。"""

    MAX_SESSIONS = 4
    CONTEXT_OUTPUT_LIMIT = 12000
    START_CAPTURE_SECONDS = 1.0
    AFTER_INPUT_CAPTURE_SECONDS = 0.6
    WAIT_POLL_SECONDS = 0.25
    QUIET_AFTER_OUTPUT_SECONDS = 2.5
    QUIET_AFTER_OUTPUT_LONGRUN_SECONDS = 5.0
    STALLED_AFTER_OUTPUT_SECONDS = 5.0
    STALLED_AFTER_OUTPUT_LONGRUN_SECONDS = 10.0
    SILENT_RUNNING_SECONDS = 4.0
    SILENT_RUNNING_LONGRUN_SECONDS = 8.0
    ACTIVE_OUTPUT_SECONDS = 8.0
    ACTIVE_OUTPUT_LONGRUN_SECONDS = 12.0
    ACTIVE_OUTPUT_MIN_CHUNKS = 3
    ACTIVE_OUTPUT_MIN_LONGRUN_CHUNKS = 4
    RECENT_OUTPUT_WINDOW_SECONDS = 10.0
    ACTIVE_OUTPUT_RECENT_CHUNKS = 3
    ACTIVE_OUTPUT_RECENT_LONGRUN_CHUNKS = 4
    ACTIVE_OUTPUT_RECENT_MIN_CHARS = 80
    _sessions: Dict[str, AgentShellSession] = {}
    _lock = threading.RLock()

    STATE_DESCRIPTIONS = {
        'completed': '进程已经退出，输出已尽量收尾。',
        'interactive_prompt': '最近输出看起来像菜单、确认、密码、REPL 或 shell 提示符，可能在等待输入。',
        'active_output': '最近仍有多次输出且空闲时间很短，进程大概率还在持续产生日志或进度。',
        'output_quiet': '已经有输出，但最近一段时间没有新输出；进程还活着，可能在后台继续或等待某个步骤。',
        'output_stalled': '曾经有多次输出，但已超过停滞阈值没有新输出；进程可能卡住、等待资源或进入长步骤。',
        'silent_running': '进程仍在运行但还没有可见输出，可能是静默任务、阻塞或启动阶段。',
        'long_running_command': '命令形态像长驻/交互/服务/日志任务，进程仍在运行。',
        'still_running': '进程仍在运行，但还没有达到更明确的判定阈值。',
        'read_capture': '这是一次快速读取，不代表命令已结束。',
        'timeout': '已达到等待窗口，进程仍在运行。',
        'stopped': '用户停止事件已经触发。',
    }

    @classmethod
    def _shell_state_thresholds(cls, long_running_hint: bool, wait_timeout: float) -> Dict[str, float]:
        return {
            'quiet_after_output': min(
                cls.QUIET_AFTER_OUTPUT_LONGRUN_SECONDS if long_running_hint else cls.QUIET_AFTER_OUTPUT_SECONDS,
                max(1.0, wait_timeout * (0.20 if long_running_hint else 0.12))
            ),
            'stalled_after_output': min(
                cls.STALLED_AFTER_OUTPUT_LONGRUN_SECONDS if long_running_hint else cls.STALLED_AFTER_OUTPUT_SECONDS,
                max(2.0, wait_timeout * (0.45 if long_running_hint else 0.20))
            ),
            'silent_running_after': min(
                cls.SILENT_RUNNING_LONGRUN_SECONDS if long_running_hint else cls.SILENT_RUNNING_SECONDS,
                max(1.5, wait_timeout * (0.35 if long_running_hint else 0.15))
            ),
            'active_output_after': min(
                cls.ACTIVE_OUTPUT_LONGRUN_SECONDS if long_running_hint else cls.ACTIVE_OUTPUT_SECONDS,
                max(3.0, wait_timeout * (0.45 if long_running_hint else 0.35))
            ),
            'active_min_chunks': cls.ACTIVE_OUTPUT_MIN_LONGRUN_CHUNKS if long_running_hint else cls.ACTIVE_OUTPUT_MIN_CHUNKS,
            'active_recent_min_chunks': cls.ACTIVE_OUTPUT_RECENT_LONGRUN_CHUNKS if long_running_hint else cls.ACTIVE_OUTPUT_RECENT_CHUNKS,
            'active_recent_min_chars': cls.ACTIVE_OUTPUT_RECENT_MIN_CHARS,
            'recent_window': min(
                cls.RECENT_OUTPUT_WINDOW_SECONDS,
                max(3.0, wait_timeout * (0.50 if long_running_hint else 0.35))
            ),
        }

    @classmethod
    def _state_description(cls, state: str) -> str:
        return cls.STATE_DESCRIPTIONS.get(state, cls.STATE_DESCRIPTIONS['still_running'])

    @classmethod
    def _state_confidence(cls, state: str) -> str:
        if state in {'completed', 'stopped'}:
            return 'high'
        if state in {'interactive_prompt', 'active_output', 'output_stalled', 'timeout'}:
            return 'medium'
        return 'low'

    @classmethod
    def _evaluate_running_state(cls, session: AgentShellSession, output: str,
                                wait_timeout: float, elapsed_total: float = 0.0,
                                long_running_hint: bool = False) -> Dict[str, Any]:
        if not session.is_running():
            return {
                'state': 'completed',
                'reason': 'process exited',
                'confidence': cls._state_confidence('completed'),
            }

        thresholds = cls._shell_state_thresholds(long_running_hint, wait_timeout)
        activity = session.output_activity_snapshot(thresholds['recent_window'])
        output_chars = int(activity['output_chars'])
        output_idle_seconds = float(activity['output_idle_seconds'])
        output_chunks = int(activity['output_chunks'])
        output_active_seconds = float(activity['output_active_seconds'])
        recent_chunks = int(activity['recent_output_chunks'])
        recent_chars = int(activity['recent_output_chars'])
        recent_span = float(activity['recent_output_span_seconds'])

        inspected_output = output or session.read_recent(3000)
        if inspected_output and looks_like_interactive_prompt(inspected_output):
            state = 'interactive_prompt'
            return {
                'state': state,
                'reason': 'recent output matches an interactive prompt pattern',
                'confidence': cls._state_confidence(state),
                **activity,
            }

        if output_chars > 0:
            recent_active = (
                recent_chunks >= thresholds['active_recent_min_chunks']
                and recent_chars >= thresholds['active_recent_min_chars']
                and output_idle_seconds < thresholds['quiet_after_output']
            )
            sustained_active = (
                output_chunks >= thresholds['active_min_chunks']
                and output_active_seconds >= thresholds['active_output_after']
                and output_idle_seconds < thresholds['quiet_after_output']
            )
            if recent_active or sustained_active:
                state = 'active_output'
                return {
                    'state': state,
                    'reason': (
                        f"recent_chunks={recent_chunks}, recent_chars={recent_chars}, "
                        f"recent_span={recent_span:.1f}s, idle={output_idle_seconds:.1f}s"
                    ),
                    'confidence': cls._state_confidence(state),
                    **activity,
                }
            if (
                output_chunks >= 2
                and output_idle_seconds >= thresholds['stalled_after_output']
            ):
                state = 'output_stalled'
                return {
                    'state': state,
                    'reason': (
                        f"output_chunks={output_chunks}, idle={output_idle_seconds:.1f}s "
                        f">= stalled_threshold={thresholds['stalled_after_output']:.1f}s"
                    ),
                    'confidence': cls._state_confidence(state),
                    **activity,
                }
            if output_idle_seconds >= thresholds['quiet_after_output']:
                state = 'output_quiet'
                return {
                    'state': state,
                    'reason': (
                        f"idle={output_idle_seconds:.1f}s "
                        f">= quiet_threshold={thresholds['quiet_after_output']:.1f}s"
                    ),
                    'confidence': cls._state_confidence(state),
                    **activity,
                }

        if elapsed_total >= thresholds['silent_running_after']:
            state = 'long_running_command' if long_running_hint else 'silent_running'
            return {
                'state': state,
                'reason': (
                    f"elapsed={elapsed_total:.1f}s >= "
                    f"silent_threshold={thresholds['silent_running_after']:.1f}s; "
                    f"long_running_hint={long_running_hint}"
                ),
                'confidence': cls._state_confidence(state),
                **activity,
            }

        if long_running_hint and elapsed_total >= thresholds['active_output_after']:
            state = 'long_running_command'
            return {
                'state': state,
                'reason': (
                    f"command matches long-running patterns and elapsed={elapsed_total:.1f}s "
                    f">= handoff_threshold={thresholds['active_output_after']:.1f}s"
                ),
                'confidence': cls._state_confidence(state),
                **activity,
            }

        state = 'still_running'
        return {
            'state': state,
            'reason': 'no terminal or useful intermediate state reached yet',
            'confidence': cls._state_confidence(state),
            **activity,
        }

    @classmethod
    def _classify_running_state(cls, session: AgentShellSession, output: str,
                                wait_timeout: float, elapsed_total: float = 0.0,
                                long_running_hint: bool = False) -> str:
        return str(cls._evaluate_running_state(
            session, output, wait_timeout, elapsed_total, long_running_hint
        ).get('state') or 'still_running')

    @classmethod
    def _format_result(cls, session: AgentShellSession, output: str, action: str) -> Dict[str, Any]:
        running = session.is_running()
        rc = session.return_code()
        status = "running" if running else f"exited:{rc}"
        activity = session.output_activity_snapshot()
        return {
            'success': True,
            'session_id': session.session_id,
            'action': action,
            'command': session.command,
            'command_hint_long_running': looks_like_long_running_command(session.command),
            'running': running,
            'return_code': rc,
            'output': output or '(暂无新输出)',
            'status': status,
            'pty': session.pty_enabled,
            'output_chars': activity['output_chars'],
            'output_idle_seconds': round(float(activity['output_idle_seconds']), 1),
            'output_chunks': activity['output_chunks'],
            'output_active_seconds': round(float(activity['output_active_seconds']), 1),
            'recent_output_chunks': activity['recent_output_chunks'],
            'recent_output_chars': activity['recent_output_chars'],
            'recent_output_span_seconds': round(float(activity['recent_output_span_seconds']), 1),
            'interactive_prompt': running and looks_like_interactive_prompt(output),
        }

    @classmethod
    def _get(cls, session_id: str) -> AgentShellSession:
        with cls._lock:
            session = cls._sessions.get(session_id)
        if not session:
            raise KeyError(f"shell 会话不存在: {session_id}")
        return session

    @classmethod
    def _cleanup_exited(cls):
        with cls._lock:
            stale = [
                sid for sid, session in cls._sessions.items()
                if not session.is_running() and time.time() - session.last_output_at > 300
            ]
            for sid in stale:
                session = cls._sessions.pop(sid, None)
                if session:
                    session.terminate()

    @classmethod
    async def _wait_for_useful_state(cls, session: AgentShellSession, already_waited: float = 0.0,
                                     stop_event: Optional[asyncio.Event] = None,
                                     long_running_hint: bool = False) -> Tuple[bool, float, float, Dict[str, Any]]:
        wait_timeout = float(AgentExecutor.get_timeout())
        started = time.monotonic()
        remaining = max(0.0, wait_timeout - max(0.0, already_waited))
        state_info: Dict[str, Any] = {
            'state': 'timeout',
            'reason': cls._state_description('timeout'),
            'confidence': cls._state_confidence('timeout'),
        }

        while session.is_running() and remaining > 0:
            if stop_event and stop_event.is_set():
                state_info = {
                    'state': 'stopped',
                    'reason': 'stop event was set while waiting',
                    'confidence': cls._state_confidence('stopped'),
                }
                break
            now = time.monotonic()
            elapsed_total = max(0.0, already_waited) + (now - started)
            state_info = cls._evaluate_running_state(
                session,
                session.read_recent(3000),
                wait_timeout,
                elapsed_total,
                long_running_hint,
            )
            if state_info.get('state') != 'still_running':
                break

            sleep_for = min(cls.WAIT_POLL_SECONDS, remaining)
            if stop_event:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
                    state_info = {
                        'state': 'stopped',
                        'reason': 'stop event was set during poll sleep',
                        'confidence': cls._state_confidence('stopped'),
                    }
                    break
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(sleep_for)
            remaining = max(0.0, wait_timeout - max(0.0, already_waited) - (time.monotonic() - started))

        waited = min(wait_timeout, max(0.0, already_waited) + (time.monotonic() - started))
        completed = not session.is_running()
        if completed:
            state_info = {
                'state': 'completed',
                'reason': 'process exited during wait',
                'confidence': cls._state_confidence('completed'),
            }
            await asyncio.sleep(0.1)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: session.wait_reader_drain(0.5))
        elif remaining <= 0 and state_info.get('state') == 'still_running':
            state_info = cls._evaluate_running_state(
                session,
                session.read_recent(3000),
                wait_timeout,
                waited,
                long_running_hint,
            )
            if state_info.get('state') == 'still_running':
                state_info = {
                    **state_info,
                    'state': 'timeout',
                    'reason': f"waited {waited:.1f}s and reached wait_timeout={wait_timeout:.1f}s",
                    'confidence': cls._state_confidence('timeout'),
                }
        return completed, waited, wait_timeout, state_info

    @classmethod
    def _attach_wait_info(cls, result: Dict[str, Any], completed: bool,
                          waited: float, wait_timeout: float, wait_state: Any = ""):
        if isinstance(wait_state, dict):
            state_info = wait_state
            wait_state_name = str(state_info.get('state') or '')
        else:
            wait_state_name = str(wait_state or "")
            state_info = {
                'state': wait_state_name,
                'reason': cls._state_description(wait_state_name),
                'confidence': cls._state_confidence(wait_state_name),
            }
        result['completed_during_wait'] = completed
        result['waited_seconds'] = round(waited, 1)
        result['wait_state'] = wait_state_name
        result['wait_state_description'] = cls._state_description(wait_state_name)
        result['wait_state_reason'] = state_info.get('reason') or result['wait_state_description']
        result['wait_state_confidence'] = state_info.get('confidence') or cls._state_confidence(wait_state_name)
        for key in (
            'recent_output_chunks',
            'recent_output_chars',
            'recent_output_span_seconds',
            'recent_output_window_seconds',
        ):
            if key in state_info:
                value = state_info[key]
                if isinstance(value, float):
                    value = round(value, 1)
                result[key] = value
        if result.get('running'):
            result['paused_for_running'] = True
            if result.get('interactive_prompt') or wait_state_name == 'interactive_prompt':
                result['pause_reason'] = 'interactive_prompt'
            elif wait_state_name == 'long_running_command':
                result['pause_reason'] = 'long_running_command'
            elif wait_state_name == 'active_output':
                result['pause_reason'] = 'active_output'
            elif wait_state_name == 'output_quiet':
                result['pause_reason'] = 'output_quiet'
            elif wait_state_name == 'output_stalled':
                result['pause_reason'] = 'output_stalled'
            elif wait_state_name == 'silent_running':
                result['pause_reason'] = 'silent_running'
            elif wait_state_name == 'timeout':
                result['pause_reason'] = 'wait_timeout'
            elif wait_state_name == 'read_capture':
                result['pause_reason'] = 'read_capture'
            elif wait_state_name == 'stopped':
                result['pause_reason'] = 'stopped'
            else:
                result['pause_reason'] = 'still_running'

    @classmethod
    async def start(cls, command: str,
                    stop_event: Optional[asyncio.Event] = None) -> Dict[str, Any]:
        command = command.strip()
        if not command:
            return {'success': False, 'output': 'shell 命令为空', 'return_code': -1}

        blocked, pattern = AgentCommandBlacklist.check(command)
        if blocked:
            # 命中的规则只写日志，不回灌给模型。
            logger.warning(f"shell 命令被黑名单拦截，命中规则: {pattern} | 命令: {command[:200]}")
            return {
                'success': False,
                'output': BLACKLIST_BLOCKED_NOTICE,
                'return_code': -1,
            }

        cls._cleanup_exited()
        with cls._lock:
            running_count = sum(1 for s in cls._sessions.values() if s.is_running())
            if running_count >= cls.MAX_SESSIONS:
                return {
                    'success': False,
                    'output': f'已达到 shell 会话上限 ({cls.MAX_SESSIONS})，请先 shellkill 一个旧会话。',
                    'return_code': -1,
                }
            session_id = uuid.uuid4().hex[:8]
            session = AgentShellSession(session_id, command)
            cls._sessions[session_id] = session

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, session.start)
            await asyncio.sleep(cls.START_CAPTURE_SECONDS)
            long_running_hint = looks_like_long_running_command(command)
            completed, waited, wait_timeout, wait_state = await cls._wait_for_useful_state(
                session,
                cls.START_CAPTURE_SECONDS,
                stop_event,
                long_running_hint=long_running_hint
            )
            if completed:
                output = session.read_snapshot(cls.CONTEXT_OUTPUT_LIMIT)
            else:
                output = session.read_delta(cls.CONTEXT_OUTPUT_LIMIT)
            result = cls._format_result(session, output, 'start')
            cls._attach_wait_info(result, completed, waited, wait_timeout, wait_state)
            return result
        except Exception as e:
            with cls._lock:
                cls._sessions.pop(session_id, None)
            logger.error(f"启动 shell 会话失败: {e}")
            return {'success': False, 'output': f'启动 shell 会话失败: {str(e)[:200]}', 'return_code': -1}

    @classmethod
    async def send_input(cls, session_id: str, steps: List[Dict[str, Any]],
                         stop_event: Optional[asyncio.Event] = None) -> Dict[str, Any]:
        try:
            AgentExecutor._validate_stdin_steps(steps)
            session = cls._get(session_id)
            if not session.is_running():
                return cls._format_result(session, session.read_delta(), 'stdin')
            loop = asyncio.get_running_loop()
            input_bytes = 0
            for step in steps:
                if stop_event and stop_event.is_set():
                    break
                step_type = step.get('type')
                if step_type == 'bytes':
                    payload = step.get('payload') or b''
                    if not isinstance(payload, (bytes, bytearray)):
                        raise ValueError("stdin bytes step payload must be bytes")
                    if not session.pty_enabled:
                        pipe_payload = step.get('pipe_payload')
                        if pipe_payload is not None:
                            if not isinstance(pipe_payload, (bytes, bytearray)):
                                raise ValueError("stdin pipe payload must be bytes")
                            payload = pipe_payload
                    if payload:
                        await loop.run_in_executor(None, lambda data=payload: session.write_bytes(data))
                        input_bytes += len(payload)
                elif step_type == 'wait':
                    seconds = float(step.get('seconds') or 0)
                    if stop_event:
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
                        except asyncio.TimeoutError:
                            pass
                    else:
                        await asyncio.sleep(seconds)
                else:
                    raise ValueError(f"unknown stdin macro step: {step_type}")
            await asyncio.sleep(cls.AFTER_INPUT_CAPTURE_SECONDS)
            long_running_hint = looks_like_long_running_command(session.command)
            completed, waited, wait_timeout, wait_state = await cls._wait_for_useful_state(
                session,
                cls.AFTER_INPUT_CAPTURE_SECONDS,
                stop_event,
                long_running_hint=long_running_hint
            )
            if completed:
                output = session.read_snapshot(cls.CONTEXT_OUTPUT_LIMIT)
            else:
                output = session.read_delta(cls.CONTEXT_OUTPUT_LIMIT)
                if not output:
                    output = session.read_recent(cls.CONTEXT_OUTPUT_LIMIT)
            result = cls._format_result(session, output, 'stdin')
            cls._attach_wait_info(result, completed, waited, wait_timeout, wait_state)
            result['input_bytes'] = input_bytes
            return result
        except Exception as e:
            logger.error(f"写入 shell 会话失败: {e}")
            return {'success': False, 'session_id': session_id, 'output': f'写入 shell 会话失败: {str(e)[:200]}', 'return_code': -1}

    @classmethod
    async def read(cls, session_id: str,
                   stop_event: Optional[asyncio.Event] = None) -> Dict[str, Any]:
        try:
            session = cls._get(session_id)
            capture_seconds = min(1.0, cls.AFTER_INPUT_CAPTURE_SECONDS)
            if stop_event:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=capture_seconds)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(capture_seconds)
            completed = not session.is_running()
            waited = capture_seconds
            wait_timeout = capture_seconds
            long_running_hint = looks_like_long_running_command(session.command)
            if completed:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, lambda: session.wait_reader_drain(0.2))
            if completed:
                output = session.read_snapshot(cls.CONTEXT_OUTPUT_LIMIT)
            else:
                output = session.read_delta(cls.CONTEXT_OUTPUT_LIMIT)
                if not output:
                    output = session.read_recent(cls.CONTEXT_OUTPUT_LIMIT)
            if completed:
                wait_state = {
                    'state': 'completed',
                    'reason': 'process exited before or during read capture',
                    'confidence': cls._state_confidence('completed'),
                }
            else:
                wait_state = cls._evaluate_running_state(
                    session,
                    output,
                    float(AgentExecutor.get_timeout()),
                    time.monotonic() - session.started_monotonic,
                    long_running_hint=long_running_hint
                )
            result = cls._format_result(session, output, 'read')
            cls._attach_wait_info(result, completed, waited, wait_timeout, wait_state)
            return result
        except Exception as e:
            return {'success': False, 'session_id': session_id, 'output': f'读取 shell 会话失败: {str(e)[:200]}', 'return_code': -1}

    @classmethod
    async def kill(cls, session_id: str) -> Dict[str, Any]:
        try:
            session = cls._get(session_id)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, session.terminate)
            output = session.read_delta()
            with cls._lock:
                cls._sessions.pop(session_id, None)
            return cls._format_result(session, output, 'kill')
        except Exception as e:
            return {'success': False, 'session_id': session_id, 'output': f'关闭 shell 会话失败: {str(e)[:200]}', 'return_code': -1}

    @classmethod
    def kill_all(cls):
        with cls._lock:
            sessions = list(cls._sessions.values())
            cls._sessions.clear()
        for session in sessions:
            session.terminate()


class TriggerConditionExpression:
    """智能解析条件表达式：默认字面量匹配，支持正则和逻辑运算。

    语法示例：
        READY                    → 字面量匹配 "READY"
        READY AND SUCCESS        → 逻辑组合
        "error.*timeout"         → 正则匹配
        /error.*/i               → 正则 + 忽略大小写
        (READY OR STARTED) AND SUCCESS  → 括号分组
    """

    def __init__(self, expression: str):
        self.expression = (expression or '').strip()
        if not self.expression:
            raise ValueError('condition.when 不能为空')
        self.tokens = self._tokenize(self.expression)
        self.index = 0
        self.ast = self._parse_or()
        if self.index != len(self.tokens):
            raise ValueError(f"condition.when 存在多余内容: {self.tokens[self.index][1]}")
        self.literals = list(dict.fromkeys(self._collect_literals(self.ast)))
        self.matched: set = set()
        self.carry = ''
        self.max_literal_length = max(len(item) for item in self.literals)

    @staticmethod
    def _tokenize(expression: str) -> List[Tuple[str, str]]:
        """智能分词：裸词视为字面量，引号和斜杠包裹的特殊处理。"""
        tokens: List[Tuple[str, str]] = []
        index = 0
        while index < len(expression):
            char = expression[index]
            if char.isspace():
                index += 1
                continue
            if char in '()':
                tokens.append((char, char))
                index += 1
                continue

            # 正则表达式：/pattern/flags
            if char == '/':
                index += 1
                pattern_chars: List[str] = []
                while index < len(expression):
                    if expression[index] == '\\' and index + 1 < len(expression):
                        pattern_chars.append(expression[index])
                        pattern_chars.append(expression[index + 1])
                        index += 2
                        continue
                    if expression[index] == '/':
                        break
                    pattern_chars.append(expression[index])
                    index += 1
                if index >= len(expression):
                    raise ValueError('condition.when 中的正则表达式没有闭合（缺少结束 /）')
                index += 1  # 跳过结束 /
                # 收集 flags（i, m, s 等）
                flags = []
                while index < len(expression) and expression[index].isalpha():
                    flags.append(expression[index])
                    index += 1
                pattern = ''.join(pattern_chars)
                if not pattern:
                    raise ValueError('condition.when 正则表达式不能为空')
                # 正则表达式作为特殊 LITERAL 存储，运行时匹配
                tokens.append(('REGEX', (pattern, ''.join(flags))))
                continue

            # 引号字符串
            if char in {'"', "'"}:
                quote = char
                index += 1
                value: List[str] = []
                while index < len(expression):
                    char = expression[index]
                    if char == '\\' and index + 1 < len(expression):
                        value.append(expression[index + 1])
                        index += 2
                        continue
                    if char == quote:
                        break
                    value.append(char)
                    index += 1
                if index >= len(expression) or expression[index] != quote:
                    raise ValueError(f'condition.when 中的字符串没有闭合（缺少结束 {quote}）')
                literal = ''.join(value)
                if not literal:
                    raise ValueError('condition.when 不允许空字符串')
                tokens.append(('LITERAL', literal))
                index += 1
                continue

            # 裸词：AND/OR 关键字或字面量
            end = index
            while end < len(expression) and not expression[end].isspace() and expression[end] not in '()/':
                end += 1
            word = expression[index:end]
            upper_word = word.upper()
            if upper_word in {'AND', 'OR'}:
                tokens.append((upper_word, upper_word))
            else:
                # 裸词自动视为字面量
                tokens.append(('LITERAL', word))
            index = end
        if not tokens:
            raise ValueError('condition.when 不能为空')
        return tokens

    def _accept(self, token_type: str) -> Optional[Tuple[str, str]]:
        if self.index < len(self.tokens) and self.tokens[self.index][0] == token_type:
            token = self.tokens[self.index]
            self.index += 1
            return token
        return None

    def _parse_or(self):
        node = self._parse_and()
        while self._accept('OR'):
            node = ('OR', node, self._parse_and())
        return node

    def _parse_and(self):
        node = self._parse_primary()
        while self._accept('AND'):
            node = ('AND', node, self._parse_primary())
        return node

    def _parse_primary(self):
        literal = self._accept('LITERAL')
        if literal:
            return ('LITERAL', literal[1])
        regex = self._accept('REGEX')
        if regex:
            return ('REGEX', regex[1])
        if self._accept('('):
            node = self._parse_or()
            if not self._accept(')'):
                raise ValueError('condition.when 缺少右括号')
            return node
        found = self.tokens[self.index][1] if self.index < len(self.tokens) else '表达式末尾'
        raise ValueError(f'condition.when 语法错误，意外内容: {found}')

    @classmethod
    def _collect_literals(cls, node) -> List[str]:
        """收集所有字面量和正则表达式（用于确定最大长度）。"""
        if node[0] == 'LITERAL':
            return [node[1]]
        if node[0] == 'REGEX':
            # 正则表达式：返回模式字符串用于估算carry长度
            pattern, _ = node[1]
            # 使用合理的最大匹配长度估算（正则可能匹配任意长度）
            return [pattern[:100] if len(pattern) < 100 else 'X' * 100]
        return cls._collect_literals(node[1]) + cls._collect_literals(node[2])

    def feed(self, output: str) -> bool:
        """增量喂入输出，检查条件是否满足。"""
        combined = self.carry + (output or '')
        for literal in self.literals:
            if literal not in self.matched and literal in combined:
                self.matched.add(literal)

        # 检查正则表达式匹配
        self._check_regex_matches(combined)

        carry_length = max(0, self.max_literal_length - 1)
        self.carry = combined[-carry_length:] if carry_length else ''
        return self.is_satisfied()

    def _check_regex_matches(self, text: str):
        """检查AST中的正则表达式是否匹配。"""
        def check_node(node):
            if node[0] == 'REGEX':
                pattern, flags = node[1]
                regex_key = f"REGEX:{pattern}:{flags}"
                if regex_key not in self.matched:
                    regex_flags = 0
                    if 'i' in flags:
                        regex_flags |= re.IGNORECASE
                    if 'm' in flags:
                        regex_flags |= re.MULTILINE
                    if 's' in flags:
                        regex_flags |= re.DOTALL
                    try:
                        if re.search(pattern, text, regex_flags):
                            self.matched.add(regex_key)
                    except re.error:
                        pass  # 忽略正则表达式错误
            elif node[0] in ('AND', 'OR'):
                check_node(node[1])
                check_node(node[2])

        check_node(self.ast)

    def is_satisfied(self) -> bool:
        """检查条件是否完全满足。"""
        def evaluate(node) -> bool:
            if node[0] == 'LITERAL':
                return node[1] in self.matched
            if node[0] == 'REGEX':
                pattern, flags = node[1]
                regex_key = f"REGEX:{pattern}:{flags}"
                return regex_key in self.matched
            if node[0] == 'AND':
                return evaluate(node[1]) and evaluate(node[2])
            return evaluate(node[1]) or evaluate(node[2])
        return evaluate(self.ast)

    def matched_literals(self) -> List[str]:
        """返回已匹配的字面量（不包括正则的内部键）。"""
        result = []
        for item in self.literals:
            if item in self.matched:
                result.append(item)
        # 添加正则匹配的描述
        for key in self.matched:
            if key.startswith('REGEX:'):
                parts = key.split(':', 2)
                if len(parts) >= 2:
                    result.append(f"/{parts[1]}/{parts[2] if len(parts) > 2 else ''}")
        return result


class _SelfTriggerMessage:
    def __init__(self, bot: Any, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id

    async def reply_text(self, text: str, **kwargs):
        return await self.bot.send_message(chat_id=self.chat_id, text=text, **kwargs)


class _SelfTriggerUpdate:
    def __init__(self, bot: Any, chat_id: int):
        self.effective_chat = type('SelfTriggerChat', (), {'id': chat_id})()
        self.message = _SelfTriggerMessage(bot, chat_id)
        self.callback_query = None


class _SelfTriggerContext:
    def __init__(self, bot: Any):
        self.bot = bot


class SelfTriggerManager:
    """持久化执行后台命令，并把完成结果作为内部系统结果唤醒 AI。"""

    WAIT_NOTICE_SECONDS = 60.0
    REPEAT_RESTART_DELAY_SECONDS = 5.0
    REPEAT_DUPLICATE_BACKOFF_BASE_SECONDS = 5.0
    REPEAT_DUPLICATE_BACKOFF_MAX_SECONDS = 300.0
    DEFAULT_TIMEZONE = os.getenv('TRIGGER_TIMEZONE', 'Asia/Shanghai')
    MAX_CAPTURE_CHARS = 200000
    # 触发器命令的硬上限。没有它的话，一个挂起的命令会一直占着 _task_locks
    # 和 _runtime_tasks 槽位，之后每次调度都只记录“上一次仍在运行，跳过”，
    # 这个任务到进程重启前都是死的，而且用户完全看不到任何提示。
    MAX_RUN_SECONDS = float(os.getenv('TRIGGER_MAX_RUN_SECONDS', '3600'))
    _application: Optional[Application] = None
    _runtime_tasks: Dict[str, asyncio.Task] = {}
    _processes: Dict[str, Any] = {}
    _task_locks: Dict[str, asyncio.Lock] = {}
    _lock = asyncio.Lock()
    _execution_tasks: set = set()
    _delivery_run_ids: set = set()
    _delivery_tasks_by_task: Dict[str, set] = {}
    _stopping = False
    _started = False

    @staticmethod
    def _compact_task_summary(value: Any, max_chars: int = 600) -> str:
        text = str(value or '').strip()
        if not text:
            return ''
        text = re.sub(r'```.*?```', ' ', text, flags=re.S)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars - 1].rstrip() + '…'

    @classmethod
    def _build_task_summary(cls, task: Dict[str, Any]) -> str:
        stored_summary = cls._compact_task_summary(task.get('summary'))
        if stored_summary:
            return stored_summary

        for source in (task.get('origin_assistant_text'), task.get('origin_user_text')):
            source_text = str(source or '')
            match = re.search(
                r'(?im)^\s*(?:任务概述|任务说明)\s*[:：]\s*(.+?)\s*$',
                source_text,
            )
            if match:
                summary = cls._compact_task_summary(match.group(1))
                if summary:
                    return summary

        original_request = cls._compact_task_summary(task.get('origin_user_text'))
        if original_request:
            return original_request

        command = cls._compact_task_summary(task.get('command'))
        if command:
            return f'执行后台命令：{command}'
        return '未记录任务说明'

    @staticmethod
    def _repeat_result_signature(run: Dict[str, Any]) -> str:
        matched_conditions = run.get('matched_conditions') or []
        if isinstance(matched_conditions, str):
            with contextlib.suppress(Exception):
                matched_conditions = json.loads(matched_conditions)
        payload = {
            'status': run.get('status'),
            'trigger_reason': run.get('trigger_reason'),
            'matched_conditions': matched_conditions,
            'exit_code': run.get('exit_code'),
            'output': str(run.get('output') or '').strip(),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _next_duplicate_backoff(cls, task: Dict[str, Any]) -> float:
        current = float(task.get('backoff_seconds') or 0)
        if current <= 0:
            return cls.REPEAT_DUPLICATE_BACKOFF_BASE_SECONDS
        return min(cls.REPEAT_DUPLICATE_BACKOFF_MAX_SECONDS, current * 2)

    @classmethod
    async def _new_task_id(cls) -> str:
        db = await BotMemoryDB.get_instance()
        while True:
            task_id = f"trg_{uuid.uuid4().hex[:6]}"
            if await db.get_trigger_task(task_id) is None:
                return task_id

    @classmethod
    def _parse_definition(cls, body: str) -> Dict[str, Any]:
        """解析 YAML 格式的 trigger 定义。

        YAML 格式示例：
            task: 监控日志错误
            schedule:
              after: 30s
              timezone: Asia/Shanghai
            condition:
              when: READY
              repeat: true
            command: |
              tail -f /var/log/app.log
        """
        body = (body or '').strip()
        if not body:
            raise ValueError('trigger 定义不能为空')

        # 解析 YAML
        try:
            config = yaml.safe_load(body)
        except yaml.YAMLError as e:
            raise ValueError(f'trigger 定义 YAML 格式错误: {str(e)}') from e

        if not isinstance(config, dict):
            raise ValueError('trigger 定义必须是 YAML 对象（键值对），不能是列表或纯文本')

        # 必填字段：task 和 command
        task_summary = config.get('task', '').strip()
        if not task_summary:
            raise ValueError('trigger 定义缺少必填字段 "task"（任务概述）')
        if len(task_summary) > 600:
            raise ValueError(f'task 字段不能超过 600 字符，当前 {len(task_summary)} 字符')

        command = config.get('command', '').strip()
        if not command:
            raise ValueError('trigger 定义缺少必填字段 "command"（要执行的 Shell 命令）')

        # 解析 schedule（可选）
        schedule = config.get('schedule', {})
        if schedule and not isinstance(schedule, dict):
            raise ValueError('schedule 字段必须是对象，例如 {after: 30s}')

        timezone_name = schedule.get('timezone', cls.DEFAULT_TIMEZONE) if schedule else cls.DEFAULT_TIMEZONE
        try:
            timezone_value = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f'未知时区: {timezone_name}') from exc

        now = datetime.now(timezone_value)
        schedule_type = 'immediate'
        schedule_expr: Optional[str] = None
        next_run_at = now.timestamp()

        if schedule:
            schedule_keys = [k for k in ('after', 'at', 'cron') if k in schedule]
            if len(schedule_keys) > 1:
                raise ValueError(f'schedule 中只能使用一个时间字段（after/at/cron），当前有: {", ".join(schedule_keys)}')

            if 'after' in schedule:
                after_value = str(schedule['after']).strip()
                duration_match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([smhdw])', after_value, re.I)
                if not duration_match:
                    raise ValueError(f'schedule.after 格式错误，应为 30s/15m/2h/1d/1w，当前: {after_value}')
                duration_value = float(duration_match.group(1))
                unit = duration_match.group(2).lower()
                seconds = duration_value * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[unit]
                if seconds <= 0:
                    raise ValueError('schedule.after 必须大于 0')
                schedule_type = 'once'
                schedule_expr = after_value
                next_run_at = (now + timedelta(seconds=seconds)).timestamp()

            elif 'at' in schedule:
                at_value = str(schedule['at']).strip()
                try:
                    run_at = datetime.fromisoformat(at_value)
                except ValueError as exc:
                    raise ValueError(f'schedule.at 格式错误，应为 YYYY-MM-DD HH:MM[:SS]，当前: {at_value}') from exc
                if run_at.tzinfo is None:
                    run_at = run_at.replace(tzinfo=timezone_value)
                else:
                    run_at = run_at.astimezone(timezone_value)
                schedule_type = 'once'
                schedule_expr = at_value
                next_run_at = run_at.timestamp()

            elif 'cron' in schedule:
                cron_value = str(schedule['cron']).strip()
                try:
                    cron_trigger = CronTrigger.from_crontab(cron_value, timezone=timezone_value)
                    next_fire = cron_trigger.get_next_fire_time(None, now)
                except Exception as exc:
                    raise ValueError(f'schedule.cron 格式错误: {cron_value}') from exc
                if next_fire is None:
                    raise ValueError(f'cron 表达式没有可执行的未来时间: {cron_value}')
                schedule_type = 'cron'
                schedule_expr = cron_value
                next_run_at = next_fire.timestamp()

        # 解析 condition（可选）
        condition_config = config.get('condition', {})
        if condition_config and not isinstance(condition_config, dict):
            raise ValueError('condition 字段必须是对象，例如 {when: "READY", repeat: true}')

        condition_expr = None
        repeat = False
        if condition_config:
            when_value = condition_config.get('when')
            if when_value:
                condition_expr = str(when_value).strip()
                if not condition_expr:
                    raise ValueError('condition.when 不能为空字符串')
                # 验证条件表达式语法
                try:
                    TriggerConditionExpression(condition_expr)
                except Exception as e:
                    raise ValueError(f'condition.when 语法错误: {e}') from e

            repeat_value = condition_config.get('repeat')
            if repeat_value is not None:
                if isinstance(repeat_value, bool):
                    repeat = repeat_value
                elif isinstance(repeat_value, str):
                    repeat_lower = repeat_value.lower()
                    if repeat_lower in {'true', 'yes', '1'}:
                        repeat = True
                    elif repeat_lower in {'false', 'no', '0'}:
                        repeat = False
                    else:
                        raise ValueError(f'condition.repeat 只能是 true/false，当前: {repeat_value}')
                else:
                    raise ValueError(f'condition.repeat 只能是布尔值（true/false），当前类型: {type(repeat_value).__name__}')

        # 验证组合约束
        if repeat and not condition_expr:
            raise ValueError('condition.repeat 为 true 时必须同时设置 condition.when')

        # 检查命令黑名单
        blocked, pattern = AgentCommandBlacklist.check(command)
        if blocked:
            logger.warning(f"trigger 命令被黑名单拦截，命中规则: {pattern} | 命令: {command[:200]}")
            raise ValueError(BLACKLIST_BLOCKED_NOTICE)

        return {
            'command': command,
            'summary': task_summary,
            'schedule_type': schedule_type,
            'schedule_expr': schedule_expr,
            'timezone': timezone_name,
            'next_run_at': next_run_at,
            'condition_expr': condition_expr,
            'repeat': repeat,
        }

    @classmethod
    async def handle_protocol(cls, target: str, body: str, bot: Any, chat_id: int,
                              conversation_id: str, origin_user_text: str,
                              origin_assistant_text: str) -> str:
        """处理 trigger-x 协议。

        支持三种形式：
        1. trigger-x + <<BEGIN_...   → 创建新任务
        2. trigger-x:show + <<BEGIN_... → 查看活跃任务
        3. trigger-x:kill:<任务ID> + <<BEGIN_... → 取消任务
        4. trigger-x:kill:all + <<BEGIN_... → 取消所有任务
        """
        normalized_target = (target or '').strip()

        # 查看活跃任务
        if normalized_target == 'show':
            return await cls.format_active_tasks()

        # 取消所有任务
        if normalized_target == 'kill:all':
            count = await cls.cancel_all()
            return f"✅ 已取消全部触发任务，共 {count} 个"

        # 取消指定任务
        if normalized_target.startswith('kill:'):
            task_id = normalized_target[5:].strip()
            if not task_id:
                raise ValueError("trigger-x:kill: 后必须指定任务 ID，例如 trigger-x:kill:trg_abc123")
            return await cls.cancel(task_id)

        # 不支持的目标
        if normalized_target:
            raise ValueError(
                f'trigger-x 不支持的目标: {normalized_target}\n'
                '支持的操作：\n'
                '  - trigger-x          → 创建新任务\n'
                '  - trigger-x:show     → 查看活跃任务\n'
                '  - trigger-x:kill:<ID> → 取消指定任务\n'
                '  - trigger-x:kill:all → 取消所有任务'
            )

        # 创建新任务
        return await cls.register(
            body, bot, chat_id, conversation_id,
            origin_user_text, origin_assistant_text,
        )

    @classmethod
    async def register(cls, body: str, bot: Any, chat_id: int, conversation_id: str,
                       origin_user_text: str, origin_assistant_text: str) -> str:
        """注册一个新的 trigger 任务。"""
        definition = cls._parse_definition(body)
        task_id = await cls._new_task_id()
        now = time.time()
        task = {
            'id': task_id,
            'chat_id': chat_id,
            'conversation_id': conversation_id,
            **definition,
            'status': 'scheduled' if definition['schedule_type'] in {'once', 'cron'} else 'pending',
            'origin_user_text': origin_user_text,
            'origin_assistant_text': origin_assistant_text,
            'created_at': now,
            'updated_at': now,
        }
        db = await BotMemoryDB.get_instance()
        await db.create_trigger_task(task)
        if cls._application is None:
            cls._application = getattr(bot, '_application', None)
        await cls._activate_task(task, recovery=False)

        schedule_text = cls._format_schedule(task)
        condition_text = f"，条件: {definition['condition_expr']}" if definition.get('condition_expr') else ''
        repeat_text = '，条件命中后自动重启监控' if definition.get('repeat') else ''
        return (
            f"✅ 已创建触发任务 {task_id}\n"
            f"📝 任务: {definition['summary']}\n"
            f"⏰ 计划: {schedule_text}{condition_text}{repeat_text}"
        )

    @classmethod
    async def cancel(cls, task_id: str) -> str:
        """取消一个触发任务。"""
        db = await BotMemoryDB.get_instance()
        task = await db.get_trigger_task(task_id)
        if task is None or task['status'] in {'completed', 'cancelled', 'failed'}:
            return f"❌ 未找到活跃的触发任务: {task_id}"
        await db.cancel_trigger_tasks(task_id)
        cls._remove_scheduler_job(task_id)
        runtime_task = cls._runtime_tasks.get(task_id)
        if runtime_task and not runtime_task.done():
            runtime_task.cancel()
        for delivery_task in list(cls._delivery_tasks_by_task.get(task_id, set())):
            if not delivery_task.done():
                delivery_task.cancel()
        process = cls._processes.get(task_id)
        if process is not None:
            await terminate_async_process(process)
        return f"✅ 已取消触发任务: {task_id}"

    @classmethod
    async def cancel_all(cls) -> int:
        db = await BotMemoryDB.get_instance()
        tasks = await db.list_trigger_tasks(active_only=True)
        count = await db.cancel_trigger_tasks()
        for task in tasks:
            cls._remove_scheduler_job(task['id'])
            runtime_task = cls._runtime_tasks.get(task['id'])
            if runtime_task and not runtime_task.done():
                runtime_task.cancel()
            for delivery_task in list(cls._delivery_tasks_by_task.get(task['id'], set())):
                if not delivery_task.done():
                    delivery_task.cancel()
            process = cls._processes.get(task['id'])
            if process is not None:
                await terminate_async_process(process)
        return count

    @classmethod
    async def shutdown(cls):
        cls._stopping = True
        runtime_tasks = [task for task in cls._runtime_tasks.values() if not task.done()]
        for runtime_task in runtime_tasks:
            runtime_task.cancel()
        for process in list(cls._processes.values()):
            await terminate_async_process(process)
        if runtime_tasks:
            await asyncio.gather(*runtime_tasks, return_exceptions=True)
        delivery_tasks = [task for task in cls._execution_tasks if not task.done()]
        for delivery_task in delivery_tasks:
            delivery_task.cancel()
        if delivery_tasks:
            await asyncio.gather(*delivery_tasks, return_exceptions=True)
        cls._runtime_tasks.clear()
        cls._processes.clear()
        cls._delivery_run_ids.clear()
        cls._delivery_tasks_by_task.clear()

    @classmethod
    async def format_active_tasks(cls) -> str:
        """格式化输出所有活跃任务。"""
        db = await BotMemoryDB.get_instance()
        tasks = await db.list_trigger_tasks(active_only=True)
        if not tasks:
            return "📋 当前没有活跃的触发任务"

        lines = [f"📋 活跃触发任务: {len(tasks)} 个\n"]
        now = time.time()
        for index, task in enumerate(tasks, start=1):
            elapsed = cls._format_elapsed(now - float(task['created_at']))
            repeat_text = '是' if task['repeat'] else '否'
            condition_text = task.get('condition_expr') or '(命令退出即触发)'
            duplicate_count = int(task.get('duplicate_count') or 0)
            backoff_until = float(task.get('backoff_until') or 0)
            backoff_text = (
                f'{max(0, int(backoff_until - now))} 秒后重试'
                if backoff_until > now else '无'
            )
            lines.append(
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"#{index} 【{task['id']}】\n"
                f"📝 任务: {cls._build_task_summary(task)}\n"
                f"📊 状态: {task['status']}\n"
                f"⏰ 计划: {cls._format_schedule(task)}\n"
                f"🎯 条件: {condition_text}\n"
                f"💻 命令: {task['command']}\n"
                f"⏱️  已存在: {elapsed}\n"
                f"🔄 重复监控: {repeat_text}\n"
                f"📈 已触发: {task['fire_count']} 次，恢复: {task['recovery_count']} 次\n"
                f"🔕 连续静默去重: {duplicate_count} 次\n"
                f"⏳ 当前退避: {backoff_text}"
            )
        return "\n".join(lines)

    @classmethod
    def _format_schedule(cls, task: Dict[str, Any]) -> str:
        schedule_type = task['schedule_type']
        next_run_at = task.get('next_run_at')
        if task.get('repeat') and next_run_at and float(next_run_at) > time.time():
            timezone_value = ZoneInfo(task['timezone'])
            local_time = datetime.fromtimestamp(float(next_run_at), timezone_value)
            return f"重复监控退避至 {local_time.isoformat(sep=' ', timespec='seconds')}"
        if schedule_type == 'immediate':
            return '立即执行'
        if schedule_type == 'cron':
            return f"cron {task['schedule_expr']} ({task['timezone']})"
        if next_run_at is None:
            return '单次任务'
        timezone_value = ZoneInfo(task['timezone'])
        local_time = datetime.fromtimestamp(float(next_run_at), timezone_value)
        return f"单次 {local_time.isoformat(sep=' ', timespec='seconds')}"

    @classmethod
    def _remove_scheduler_job(cls, task_id: str):
        if cls._application is None or cls._application.job_queue is None:
            return
        job_id = f'self-trigger:{task_id}'
        with contextlib.suppress(Exception):
            cls._application.job_queue.scheduler.remove_job(job_id)

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        seconds = max(0, int(seconds))
        if seconds < 60:
            return f"{seconds}秒"
        if seconds < 3600:
            return f"{seconds // 60}分钟"
        return f"{seconds // 3600}小时{(seconds % 3600) // 60}分钟"

    @classmethod
    async def startup(cls, application: Application):
        if cls._started and cls._application is application and not cls._stopping:
            return
        cls._application = application
        cls._stopping = False
        cls._started = True
        db = await BotMemoryDB.get_instance()
        interrupted_count = await db.interrupt_running_trigger_runs()
        if interrupted_count:
            logger.warning(f'检测到 {interrupted_count} 个因进程重启中断的 trigger run，将恢复任务')
        resumed_legacy_count = await db.resume_legacy_auto_paused_repeat_tasks()
        if resumed_legacy_count:
            logger.warning(f'已恢复 {resumed_legacy_count} 个被旧版消息风暴熔断暂停的 repeat 任务')
        interrupted_deliveries = await db.finalize_interrupted_trigger_deliveries()
        for run in interrupted_deliveries:
            task = await db.get_trigger_task(run['task_id'])
            if task is None or not task.get('repeat') or run.get('status') != 'condition_matched':
                continue
            backoff_until = time.time() + cls.REPEAT_DUPLICATE_BACKOFF_BASE_SECONDS
            await db.update_trigger_task(
                task['id'],
                status='pending',
                next_run_at=backoff_until,
                last_result_hash=cls._repeat_result_signature(run),
                duplicate_count=0,
                backoff_seconds=cls.REPEAT_DUPLICATE_BACKOFF_BASE_SECONDS,
                backoff_until=backoff_until,
                last_error=None,
            )
        if interrupted_deliveries:
            logger.warning(
                f'检测到 {len(interrupted_deliveries)} 个已开始但未确认完成的 trigger 投递，'
                '为避免重启后重复回复，已停止自动重放'
            )

        active_tasks = await db.list_trigger_tasks(active_only=True)
        active_task_map = {task['id']: task for task in active_tasks}
        for task in active_tasks:
            if not task.get('repeat') or task.get('last_result_hash'):
                continue
            latest_delivered = await db.get_latest_delivered_trigger_run(task['id'])
            if latest_delivered is None:
                continue
            result_hash = cls._repeat_result_signature(latest_delivered)
            await db.update_trigger_task(
                task['id'], last_result_hash=result_hash,
            )
            task['last_result_hash'] = result_hash

        undelivered_runs = await db.list_undelivered_trigger_runs()

        repeat_backlogs: Dict[str, List[Dict[str, Any]]] = {}
        for run in undelivered_runs:
            if run.get('repeat'):
                repeat_backlogs.setdefault(run['task_id'], []).append(run)
        suppressed_backlog_ids = set()
        for task_id, runs in repeat_backlogs.items():
            if len(runs) <= 1:
                continue
            runs.sort(key=lambda item: float(item.get('finished_at') or 0), reverse=True)
            stale_ids = [run['run_id'] for run in runs[1:]]
            suppressed = await db.suppress_trigger_runs(
                stale_ids, '旧版 repeat 调度产生积压，启动时仅保留最新结果',
            )
            suppressed_backlog_ids.update(stale_ids)
            logger.warning(
                f'repeat 任务 {task_id} 有 {len(runs)} 个未投递结果，'
                f'已合并 {suppressed} 个旧结果，仅保留最新结果'
            )
        if suppressed_backlog_ids:
            undelivered_runs = [
                run for run in undelivered_runs if run['run_id'] not in suppressed_backlog_ids
            ]

        retained_runs = []
        for run in undelivered_runs:
            if not run.get('repeat'):
                retained_runs.append(run)
                continue
            task = active_task_map.get(run['task_id']) or await db.get_trigger_task(run['task_id'])
            if task is None or not task.get('last_result_hash'):
                retained_runs.append(run)
                continue
            if cls._repeat_result_signature(run) != task['last_result_hash']:
                retained_runs.append(run)
                continue

            await db.suppress_trigger_runs(
                [run['run_id']], '与上次已投递结果完全相同，启动时静默去重',
            )
            backoff_seconds = cls._next_duplicate_backoff(task)
            backoff_until = time.time() + backoff_seconds
            duplicate_count = int(task.get('duplicate_count') or 0) + 1
            await db.update_trigger_task(
                task['id'],
                status='pending',
                next_run_at=backoff_until,
                backoff_until=backoff_until,
                backoff_seconds=backoff_seconds,
                duplicate_count=duplicate_count,
                last_error=None,
            )
            task.update({
                'status': 'pending',
                'next_run_at': backoff_until,
                'backoff_until': backoff_until,
                'backoff_seconds': backoff_seconds,
                'duplicate_count': duplicate_count,
            })
            logger.info(
                f"repeat 任务 {task['id']} 的积压结果与上次相同，启动时静默去重，"
                f'{backoff_seconds:g} 秒后继续监控'
            )
        undelivered_runs = retained_runs

        repeat_tasks_waiting_delivery = set()
        for run in undelivered_runs:
            await cls._reconcile_finished_run_task(run)
            if run.get('repeat'):
                repeat_tasks_waiting_delivery.add(run['task_id'])
                await db.update_trigger_task(
                    run['task_id'], status='waiting_delivery', next_run_at=None,
                )
            cls._schedule_delivery(run['run_id'], run['task_id'])

        tasks = await db.list_trigger_tasks(active_only=True)
        for task in tasks:
            try:
                if task.get('repeat') and task['id'] in repeat_tasks_waiting_delivery:
                    logger.info(f"repeat 任务 {task['id']} 等待上次结果投递完成后再恢复")
                    continue
                await cls._activate_task(task, recovery=task['status'] == 'recovering')
            except Exception as exc:
                logger.error(f"恢复 trigger 任务 {task['id']} 失败: {exc}", exc_info=True)
                await db.update_trigger_task(
                    task['id'], status='failed', last_error=str(exc)[:1000],
                    failure_count=int(task['failure_count']) + 1,
                )
        logger.info(f'✅ trigger 持久化任务恢复完成，活跃任务 {len(tasks)} 个')

    @classmethod
    async def _reconcile_finished_run_task(cls, run: Dict[str, Any]):
        db = await BotMemoryDB.get_instance()
        task = await db.get_trigger_task(run['task_id'])
        if task is None or task['status'] != 'running':
            return
        counters = {
            'fire_count': int(task['fire_count']) + 1,
            'failure_count': int(task['failure_count']) + int(run['status'] in {'failed', 'condition_unmatched'}),
            'last_error': run.get('error'),
        }
        if task['schedule_type'] == 'cron':
            await db.update_trigger_task(
                task['id'], status='scheduled', next_run_at=cls._next_cron_timestamp(task),
                last_finished_at=run['finished_at'], **counters,
            )
        elif task['repeat'] and run['status'] == 'condition_matched':
            await db.update_trigger_task(
                task['id'], status='waiting_delivery', next_run_at=None,
                last_finished_at=run['finished_at'], **counters,
            )
        else:
            await db.update_trigger_task(
                task['id'], status='completed', next_run_at=None,
                last_finished_at=run['finished_at'], **counters,
            )

    @classmethod
    async def _activate_task(cls, task: Dict[str, Any], recovery: bool):
        if cls._stopping or task['status'] in {'completed', 'cancelled', 'failed'}:
            return
        schedule_type = task['schedule_type']
        now = time.time()
        next_run_at = float(task.get('next_run_at') or now)

        if task.get('repeat') and next_run_at > now:
            cls._add_date_job(task['id'], next_run_at, 'repeat_backoff')
            return

        if schedule_type == 'cron':
            if next_run_at <= now:
                cls._launch_runtime(task['id'], next_run_at, 'cron_misfire')
            cls._add_cron_job(task)
            return

        if schedule_type == 'once' and next_run_at > now:
            cls._add_date_job(task['id'], next_run_at, 'scheduled')
            return

        reason = 'recovery' if recovery else ('overdue' if schedule_type == 'once' else 'immediate')
        scheduled_at = now if recovery or schedule_type == 'immediate' else next_run_at
        cls._launch_runtime(task['id'], scheduled_at, reason)

    @classmethod
    def _add_date_job(cls, task_id: str, scheduled_at: float, reason: str):
        if cls._application is None or cls._application.job_queue is None:
            raise RuntimeError('trigger 调度器尚未初始化')
        cls._application.job_queue.scheduler.add_job(
            cls._scheduled_job,
            trigger=DateTrigger(run_date=datetime.fromtimestamp(scheduled_at).astimezone()),
            args=[task_id, scheduled_at, reason],
            id=f'self-trigger:{task_id}',
            replace_existing=True,
            misfire_grace_time=None,
        )

    @classmethod
    def _add_cron_job(cls, task: Dict[str, Any]):
        if cls._application is None or cls._application.job_queue is None:
            raise RuntimeError('trigger 调度器尚未初始化')
        cron_trigger = CronTrigger.from_crontab(
            task['schedule_expr'],
            timezone=ZoneInfo(task['timezone']),
        )
        cls._application.job_queue.scheduler.add_job(
            cls._cron_job,
            trigger=cron_trigger,
            args=[task['id']],
            id=f"self-trigger:{task['id']}",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=None,
        )

    @classmethod
    async def _scheduled_job(cls, task_id: str, scheduled_at: float, reason: str):
        cls._launch_runtime(task_id, scheduled_at, reason)

    @classmethod
    async def _cron_job(cls, task_id: str):
        scheduled_at = float(int(time.time() // 60) * 60)
        cls._launch_runtime(task_id, scheduled_at, 'cron')

    @classmethod
    def _launch_runtime(cls, task_id: str, scheduled_at: float, reason: str):
        if cls._stopping:
            return
        existing = cls._runtime_tasks.get(task_id)
        if existing and not existing.done():
            logger.warning(f'trigger 任务 {task_id} 上一次仍在运行，跳过本次 {reason}')
            return
        runtime_task = asyncio.create_task(cls._execute_task(task_id, scheduled_at, reason))
        cls._runtime_tasks[task_id] = runtime_task

        def cleanup(done_task: asyncio.Task):
            if cls._runtime_tasks.get(task_id) is done_task:
                cls._runtime_tasks.pop(task_id, None)

        runtime_task.add_done_callback(cleanup)

    @classmethod
    async def _execute_task(cls, task_id: str, scheduled_at: float, reason: str):
        task_lock = cls._task_locks.setdefault(task_id, asyncio.Lock())
        async with task_lock:
            db = await BotMemoryDB.get_instance()
            task = await db.get_trigger_task(task_id)
            if task is None or task['status'] in {'completed', 'cancelled', 'failed'}:
                return

            run, created = await db.create_trigger_run(task_id, scheduled_at, reason)
            if not created:
                return
            run_id = run['run_id']
            await db.update_trigger_task(
                task_id, status='running', last_started_at=time.time(), last_error=None,
            )

            try:
                result = await cls._run_trigger_command(task, run_id)
            except asyncio.CancelledError:
                process = cls._processes.get(task_id)
                if process is not None:
                    await terminate_async_process(process)
                await db.finish_trigger_run(
                    run_id, status='interrupted', finished_at=time.time(),
                    delivered_at=time.time(),
                    error='Bot 正在关闭，后台进程已终止' if cls._stopping else '任务已取消',
                )
                latest = await db.get_trigger_task(task_id)
                if latest and latest['status'] != 'cancelled':
                    await db.update_trigger_task(task_id, status='recovering')
                raise
            except Exception as exc:
                logger.error(f'trigger 任务 {task_id} 执行失败: {exc}', exc_info=True)
                result = {
                    'status': 'failed', 'trigger_reason': 'execution_error',
                    'matched_conditions': [], 'exit_code': -1, 'output': '',
                    'output_path': None, 'error': str(exc)[:2000],
                }
            finally:
                cls._processes.pop(task_id, None)

            finished_at = time.time()
            await db.finish_trigger_run(
                run_id,
                finished_at=finished_at,
                status=result['status'],
                trigger_reason=result['trigger_reason'],
                matched_conditions=json.dumps(result['matched_conditions'], ensure_ascii=False),
                exit_code=result['exit_code'],
                output=result['output'],
                output_path=result['output_path'],
                error=result['error'],
            )

            latest = await db.get_trigger_task(task_id)
            if latest is None or latest['status'] == 'cancelled':
                return

            if latest['repeat'] and result['status'] == 'condition_matched':
                result_hash = cls._repeat_result_signature(result)
                duplicate_result = bool(
                    latest.get('last_result_hash') and latest['last_result_hash'] == result_hash
                )
                if duplicate_result:
                    backoff_seconds = cls._next_duplicate_backoff(latest)
                    backoff_until = time.time() + backoff_seconds
                    result['status'] = 'suppressed'
                    result['trigger_reason'] = 'duplicate_suppressed'
                    result['error'] = None
                    await db.finish_trigger_run(
                        run_id,
                        status=result['status'],
                        trigger_reason=result['trigger_reason'],
                        delivered_at=time.time(),
                        error=result['error'],
                    )

                    fire_count = int(latest['fire_count']) + 1
                    duplicate_count = int(latest.get('duplicate_count') or 0) + 1
                    await db.update_trigger_task(
                        task_id,
                        status='pending',
                        next_run_at=backoff_until,
                        backoff_until=backoff_until,
                        backoff_seconds=backoff_seconds,
                        duplicate_count=duplicate_count,
                        last_finished_at=finished_at,
                        fire_count=fire_count,
                        last_error=None,
                    )
                    logger.info(
                        f'repeat 任务 {task_id} 结果与上次相同，静默抑制第 {duplicate_count} 次，'
                        f'{backoff_seconds:g} 秒后重试'
                    )
                    if not cls._stopping:
                        try:
                            cls._add_date_job(task_id, backoff_until, 'repeat_backoff')
                        except Exception as exc:
                            logger.error(f'repeat 任务 {task_id} 安排退避重试失败: {exc}', exc_info=True)
                    return

            fire_count = int(latest['fire_count']) + 1
            failure_count = int(latest['failure_count']) + int(result['status'] in {'failed', 'condition_unmatched'})
            task_status = 'completed'
            next_run_at: Optional[float] = None

            if latest['schedule_type'] == 'cron':
                task_status = 'scheduled'
                next_run_at = cls._next_cron_timestamp(latest)
            elif latest['repeat'] and result['status'] == 'condition_matched' and not cls._stopping:
                task_status = 'waiting_delivery'

            await db.update_trigger_task(
                task_id,
                status=task_status,
                next_run_at=next_run_at,
                last_finished_at=finished_at,
                fire_count=fire_count,
                failure_count=failure_count,
                last_error=result['error'],
            )
            cls._schedule_delivery(run_id, task_id)

    @classmethod
    def _next_cron_timestamp(cls, task: Dict[str, Any]) -> Optional[float]:
        timezone_value = ZoneInfo(task['timezone'])
        now = datetime.now(timezone_value)
        trigger = CronTrigger.from_crontab(task['schedule_expr'], timezone=timezone_value)
        next_fire = trigger.get_next_fire_time(None, now)
        return next_fire.timestamp() if next_fire else None

    @classmethod
    async def _run_trigger_command(cls, task: Dict[str, Any], run_id: str) -> Dict[str, Any]:
        command = task['command'].strip()
        blocked, pattern = AgentCommandBlacklist.check(command)
        if blocked:
            logger.warning(
                f"触发器命令被黑名单拦截，命中规则: {pattern} | 命令: {command[:200]}"
            )
            return {
                'status': 'failed', 'trigger_reason': 'blacklist',
                'matched_conditions': [], 'exit_code': -1, 'output': '',
                'output_path': None,
                'error': BLACKLIST_BLOCKED_NOTICE,
            }

        condition = TriggerConditionExpression(task['condition_expr']) if task.get('condition_expr') else None
        now = datetime.now()
        output_dir = os.path.join(COMMAND_OUTPUT_DIR, now.strftime('%Y-%m-%d'))
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"trigger_{task['id']}_{run_id}.txt")
        captured_parts: List[str] = []
        captured_length = 0
        output_truncated = False
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        matched = False

        kwargs: Dict[str, Any] = {}
        if os.name != 'nt':
            kwargs['start_new_session'] = True
            kwargs['executable'] = '/bin/bash'
        else:
            kwargs['creationflags'] = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=AgentExecutor.WORK_DIR,
            env={**os.environ, 'LANG': 'en_US.UTF-8'},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **kwargs,
        )
        cls._processes[task['id']] = process
        run_deadline = time.monotonic() + cls.MAX_RUN_SECONDS
        run_timed_out = False

        with open(output_path, 'w', encoding='utf-8', errors='replace') as output_file:
            output_file.write(f"Command:\n{command}\n\nStarted at: {datetime.now().isoformat(timespec='seconds')}\n\nOutput:\n")
            if process.stdout is None:
                raise RuntimeError('触发器子进程没有可读的 stdout')
            while True:
                remaining = run_deadline - time.monotonic()
                if remaining <= 0:
                    run_timed_out = True
                    await terminate_async_process(process)
                    break
                try:
                    chunk = await asyncio.wait_for(
                        process.stdout.read(4096), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    run_timed_out = True
                    await terminate_async_process(process)
                    break
                if not chunk:
                    break
                text_chunk = decoder.decode(chunk)
                output_file.write(text_chunk)
                if captured_length < cls.MAX_CAPTURE_CHARS:
                    remaining = cls.MAX_CAPTURE_CHARS - captured_length
                    captured_parts.append(text_chunk[:remaining])
                    captured_length += min(len(text_chunk), remaining)
                    if len(text_chunk) > remaining:
                        output_truncated = True
                else:
                    output_truncated = True
                if condition and not matched and condition.feed(text_chunk):
                    matched = True
                    await terminate_async_process(process)
            final_text = decoder.decode(b'', final=True)
            if final_text:
                output_file.write(final_text)
                if captured_length < cls.MAX_CAPTURE_CHARS:
                    captured_parts.append(final_text[:cls.MAX_CAPTURE_CHARS - captured_length])
            await process.wait()
            if run_timed_out:
                output_file.write(
                    f"\n\n[超时] 命令运行超过 {int(cls.MAX_RUN_SECONDS)} 秒，已被强制终止。\n"
                )
            output_file.write(f"\n\nFinished at: {datetime.now().isoformat(timespec='seconds')}\nExit code: {process.returncode}\n")

        output = ''.join(captured_parts).strip() or '(无输出)'
        if output_truncated:
            output += f"\n\n[输出过长，内存结果仅保留前 {cls.MAX_CAPTURE_CHARS} 字符；完整输出见文件]"
        matched_conditions = condition.matched_literals() if condition else []
        exit_code = process.returncode if process.returncode is not None else -1
        if run_timed_out:
            # 超时要覆盖条件判定：条件未命中不是因为进程正常退出，
            # 而是被我们杀掉的，必须如实告诉用户和模型。
            output += f"\n\n[超时] 命令运行超过 {int(cls.MAX_RUN_SECONDS)} 秒，已被强制终止。"
            status = 'failed'
            trigger_reason = 'timeout'
            error = f'命令运行超过 {int(cls.MAX_RUN_SECONDS)} 秒未结束，已被强制终止'
        elif condition:
            status = 'condition_matched' if matched else 'condition_unmatched'
            trigger_reason = 'condition_matched' if matched else 'process_exited_before_condition'
            error = None if matched else '进程已退出，但条件表达式未满足'
        else:
            status = 'completed' if exit_code == 0 else 'failed'
            trigger_reason = 'process_exit'
            error = None if exit_code == 0 else f'命令退出码为 {exit_code}'
        return {
            'status': status,
            'trigger_reason': trigger_reason,
            'matched_conditions': matched_conditions,
            'exit_code': exit_code,
            'output': output,
            'output_path': to_display_path(output_path),
            'error': error,
        }

    @classmethod
    def _schedule_delivery(cls, run_id: str, task_id: str):
        if cls._stopping or run_id in cls._delivery_run_ids:
            return
        cls._delivery_run_ids.add(run_id)
        execution_task = asyncio.create_task(cls._deliver_run(run_id))
        cls._execution_tasks.add(execution_task)
        cls._delivery_tasks_by_task.setdefault(task_id, set()).add(execution_task)

        def cleanup(done_task: asyncio.Task):
            cls._execution_tasks.discard(done_task)
            cls._delivery_run_ids.discard(run_id)
            task_deliveries = cls._delivery_tasks_by_task.get(task_id)
            if task_deliveries is not None:
                task_deliveries.discard(done_task)
                if not task_deliveries:
                    cls._delivery_tasks_by_task.pop(task_id, None)

        execution_task.add_done_callback(cleanup)

    @classmethod
    async def _deliver_run(cls, run_id: str):
        db = await BotMemoryDB.get_instance()
        run = await db.get_trigger_run(run_id)
        if run is None or run.get('delivered_at') is not None or run.get('finished_at') is None:
            return
        task = await db.get_trigger_task(run['task_id'])
        if task is None:
            return
        if task['status'] == 'cancelled':
            await db.finish_trigger_run(run_id, delivered_at=time.time())
            return
        if cls._application is None:
            logger.warning(f'trigger run {run_id} 暂时无法投递：Application 未初始化')
            return
        if not await db.claim_trigger_run_delivery(run_id, time.time()):
            logger.info(f'trigger run {run_id} 已由其他投递协程领取，跳过重复投递')
            return

        matched_conditions: List[str] = []
        with contextlib.suppress(Exception):
            matched_conditions = json.loads(run.get('matched_conditions') or '[]')
        reason_text = {
            'condition_matched': '输出条件已经满足',
            'process_exited_before_condition': '进程已退出，但输出条件未完全满足',
            'process_exit': '后台命令已经结束',
            'execution_error': '后台命令执行异常',
            'blacklist': '后台命令被安全策略拦截',
            'recovery': 'Bot 重启后恢复执行的后台命令已经结束',
            'cron': 'cron 后台命令本次运行已经结束',
            'cron_misfire': 'Bot 启动后补跑了一次错过的 cron 任务',
        }.get(run.get('trigger_reason'), str(run.get('trigger_reason') or '后台任务已产生结果'))
        output_text = clip_middle_text(str(run.get('output') or '(无输出)'), 24000, '后台输出')
        original_request = clip_middle_text(str(task.get('origin_user_text') or '(未记录)'), 4000, '原始请求')
        task_summary = cls._build_task_summary(task)
        condition_text = task.get('condition_expr') or '(无；命令退出即完成)'
        matched_text = ', '.join(matched_conditions) if matched_conditions else '(无)'
        warning_statuses = {'failed', 'condition_unmatched', 'interrupted'}
        notice_emoji = '⚠️' if run.get('status') in warning_statuses or run.get('error') else '🔔'
        visible_notice = (
            f"{notice_emoji} 后台任务已产生结果\n"
            f"任务概述：{task_summary}\n"
            f"结果状态：{reason_text}\n"
            f"任务 ID：{task['id']}\n"
            f"系统已记录完整执行结果，正在检查是否继续唤醒 AI。"
        )
        internal_result = (
            f"[后台任务结果]\n"
            f"这是系统在未来时间自动注入的真实执行结果，不是用户刚发送的新请求。"
            f"Telegram 中已经显示过任务完成系统提醒和 Agent 轮数整理提示。请根据任务概述和执行结果自然地继续处理，"
            f"不要重复系统提醒、不要复述内部协议。需要发送文件时可继续使用 sendfile。\n\n"
            f"任务 ID: {task['id']}\n"
            f"任务概述: {task_summary}\n"
            f"原始用户请求: {original_request}\n"
            f"后台命令: {task['command']}\n"
            f"结果原因: {reason_text}\n"
            f"条件表达式: {condition_text}\n"
            f"已匹配条件: {matched_text}\n"
            f"退出码: {run.get('exit_code')}\n"
            f"错误: {run.get('error') or '(无)'}\n"
            f"完整输出文件: {run.get('output_path') or '(无)'}\n\n"
            f"命令输出:\n{output_text}"
        )

        bot = cls._application.bot
        update = _SelfTriggerUpdate(bot, int(task['chat_id']))
        context = _SelfTriggerContext(bot)
        lock_acquired = asyncio.Event()
        process_task: Optional[asyncio.Task] = None
        wait_notice_task: Optional[asyncio.Task] = None
        try:
            if run.get('notice_started_at') is None:
                await db.finish_trigger_run(run_id, notice_started_at=time.time())
                try:
                    await bot.send_message(
                        chat_id=int(task['chat_id']),
                        text=visible_notice,
                    )
                except Exception as exc:
                    logger.warning(f"发送 trigger 可见提醒失败 {run_id}: {exc}")
                else:
                    await db.finish_trigger_run(run_id, notice_sent_at=time.time())

            latest_task = await db.get_trigger_task(task['id'])
            if latest_task is None or latest_task['status'] == 'cancelled':
                await db.finish_trigger_run(run_id, delivered_at=time.time())
                return
            # 🔔 可见提醒只发 Telegram 界面，不写入 AI 历史：任务概述/状态/ID 已全部包含在 internal_result 中，
            # 避免每次触发在历史里产生 "[系统操作] 🔔" + "[后台任务结果]" 两条重复记录。
            await GlobalRecorder.record_user_message(
                internal_result,
                MessageType.USER_TEXT,
                int(task['chat_id']),
            )
            process_task = asyncio.create_task(process_conversation(
                update,
                context,
                internal_result,
                lock_acquired_event=lock_acquired,
                force_agent_mode=True,
                reset_agent_iterations=False,
                agent_origin=AgentTurnOrigin.trigger(task['id'], run_id),
            ))
            wait_notice_task = asyncio.create_task(
                cls._notify_if_waiting(task, bot, lock_acquired, process_task)
            )
            await process_task
            await db.finish_trigger_run(run_id, delivered_at=time.time())
            if task.get('repeat') and run.get('status') == 'condition_matched':
                await db.update_trigger_task(
                    task['id'],
                    last_result_hash=cls._repeat_result_signature(run),
                    duplicate_count=0,
                    backoff_seconds=0,
                    backoff_until=None,
                )
            await cls._restart_repeat_after_delivery(task['id'], run)
        except asyncio.CancelledError:
            if process_task and not process_task.done():
                process_task.cancel()
            raise
        except Exception as exc:
            logger.error(f"投递 trigger run {run_id} 失败: {exc}", exc_info=True)
            with contextlib.suppress(Exception):
                await db.finish_trigger_run(run_id, delivered_at=time.time())
                if task.get('repeat') and run.get('status') == 'condition_matched':
                    await db.update_trigger_task(
                        task['id'],
                        last_result_hash=cls._repeat_result_signature(run),
                        duplicate_count=0,
                        backoff_seconds=0,
                        backoff_until=None,
                    )
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat_id=int(task['chat_id']),
                    text=f"⚠️ 后台任务 {task['id']} 已产生结果，但 AI 唤醒失败: {str(exc)[:200]}",
                )
            with contextlib.suppress(Exception):
                await cls._restart_repeat_after_delivery(task['id'], run)
        finally:
            if wait_notice_task and not wait_notice_task.done():
                wait_notice_task.cancel()

    @classmethod
    async def _restart_repeat_after_delivery(cls, task_id: str, run: Dict[str, Any]):
        if cls._stopping or run.get('status') != 'condition_matched':
            return
        db = await BotMemoryDB.get_instance()
        task = await db.get_trigger_task(task_id)
        if task is None or not task.get('repeat') or task.get('status') != 'waiting_delivery':
            return

        await asyncio.sleep(cls.REPEAT_RESTART_DELAY_SECONDS)
        if cls._stopping:
            return
        task = await db.get_trigger_task(task_id)
        if task is None or not task.get('repeat') or task.get('status') != 'waiting_delivery':
            return
        scheduled_at = time.time()
        await db.update_trigger_task(task_id, status='pending', next_run_at=scheduled_at)
        cls._launch_runtime(task_id, scheduled_at, 'repeat')

    @classmethod
    async def _notify_if_waiting(cls, task: Dict[str, Any], bot: Any,
                                 lock_acquired: asyncio.Event, process_task: asyncio.Task):
        try:
            await asyncio.wait_for(lock_acquired.wait(), timeout=cls.WAIT_NOTICE_SECONDS)
        except asyncio.TimeoutError:
            if not process_task.done():
                with contextlib.suppress(Exception):
                    await bot.send_message(
                        chat_id=int(task['chat_id']),
                        text=f"⏳ 后台任务 {task['id']} 已完成，正在等待当前对话处理结束。",
                    )


# --- ☆ 模型调用逻辑 ☆ ---
