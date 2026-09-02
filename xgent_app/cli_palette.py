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

粘贴
----
自己接管按键就得自己处理粘贴。终端把粘贴的内容当普通按键流送过来，里面的换行
和"用户按了回车"在字节上一模一样——不区分的话，粘一段多行日志就等于替用户按了
几十次回车，一行一条全发给 AI。解法是括号粘贴：进 raw 模式时开 \x1b[?2004h，
之后粘贴内容会被包在 \x1b[200~ … \x1b[201~ 里整块到达，read_line 把它当一次
输入收下（见 _apply_paste）。

不是所有终端都认这个开关（老 conhost、部分 SSH 客户端），所以还有第二道：读到
回车时看看后面是不是还紧跟着字节（_read_burst）。人按回车之后 5ms 内不可能再
送来一个字节，粘贴则必然还有——时序是这类终端唯一剩下的线索。

降级路径
--------
不是 TTY（管道、重定向）、平台没有 termios（Windows）、或者用户设了
XGENT_CLI_NO_SLASH_HINT 时，``interactive_supported()`` 返回 False，调用方
退回原来的 input() + readline，一切照旧。

常驻模式
--------
CLI 主循环（xgent_cli）以"会话"为单位使用本类：open() 进 raw 并藏掉终端
光标，之后 await_line() 一圈一圈地跑，AI 输出期间也不停——用户可以边看流式
回复边把下一句话打好。为此本类和渲染层（cli_render.TerminalScreen）按如下
协议配合：

  - 挂载到渲染层后，屏幕每次打印前调 withdraw() 撤框（光标精确回到输出流
    末行行首），打印后调 restore() 把框画回输出下方；
  - 撤框期间按键照常进缓冲，只是不上屏（_suppressed），restore 时一次性
    补画；
  - 忙时（上一轮还在跑）回车不提交：调 on_busy_submit 让调用方提示"系统
    仍在处理上一个请求"，草稿留在输入行，等空闲后再按一次回车发送。

框内光标是自绘的反色块（终端原生光标整个会话被 ?25l 藏掉，免得它在重绘
间隙闪到框外），左右移动的落点始终可见——这是"光标看不见"问题的修复。
"""

from __future__ import annotations

import atexit
import contextlib
import os
import re
import sys
import threading
from collections import OrderedDict
from typing import Callable, List, Optional, Sequence, Tuple

try:  # Windows 没有这几个模块
    import termios
    import tty
    import select
except ImportError:  # pragma: no cover - 平台相关
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]
    select = None  # type: ignore[assignment]

from .cli_render import (
    BOX_CHROME,
    Palette,
    box_block,
    char_width,
    display_width,
    pad_to_width,
    terminal_size,
    visual_row_of_col,
    visual_rows,
)

# 面板最多显示几条。再多屏幕就被候选吃光了，而且超过十来条时用户本来就该
# 继续打字收窄，而不是用方向键一条条翻。
MAX_ROWS = 8

_ESC = "\x1b"
_CTRL_C = "\x03"
_CTRL_D = "\x04"

# 括号粘贴（bracketed paste）。开启后终端会把粘贴的内容包在 200~/201~ 之间
# 送过来，我们才分得清"用户按了回车"和"粘贴的文本里有换行"。不开的话，粘贴
# 一段多行文本 = 每个换行都被当成一次回车提交，用户只是贴了点日志，却眼睁睁
# 看着几十条消息被逐行发给 AI——这正是这个开关存在的理由。
_BRACKETED_ON = "\x1b[?2004h"
_BRACKETED_OFF = "\x1b[?2004l"
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"
# 终端光标的藏/还。常驻模式下框每次重画之间原生光标会闪到框外，干脆整个
# 会话藏掉，框内光标用反色块自绘（_overlay_reverse_cursor）。
_CURSOR_HIDE = "\x1b[?25l"
_CURSOR_SHOW = "\x1b[?25h"
_REVERSE_ON = "\x1b[7m"
_REVERSE_OFF = "\x1b[27m"
# 结束标记迟迟不来（终端只发了开始标记就断了）时，等这么久就认栽收工。
_PASTE_IDLE_TIMEOUT = 1.0
# 没有括号粘贴的终端（老 conhost、部分 SSH 客户端）靠"回车后面还紧跟着字节"
# 来识别粘贴：人按下回车之后不可能在 _BURST_PEEK 之内再送来一个字节（键盘
# 扫描本身就要十几毫秒），而粘贴是整块灌进 tty 缓冲区的，回车后面必然还有。
# _BURST_IDLE 是这一整块读到"安静"为止的判据。
_BURST_PEEK = 0.005
_BURST_IDLE = 0.05
# 单行且不超过这个显示宽度的粘贴直接进输入行；再长或者带换行的改用占位符，
# 见 SlashPalette._apply_paste。
_PASTE_INLINE_MAX = 200

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _normalize_paste(data: bytes) -> str:
    """把粘贴过来的原始字节整理成可以放进输入行的文本。

    \\r\\n / \\r 统一成 \\n（终端行尾风格不一）；除换行和制表符外的控制字符
    全部丢掉——那些多半是被一起复制走的 ANSI 残渣，留着只会污染发给 AI 的
    正文。末尾的换行也去掉：复制整行时通常会带一个，用户并不是想让消息以
    空行结尾。
    """
    text = data.decode("utf-8", "replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        ch for ch in text
        if ch in ("\n", "\t") or (ch >= " " and ch != "\x7f")
    )
    return text.rstrip("\n")


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
    """cbreak + 括号粘贴 + 一个 atexit 兜底。

    用 cbreak 而不是 setraw：cbreak 保留 ISIG，Ctrl+C 仍然走进程的 SIGINT
    处理器，xgent_cli 那套"回答中断 / 空闲退出"的语义不用改。代价是这里读不到
    \\x03，所以中断完全交给信号处理器。

    进出的同时开关括号粘贴（_BRACKETED_ON/OFF）：开着的时候粘贴内容会被终端
    包在 \\x1b[200~ … \\x1b[201~ 里整块送来，读取循环才能把它当"一次粘贴"而
    不是"一串回车"。离开时必须关掉，否则用户回到 shell 后粘贴会看见裸的
    200~/201~ 标记。

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
        _write_raw(_BRACKETED_ON)
        return self

    def __exit__(self, *_exc) -> None:
        _write_raw(_BRACKETED_OFF)
        if self.saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            except Exception:
                pass
        _RawMode._restore = None


def _write_raw(text: str) -> None:
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        pass


# open() 被调用过 = 本进程真正接管过终端（进 raw / 藏光标）。探针和子进程
# 只 import 本模块、从不 open()，退出时绝不能往它们捕获的 stdout 里写
# 转义码——test_cli_input 的探针靠"stdout 最后一行是 JSON"传结果，尾巴上
# 多一段 ?25h 就全线解析失败。
_TERMINAL_TAKEN_OVER = False


@atexit.register
def _restore_terminal_on_exit() -> None:  # pragma: no cover - 退出路径
    if not _TERMINAL_TAKEN_OVER:
        return
    # 光标还原是无条件的：raw 哪怕没进成（_restore 为空），?25l 也可能
    # 已经发出去了（open 的写序），退出后 shell 里没有光标比没有回显更难受。
    _write_raw(_CURSOR_SHOW)
    _write_raw(_BRACKETED_OFF)
    pending = _RawMode._restore
    if not pending:
        return
    fd, saved = pending
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    except Exception:
        pass


class SlashPalette:
    """常驻输入框：会话期间挂在屏幕底部，读键 + 画屏 + 提交。

    调用方给三样东西：提示符、候选来源、渲染候选行的函数。面板不关心命令
    从哪来，也不关心行长什么样——那是 xgent_cli 的排版职责。

    生命周期：open() 进 raw 藏光标 → await_line() 一圈圈跑（忙时回车被
    busy_check 挡下、草稿保留）→ close() 退 raw 还光标抹框。_render 可能
    同时被读线程（按键）和主线程（restore/set_prompt）调用，_io_lock 把
    画屏串行化；渲染层打印输出前 withdraw()、打完 restore()，框永远不会
    被输出冲掉，输出也永远不会被框挡住。
    """

    def __init__(
        self,
        prompt: str,
        list_commands: Callable[[str], Sequence[str]],
        render_rows: Callable[[Sequence[str], int], List[str]],
        history: Optional[List[str]] = None,
        abort_check: Optional[Callable[[], bool]] = None,
        box_ansi: str = "",
        busy_check: Optional[Callable[[], bool]] = None,
        on_busy_submit: Optional[Callable[[str], None]] = None,
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
        # busy_check / on_busy_submit：忙时（上一轮对话还在跑）回车的去处——
        # 不提交、不清输入行，调 on_busy_submit 让调用方提示"仍在处理上一
        # 个请求"，草稿留着等空闲后再按一次回车。对齐 Telegram 端忙闸门
        # 的语义，不做排队。
        self.busy_check = busy_check
        self.on_busy_submit = on_busy_submit
        # box_ansi：输入框框线的颜色。用的是提示符那一档的颜色（红=状态输入中、
        # 黄=菜单导航中、绿=空闲），框线跟着提示符一起变，"这行字会去哪"这个
        # 信号就从一个字符扩大成整个框。
        self.box_ansi = box_ansi
        self._box_palette = Palette(bool(box_ansi))

        self.buffer = ""
        self.cursor = 0
        self.matches: List[str] = []
        self.selected = 0
        # 上次画屏后光标停在输入行的第几个可视行。输入超宽折行后光标不在
        # 首行，重画前必须先上移回来，这是长输入"疯狂复制"bug 的账本。
        self._cursor_row = 0
        # 上次画屏时的终端宽度。窗口一被拖动，终端会把已经打出去的行按新
        # 宽度重排，_cursor_row 这本"第几个可视行"的账当场作废——照着它
        # 上移就会把光标停在块中间，接下来的 [J 清掉的是别人的内容，
        # 屏幕上留下半截边框。宽度变了就按新宽度重算旧框高度（见 _render）。
        self._last_width = 0
        # 上次画屏的实际内容与光标位置：宽度变化时旧框被终端折行重排、
        # 高度变了，要按新宽度把"框顶到光标"的行数重新算出来才能上移回
        # 框顶擦干净，否则旧框残骸留在新框上面（见 _render）。
        self._drawn_lines: List[str] = []
        self._drawn_row = 0
        self._drawn_col = 0
        self._history_index: Optional[int] = None
        self._history_stash = ""
        # 占位符 -> 真正的粘贴正文。大段粘贴不进输入行本体（见 _apply_paste）。
        self._pastes: "OrderedDict[str, str]" = OrderedDict()
        self._paste_seq = 0
        # 已经从 fd 读出来、但还没当成按键消化掉的字节。粘贴是整块读的
        # （os.read 一次拿一大片），结束标记后面可能还粘着用户随后敲的键；
        # 丢掉它们等于吞掉用户的按键，所以退回这里，下一次读键先吃它。
        self._pending = bytearray()
        # ---- 常驻模式的状态 ----
        # 画屏互斥锁：_render/withdraw/restore/set_prompt 可能同时来自读线程
        # （按键）和主线程（渲染层复位、提示符换档），RLock 允许同线程重入
        # （restore → _render）。
        self._io_lock = threading.RLock()
        self._opened = False   # open() 进过 raw：会话级开关
        self._closed = False   # close() 之后永久拒绝再画（退出/退役路径）
        self._shown = False    # 框此刻在屏幕上（withdraw 会撤掉、_render 会画上）
        self._suppressed = False  # 渲染层正在打印输出：按键进缓冲但不上屏
        self._raw: Optional[_RawMode] = None

    # ---------------- 输入 ----------------

    def _ready(self, fd: int, timeout: float) -> bool:
        """timeout 秒内有没有字节可读。退回的字节算"立刻可读"。"""
        if self._pending:
            return True
        return self._fd_ready(fd, timeout)

    def _fd_ready(self, fd: int, timeout: float) -> bool:
        """只问 fd，不看退回缓冲。要真去 os.read 之前用这个，免得空读阻塞。"""
        if select is None:
            return True  # 没有 select 只能盲读（会阻塞），交给调用方
        ready, _, _ = select.select([fd], [], [], timeout)
        return bool(ready)

    def _read_byte(self, fd: int) -> bytes:
        if self._pending:
            return bytes([self._pending.pop(0)])
        first = os.read(fd, 1)
        if not first:
            raise EOFError
        return first

    def _read_byte_polling(self, fd: int) -> bytes:
        """带取消轮询的读字节。select 不可用（非 POSIX）时退回阻塞读。

        顺带盯着终端宽度：用户拖动窗口时不会按任何键，光靠"下次按键再重画"
        的话，输入框会一直保持旧宽度挂在那儿——横向拖宽是空一截，拖窄则被
        终端折行成残缺的两截边框。这里每 0.2 秒瞄一眼宽度，变了就立刻重画，
        松开鼠标基本就对齐了。用轮询而不是 SIGWINCH：信号处理器会在 raw
        模式下打断正在进行的 read，还要跨线程转发，得不偿失。
        """
        if self._pending:
            return self._read_byte(fd)
        if select is None:
            return self._read_byte(fd)
        while True:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                return self._read_byte(fd)
            if self.abort_check is not None and self.abort_check():
                raise KeyboardInterrupt
            if self._last_width and terminal_size()[0] != self._last_width:
                self._render()

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
            data += self._read_byte(fd)
        return data.decode("utf-8", "replace")

    def _read_escape(self, fd: int) -> str:
        """区分裸 Esc 和方向键这类 CSI 序列。

        裸 Esc 后面不会紧跟别的字节，所以用一个极短的 select 超时来判断：
        20ms 内没有后续字节就是用户真的按了 Esc。
        """
        ready = self._ready(fd, 0.02)
        if not ready:
            return _ESC
        second = self._read_byte(fd).decode("latin-1")
        if second not in ("[", "O"):
            return _ESC + second
        seq = _ESC + second
        while True:
            # 粘贴开始标记只要拼到一半就必须等下去：网络终端可能把
            # \x1b[200~ 拆成两个包，20ms 等不到后半截就会把这次粘贴当成
            # 一串普通按键读，换行又变回"回车提交"。
            timeout = 0.2 if _PASTE_START.startswith(seq) else 0.02
            if not self._ready(fd, timeout):
                break
            ch = self._read_byte(fd).decode("latin-1")
            seq += ch
            if ch.isalpha() or ch == "~":
                break
        return seq

    # ---------------- 粘贴 ----------------

    def _read_paste(self, fd: int) -> str:
        """读完 \\x1b[200~ 之后、\\x1b[201~ 之前的整块内容。

        一次粘贴通常分好几次 read 才收完（终端和 pty 都有缓冲区上限），所以
        边读边在**累积的字节**里找结束标记——标记本身也可能被切成两半落在
        两次 read 里，只查最后一块会漏。

        结束标记之后如果还粘着字节（用户紧接着敲的键），退回 _pending 让
        下一次读键去消化，不能连同粘贴一起丢掉。
        """
        end = _PASTE_END.encode()
        data = bytearray(self._pending)
        self._pending = bytearray()
        while True:
            index = data.find(end)
            if index >= 0:
                self._pending = bytearray(data[index + len(end):])
                return _normalize_paste(bytes(data[:index]))
            if not self._ready(fd, _PASTE_IDLE_TIMEOUT):
                # 结束标记没来（粘贴被打断、终端行为异常）：手里这些
                # 就当作全部内容，总好过把后面的按键当粘贴一直吞。
                return _normalize_paste(bytes(data))
            chunk = os.read(fd, 65536)
            if not chunk:
                return _normalize_paste(bytes(data))
            data += chunk

    def _read_burst(self, fd: int) -> Optional[str]:
        """回车后面是不是还紧跟着一整块字节？是就整块读回来，否则 None。

        这是给**不支持括号粘贴**的终端兜底的。那种终端把粘贴当普通按键流发，
        里面的换行和真回车在字节上没有区别，唯一还留下的线索就是时序：粘贴
        是一整块灌进 tty 缓冲区的，第一个换行后面必然立刻还有字节；人手按
        回车之后 5ms 内不可能再送来一个字节。

        顺带治了另一个毛病：老实现一次只读一个字节，CLI 一边发消息一边没人
        排空 tty，4096 字节的缓冲区一满就丢字节，粘贴的汉字被从中间截断成
        乱码（用户看到的"�见。"）。这里一次 64KB 整块吞下，来不及溢出。
        """
        if select is None:
            return None  # 没有 select 就没法看时序，只能相信这是真回车
        if not self._ready(fd, _BURST_PEEK):
            return None
        data = bytearray(self._pending)
        self._pending = bytearray()
        while self._fd_ready(fd, _BURST_IDLE if data else 0.0):
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
        return _normalize_paste(bytes(data))

    def _paste_placeholder(self, text: str) -> str:
        """给一段粘贴生成占位符。带序号是为了两次粘贴同样内容也能各自还原。"""
        self._paste_seq += 1
        lines = text.count("\n") + 1
        size = f"{lines} 行 · {len(text)} 字" if lines > 1 else f"{len(text)} 字"
        return f"[粘贴 {size} #{self._paste_seq}]"

    def _apply_paste(self, text: str) -> None:
        """把粘贴内容放进输入行。

        短的单行粘贴（贴个路径、token、URL）直接插进去，手感和打字一样。
        带换行、带制表符或者很长的，插的是占位符、正文另存——理由有两条：
          1. 输入行的光标记账（_render 的 _cursor_row）建立在"内容不超过
             一屏"上，把几十行日志摊进输入行会当场画崩；
          2. 用户贴一大段日志是想让它当**一条**消息的正文，不是想在终端里
             逐行审阅它。
        提交时 _expand_pastes 再把正文原样还回去。
        """
        if not text:
            return
        simple = "\n" not in text and "\t" not in text
        if simple and display_width(text) <= _PASTE_INLINE_MAX:
            self._insert(text)
            return
        marker = self._paste_placeholder(text)
        self._pastes[marker] = text
        self._insert(marker)

    def _expand_pastes(self, line: str) -> str:
        """把占位符换回粘贴正文。没粘贴过就是原样返回。"""
        for marker, text in self._pastes.items():
            if marker in line:
                line = line.replace(marker, text)
        return line

    def _take_paste_before_cursor(self) -> Optional[str]:
        """光标左边紧挨着的那个占位符（没有就返回 None）。

        占位符是一个整体，退格该把它整块删掉：让用户一格一格啃掉
        "[粘贴 118 行 · 4821 字 #1]" 这二十来个字符毫无意义，中途还会把它
        啃成一段还不回原文的死文本。
        """
        head = self.buffer[:self.cursor]
        for marker in reversed(list(self._pastes)):
            if head.endswith(marker):
                return marker
        return None

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

    def _layout(self, avail: int) -> Tuple[List[str], int, int]:
        """把输入内容切成框内每行放得下的若干段，并算出光标落在哪一段第几列。

        不能交给终端自动折行：框内每一行左右都要补上框线，必须自己知道断点
        在哪。断点按**显示宽度**算，CJK 占两列——按字符数算的话一行中文会
        把右框线顶出去半个屏幕。
        """
        rows: List[str] = [""]
        col = 0
        cur_row, cur_col = 0, 0
        for index, ch in enumerate(self.buffer):
            ch_width = char_width(ch)
            if col + ch_width > avail:
                rows.append("")
                col = 0
            if index == self.cursor:
                cur_row, cur_col = len(rows) - 1, col
            rows[-1] += ch
            col += ch_width
        if self.cursor >= len(self.buffer):
            if col >= avail:
                # 最后一行正好填满：光标要落到下一行行首，否则它会压在
                # 右框线上（也会被下一次画屏算错行数）。
                rows.append("")
                col = 0
            cur_row, cur_col = len(rows) - 1, col
        return rows, cur_row, cur_col

    def _fit_rows(self, rows: List[str], cur_row: int, budget: int) -> Tuple[List[str], int]:
        """输入行比预算还多时，开一个跟着光标走的窗口。

        终端只有那么高。画出去的行数一旦超过屏幕，终端会滚动，而重画靠的是
        "上移 N 行"——滚过之后这个 N 就对不上了，每敲一个键就在下面多叠一份
        （历史上"长输入疯狂复制"那个 bug）。宁可少显示几行也不能让记账失真。
        """
        if len(rows) <= budget:
            return rows, cur_row
        start = min(max(0, cur_row - budget + 1), len(rows) - budget)
        return rows[start:start + budget], cur_row - start

    def _overlay_reverse_cursor(self, rows: List[str], cur_row: int,
                                cur_col: int) -> List[str]:
        """把框内光标位置的字符换成反色块（行尾补一个反色空格）。

        终端原生光标在常驻会话里被 ?25l 藏掉了——它在整块重画的间隙会闪到
        框外、还会停在框线上，视觉上比没有光标更乱。反色块跟着内容一起
        重画，光标落在哪个字符上一目了然，左右移动有明确落点。

        按显示宽度走到 cur_col 拿到字符偏移（cur_col 永远落在字符边界上，
        这是 _layout 的保证）；宽字符整体反色，占两列的块不会只黑一半。
        """
        if not 0 <= cur_row < len(rows):
            return rows
        row = rows[cur_row]
        col = 0
        for offset, ch in enumerate(row):
            if col == cur_col:
                return (rows[:cur_row]
                        + [row[:offset] + _REVERSE_ON + ch + _REVERSE_OFF
                           + row[offset + len(ch):]]
                        + rows[cur_row + 1:])
            col += char_width(ch)
        # 走到行尾都没对上列号：光标在行尾，补一个反色空格当块。
        return (rows[:cur_row]
                + [row + _REVERSE_ON + " " + _REVERSE_OFF]
                + rows[cur_row + 1:])

    def _render(self) -> None:
        """整块重画：输入框 + 候选面板，然后把光标放回框内。

        位置全部按**可视行**记账（_cursor_row = 光标在整块里的第几行）：
          - 重画前先上移回整块的第一行再 \r\x1b[J 清屏，否则清不干净，
            每敲一个键就在下面多叠一份；
          - 画完之后用 CSI A + CSI G 把光标精确放回框内的逻辑位置，列号
            要加上左框线占的两列（"│ "），并按 CJK 双宽折算。

        常驻模式：持 _io_lock 串行化（读线程按键 / 主线程 restore 都会进
        这里）；被 withdraw 压制或已 close 时直接跳过，内容等 restore 时
        一次性补画。
        """
        with self._io_lock:
            if self._closed or self._suppressed:
                return
            width, height = terminal_size()
            width = max(20, width)
            inner = width - BOX_CHROME
            prompt_width = display_width(self.prompt)
            avail = max(4, inner - prompt_width)

            rows, cur_row, cur_col = self._layout(avail)
            # 候选面板挂在框下面，缩进 2 格对齐框内文字——不缩进的话它贴着屏幕
            # 左边，和框里的输入错开一格，看起来像两个不相干的东西。
            candidates = [" " * 2 + row for row in self._candidate_rows(width - 2)]
            # 高度预算：整块（上下框线 + 输入行 + 候选）不能顶满屏幕。先砍候选，
            # 再对输入行开窗口——候选是辅助信息，正在编辑的那一行不能不见。
            budget = max(3, height - 1)
            room = budget - 2 - len(candidates)
            if room < 1:
                candidates = []
                room = budget - 2
            rows, cur_row = self._fit_rows(rows, cur_row, max(1, room))
            rows = self._overlay_reverse_cursor(rows, cur_row, cur_col)

            body = [
                (self.prompt if index == 0 else " " * prompt_width) + row
                for index, row in enumerate(rows)
            ]
            lines = box_block(body, width, "", self._box_palette, self.box_ansi)
            lines.extend(candidates)

            out: List[str] = []
            # 终端被拖宽/拖窄之后，上一次画的那块已经被终端按新宽度重排过，
            # _cursor_row 记的"第几个可视行"不再对应任何东西。终端折行时光标
            # 锚定在文本位置，所以旧框顶部到光标的行数可以按新宽度重算：光标
            # 之前每一行折成几行（visual_rows 精确模拟终端折行，宽字符不跨行
            # ——ceil 除法在这种边界会少算 1 行，上移不到位留下残骸），加上
            # 光标在自己那行内掉到第几个折行段。上移这个行数就回到旧框顶部，
            # 清屏后旧框被整块擦掉重画。
            if (self._last_width and self._last_width != width
                    and self._drawn_lines
                    and self._drawn_row < len(self._drawn_lines)):
                up = sum(visual_rows(line, width)
                         for line in self._drawn_lines[:self._drawn_row])
                up += visual_row_of_col(self._drawn_lines[self._drawn_row],
                                        width, self._drawn_col)
                if up:
                    out.append(f"\x1b[{up}A")
            elif self._cursor_row:
                out.append(f"\x1b[{self._cursor_row}A")
            out.append("\r\x1b[J")
            out.append("\n".join(lines))

            target_row = 1 + cur_row  # 1 = 上边框
            up = (len(lines) - 1) - target_row
            if up > 0:
                out.append(f"\x1b[{up}A")
            out.append("\r")
            # 左框线 "│ " 占两列，行首还有提示符（续行是等宽空格）。
            cursor_col = 2 + prompt_width + cur_col
            out.append(f"\x1b[{min(cursor_col, width - 1) + 1}G")
            self._cursor_row = target_row
            self._last_width = width
            # 记下这次画的内容和光标位置，宽度再变时按当时的宽度重算框高。
            self._drawn_lines = list(lines)
            self._drawn_row = target_row
            self._drawn_col = cursor_col
            self._shown = True
            sys.stdout.write("".join(out))
            sys.stdout.flush()

    # ---------------- 常驻生命周期 ----------------

    def open(self) -> None:
        """进 raw 模式并藏掉终端光标，整个 CLI 会话只做一次。

        read_line 时代每次读一行都进出一次 raw；常驻输入框要求会话期间
        一直处于 raw（AI 输出时也要继续收按键），模式开关收进这里和
        close()。终端光标随之隐藏：框内光标改由反色块自绘，免得原生光标
        在整块重画的间隙闪到框外。atexit 兜底——主线程退出时读线程可能
        还阻塞在 os.read 上，close() 不一定跑得到。
        """
        global _TERMINAL_TAKEN_OVER
        with self._io_lock:
            if self._opened or self._closed:
                return
            _TERMINAL_TAKEN_OVER = True
            self._raw = _RawMode(sys.stdin.fileno())
            self._raw.__enter__()
            self._opened = True
        _write_raw(_CURSOR_HIDE)
        try:
            atexit.register(self.close)
        except Exception:
            pass

    def close(self) -> None:
        """抹框、还终端光标、退 raw。幂等：退出路径上会被多处调用。"""
        with self._io_lock:
            if self._closed:
                return
            self._closed = True
            self._opened = False
            self._suppressed = False
            if self._shown:
                out = f"\x1b[{self._cursor_row}A" if self._cursor_row else ""
                _write_raw(out + "\r\x1b[J")
                self._shown = False
                self._cursor_row = 0
        _write_raw(_CURSOR_SHOW)
        raw, self._raw = self._raw, None
        if raw is not None:
            raw.__exit__(None, None, None)
        try:
            atexit.unregister(self.close)
        except Exception:
            pass

    def await_line(self) -> str:
        """等一行提交（常驻模式）：画框 → 按键循环 → 回车提交。

        与 read_line 时代的区别：raw 模式由 open()/close() 管理会话级开关，
        这里只负责画和读；提交后框原地清空保留（不擦不回显），主循环把
        用户这句话打成正式消息块，渲染层会在打印前让位、打印后复位框。
        Ctrl+C/EOF 走 abort 轮询抛上来，草稿作废、框保留。
        """
        self._render()
        try:
            return self._event_loop(sys.stdin.fileno())
        except (EOFError, KeyboardInterrupt):
            self._discard_draft()
            raise

    def set_prompt(self, prompt: str, box_ansi: str = "") -> None:
        """换提示符/框线颜色（红=状态输入、黄=菜单、绿=空闲），框原地重画。

        主循环每次回到 read() 时调用——状态机进出的档位变化通过框色告诉
        用户"这行字会去哪"。内容不变时跳过重画，避免每轮空刷一次屏。
        """
        with self._io_lock:
            if self.prompt == prompt and self.box_ansi == box_ansi:
                return
            self.prompt = prompt
            self.box_ansi = box_ansi
            self._box_palette = Palette(bool(box_ansi))
            self._render()

    def withdraw(self) -> None:
        """把框从屏幕上撤掉，光标落回框顶行行首（= 输出流的当前位置）。

        给渲染层（cli_render.TerminalScreen）让位：打印输出前调用，打完
        调 restore() 复位。撤框后按键照常进缓冲、只是不上屏，内容在
        restore 时一次性补画——打字手感不受打印节奏影响。

        光标记账（_cursor_row/_drawn_lines）一并清零：宽度重算的账本只在
        "框还在屏幕上"时有效，撤掉的框没有"旧框顶"可言。
        """
        with self._io_lock:
            self._suppressed = True
            if self._shown:
                out = f"\x1b[{self._cursor_row}A" if self._cursor_row else ""
                _write_raw(out + "\r\x1b[J")
                self._shown = False
                self._cursor_row = 0
                self._drawn_lines = []
                self._drawn_row = 0

    def restore(self) -> None:
        """把框画回输出流末尾下方（withdraw 的对侧）。"""
        with self._io_lock:
            self._suppressed = False
            if self._closed:
                return
            self._render()

    def _reset_line(self) -> None:
        """清空输入行状态（提交后/取消后共用）。"""
        self.buffer = ""
        self.cursor = 0
        self.matches = []
        self.selected = 0
        self._history_index = None
        # 占位符映射一并清掉：正文已经随提交展开，留着只会让用户手打出的
        # 同形文本被错误还原成上一段粘贴。
        self._pastes.clear()

    def _submit(self) -> str:
        """回车提交：展开粘贴占位符，框原地清空保留，返回这一行。"""
        line = self._expand_pastes(self.buffer)
        self._reset_line()
        self._render()
        return line

    def _discard_draft(self) -> None:
        """作废草稿（Ctrl+C 取消 / EOF）：清缓冲，框保留。"""
        self._reset_line()
        self._render()

    # ---------------- 编辑动作 ----------------

    def _insert(self, text: str) -> None:
        self.buffer = self.buffer[:self.cursor] + text + self.buffer[self.cursor:]
        self.cursor += len(text)
        self._history_index = None

    def _backspace(self) -> None:
        if self.cursor:
            marker = self._take_paste_before_cursor()
            if marker is not None:
                cut = self.cursor - len(marker)
                self.buffer = self.buffer[:cut] + self.buffer[self.cursor:]
                self.cursor = cut
                self._pastes.pop(marker, None)
                self._history_index = None
                return
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

    def _event_loop(self, fd: int) -> str:
        while True:
            key = self._read_key(fd)

            if key in ("\r", "\n"):
                # 终端不支持括号粘贴时，粘贴里的换行和真回车字节上一模一样，
                # 只能靠"后面还紧跟着东西"来分辨。是粘贴就把整块收下当正文，
                # 绝不能替用户提交——那就是"贴一段日志，被逐行发给 AI"。
                burst = self._read_burst(fd)
                if burst:
                    self._apply_paste("\n" + burst)
                    self._refresh_matches()
                    self._render()
                    continue
                # 面板开着且用户没打全 -> 回车 = 选中这条并直接执行，
                # 这是"数字/方向键选菜单"在命令面板上的等价物。
                if self.matches and self.buffer[1:] != self.matches[self.selected]:
                    self._accept_selection()
                    self._refresh_matches()
                # 忙闸门：上一轮还在跑（或主循环还没回到等输入的位置）时，
                # 回车不提交——草稿留在输入行，提示用户稍候，对齐 Telegram
                # 端的忙语义；不做排队，等空闲后再按一次回车即发送。
                if self.busy_check is not None and self.busy_check():
                    if self.buffer and self.on_busy_submit is not None:
                        try:
                            self.on_busy_submit(self.buffer)
                        except Exception:
                            pass
                    self._render()
                    continue
                return self._submit()

            if key == "\t":
                # Tab = 只填进输入行、不执行，方便接着写参数。
                if self.matches:
                    self._accept_selection()
                    self._refresh_matches()
            elif key == _PASTE_START:
                # 粘贴：整块收进来当一次输入，里面的换行是**正文**，不是回车。
                self._apply_paste(self._read_paste(fd))
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
                # 这个字符后面还紧跟着一整块？那它是粘贴的第一个字，不是打字。
                # 只在回车上做时序判断是不够的：粘一行末尾带换行的文本
                # （"hello\n"），第一个换行前面没有别的换行，回车那一刻缓冲区
                # 已经空了，于是"粘完自动发出去"。从第一个字符就开始认粘贴，
                # 才能把那个尾随换行连同正文一起收走。
                burst = self._read_burst(fd)
                if burst:
                    self._apply_paste(burst)

            self._refresh_matches()
            self._render()
