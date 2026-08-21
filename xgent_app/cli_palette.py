"""`/` 命令下拉面板：raw 模式下的一行编辑器 + 实时过滤的候选列表。

为什么不用 readline 做这件事
--------------------------
readline 能做的只有"按 Tab 把候选**打印**出来"——列表打完就是一堆滚上去的
死文本，不能高亮、不能用方向键选、不会随着你继续打字收窄。codex / claude code
那种手感（打出 `/` 立刻在输入行下面浮出一个列表，边打边筛，↑↓ 选，回车执行）
需要自己接管按键并重画屏幕，readline 没有这个位置可以插进去。

所以这里自己实现一个够用的行编辑器：光标移动、退格、Home/End、Ctrl+A/E/U/K/W、
历史翻页，外加 `/` 开头时的候选面板。不是要复刻 readline 的全部功能，只要覆盖
在终端里敲一行命令真正会用到的那些键。

降级路径
--------
不是 TTY（管道、重定向）、平台没有 termios（Windows）、或者用户设了
XGENT_CLI_NO_SLASH_HINT 时，``interactive_supported()`` 返回 False，调用方
退回原来的 input() + readline，一切照旧。
"""

from __future__ import annotations

import atexit
import os
import sys
from typing import Callable, List, Optional, Sequence, Tuple

try:  # Windows 没有这两个模块
    import termios
    import tty
    import select
except ImportError:  # pragma: no cover - 平台相关
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]
    select = None  # type: ignore[assignment]

from .cli_render import display_width

# 面板最多显示几条。再多屏幕就被候选吃光了，而且超过十来条时用户本来就该
# 继续打字收窄，而不是用方向键一条条翻。
MAX_ROWS = 8

_ESC = "\x1b"
_CTRL_C = "\x03"
_CTRL_D = "\x04"


def interactive_supported() -> bool:
    if os.environ.get("XGENT_CLI_NO_SLASH_HINT"):
        return False
    if termios is None or tty is None or select is None:
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


class _RawMode:
    """cbreak + 一个 atexit 兜底。

    用 cbreak 而不是 setraw：cbreak 保留 ISIG，Ctrl+C 仍然走进程的 SIGINT
    处理器，xgent_cli 那套"回答中断 / 空闲退出"的语义不用改。代价是这里读不到
    \\x03，所以中断完全交给信号处理器。

    atexit 兜底是给"主线程收到 KeyboardInterrupt 退出、而读取线程还阻塞在
    read() 上"这种情况用的——那时 finally 不一定跑得到，终端会留在无回显
    状态，用户下一条 shell 命令就看不见自己打的字了。
    """

    _restore: Optional[Tuple[int, list]] = None

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.saved = None

    def __enter__(self) -> "_RawMode":
        self.saved = termios.tcgetattr(self.fd)
        _RawMode._restore = (self.fd, self.saved)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *_exc) -> None:
        if self.saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            except Exception:
                pass
        _RawMode._restore = None


@atexit.register
def _restore_terminal_on_exit() -> None:  # pragma: no cover - 退出路径
    pending = _RawMode._restore
    if not pending:
        return
    fd, saved = pending
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    except Exception:
        pass


