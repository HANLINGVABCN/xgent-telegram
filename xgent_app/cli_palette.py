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
import re
import sys
from typing import Callable, List, Optional, Sequence, Tuple

try:  # Windows 没有这几个模块
    import termios
    import tty
    import select
except ImportError:  # pragma: no cover - 平台相关
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]
    select = None  # type: ignore[assignment]

from .cli_render import char_width, display_width, pad_to_width, terminal_size

# 面板最多显示几条。再多屏幕就被候选吃光了，而且超过十来条时用户本来就该
# 继续打字收窄，而不是用方向键一条条翻。
MAX_ROWS = 8

_ESC = "\x1b"
_CTRL_C = "\x03"
_CTRL_D = "\x04"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _clip_ansi(text: str, width: int) -> str:
    """按显示宽度截断，ANSI 转义序列不占宽度也不被切断。

    候选行右侧要拼滚动条列，各行必须先截到统一宽度再对齐，否则一条超长
    的命令说明会把滚动条顶出屏幕、还会自己折行破坏行数记账。
    """
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    parts: List[str] = []
    used = 0
    pos = 0

    def _take(plain: str) -> bool:
        nonlocal used
        for ch in plain:
            if used >= width:
                return False
            parts.append(ch)
            used += char_width(ch)
        return True

    for match in _ANSI_RE.finditer(text):
        if not _take(text[pos:match.start()]):
            return "".join(parts)
        parts.append(match.group(0))
        pos = match.end()
    _take(text[pos:])
    return "".join(parts)


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
        abort_check: Optional[Callable[[], bool]] = None,
        echo_ansi: str = "",
    ) -> None:
        self.prompt = prompt
        self.list_commands = list_commands
        self.render_rows = render_rows
        self.history = history if history is not None else []
        # abort_check：等键时以 0.2s 粒度轮询它，变 True 就抛 KeyboardInterrupt。
        # cbreak 保留 ISIG，Ctrl+C 走的是信号而不是输入字节，阻塞中的 os.read
        # 永远醒不过来；没有这个轮询，"状态输入中 Ctrl+C 取消"就得等用户
        # 再敲一个键才生效。
        self.abort_check = abort_check
        # echo_ansi：回车提交后回显那一行用的 ANSI 前缀（用户输入淡蓝着色）。
        self.echo_ansi = echo_ansi

        self.buffer = ""
        self.cursor = 0
        self.matches: List[str] = []
        self.selected = 0
        self.drawn_rows = 0
        # 上次画屏后光标停在输入行的第几个可视行。输入超宽折行后光标不在
        # 首行，重画前必须先上移回来，这是长输入"疯狂复制"bug 的账本。
        self._cursor_row = 0
        self._history_index: Optional[int] = None
        self._history_stash = ""

    # ---------------- 输入 ----------------

    def _read_byte(self, fd: int) -> bytes:
        first = os.read(fd, 1)
        if not first:
            raise EOFError
        return first

    def _read_byte_polling(self, fd: int) -> bytes:
        """带取消轮询的读字节。select 不可用（非 POSIX）时退回阻塞读。"""
        if select is None or self.abort_check is None:
            return self._read_byte(fd)
        while True:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                return self._read_byte(fd)
            if self.abort_check():
                raise KeyboardInterrupt

    def _read_key(self, fd: int) -> str:
        """读一个"键"。多字节 UTF-8 和 ESC 序列都在这里收拢成一个 token。"""
        first = self._read_byte_polling(fd)
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
        """候选太多时开一个滑动窗口，把选中项留在窗口里。

        窗口锚在顶部：往下选时选中项一路走到窗口**最后一行**才开始滚动，
        而不是悬在中间半空——"继续往下选、放到底部"才是菜单的自然手感。
        """
        total = len(self.matches)
        if total <= MAX_ROWS:
            return self.matches, self.selected
        start = max(0, min(self.selected - MAX_ROWS + 1, total - MAX_ROWS))
        return self.matches[start:start + MAX_ROWS], self.selected - start

    def _scrollbar(self, visible: int, local: int) -> List[str]:
        """右侧滚动条的每行字符：┃ 是滑块，┆ 是轨道。

        滑块长度按"可见窗口占总列表的比例"缩放，位置按窗口起点在总列表
        中的比例折算——和图形界面滚动条同一个数学，只是画成一列字符。
        """
        total = len(self.matches)
        thumb_len = max(1, round(visible * visible / total))
        max_pos = visible - thumb_len
        window_start = self.selected - local
        span = max(1, total - visible)
        thumb_pos = 0 if max_pos <= 0 else min(max_pos, round(window_start * max_pos / span))
        return [
            "┃" if thumb_pos <= i < thumb_pos + thumb_len else "┆"
            for i in range(visible)
        ]

    def _candidate_rows(self, width: int) -> List[str]:
        """完整候选区行：候选行（可能拼上滚动条列）+ 隐藏条数提示。"""
        if not self.matches:
            return []
        window, local = self._visible()
        rows = list(self.render_rows(window, local))
        hidden = len(self.matches) - len(window)
        if hidden <= 0:
            return rows
        gutter = width - 2
        rows = [pad_to_width(_clip_ansi(row, gutter), gutter) for row in rows]
        rows = [row + " " + bar for row, bar in zip(rows, self._scrollbar(len(window), local))]
        rows.append(f"  … 还有 {hidden} 条 · ↑↓ 选择 · 继续输入可收窄")
        return rows

    def _render(self) -> None:
        """整块重画。所有位置都按**可视行**算，而不是按字符串行数算。

        prompt + buffer 超过终端宽度时会被终端自动折成多个可视行，光标
        停在哪一行哪一列必须显式记账（_cursor_row）：
          - 重画前先上移到输入行的**第一个**可视行再 \r\x1b[J 清屏——
            老实现假设输入行只占一行，折行后 \r 回到的只是折行中段，
            清不干净旧内容，每敲一个键就在下面多叠一份，正是
            "长输入疯狂复制好几行"那个 bug；
          - 画完候选后用 CSI A + CSI G 把光标精确放回逻辑位置，列号
            按 CJK 双宽折算，还要吃掉 xterm 的延迟换行边界（写到整行
            末尾时光标仍停在上一行行尾，不会提前跳到下一行行首）。
        """
        width = max(20, terminal_size()[0])
        prompt_width = display_width(self.prompt)
        end_col = prompt_width + display_width(self.buffer)
        # 输入行（prompt+buffer）占几个可视行；ceil 除法。
        input_rows = max(1, -(-end_col // width))
        cursor_col = prompt_width + display_width(self.buffer[:self.cursor])
        cur_row, cur_col = divmod(cursor_col, width)
        if cur_col == 0 and cursor_col > 0 and cursor_col == end_col:
            cur_row -= 1
            cur_col = width  # 延迟换行：物理光标还在上一行行尾

        out: List[str] = []
        if self._cursor_row:
            out.append(f"\x1b[{self._cursor_row}A")
        out.append("\r\x1b[J")
        out.append(self.prompt)
        out.append(self.buffer)

        rows = self._candidate_rows(width)
        if rows:
            out.append("\n" + "\n".join(rows))
            # 此刻光标在最后一条候选的行尾，位于输入首行下方
            # input_rows-1+len(rows) 行；要回到逻辑光标行 cur_row。
            up = len(rows) + (input_rows - 1) - cur_row
        else:
            up = (input_rows - 1) - cur_row
        if up > 0:
            out.append(f"\x1b[{up}A")
        out.append("\r")
        if cur_col:
            out.append(f"\x1b[{min(cur_col, width - 1) + 1}G")
        self._cursor_row = cur_row
        self.drawn_rows = len(rows)
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _finish(self) -> None:
        """收尾：抹掉面板，把光标留在输入行的下一行。

        提交的那一行按 echo_ansi 着色回显——用户敲的命令/消息在终端里
        染成淡蓝，和 AI 输出的青绿、系统的灰阶区分开。
        """
        out: List[str] = []
        if self._cursor_row:
            out.append(f"\x1b[{self._cursor_row}A")
        body = self.buffer
        if body and self.echo_ansi:
            body = f"{self.echo_ansi}{body}\x1b[0m"
        out.append("\r\x1b[J" + self.prompt + body + "\n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self.drawn_rows = 0
        self._cursor_row = 0

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
        self._cursor_row = 0
        with _RawMode(fd):
            self._render()
            try:
                return self._event_loop(fd)
            except (EOFError, KeyboardInterrupt):
                # 中断/EOF 也要把面板收干净再走：光标留在半行上，下一条
                # 输出会直接糊在候选列表中间。
                self._finish()
                raise

    def _event_loop(self, fd: int) -> str:
        while True:
            key = self._read_key(fd)

            if key in ("\r", "\n"):
                # 面板开着且用户没打全 -> 回车 = 选中这条并直接执行，
                # 这是"数字/方向键选菜单"在命令面板上的等价物。
                if self.matches and self.buffer[1:] != self.matches[self.selected]:
                    self._accept_selection()
                return self.buffer

            if key == "\t":
                # Tab = 只填进输入行、不执行，方便接着写参数。
                if self.matches:
                    self._accept_selection()
                    self._refresh_matches()
            elif key == _CTRL_D:
                if not self.buffer:
                    raise EOFError
                self._delete()
            elif key == _CTRL_C:  # cbreak 下通常收不到，留着以防万一
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
