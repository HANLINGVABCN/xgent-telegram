"""CLI 全屏终端前端（prompt_toolkit）。

交互：单击代码块任意行原地展开/收起；按住拖选高亮，拖到上下边缘自动翻页，
松开自动复制到剪贴板（OSC 52 + 本机剪贴板命令）；滚轮滚动输出；点输出区退出
输入框，直接打字自动回到输入框。F2 可把鼠标让给终端原生选择。

折叠语义与 Telegram / 网页完全一致（spec）
----------------------------------------
折叠开时，每个 `*-x` 协议块经 render_folded_html 变成
``<blockquote expandable>块头 · N 行 <pre>前 50 行</pre> [已折叠 K 行]</blockquote>``，
MessageRenderer 把它画成：

    🔧 run · 55 行          ← 块头行
    │ l0 … │ l49           ← 代码条（最多 50 行）
    已折叠 5 行              ← 仅正文 > 50 行时存在，纯文字

TUI 把这一组识别成一个折叠块：**默认收起 = 只显示「▸ 块头」一行**；
Enter / 点击 → 原地展开成「▾ 块头 + 前 50 行 (+ 已折叠 K 行)」，再点收起。
「已折叠 K 行」只是文字，不可点。折叠关时没有块头行，一切按原文显示。

架构：只换绘制/输入半边
------------------------
`PtScreen` 与 `cli_render.TerminalScreen` 同接口，CliBot 照旧调
``self.screen.*``；relay / 落库在 CliBot 方法边界、拿原始文本，本模块不碰。

opt-in + 兜底：`tui_enabled()` 为假或 pt 起不来 → xgent_cli 走 legacy 行式渲染器。
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .cli_render import (
    MessageRenderer,
    Palette,
    content_width,
    terminal_size,
    _FOLD_HEADER_RE as _HEADER_RE,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# 折叠块头正则单一真源在 cli_render._FOLD_HEADER_RE，这里直接复用（原先两处
# 各自 re.compile 同一份字面，改一处漏一处就会失步）。
# 「已折叠 K 行」纯文字标签
_FOLDED_LABEL_RE = re.compile(r"^已折叠\s*\d+\s*行$")

# 折叠块目标：(message_id, block_index)。None = 不是折叠块。
ToggleTarget = Optional[Tuple[int, int]]

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# 收起态预览行数（与网页一致）；正文 ≤ 这么多行且无「已折叠」标签的块不折叠。
_PREVIEW_LINES = 3


def _strip_ansi(line: str) -> str:
    return _ANSI_RE.sub("", line)


def _is_code_bar(line: str) -> bool:
    """`MessageRenderer._render_pre` 画的代码条行（`│ …`）。"""
    return _strip_ansi(line).lstrip().startswith("│")


def _is_fold_header(line: str) -> bool:
    plain = _strip_ansi(line).strip()
    return bool(plain) and not plain.startswith("│") and bool(_HEADER_RE.search(plain))


def _is_folded_label(line: str) -> bool:
    return bool(_FOLDED_LABEL_RE.match(_strip_ansi(line).strip()))


def _match_header(lines: Sequence[str], i: int) -> Optional[Tuple[str, int]]:
    """第 i 行是折叠块头就返回 (块头, 下一行下标)。块头由 MessageRenderer 保证不折行。"""
    return (lines[i], i + 1) if _is_fold_header(lines[i]) else None


@dataclass
class Block:
    """一条消息里的一段。

    collapsible=False：散文，``lines`` 原样显示。
    collapsible=True：折叠块，``header`` 是块头行，``lines`` 是正文（代码条 +
    可选的「已折叠 K 行」标签）。收起只显示块头；展开显示块头 + lines。
    """

    lines: List[str]
    collapsible: bool
    expanded: bool = False
    header: str = ""

    @property
    def code_lines(self) -> List[str]:
        return [ln for ln in self.lines if not _is_folded_label(ln)]

    @property
    def foldable(self) -> bool:
        """协议块但正文不超过 3 行且无标签：不折叠，整块直接显示，无箭头。"""
        return self.collapsible and (
            len(self.code_lines) > _PREVIEW_LINES or len(self.code_lines) != len(self.lines))


@dataclass
class TuiMessage:
    message_id: Optional[int]
    blocks: List[Block]
    leading_blank: bool = True
    key: int = 0          # 模型内部唯一键：无 message_id 的消息（/getchat 历史）也能被选中/展开
    committed: bool = False  # 已写进终端滚动区（之后只能在 Ctrl+T 记录页里展开）


def segment_lines(lines: Sequence[str]) -> List[Block]:
    """把渲染好的 ANSI 行切成 [散文 | 折叠块] 序列。

    折叠块 = 块头行 +（跳过空行）≥1 行代码条 +（跳过空行）可选「已折叠 K 行」。
    没有块头的代码条（普通 markdown 代码、折叠关）一律当散文原样显示。
    """
    blocks: List[Block] = []
    prose: List[str] = []

    def _flush() -> None:
        if prose:
            blocks.append(Block(list(prose), collapsible=False))
            prose.clear()

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        hdr = _match_header(lines, i)
        if hdr is not None:
            line, j = hdr
            while j < n and not _strip_ansi(lines[j]).strip():
                j += 1
            if j < n and _is_code_bar(lines[j]):
                body: List[str] = []
                while j < n and _is_code_bar(lines[j]):
                    body.append(lines[j])
                    j += 1
                k = j
                while k < n and not _strip_ansi(lines[k]).strip():
                    k += 1
                if k < n and _is_folded_label(lines[k]):
                    body.append(lines[k])
                    j = k + 1
                _flush()
                blocks.append(Block(body, collapsible=True, header=line))
                i = j
                continue
            line = lines[i]
        prose.append(line)
        i += 1
    _flush()
    return blocks


class MessageModel:
    """全量消息 + 每个折叠块的展开态（以 message_id 为键，历史消息也能原地改）。"""

    def __init__(self, palette: Optional[Palette] = None) -> None:
        self.palette = palette or Palette(False)
        self.messages: List[TuiMessage] = []
        self._index: Dict[int, TuiMessage] = {}   # message_id → 消息
        self._keyed: Dict[int, TuiMessage] = {}   # 内部 key → 消息
        self._next_anon = -1
        self.revision = 0  # 每次变更自增，渲染层据此复用缓存

    def _touch(self) -> None:
        self.revision += 1

    def upsert(self, message_id: Optional[int], lines: Sequence[str],
               leading_blank: bool = True) -> None:
        """新增或原地替换；替换时按「第几个折叠块」保留展开态（流式编辑不把
        用户展开的块收回去）。message_id 为 None 一律追加。"""
        blocks = segment_lines(lines)
        old = self._index.get(message_id) if message_id is not None else None
        if old is not None:
            old_folds = [b for b in old.blocks if b.foldable]
            new_folds = [b for b in blocks if b.foldable]
            for ob, nb in zip(old_folds, new_folds):
                nb.expanded = ob.expanded
            old.blocks = blocks
            old.leading_blank = leading_blank
            if old.committed:
                # 已写进滚动区的消息又被编辑（少见：旧菜单被点）：滚动区改不了，
                # 挪到末尾重新进实时区，稍后作为新的一份写出。
                old.committed = False
                try:
                    self.messages.remove(old)
                except ValueError:
                    pass
                self.messages.append(old)
        else:
            if message_id is not None:
                key = message_id
            else:
                key, self._next_anon = self._next_anon, self._next_anon - 1
            msg = TuiMessage(message_id, blocks, leading_blank, key)
            self.messages.append(msg)
            self._keyed[key] = msg
            if message_id is not None:
                self._index[message_id] = msg
        self._touch()

    def has(self, message_id: int) -> bool:
        return message_id in self._index

    def remove(self, message_id: int) -> bool:
        msg = self._index.pop(message_id, None)
        if msg is None:
            return False
        self._keyed.pop(msg.key, None)
        try:
            self.messages.remove(msg)
        except ValueError:
            pass
        self._touch()
        return True

    def _block(self, target: ToggleTarget) -> Optional[Block]:
        if target is None:
            return None
        msg = self._keyed.get(target[0])
        if msg is None or not (0 <= target[1] < len(msg.blocks)):
            return None
        blk = msg.blocks[target[1]]
        return blk if blk.foldable else None

    def toggle(self, message_id: int, block_idx: int) -> bool:
        blk = self._block((message_id, block_idx))
        if blk is None:
            return False
        blk.expanded = not blk.expanded
        self._touch()
        return True

    def is_expanded(self, target: ToggleTarget) -> bool:
        blk = self._block(target)
        return bool(blk and blk.expanded)

    def set_all_expanded(self, expanded: bool) -> None:
        for msg in self.messages:
            for blk in msg.blocks:
                if blk.foldable:
                    blk.expanded = expanded
        self._touch()

    def foldable_targets(self) -> List[Tuple[int, int]]:
        """按显示顺序列出全部折叠块（只含有 message_id 的消息）。"""
        out: List[Tuple[int, int]] = []
        for msg in self.messages:
            for bi, blk in enumerate(msg.blocks):
                if blk.foldable:
                    out.append((msg.key, bi))
        return out

    def message_of(self, target: ToggleTarget) -> Optional[TuiMessage]:
        return self._keyed.get(target[0]) if target else None

    def counts(self) -> Tuple[int, int]:
        """(折叠块总数, 已展开数)，给状态栏用。"""
        total = opened = 0
        for msg in self.messages:
            for blk in msg.blocks:
                if blk.foldable:
                    total += 1
                    opened += blk.expanded
        return total, opened

    def _header_row(self, blk: Block, selected: bool) -> str:
        """块头特殊显示：箭头 + 反色协议名（file-x 路径）+ 灰色「· N 行」。"""
        pal = self.palette
        plain = _strip_ansi(blk.header)
        indent = plain[:len(plain) - len(plain.lstrip(" "))]
        text = plain.strip()
        name, sep, rest = text.rpartition(" · ")
        if not sep:
            name, rest = text, ""
        arrow = ("▾" if blk.expanded else "▸") if blk.foldable else "■"
        lead = indent[:-2] + pal.paint("❯ ", pal.accent, pal.bold) if selected else indent
        tag = pal.paint(f" {name} ", pal.bold, "\x1b[48;5;60m", "\x1b[38;5;231m") if pal.enabled else f"[{name}]"
        tail = pal.paint(f" · {rest}", pal.muted) if rest else ""
        mark = pal.paint(arrow, pal.accent, pal.bold) if selected else pal.paint(arrow, pal.accent)
        return f"{lead}{mark} {tag}{tail}"

    def _label_row(self, blk: Block, ln: str) -> str:
        pal = self.palette
        indent = _strip_ansi(blk.header)[:2]
        return indent + pal.paint(f"┄┄ {_strip_ansi(ln).strip()} ┄┄", pal.muted, pal.italic)

    @staticmethod
    def _hidden_count(blk: Block) -> int:
        m = re.search(r"·\s*(\d+)\s*行$", _strip_ansi(blk.header).strip())
        total = int(m.group(1)) if m else len(blk.code_lines)
        return max(0, total - _PREVIEW_LINES)

    def _message_rows(self, msg: TuiMessage, selected: ToggleTarget,
                      hint: Optional[str]) -> List[Tuple[str, ToggleTarget]]:
        pal = self.palette
        rows: List[Tuple[str, ToggleTarget]] = []
        for bi, blk in enumerate(msg.blocks):
            if not blk.collapsible:
                rows.extend((ln, None) for ln in blk.lines)
                continue
            target: ToggleTarget = (msg.key, bi) if blk.foldable else None
            rows.append((self._header_row(blk, target is not None and target == selected), target))
            if blk.foldable and not blk.expanded:
                rows.extend((ln, target) for ln in blk.code_lines[:_PREVIEW_LINES])
                if hint:
                    indent = _strip_ansi(blk.header)[:2]
                    rows.append((indent + pal.paint(
                        f"  … 还有 {self._hidden_count(blk)} 行 · {hint}", pal.muted, pal.italic), None))
                continue
            for ln in blk.lines:
                if _is_folded_label(ln):
                    rows.append((self._label_row(blk, ln), None))
                else:
                    rows.append((ln, target))
        return rows

    def render_rows(self, selected: ToggleTarget = None,
                    messages: Optional[Sequence[TuiMessage]] = None,
                    hint: Optional[str] = None) -> List[Tuple[str, ToggleTarget]]:
        """摊平成 (行文本, 折叠目标)。

        可折叠块：收起 = 块头 + 前 3 行预览（hint 给了就再加一行「… 还有 N 行 · hint」）；
        展开 = 块头 + 前 50 行 + 「已折叠 K 行」。≤3 行的块整块显示、不带目标。
        「已折叠 K 行」只是文字，不带目标。
        """
        msgs = self.messages if messages is None else list(messages)
        rows: List[Tuple[str, ToggleTarget]] = []
        for mi, msg in enumerate(msgs):
            if mi > 0 and msg.leading_blank:
                rows.append(("", None))
            rows.extend(self._message_rows(msg, selected, hint))
        return rows

    def commit_rows(self, msg: TuiMessage, first: bool = False) -> List[str]:
        """写进终端滚动区的那几行（收起的块带「… 还有 N 行 · Ctrl+T 展开查看」）。"""
        rows = [t for t, _ in self._message_rows(msg, None, "Ctrl+T 展开查看")]
        return ([""] if (msg.leading_blank and not first) else []) + rows

    def live_messages(self) -> List[TuiMessage]:
        return [m for m in self.messages if not m.committed]


class PtScreen:
    """与 `cli_render.TerminalScreen` 同接口，但写进 `MessageModel`。"""

    def __init__(self, palette: Optional[Palette] = None,
                 width: Optional[int] = None) -> None:
        self.palette = palette if palette is not None else Palette(True)
        self._forced_width = width
        self.model = MessageModel(self.palette)
        # 挂上 App 后指向重画回调；未挂时空操作，便于单测。
        self.on_change: Callable[[], None] = lambda: None

    @property
    def width(self) -> int:
        if self._forced_width:
            return self._forced_width
        # 右侧留 1 列给滚动条，免得每行都被软折一次。
        return max(20, terminal_size()[0] - 1)

    @property
    def height(self) -> int:
        return terminal_size()[1]

    def renderer(self) -> MessageRenderer:
        return MessageRenderer(self.palette, content_width(self.width))

    def print_block(self, lines: Sequence[str], message_id: Optional[int] = None,
                    leading_blank: bool = True) -> None:
        self.model.upsert(message_id, list(lines), leading_blank)
        self.on_change()

    def update_block(self, lines: Sequence[str], message_id: int) -> bool:
        if not self.model.has(message_id):
            return False
        self.model.upsert(message_id, list(lines))
        self.on_change()
        return True

    def print_plain(self, text: str = "") -> None:
        self.model.upsert(None, [text], leading_blank=False)
        self.on_change()

    def notice(self, text: str, level: str = "info") -> None:
        pal = self.palette
        marker, style = {
            "info": ("ℹ", pal.muted),
            "ok": ("✓", pal.ok),
            "warn": ("!", pal.warn),
            "err": ("✗", pal.err),
        }.get(level, ("ℹ", pal.muted))
        self.print_plain(f"{pal.paint(marker, style)} {text}")

    def invalidate(self) -> None:
        self.on_change()


class TuiUnavailable(RuntimeError):
    """pt 不可用或 App 启动失败。调用方据此回退 legacy 行式渲染器。"""


@dataclass
class TuiHooks:
    """xgent_cli 注入的回调：路由/一轮处理仍归 xgent_cli，TUI 只管画屏与收键。"""

    dispatch: Callable[[str], Any]           # async (text) -> bool（True 表示退出）
    banner: Callable[[], None] = lambda: None
    prompt_text: Callable[[], str] = lambda: "❯ "
    command_names: Callable[[], Sequence[str]] = lambda: ()
    describe_command: Callable[[str], str] = lambda _n: ""
    turn_active: Callable[[], bool] = lambda: False
    request_stop: Callable[[], bool] = lambda: False
    remember_history: Callable[[str], None] = lambda _line: None
    history_file: Optional[str] = None
    menu_message_id: Callable[[], Optional[int]] = lambda: None   # 当前菜单留在实时区原地更新


def _env_truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _pt_available() -> bool:
    try:
        import prompt_toolkit  # noqa: F401
        return True
    except Exception:
        return False


def tui_enabled() -> bool:
    """该不该启用全屏 TUI：显式开关 + 没被 NO_TUI 否决 + 双向 TTY + pt 可用。"""
    if not _env_truthy(os.environ.get("XGENT_CLI_TUI")):
        return False
    if os.environ.get("XGENT_CLI_NO_TUI"):
        return False
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
    except Exception:
        return False
    return _pt_available()


def slash_completions(text: str, names: Sequence[str],
                      describe: Callable[[str], str]) -> List[Tuple[str, str]]:
    """`/` 前缀补全：整行以 / 开头且还没空格时按前缀筛命令 → [(补全, 说明)]。"""
    if not text.startswith("/") or " " in text:
        return []
    prefix = text[1:].lower()
    return [("/" + name, describe(name)) for name in names
            if str(name).lower().startswith(prefix)]


def copy_to_clipboard(text: str, write_raw: Optional[Callable[[str], None]] = None) -> bool:
    """复制到系统剪贴板：OSC 52（SSH/WSL/多数现代终端都认）+ 本机剪贴板命令兜底。"""
    import base64
    import shutil
    import subprocess

    ok = False
    if write_raw is not None:
        try:
            b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
            write_raw(f"\x1b]52;c;{b64}\x07")
            ok = True
        except Exception:
            pass
    candidates = []
    if sys.platform.startswith("win") or shutil.which("clip.exe"):
        candidates.append((["clip.exe"], "utf-16le"))
    candidates += [(["pbcopy"], "utf-8"), (["wl-copy"], "utf-8"),
                   (["xclip", "-selection", "clipboard"], "utf-8"),
                   (["xsel", "--clipboard", "--input"], "utf-8")]
    for cmd, enc in candidates:
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, input=text.encode(enc), timeout=3, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    return ok


def block_text(blk: "Block", whole: bool = True) -> str:
    """折叠块可复制的纯文本：块头 + 代码，去掉竖条与缩进。"""
    out = [_strip_ansi(blk.header).strip()] if whole else []
    for ln in blk.code_lines:
        plain = _strip_ansi(ln)
        idx = plain.find("│")
        out.append(plain[idx + 2:] if idx >= 0 else plain.strip())
    return "\n".join(out)


def hint_text(mode: str) -> str:
    """底栏按上下文给出「此刻能按什么」。纯函数便于单测。"""
    return {
        "browse": "Tab/Shift+Tab 换块 · Enter/空格 展开收起 · y 复制代码 · Y 复制整条消息 · a/z 全展开/全收起 · Esc 取消选中",
        "busy": "生成中 · Ctrl+C 中断 · 滚轮/PgUp/PgDn 滚动 · 可先打下一句",
        "exit": "再按一次 Ctrl+C 退出",
        "input": "直接打字或 ↑↓ 回到输入框 · 点击代码块展开/收起 · 拖选自动复制 · 滚轮/PgUp/PgDn 翻页"
                 " · Tab 选块 · F2 改用终端原生选择",
        "typing": "Enter 发送 · Alt+Enter 换行 · ↑↓ 输入历史 · / 命令补全 · 滚轮/PgUp/PgDn 翻页"
                  " · 点击代码块展开 · 拖选自动复制",
    }.get(mode, "")


async def run_tui(screen: "PtScreen", hooks: TuiHooks) -> None:
    """跑全屏 App。pt 导入或建 App 失败抛 `TuiUnavailable`（调用方回退 legacy）。"""
    try:
        import asyncio as _asyncio

        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.filters import Condition, has_focus
        from prompt_toolkit.formatted_text import ANSI, to_formatted_text
        from prompt_toolkit.formatted_text.utils import fragment_list_width
        from prompt_toolkit.history import FileHistory, InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import (
            Float, FloatContainer, HSplit, Window)
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.dimension import D
        from prompt_toolkit.layout.margins import ScrollbarMargin
        from prompt_toolkit.layout.menus import CompletionsMenu
        from prompt_toolkit.mouse_events import MouseButton, MouseEventType
        from prompt_toolkit.styles import Style
    except Exception as exc:  # pt 不在/装坏 → 回退 legacy
        raise TuiUnavailable(str(exc)) from exc

    model = screen.model
    app_ref: List[Any] = []
    state: Dict[str, Any] = {
        "sel": None,          # 浏览态选中的折叠块；None = 不在浏览态
        "busy": False,        # 本地提交的一轮进行中
        "exit_armed": 0.0,    # 空闲 Ctrl+C 第一次按下的时间
        "mouse": True,        # 接管鼠标：点击展开、滚轮滚动、自制拖选复制；F2 让给终端原生选择
        "flash": "",          # 底栏临时提示
        "flash_until": 0.0,
    }

    def _invalidate() -> None:
        if app_ref:
            app_ref[0].invalidate()

    def _flash(text: str, seconds: float = 2.5) -> None:
        state["flash"] = text
        state["flash_until"] = time.monotonic() + seconds
        _invalidate()

    def _busy() -> bool:
        try:
            return state["busy"] or bool(hooks.turn_active())
        except Exception:
            return state["busy"]

    def _copy(text: str, what: str) -> None:
        try:
            out = app_ref[0].output
            ok = copy_to_clipboard(text, lambda raw: (out.write_raw(raw), out.flush()))
        except Exception:
            ok = copy_to_clipboard(text)
        _flash(f"已复制{what}（{len(text)} 字）" if ok else "复制失败：终端不支持 OSC52 且没有剪贴板命令")

    # -- 输出区：自管滚动的 Window ----------------------------------------
    class _ScrollWindow(Window):
        """滚动完全自管。pt 默认滚动要让「光标」可见，而输出区没有光标（恒为
        第 0 行），会把视口一直拽回顶部。这里只把 vertical_scroll（行号）夹进
        [0, max_top]，follow=True 时恒贴底。"""

        follow = True
        max_top = 0

        def _scroll(self, ui_content, width, height):
            self.horizontal_scroll = 0
            self.vertical_scroll_2 = 0
            used, top = 0, ui_content.line_count
            while top > 0:
                h = ui_content.get_height_for_line(top - 1, width, self.get_line_prefix)
                if used + h > height:
                    break
                used += h
                top -= 1
            self.max_top = top
            target = state.pop("reveal", None)
            if target is not None:
                # 展开/选块后让块头落在视口上 1/3 处（新高度这一帧才知道）
                idx = _row_of(target)
                cur = top if self.follow else self.vertical_scroll
                if idx >= 0 and not (cur <= idx < cur + height - 1):
                    self.follow = False
                    self.vertical_scroll = max(0, idx - height // 3)
            if self.follow:
                self.vertical_scroll = top
            else:
                self.vertical_scroll = max(0, min(self.vertical_scroll, top))
                if self.vertical_scroll >= top:
                    self.follow = True

        def scroll_by(self, delta: int) -> None:
            cur = self.max_top if self.follow else self.vertical_scroll
            new = max(0, cur + delta)
            if new >= self.max_top:
                self.follow = True
                self.vertical_scroll = self.max_top
            else:
                self.follow = False
                self.vertical_scroll = new

        def scroll_to(self, line: int) -> None:
            self.scroll_by(line - (self.max_top if self.follow else self.vertical_scroll))

        def _scroll_up(self) -> None:  # pt 自带滚轮路径也走我们的逻辑
            self.scroll_by(-3)

        def _scroll_down(self) -> None:
            self.scroll_by(3)

    # -- 输出区内容（带缓存：只在模型/选中变化时重建） --------------------
    ansi_cache: Dict[str, list] = {}
    frag_cache: Dict[str, Any] = {"key": None, "frags": [], "rows": []}

    def _ansi(text: str) -> list:
        frags = ansi_cache.get(text)
        if frags is None:
            frags = to_formatted_text(ANSI(text)) if text else []
            if len(ansi_cache) > 50000:
                ansi_cache.clear()
            ansi_cache[text] = frags
        return frags

    # 拖选：anchor/end 是 (内容行号, 行内字符下标)；pt 把点击位置换算成的正是这个坐标。
    drag: Dict[str, Any] = {"anchor": None, "end": None, "active": False,
                            "moved": False, "edge": 0}

    def _sel_range():
        a, b = drag["anchor"], drag["end"]
        if a is None or b is None or a == b:
            return None
        return (a, b) if a <= b else (b, a)

    def _visible_top_height():
        info = output_window.render_info
        top = output_window.max_top if output_window.follow else output_window.vertical_scroll
        return top, (info.window_height if info else 10)

    def _selected_text() -> str:
        rng = _sel_range()
        if rng is None:
            return ""
        (r0, c0), (r1, c1) = rng
        rows = frag_cache["rows"]
        out: List[str] = []
        for r in range(r0, min(r1, len(rows) - 1) + 1):
            plain = _strip_ansi(rows[r][0])
            a = c0 if r == r0 else 0
            b = c1 + 1 if r == r1 else len(plain)
            if plain.lstrip().startswith("│"):
                # 代码行：界面装饰（缩进 + │ 竖条）不进剪贴板，只复制代码本身
                a = max(a, plain.index("│") + 2)
            out.append(plain[a:b].rstrip())
        return "\n".join(out)

    def _make_handler(target: ToggleTarget):
        def _mouse(ev):
            et = ev.event_type
            pos = (ev.position.y, ev.position.x)
            if et == MouseEventType.SCROLL_UP:
                output_window.scroll_by(-3)
            elif et == MouseEventType.SCROLL_DOWN:
                output_window.scroll_by(3)
            elif et == MouseEventType.MOUSE_DOWN and ev.button == MouseButton.LEFT:
                drag.update(anchor=pos, end=pos, active=True, moved=False, edge=0)
                if app_ref:  # 点输出区 → 退出输入框（打字会自动回来）
                    app_ref[0].layout.focus(output_window)
            elif et == MouseEventType.MOUSE_MOVE and drag["active"]:
                drag["end"] = pos
                drag["moved"] = drag["moved"] or pos != drag["anchor"]
                top, height = _visible_top_height()
                drag["edge"] = -1 if pos[0] <= top else (1 if pos[0] >= top + height - 1 else 0)
            elif et == MouseEventType.MOUSE_UP:
                was_drag = drag["active"] and drag["moved"] and _sel_range() is not None
                drag.update(active=False, edge=0)
                if was_drag:
                    text = _selected_text()
                    if text:
                        _copy(text, "选中内容")
                else:
                    drag.update(anchor=None, end=None)
                    if target is not None:          # 单击代码块任意行 → 原地展开/收起
                        model.toggle(*target)
                        if state["sel"] is not None:
                            state["sel"] = target
            else:
                return NotImplemented
            _invalidate()
            return None
        return _mouse

    def _edge_handler(direction: int):
        """拖选拖出输出区（到顶栏/输入框分隔线）：继续朝那个方向自动翻页。"""
        def _mouse(ev):
            if ev.event_type == MouseEventType.MOUSE_MOVE and drag["active"]:
                drag["edge"] = direction
                return None
            if ev.event_type == MouseEventType.MOUSE_UP and drag["active"]:
                drag.update(active=False, edge=0)
                text = _selected_text()
                if text:
                    _copy(text, "选中内容")
                _invalidate()
                return None
            return NotImplemented
        return _mouse

    plain_handler = _make_handler(None)
    handler_cache: Dict[Any, Any] = {}

    def _highlight(frags: list, a: int, b: int) -> list:
        """把一行 fragments 里 [a, b) 字符区间加反色（选区高亮）。"""
        out: list = []
        i = 0
        for style, text, *rest in frags:
            n = len(text)
            s0, s1 = max(a, i), min(b, i + n)
            if s0 >= s1:
                out.append((style, text, *rest))
            else:
                if s0 > i:
                    out.append((style, text[:s0 - i], *rest))
                out.append((style + " reverse", text[s0 - i:s1 - i], *rest))
                if s1 < i + n:
                    out.append((style, text[s1 - i:], *rest))
            i += n
        return out

    def _rows():
        busy = _busy()
        frame = _SPINNER[int(time.monotonic() * 10) % len(_SPINNER)] if busy else ""
        rng = _sel_range()
        key = (model.revision, state["sel"], frame, rng)
        if frag_cache["key"] != key:
            rows = model.render_rows(state["sel"])
            if busy:
                pal = model.palette
                rows = rows + [("", None), ("  " + pal.paint(f"{frame} 输出中…", pal.accent), None)]
            out: list = []
            for r, (text, target) in enumerate(rows):
                handler = plain_handler if target is None else handler_cache.setdefault(
                    target, _make_handler(target))
                line = [(style, t, handler) for style, t, *_ in _ansi(text)]
                if rng is not None and rng[0][0] <= r <= rng[1][0]:
                    plain_len = len(_strip_ansi(text))
                    a = rng[0][1] if r == rng[0][0] else 0
                    b = rng[1][1] + 1 if r == rng[1][0] else max(plain_len, 1)
                    if not line:
                        line = [("", " ", handler)]
                    line = _highlight(line, a, b)
                out.extend(line)
                out.append(("", "\n", handler))
            if out:
                out.pop()
            frag_cache.update(key=key, frags=out, rows=rows)
        return frag_cache

    output_window = _ScrollWindow(
        content=FormattedTextControl(lambda: _rows()["frags"], focusable=True,
                                     show_cursor=False),
        wrap_lines=True,
        right_margins=[ScrollbarMargin(display_arrows=False)],
        # 内容少时只占内容高度（输入框紧跟在内容下面），多余高度给最底下的 filler。
        dont_extend_height=True,
    )

    # -- 选块（浏览态） ----------------------------------------------------
    def _row_of(target: ToggleTarget) -> int:
        for idx, (_t, tgt) in enumerate(_rows()["rows"]):
            if tgt == target:
                return idx
        return -1

    def _reveal(target: ToggleTarget) -> None:
        # 下一帧（_ScrollWindow._scroll）按新内容高度定位
        if target is not None:
            state["reveal"] = target
            _invalidate()

    def _select(i: int) -> bool:
        targets = model.foldable_targets()
        if not targets:
            state["sel"] = None
            return False
        state["sel"] = targets[max(0, min(i, len(targets) - 1))]
        _reveal(state["sel"])
        return True

    def _sel_index() -> int:
        try:
            return model.foldable_targets().index(state["sel"])
        except ValueError:
            return -1

    # -- 输入区 ------------------------------------------------------------
    class _SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            try:
                names = list(hooks.command_names() or ())
            except Exception:
                names = []
            for full, meta in slash_completions(text, names, hooks.describe_command):
                yield Completion(full, start_position=-len(text),
                                 display=full, display_meta=meta or "")

    try:
        history = (FileHistory(hooks.history_file)
                   if hooks.history_file else InMemoryHistory())
    except Exception:
        history = InMemoryHistory()

    input_buffer = Buffer(history=history, completer=_SlashCompleter(),
                          complete_while_typing=True, multiline=True)

    def _prompt_frags() -> list:
        try:
            return to_formatted_text(ANSI(hooks.prompt_text()))
        except Exception:
            return [("class:prompt", "❯ ")]

    def _prefix(line_number, wrap_count):
        frags = _prompt_frags()
        if line_number == 0 and not wrap_count:
            return frags
        return [("", " " * fragment_list_width(frags))]

    input_window = Window(
        content=BufferControl(buffer=input_buffer),
        height=D(min=1, max=8), get_line_prefix=_prefix, wrap_lines=True,
        dont_extend_height=True)

    # -- 顶栏 / 分隔 / 底栏 ---------------------------------------------------
    def _columns() -> int:
        try:
            return app_ref[0].output.get_size().columns
        except Exception:
            return 80

    def _status_bar():
        left = [("class:title.name", " ◆ xgent "), ("class:title", " ")]
        if _busy():
            frame = _SPINNER[int(time.monotonic() * 10) % len(_SPINNER)]
            left.append(("class:title.busy", f"{frame} 生成中"))
        else:
            left.append(("class:title.ok", "● 就绪"))
        right: list = []
        total, opened = model.counts()
        if total:
            right.append(("class:title", f"折叠块 {opened}/{total} 展开  "))
        if not output_window.follow:
            below = max(0, output_window.max_top - output_window.vertical_scroll)
            right.append(("class:title.warn", f"↓ 下方还有 {below} 行 · Ctrl+End 回底 "))
        else:
            right.append(("class:title", "跟随最新 "))
        if not state["mouse"]:
            right.append(("class:title.warn", " 终端原生选择(F2 切回) "))
        pad = max(1, _columns() - fragment_list_width(left) - fragment_list_width(right))
        return left + [("class:title", " " * pad)] + right

    def _mode() -> str:
        if state["exit_armed"] and time.monotonic() - state["exit_armed"] < 2.0:
            return "exit"
        if state["sel"] is not None:
            return "browse"
        if app_ref and app_ref[0].layout.has_focus(input_window):
            return "typing"
        if _busy():
            return "busy"
        return "input"

    def _hint_bar():
        if state["flash"] and time.monotonic() < state["flash_until"]:
            return [("class:hint.flash", " " + state["flash"])]
        return [("class:hint", " " + hint_text(_mode()))]

    def _input_rule():
        typing = bool(app_ref) and app_ref[0].layout.has_focus(input_window)
        label = " 输入中 · Esc 回到浏览 " if typing else " 输入（直接打字即可） "
        rest = max(0, _columns() - fragment_list_width([("", label)]) - 2)
        return [("class:rule", "──"), ("class:rule.label", label), ("class:rule", "─" * rest)]

    up_edge, down_edge = _edge_handler(-1), _edge_handler(1)
    status_window = Window(FormattedTextControl(
        lambda: [(st, t, up_edge) for st, t, *_ in _status_bar()]), height=1, style="class:title")
    rule_window = Window(FormattedTextControl(
        lambda: [(st, t, down_edge) for st, t, *_ in _input_rule()]), height=1)
    hint_window = Window(FormattedTextControl(_hint_bar), height=1, style="class:hint")

    # -- 键位 --------------------------------------------------------------
    # 两种焦点：
    #   浏览（默认，焦点在输出区）：↑↓/滚轮滚动、PgUp/PgDn 翻页、Tab 选折叠块、
    #     Enter 展开/收起、y/Y 复制；直接打字 → 自动进入输入框。
    #   输入（光标在输入框）：↑↓ 翻输入历史/移动光标、Enter 发送、Esc 回浏览。
    # 这样滚轮（终端把它转成 ↑↓）只滚输出，只有光标进了输入框才动输入历史。
    kb = KeyBindings()
    in_input = has_focus(input_window)
    in_out = has_focus(output_window)
    has_sel = Condition(lambda: state["sel"] is not None)

    def _to_input(event) -> None:
        state["sel"] = None
        event.app.layout.focus(input_window)
        _invalidate()

    def _to_output(event) -> None:
        event.app.layout.focus(output_window)
        _invalidate()

    @kb.add("enter", filter=in_input)
    def _submit(event):
        buf = input_buffer
        if buf.complete_state and buf.complete_state.current_completion:
            buf.apply_completion(buf.complete_state.current_completion)
            return
        text = buf.text
        if not text.strip():
            buf.reset()
            return
        if _busy():
            _flash("还在生成中：草稿已保留，Ctrl+C 可中断当前回答")
            return
        buf.append_to_history()
        try:
            hooks.remember_history(text)
        except Exception:
            pass
        buf.reset()
        state["busy"] = True
        output_window.follow = True  # 发出新消息 → 回到底部跟随

        async def _go():
            should_exit = False
            try:
                should_exit = bool(await hooks.dispatch(text))
            except Exception as exc:  # 一轮内部异常不能掀翻 TUI
                try:
                    screen.notice(f"处理出错：{exc}", "err")
                except Exception:
                    pass
            finally:
                state["busy"] = False
                _invalidate()
            if should_exit:
                event.app.exit()

        event.app.create_background_task(_go())

    @kb.add("escape", "enter", filter=in_input)
    @kb.add("c-j", filter=in_input)
    def _newline(event):
        input_buffer.insert_text("\n")

    @kb.add("tab", filter=in_input)
    def _tab(event):
        buf = input_buffer
        if buf.complete_state:
            buf.complete_next()
        elif buf.text.startswith("/"):
            buf.start_completion(select_first=True)
        else:
            buf.insert_text("    ")

    @kb.add("s-tab", filter=in_input)
    def _stab(event):
        if input_buffer.complete_state:
            input_buffer.complete_previous()

    @kb.add("escape", filter=in_input)
    def _esc_input(event):
        if input_buffer.complete_state:
            input_buffer.cancel_completion()
        else:
            _to_output(event)   # 草稿保留

    @kb.add("c-p", filter=in_input)
    def _hist_prev(event):
        input_buffer.history_backward()

    @kb.add("c-n", filter=in_input)
    def _hist_next(event):
        input_buffer.history_forward()

    @kb.add("c-c")
    def _interrupt(event):
        if _busy():
            try:
                hooks.request_stop()
            except Exception:
                pass
            _flash("已请求中断当前回答…")
            return
        if state["sel"] is not None:
            state["sel"] = None
            _invalidate()
            return
        if input_buffer.text:
            input_buffer.reset()
            return
        now = time.monotonic()
        if state["exit_armed"] and now - state["exit_armed"] < 2.0:
            event.app.exit()
            return
        state["exit_armed"] = now
        _invalidate()

        async def _disarm():
            await _asyncio.sleep(2.1)
            _invalidate()
        event.app.create_background_task(_disarm())

    @kb.add("c-d")
    def _eof(event):
        if not input_buffer.text:
            event.app.exit()
        elif event.app.layout.has_focus(input_window):
            input_buffer.delete()

    @kb.add("c-o")
    def _toggle_latest(event):
        targets = model.foldable_targets()
        if not targets:
            _flash("当前没有可折叠的代码块")
            return
        tgt = targets[-1]
        model.toggle(*tgt)
        _reveal(tgt)
        _invalidate()

    @kb.add("c-l")
    def _redraw(event):
        event.app.renderer.clear()

    @kb.add("f2")
    def _mouse_toggle(event):
        state["mouse"] = not state["mouse"]
        _flash("已接管鼠标：点击展开、滚轮滚动、拖选自动复制（F2 切到终端原生选择）"
               if state["mouse"] else "已切到终端原生选择：鼠标拖选由终端处理，滚轮变 ↑↓（F2 切回）")

    # 滚动
    def _page() -> int:
        info = output_window.render_info
        return max(1, (info.window_height if info else 10) - 2)

    @kb.add("pageup")
    def _pgup(event):
        output_window.scroll_by(-_page())
        _invalidate()

    @kb.add("pagedown")
    def _pgdn(event):
        output_window.scroll_by(_page())
        _invalidate()

    # ↑↓ 只管输入框（历史 / 光标），从不翻页：焦点在输出区时也先回到输入框再处理。
    # 翻页交给滚轮 / PgUp / PgDn。
    @kb.add("up", filter=in_out)
    def _up_to_input(event):
        _to_input(event)
        input_buffer.auto_up()

    @kb.add("down", filter=in_out)
    def _down_to_input(event):
        _to_input(event)
        input_buffer.auto_down()

    @kb.add("home", filter=in_out)
    @kb.add("c-home")
    def _top(event):
        output_window.scroll_to(0)
        _invalidate()

    @kb.add("end", filter=in_out)
    @kb.add("c-end")
    def _bottom(event):
        output_window.follow = True
        _invalidate()

    # 折叠块：Tab/Shift+Tab 选块，Enter/空格 展开收起
    @kb.add("tab", filter=in_out)
    def _next(event):
        targets = model.foldable_targets()
        if not targets:
            _flash("当前没有可折叠的代码块")
            return
        i = _sel_index()
        _select(len(targets) - 1 if i < 0 else (i + 1) % len(targets))
        _invalidate()

    @kb.add("s-tab", filter=in_out)
    def _prev(event):
        targets = model.foldable_targets()
        if not targets:
            return
        i = _sel_index()
        _select(len(targets) - 1 if i <= 0 else i - 1)
        _invalidate()

    @kb.add("enter", filter=in_out)
    def _enter_out(event):
        if state["sel"] is not None:
            model.toggle(*state["sel"])
            _reveal(state["sel"])
            _invalidate()
        else:
            _to_input(event)

    @kb.add("space", filter=in_out & has_sel)
    def _space_toggle(event):
        model.toggle(*state["sel"])
        _reveal(state["sel"])
        _invalidate()

    @kb.add("escape", filter=in_out)
    def _esc_out(event):
        state["sel"] = None
        _invalidate()

    @kb.add("y", filter=in_out & has_sel)
    def _copy_block(event):
        blk = model._block(state["sel"])
        if blk is not None:
            _copy(block_text(blk, whole=False), "代码")

    @kb.add("Y", filter=in_out & has_sel)
    def _copy_message(event):
        msg = model.message_of(state["sel"])
        if msg is None:
            return
        parts: List[str] = []
        for blk in msg.blocks:
            if blk.collapsible:
                parts.append(block_text(blk))
            else:
                parts.extend(_strip_ansi(ln).strip() for ln in blk.lines)
        _copy("\n".join(parts).strip(), "整条消息")

    @kb.add("a", filter=in_out & has_sel)
    def _expand_all(event):
        model.set_all_expanded(True)
        _reveal(state["sel"])
        _invalidate()

    @kb.add("z", filter=in_out & has_sel)
    def _collapse_all(event):
        model.set_all_expanded(False)
        _reveal(state["sel"])
        _invalidate()

    # 浏览态直接打字 → 进入输入框并把这个字写进去
    from prompt_toolkit.keys import Keys

    @kb.add(Keys.Any, filter=in_out)
    def _type_to_input(event):
        data = event.data
        if not data or not data.isprintable():
            return
        _to_input(event)
        input_buffer.insert_text(data)

    # -- 布局 / App ---------------------------------------------------------
    body = FloatContainer(
        HSplit([
            status_window,
            output_window,
            rule_window,
            input_window,
            hint_window,
            Window(),          # filler：内容少时空白只在最底下（内容贴顶，像普通终端）
        ]),
        floats=[Float(xcursor=True, ycursor=True,
                      content=CompletionsMenu(max_height=10, scroll_offset=1))],
    )
    style = Style.from_dict({
        "title": "bg:#1e2230 #a9b1d6",
        "title.name": "bg:#7aa2f7 #1a1b26 bold",
        "title.ok": "bg:#1e2230 #9ece6a",
        "title.busy": "bg:#1e2230 #e0af68 bold",
        "title.warn": "bg:#1e2230 #ff9e64",
        "rule": "#3b4261",
        "rule.label": "#7aa2f7",
        "hint": "#7f849c",
        "hint.flash": "#e0af68 bold",
        "completion-menu": "bg:#24283b #c0caf5",
        "completion-menu.completion.current": "bg:#7aa2f7 #1a1b26",
        "completion-menu.meta.completion": "bg:#1f2335 #7f849c",
        "completion-menu.meta.completion.current": "bg:#3d59a1 #c0caf5",
        "scrollbar.background": "bg:#1e2230",
        "scrollbar.button": "bg:#565f89",
    })

    try:
        app = Application(
            layout=Layout(body, focused_element=input_window),   # 一进来就聚焦输入框
            key_bindings=kb, style=style, full_screen=True,
            mouse_support=Condition(lambda: state["mouse"]),
            min_redraw_interval=0.03,
        )
    except Exception as exc:
        raise TuiUnavailable(str(exc)) from exc
    app_ref.append(app)

    def _on_change() -> None:
        # 新内容只在 follow 时贴底（_ScrollWindow._scroll 处理）；上滚回看时不打扰。
        app.invalidate()

    async def _ticker():
        # 生成中 / 临时提示期间定时重画（转圈 + 提示过期），空闲不耗 CPU。
        while True:
            await _asyncio.sleep(0.1)
            if drag["active"] and drag["edge"]:
                output_window.scroll_by(drag["edge"])
                top, height = _visible_top_height()
                last = max(0, len(frag_cache["rows"]) - 1)
                row = max(0, top) if drag["edge"] < 0 else min(last, top + height - 1)
                drag["end"] = (row, 0 if drag["edge"] < 0 else 10 ** 6)
                app.invalidate()
            if _busy() or state["flash"] or state["exit_armed"]:
                if state["flash"] and time.monotonic() >= state["flash_until"]:
                    state["flash"] = ""
                if state["exit_armed"] and time.monotonic() - state["exit_armed"] >= 2.0:
                    state["exit_armed"] = 0.0
                app.invalidate()

    def _alt_scroll(on: bool) -> None:
        try:
            app.output.write_raw("\x1b[?1007h" if on else "\x1b[?1007l")
            app.output.flush()
        except Exception:
            pass

    def _pre_run():
        output_window.follow = True
        # 不开终端「备用滚动」：否则 F2 让出鼠标后滚轮会变成 ↑↓ 去翻输入历史
        _alt_scroll(False)
        app.layout.focus(input_window)
        app.create_background_task(_ticker())

    screen.on_change = _on_change
    try:
        hooks.banner()
    except Exception:
        pass
    try:
        await app.run_async(pre_run=_pre_run)
    finally:
        _alt_scroll(False)
        screen.on_change = lambda: None