class SlashPalette:
    """一次 read_line() = 读一行，期间自己画屏。

    调用方给三样东西：提示符、候选来源、渲染候选行的函数。面板不关心命令
    从哪来，也不关心行长什么样——那是 xgent_cli 的排版职责。
    """

    def __init__(
        self,
        prompt: str,
        list_commands: Callable[[str], Sequence[str]],
        render_rows: Callable[[Sequence[str], int], List[str]],
        history: Optional[List[str]] = None,
    ) -> None:
        self.prompt = prompt
        self.list_commands = list_commands
        self.render_rows = render_rows
        self.history = history if history is not None else []

        self.buffer = ""
        self.cursor = 0
        self.matches: List[str] = []
        self.selected = 0
        self.drawn_rows = 0
        self._history_index: Optional[int] = None
        self._history_stash = ""

    # ---------------- 输入 ----------------

    def _read_key(self, fd: int) -> str:
        """读一个"键"。多字节 UTF-8 和 ESC 序列都在这里收拢成一个 token。"""
        first = os.read(fd, 1)
        if not first:
            raise EOFError
        byte = first[0]
        if byte == 0x1B:
            return self._read_escape(fd)
        # UTF-8 前导字节 -> 把后续字节补齐，否则中文会被拆成乱码
        extra = 0
        if 0xC0 <= byte < 0xE0:
            extra = 1
        elif 0xE0 <= byte < 0xF0:
            extra = 2
        elif byte >= 0xF0:
            extra = 3
        data = first
        for _ in range(extra):
            data += os.read(fd, 1)
        return data.decode("utf-8", "replace")

    def _read_escape(self, fd: int) -> str:
        """区分裸 Esc 和方向键这类 CSI 序列。

        裸 Esc 后面不会紧跟别的字节，所以用一个极短的 select 超时来判断：
        20ms 内没有后续字节就是用户真的按了 Esc。
        """
        ready, _, _ = select.select([fd], [], [], 0.02)
        if not ready:
            return _ESC
        second = os.read(fd, 1).decode("latin-1")
        if second not in ("[", "O"):
            return _ESC + second
        seq = _ESC + second
        while True:
            ready, _, _ = select.select([fd], [], [], 0.02)
            if not ready:
                break
            ch = os.read(fd, 1).decode("latin-1")
            seq += ch
            if ch.isalpha() or ch == "~":
                break
        return seq

    # ---------------- 候选 ----------------

    def _menu_open(self) -> bool:
        """面板只在"整行就是第一个词、且以 / 开头"时出现。

        句子中间的路径（`看看 /home/x`）不该弹命令列表，所以一旦出现空格就
        收起来——那时用户在写参数，不是在挑命令。
        """
        return self.buffer.startswith("/") and " " not in self.buffer

    def _refresh_matches(self) -> None:
        if not self._menu_open():
            self.matches = []
            self.selected = 0
            return
        previous = self.matches[self.selected] if self.matches else None
        self.matches = list(self.list_commands(self.buffer[1:]))
        if previous in self.matches:
            self.selected = self.matches.index(previous)
        else:
            self.selected = 0

    # ---------------- 画屏 ----------------

    def _visible(self) -> Tuple[List[str], int]:
        """候选太多时开一个滑动窗口，把选中项始终留在窗口里。"""
        total = len(self.matches)
        if total <= MAX_ROWS:
            return self.matches, self.selected
        start = min(max(0, self.selected - MAX_ROWS // 2), total - MAX_ROWS)
        return self.matches[start:start + MAX_ROWS], self.selected - start

    def _render(self) -> None:
        out = ["\r\x1b[J"]  # 回到行首，抹掉从这里往下的所有内容
        out.append(self.prompt)
        out.append(self.buffer)

        rows: List[str] = []
        if self.matches:
            window, local = self._visible()
            rows = self.render_rows(window, local)
            hidden = len(self.matches) - len(window)
            if hidden > 0:
                rows.append(f"  … 还有 {hidden} 条，继续输入可收窄")
        if rows:
            out.append("\n" + "\n".join(rows))
            out.append(f"\x1b[{len(rows)}A")  # 回到输入行

        column = display_width(self.prompt) + display_width(self.buffer[:self.cursor])
        out.append(f"\r\x1b[{column + 1}G")
        self.drawn_rows = len(rows)
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _finish(self) -> None:
        """收尾：抹掉面板，把光标留在输入行的下一行。"""
        sys.stdout.write("\r\x1b[J" + self.prompt + self.buffer + "\n")
        sys.stdout.flush()
        self.drawn_rows = 0

    # ---------------- 编辑动作 ----------------

    def _insert(self, text: str) -> None:
        self.buffer = self.buffer[:self.cursor] + text + self.buffer[self.cursor:]
        self.cursor += len(text)
        self._history_index = None

    def _backspace(self) -> None:
        if self.cursor:
            self.buffer = self.buffer[:self.cursor - 1] + self.buffer[self.cursor:]
            self.cursor -= 1
            self._history_index = None

    def _delete(self) -> None:
        if self.cursor < len(self.buffer):
            self.buffer = self.buffer[:self.cursor] + self.buffer[self.cursor + 1:]
            self._history_index = None

    def _delete_word(self) -> None:
        left = self.buffer[:self.cursor].rstrip()
        cut = left.rfind(" ") + 1
        self.buffer = self.buffer[:cut] + self.buffer[self.cursor:]
        self.cursor = cut
        self._history_index = None

    def _accept_selection(self) -> None:
        """把高亮那条填进输入行，光标落到末尾，面板收起。"""
        if not self.matches:
            return
        self.buffer = "/" + self.matches[self.selected]
        self.cursor = len(self.buffer)
        self.matches = []

    def _history_move(self, delta: int) -> None:
        if not self.history:
            return
        if self._history_index is None:
            if delta > 0:
                return
            self._history_stash = self.buffer
            self._history_index = len(self.history)
        index = self._history_index + delta
        if index < 0:
            index = 0
        if index >= len(self.history):
            self._history_index = None
            self.buffer = self._history_stash
            self.cursor = len(self.buffer)
            return
        self._history_index = index
        self.buffer = self.history[index]
        self.cursor = len(self.buffer)

    # ---------------- 主循环 ----------------

    def read_line(self) -> str:
        fd = sys.stdin.fileno()
        with _RawMode(fd):
            self._render()
            while True:
                key = self._read_key(fd)

                if key in ("\r", "\n"):
                    # 面板开着且用户没打全 -> 回车 = 选中这条并直接执行，
                    # 这是"数字/方向键选菜单"在命令面板上的等价物。
                    if self.matches and self.buffer[1:] != self.matches[self.selected]:
                        self._accept_selection()
                    self._finish()
                    return self.buffer

                if key == "\t":
                    # Tab = 只填进输入行、不执行，方便接着写参数。
                    if self.matches:
                        self._accept_selection()
                        self._refresh_matches()
                elif key == _CTRL_D:
                    if not self.buffer:
                        self._finish()
                        raise EOFError
                    self._delete()
                elif key == _CTRL_C:  # cbreak 下通常收不到，留着以防万一
                    self._finish()
                    raise KeyboardInterrupt
                elif key in ("\x7f", "\b"):
                    self._backspace()
                elif key == "\x01":  # Ctrl+A
                    self.cursor = 0
                elif key == "\x05":  # Ctrl+E
                    self.cursor = len(self.buffer)
                elif key == "\x15":  # Ctrl+U
                    self.buffer = self.buffer[self.cursor:]
                    self.cursor = 0
                elif key == "\x0b":  # Ctrl+K
                    self.buffer = self.buffer[:self.cursor]
                elif key == "\x17":  # Ctrl+W
                    self._delete_word()
                elif key == _ESC:
                    self.matches = []
                    self._render()
                    continue
                elif key in ("\x1b[A", "\x1bOA"):  # ↑
                    if self.matches:
                        self.selected = (self.selected - 1) % len(self.matches)
                    else:
                        self._history_move(-1)
                elif key in ("\x1b[B", "\x1bOB"):  # ↓
                    if self.matches:
                        self.selected = (self.selected + 1) % len(self.matches)
                    else:
                        self._history_move(1)
                elif key in ("\x1b[C", "\x1bOC"):  # →
                    self.cursor = min(len(self.buffer), self.cursor + 1)
                elif key in ("\x1b[D", "\x1bOD"):  # ←
                    self.cursor = max(0, self.cursor - 1)
                elif key in ("\x1b[H", "\x1bOH", "\x1b[1~"):
                    self.cursor = 0
                elif key in ("\x1b[F", "\x1bOF", "\x1b[4~"):
                    self.cursor = len(self.buffer)
                elif key == "\x1b[3~":
                    self._delete()
                elif key.startswith(_ESC):
                    pass  # 不认识的转义序列，忽略掉别插进输入
                elif key.isprintable():
                    self._insert(key)

                self._refresh_matches()
                self._render()
