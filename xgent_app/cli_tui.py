"""CLI 全屏终端前端（prompt_toolkit）。

交互：单击代码块任意行原地展开/收起；按住拖选高亮，拖到上下边缘自动翻页，
松开自动复制到剪贴板（OSC 52 + 本机剪贴板命令）；滚轮滚动输出；点输出区退出
输入框，直接打字自动回到输入框。F2 切换终端原生选择；原生模式下 ↑↓/PgUp/PgDn 翻页，
支持 DECSET 1007 的终端也可用滚轮。选区只重画可见行，剪贴板命令不阻塞输入。

折叠与完整输出
--------------
Web/TUI 收到完整标准 HTML，默认收起大块，展开显示完整正文；Telegram 独立限长。
复制代码使用原始正文，不使用按终端宽度换行后的文字。
旧预览若携带 data-raw，可无执行地恢复；缺少原文的旧数据不伪造内容。
F3 打开当前/最近的命令输出存档，F5/F6 每次读取一页；不把超大日志读进内存。

架构：只换绘制/输入半边
------------------------
`PtScreen` 与 `cli_render.TerminalScreen` 同接口，CliBot 照旧调
``self.screen.*``；relay / 落库在 CliBot 方法边界、拿原始文本，本模块不碰。

opt-in + 兜底：`tui_enabled()` 为假或 pt 起不来 → xgent_cli 走 legacy 行式渲染器。
"""

from __future__ import annotations

import os
import copy
import html
import re
import sys
import time
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import groupby
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .cli_render import (
    MessageRenderer,
    Palette,
    content_width,
    terminal_size,
    wrap_line,
    split_control_buttons,
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


@lru_cache(maxsize=4096)
def _line_fragments(text: str) -> tuple:
    """Parse only requested lines, coalescing ANSI's per-character fragments.

    A bounded LRU avoids the old 50k-entry wholesale cache eviction. Keeping
    prompt_toolkit imports lazy preserves the legacy CLI fallback.
    """
    if not text:
        return ()
    if "\x1b" not in text:
        return (("", text),)
    from prompt_toolkit.formatted_text import ANSI, to_formatted_text
    return tuple((style, "".join(item[1] for item in items))
                 for style, items in groupby(to_formatted_text(ANSI(text)), key=lambda item: item[0]))


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

    _code_lines: List[str] = field(init=False, repr=False)
    _foldable: bool = field(init=False, repr=False)
    total_lines: int = field(init=False)

    def __post_init__(self) -> None:
        # Block 的正文在 upsert 时整体替换；展开/选择只改状态，不应重跑正则。
        self._code_lines = [line for line in self.lines if not _is_folded_label(line)]
        self.source_body = next((getattr(line, 'source_body', None) for line in self.lines
                                 if getattr(line, 'source_body', None) is not None), None)
        self._foldable = self.collapsible and (
            len(self._code_lines) > _PREVIEW_LINES or len(self._code_lines) != len(self.lines))
        match = re.search(r"·\s*(\d+)\s*行$", _strip_ansi(self.header).strip())
        self.total_lines = int(match.group(1)) if match else len(self._code_lines)

    @property
    def code_lines(self) -> List[str]:
        return self._code_lines

    @property
    def foldable(self) -> bool:
        return self._foldable


@dataclass
class TuiMessage:
    message_id: Optional[int]
    blocks: List[Block]
    leading_blank: bool = True
    key: int = 0          # 模型内部唯一键：无 message_id 的消息（/getchat 历史）也能被选中/展开
    committed: bool = False  # 兼容旧滚动区接口；全屏 TUI 保留全部消息。
    attention: bool = False
    output_path: Optional[str] = None
    rows_key: Any = field(default=None, repr=False, compare=False)
    rows_cache: List[Tuple[str, ToggleTarget]] = field(default_factory=list, repr=False, compare=False)
    fold_counts: Tuple[int, int] = field(default=(0, 0), repr=False, compare=False)


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

    def __init__(self, palette: Optional[Palette] = None, preview_lines: int = _PREVIEW_LINES) -> None:
        self.palette = palette or Palette(False)
        self.preview_lines = max(0, preview_lines)
        self.messages: List[TuiMessage] = []
        self._index: Dict[int, TuiMessage] = {}   # message_id → 消息
        self._keyed: Dict[int, TuiMessage] = {}   # 内部 key → 消息
        self._next_anon = -1
        self.revision = 0  # 每次变更自增，渲染层据此复用缓存
        self._counts_cache = (0, 0)
        self._targets_revision = -1
        self._targets_cache: List[Tuple[int, int]] = []

    def _touch(self) -> None:
        self.revision += 1

    def _changed_message(self, msg: TuiMessage) -> None:
        folds = [block for block in msg.blocks if block.foldable]
        counts = (len(folds), sum(block.expanded for block in folds))
        self._counts_cache = tuple(
            old + new - previous
            for old, new, previous in zip(self._counts_cache, counts, msg.fold_counts))
        msg.fold_counts = counts
        msg.rows_key = None
        msg.rows_cache = []

    def upsert(self, message_id: Optional[int], lines: Sequence[str],
               leading_blank: bool = True) -> None:
        """新增或原地替换；替换时按「第几个折叠块」保留展开态（流式编辑不把
        用户展开的块收回去）。message_id 为 None 一律追加。"""
        blocks = segment_lines(lines)
        attention = any(_strip_ansi(line).lstrip().startswith(("❌", "⚠")) for line in lines)
        output_path = next((getattr(line, 'output_path', None) for line in lines
                            if getattr(line, 'output_path', None)), None)
        if not output_path:
            # 旧结果只有可见路径行；仍需通过 command_outputs 白名单才能读取。
            for line in lines:
                match = re.match(r'^\s*(?:完整输出|输出存档（已截断）):\s*`?(.+?)`?\s*$', _strip_ansi(line))
                if match and match[1].strip('`').endswith('.txt'):
                    output_path = match[1].strip('`')
                    break
        old = self._index.get(message_id) if message_id is not None else None
        if old is not None:
            old_folds = [b for b in old.blocks if b.foldable]
            new_folds = [b for b in blocks if b.foldable]
            for ob, nb in zip(old_folds, new_folds):
                nb.expanded = ob.expanded
            if attention and not old.attention:
                for block in new_folds:
                    block.expanded = True
            old.attention = attention
            old.output_path = output_path
            old.blocks = blocks
            old.leading_blank = leading_blank
            self._changed_message(old)
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
            msg = TuiMessage(message_id, blocks, leading_blank, key, attention=attention, output_path=output_path)
            if attention:
                for block in blocks:
                    if block.foldable:
                        block.expanded = True
            self.messages.append(msg)
            self._changed_message(msg)
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
        self._counts_cache = tuple(a - b for a, b in zip(self._counts_cache, msg.fold_counts))
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
        self._changed_message(self._keyed[message_id])
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
            self._changed_message(msg)
        self._touch()

    def foldable_targets(self) -> List[Tuple[int, int]]:
        """按显示顺序列出全部折叠块（只含有 message_id 的消息）。"""
        if self._targets_revision == self.revision:
            return self._targets_cache
        out: List[Tuple[int, int]] = []
        for msg in self.messages:
            for bi, blk in enumerate(msg.blocks):
                if blk.foldable:
                    out.append((msg.key, bi))
        self._targets_revision = self.revision
        self._targets_cache = out
        return out

    def message_of(self, target: ToggleTarget) -> Optional[TuiMessage]:
        return self._keyed.get(target[0]) if target else None

    def counts(self) -> Tuple[int, int]:
        """(折叠块总数, 已展开数)，给状态栏用。"""
        return self._counts_cache

    def _header_row(self, blk: Block, selected: bool) -> str:
        """单行块头：箭头 + 协议名 + 次要行数；不用背景色标签。"""
        pal = self.palette
        plain = _strip_ansi(blk.header)
        indent = plain[:len(plain) - len(plain.lstrip(" "))]
        text = plain.strip()
        name, sep, rest = text.rpartition(" · ")
        if not sep:
            name, rest = text, ""
        arrow = ("▾" if blk.expanded else "▸") if blk.foldable else "■"
        lead = indent[:-2] + pal.paint("❯ ", pal.accent, pal.bold) if selected else indent
        tag = pal.paint(name, pal.accent, pal.bold if selected else "") if pal.enabled else f"[{name}]"
        tail = pal.paint(f" · {rest}", pal.muted) if rest else ""
        mark = pal.paint(arrow, pal.accent, pal.bold) if selected else pal.paint(arrow, pal.accent)
        return f"{lead}{mark} {tag}{tail}"

    def _label_row(self, blk: Block, ln: str) -> str:
        pal = self.palette
        indent = _strip_ansi(blk.header)[:2]
        return indent + pal.paint(f"┄┄ {_strip_ansi(ln).strip()} ┄┄", pal.muted, pal.italic)

    def _hidden_count(self, blk: Block) -> int:
        return max(0, blk.total_lines - self.preview_lines)

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
                rows.extend((ln, target) for ln in blk.code_lines[:self.preview_lines])
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
        展开 = 块头 + 完整正文；旧预览的「已折叠 K 行」只作缺失说明。≤3 行的块整块显示、不带目标。
        「已折叠 K 行」只是文字，不带目标。
        """
        msgs = self.messages if messages is None else list(messages)
        rows: List[Tuple[str, ToggleTarget]] = []
        for mi, msg in enumerate(msgs):
            if mi > 0 and msg.leading_blank:
                rows.append(("", None))
            # 选中块只影响所在消息；流式更新也只使对应消息的布局失效。
            local_selected = selected if selected is not None and selected[0] == msg.key else None
            key = (local_selected, hint)
            if msg.rows_key != key:
                msg.rows_cache = self._message_rows(msg, local_selected, hint)
                msg.rows_key = key
            rows.extend(msg.rows_cache)
        return rows

    def commit_rows(self, msg: TuiMessage, first: bool = False) -> List[str]:
        """写进终端滚动区的那几行（收起的块带「… 还有 N 行 · Ctrl+T 展开查看」）。"""
        rows = [t for t, _ in self._message_rows(msg, None, "Ctrl+T 展开查看")]
        return ([""] if (msg.leading_blank and not first) else []) + rows

    def live_messages(self) -> List[TuiMessage]:
        return [m for m in self.messages if not m.committed]



@lru_cache(maxsize=1024)
def _cached_paragraph(text: str, width: int) -> tuple:
    return tuple(wrap_line(text, width))


def _tui_wrap_text(text: str, width: int) -> Sequence[str]:
    # 累计流式文本的旧段落保持不变；只重排新段落。巨段落不进入缓存。
    return _cached_paragraph(text, width) if len(text) <= 2048 else wrap_line(text, width)


class _SourceCodeLine(str):
    def __new__(cls, text: str, source_body: str):
        value = super().__new__(cls, text)
        value.source_body = source_body
        return value


class _ArchiveLine(str):
    """Carry display-only archive metadata through the existing list-of-lines API."""
    def __new__(cls, text: str, output_path: str):
        value = super().__new__(cls, text)
        value.output_path = output_path
        return value


class TuiMessageRenderer(MessageRenderer):
    """只供全屏 TUI 使用：无双横线气泡/整行底色，legacy 和其他通道不变。"""

    def __init__(self, palette: Palette, width: int):
        super().__init__(palette, width, wrap_text=_tui_wrap_text)

    def _render_pre(self, raw: str, body_width: int) -> List[str]:
        lines = super()._render_pre(raw, body_width)
        if lines:
            lines[0] = _SourceCodeLine(lines[0], html.unescape(re.sub(r"<[^>]+>", "", raw)))
        return lines

    def _indent_pre_line(self, line: str, indent: str) -> str:
        if hasattr(line, 'source_body'):
            return _SourceCodeLine(indent + line, line.source_body)
        return indent + line

    def render_text(self, text: str, parse_mode: Any = None, indent: str = "  ") -> List[str]:
        lines = super().render_text(text, parse_mode, indent)
        if lines and parse_mode is not None and "html" in str(parse_mode).lower():
            match = re.search(r'<blockquote\b[^>]*\bdata-output-path="([^"]+)"', str(text))
            if match:
                lines[0] = _ArchiveLine(lines[0], html.unescape(match[1]))
        return lines

    def render_message(self, text: str, buttons: Sequence[Tuple[str, str]] = (),
                       parse_mode: Any = None, title: str = "XGent",
                       marker: str = "◆", style: str = "ai") -> List[str]:
        lines = self.render_text(text, parse_mode, indent="  ")
        menu_buttons, hints = split_control_buttons(buttons)
        button_lines = self.render_buttons(menu_buttons, indent="  ")
        if button_lines:
            if lines:
                lines.append("")
            lines.extend(button_lines)
        lines.extend(self.render_hints(hints, indent="  "))
        if not any(line.strip() for line in lines):
            return []
        pal = self.palette
        if style == "token":
            return [pal.paint(_strip_ansi(line), pal.muted) for line in lines]
        color = pal.accent if style == "ai" else ""
        return [pal.paint(title, color, pal.bold)] + lines


class PtScreen:
    """与 `cli_render.TerminalScreen` 同接口，但写进 `MessageModel`。"""

    def __init__(self, palette: Optional[Palette] = None,
                 width: Optional[int] = None) -> None:
        self.palette = copy.copy(palette) if palette is not None else Palette(True)
        if self.palette.enabled:
            # 终端默认背景/正文色，只保留一个强调色和必要的成功/错误色。
            self.palette.accent = self.palette.ai = "\x1b[38;5;75m"
            self.palette.user = self.palette.code = ""
        self._forced_width = width
        self.model = MessageModel(self.palette, preview_lines=0)
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
        return TuiMessageRenderer(self.palette, content_width(self.width))

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
    status_text: Callable[[], str] = lambda: ""
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
    matches = [name for name in names if str(name).lower().startswith(prefix)]
    # /s、/st 等候选优先主菜单；裸 / 和其他候选的相对顺序保持不变。
    if prefix:
        matches.sort(key=lambda name: str(name).lower() != "start")
    return [("/" + name, describe(name)) for name in matches]


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
    if blk.source_body is not None:
        return (_strip_ansi(blk.header).strip() + "\n" if whole else "") + blk.source_body
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
        "native": "终端原生拖选 · ↑↓ / PgUp / PgDn 翻页 · Ctrl+Home/End 首尾 · F2 恢复 TUI 鼠标",
        "typing": "Enter 发送 · Alt+Enter 换行 · ↑↓ 输入历史 · / 命令补全 · 滚轮/PgUp/PgDn 翻页"
                  " · 点击代码块展开 · 拖选自动复制",
    }.get(mode, "")


def compact_hint_text(mode: str) -> str:
    return {
        "archive": "F3 返回 · F5/F6 翻页 · y 复制本页",
        "browse": "Enter 展开 · y 复制 · F3 存档 · F1 帮助",
        "busy": "Ctrl+C 中断 · 可编辑草稿 · F1 帮助",
        "exit": "再按一次 Ctrl+C 退出",
        "native": "F2 恢复鼠标 · F1 帮助",
    }.get(mode, "Enter 发送 · Alt+Enter 换行 · F1 帮助")


async def run_tui(screen: "PtScreen", hooks: TuiHooks) -> None:
    """跑全屏 App。pt 导入或建 App 失败抛 `TuiUnavailable`（调用方回退 legacy）。"""
    try:
        import asyncio as _asyncio

        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.document import Document
        from prompt_toolkit.widgets import TextArea
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.filters import Condition, has_focus
        from prompt_toolkit.formatted_text import ANSI, to_formatted_text
        from prompt_toolkit.formatted_text.utils import fragment_list_width
        from prompt_toolkit.history import FileHistory, InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import (
            ConditionalContainer, Float, FloatContainer, HSplit, Window)
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIContent, UIControl
        from prompt_toolkit.layout.dimension import D
        from prompt_toolkit.layout.margins import ConditionalMargin, ScrollbarMargin
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
        "help": False,
        "archive": False,
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

    copy_lock = _asyncio.Lock()

    def _copy(text: str, what: str) -> None:
        # Clipboard commands may block for seconds. Never run them on the input
        # event loop; serialize copies so the newest selection wins in order.
        async def perform():
            import base64
            async with copy_lock:
                ok = False
                try:
                    encoded = await _asyncio.to_thread(
                        lambda: base64.b64encode(text.encode("utf-8")).decode("ascii"))
                    out = app_ref[0].output
                    out.write_raw(f"\x1b]52;c;{encoded}\x07")
                    out.flush()
                    ok = True
                except Exception:
                    pass
                try:
                    ok = await _asyncio.to_thread(copy_to_clipboard, text) or ok
                except Exception:
                    pass
                _flash(f"已复制{what}（{len(text)} 字）" if ok
                       else "复制失败：终端不支持 OSC52 且没有剪贴板命令")
        if app_ref:
            app_ref[0].create_background_task(perform())

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
                if self.vertical_scroll >= top and not drag["active"]:
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

    # Cache only structural rows. Selection/spinner changes must not flatten
    # the entire conversation or split all its fragments for mouse hit testing.
    frag_cache: Dict[str, Any] = {"key": None, "rows": []}

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

    def _drag_edge(pos) -> int:
        # Mouse positions are logical (row, character), not physical screen
        # lines. A wrapped line can occupy the whole viewport: do not mistake
        # every move within it for a drag at the top/bottom edge.
        info = output_window.render_info
        if info is None:
            return 0
        candidates = [(column, screen_row)
                      for screen_row, (row, column) in info.visible_line_to_row_col.items()
                      if row == pos[0] and column <= pos[1]]
        if not candidates:
            return 0
        screen_row = max(candidates)[1]
        return -1 if screen_row <= 0 else (1 if screen_row >= info.window_height - 1 else 0)

    def _selected_text() -> str:
        rng = _sel_range()
        if rng is None:
            return ""
        (r0, c0), (r1, c1) = rng
        rows = _rows()["rows"]
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
                if output_window.follow:
                    output_window.vertical_scroll = output_window.max_top
                    output_window.follow = False
                if app_ref:  # 点输出区 → 退出输入框（打字会自动回来）
                    app_ref[0].layout.focus(output_window)
            elif et == MouseEventType.MOUSE_MOVE and drag["active"]:
                if drag["end"] == pos:
                    return None
                drag["end"] = pos
                drag["moved"] = drag["moved"] or pos != drag["anchor"]
                drag["edge"] = _drag_edge(pos)
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
        key = (model.revision, state["sel"])
        if frag_cache["key"] != key:
            frag_cache.update(key=key, rows=model.render_rows(state["sel"]))
        return frag_cache

    class _OutputControl(UIControl):
        """Lazy rows and O(1) row hit testing instead of one huge fragment list."""

        def is_focusable(self):
            return True

        def create_content(self, width, height):
            rows = _rows()["rows"]
            count = len(rows)
            rng = _sel_range()

            def get_line(row):
                if row < 0:
                    return []
                if row >= count:
                    return []
                text = rows[row][0]
                line = _line_fragments(text)
                if rng is not None and rng[0][0] <= row <= rng[1][0]:
                    a = rng[0][1] if row == rng[0][0] else 0
                    b = rng[1][1] + 1 if row == rng[1][0] else max(len(_strip_ansi(text)), 1)
                    return _highlight(line or (("", " "),), a, b)
                return list(line)

            return UIContent(get_line=get_line, line_count=max(1, count),
                             show_cursor=False)

        def preferred_height(self, width, max_available_height, wrap_lines, get_line_prefix):
            content = self.create_content(width, max_available_height)
            if not wrap_lines:
                return min(content.line_count, max_available_height)
            if width <= 0 or max_available_height <= 0:
                return 0
            height = 0
            for row in range(content.line_count):
                height += content.get_height_for_line(row, width, get_line_prefix)
                if height >= max_available_height:
                    return max_available_height
            return height

        def mouse_handler(self, ev):
            rows = _rows()["rows"]
            row = ev.position.y
            target = rows[row][1] if 0 <= row < len(rows) else None
            if target is None:
                return plain_handler(ev)
            handler = handler_cache.get(target)
            if handler is None:
                handler = handler_cache[target] = _make_handler(target)
            return handler(ev)

    output_window = _ScrollWindow(
        content=_OutputControl(),
        wrap_lines=True,
        right_margins=[ConditionalMargin(
            ScrollbarMargin(display_arrows=False),
            filter=Condition(lambda: output_window.max_top > 0),
        )],
        height=D(min=1, weight=1),
        dont_extend_height=False,
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
        height=D(min=1, max=6), get_line_prefix=_prefix, wrap_lines=True,
        dont_extend_height=True)

    # -- 顶栏 / 分隔 / 底栏 ---------------------------------------------------
    def _columns() -> int:
        try:
            return app_ref[0].output.get_size().columns
        except Exception:
            return 80

    def _status_bar():
        columns = _columns()
        left = [("class:title.name", " xgent ")]
        if columns >= 60:
            try:
                context = str(hooks.status_text() or "").replace("\n", " ")[:40]
            except Exception:
                context = ""
            if context:
                left.append(("class:title", " · " + context + " "))
        if _busy():
            frame = _SPINNER[int(time.monotonic() * 10) % len(_SPINNER)]
            right = [("class:title.busy", f"{frame} 生成中 ")]
        else:
            right = [("class:title.ok", "● 就绪 ")]
        if not output_window.follow and columns >= 50:
            right.insert(0, ("class:title", "Ctrl+End 回到最新  "))
        if not state["mouse"] and columns >= 70:
            right.insert(0, ("class:title", "原生选择  "))
        # 状态不能挤掉右侧的运行/停止反馈。
        while len(left) > 1 and fragment_list_width(left + right) >= columns:
            left.pop()
        pad = max(1, columns - fragment_list_width(left + right))
        return left + [("class:title", " " * pad)] + right

    def _mode() -> str:
        if state["archive"]:
            return "archive"
        if state["exit_armed"] and time.monotonic() - state["exit_armed"] < 2.0:
            return "exit"
        if not state["mouse"]:
            return "native"
        if state["sel"] is not None:
            return "browse"
        if _busy():
            return "busy"
        if app_ref and app_ref[0].layout.has_focus(input_window):
            return "typing"
        return "input"

    def _hint_bar():
        if state["flash"] and time.monotonic() < state["flash_until"]:
            return [("class:hint.flash", " " + state["flash"])]
        return [("class:hint", " " + compact_hint_text(_mode()))]

    def _input_rule():
        typing = bool(app_ref) and app_ref[0].layout.has_focus(input_window)
        label = (" 消息草稿（F3 返回编辑） " if state["archive"] else
                 " 草稿（生成中） " if _busy() else (" 消息 " if typing else " 消息 · 直接打字即可 "))
        rest = max(0, _columns() - fragment_list_width([("", label)]) - 2)
        return [("class:rule", "──"), ("class:rule.label", label), ("class:rule", "─" * rest)]

    up_edge, down_edge = _edge_handler(-1), _edge_handler(1)
    status_window = Window(FormattedTextControl(
        lambda: [(st, t, up_edge) for st, t, *_ in _status_bar()]), height=1, style="class:title")
    rule_window = Window(FormattedTextControl(
        lambda: [(st, t, down_edge) for st, t, *_ in _input_rule()]), height=1)
    hint_window = Window(FormattedTextControl(_hint_bar), height=1, style="class:hint")
    help_window = ConditionalContainer(
        Window(FormattedTextControl(
            " 操作帮助 · F1 关闭\n"
            " Enter 发送 · Alt+Enter 换行\n"
            " Ctrl+C 中断/退出 · Esc 浏览\n"
            " Tab / Shift+Tab 补全或选块\n"
            " 浏览：Enter 折叠 · y/Y 复制\n"
            " PgUp/PgDn 翻页 · Ctrl+Home/End 首尾\n"
            " Ctrl+O 最近工具 · F2 原生选择\n"
            " F3 输出存档 · F5/F6 存档翻页\n"
            " 鼠标：滚轮翻页 · 拖选复制"),
            height=D(min=1, max=10), dont_extend_height=True,
            wrap_lines=True, style="class:hint"),
        filter=Condition(lambda: state["help"]),
    )

    archive_state = {"path": "", "page": None, "previous": [], "loading": False, "generation": 0}
    archive_area = TextArea(text="", read_only=True, scrollbar=True, wrap_lines=True)
    viewing_archive = Condition(lambda: state["archive"])

    def archive_title():
        page = archive_state["page"]
        if archive_state["loading"]:
            return " 读取存档中… · F3 关闭"
        if not page:
            return " 输出存档 · F3 关闭"
        filename = page['filename']
        if len(filename) > 28:
            filename = filename[:12] + '…' + filename[-12:]
        return f" {filename} · {page['offset']}–{page['next_offset']}/{page['size']} bytes"

    archive_window = ConditionalContainer(HSplit([
        Window(FormattedTextControl(archive_title), height=1, style="class:hint"),
        archive_area,
    ]), filter=viewing_archive)

    def close_archive():
        state["archive"] = False
        state["sel"] = None
        archive_state["generation"] += 1
        archive_state.update(loading=False, page=None, previous=[])
        archive_area.buffer.set_document(Document(""), bypass_readonly=True)
        app_ref[0].layout.focus(input_window)
        _invalidate()

    def load_archive(offset, previous):
        if archive_state["loading"]:
            return
        archive_state["loading"] = True
        generation = archive_state["generation"]
        path = archive_state["path"]
        async def read():
            from .output_archive import read_output_page, OutputArchiveError
            try:
                page = await _asyncio.to_thread(read_output_page, path, offset)
                if generation != archive_state["generation"]:
                    return
                archive_state.update(page=page, previous=previous)
                archive_area.buffer.set_document(Document(page["text"]), bypass_readonly=True)
            except OutputArchiveError as exc:
                if generation == archive_state["generation"]:
                    _flash(str(exc))
            finally:
                if generation == archive_state["generation"]:
                    archive_state["loading"] = False
                    _invalidate()
        app_ref[0].create_background_task(read())
        _invalidate()

    # -- 键位 --------------------------------------------------------------
    # 默认聚焦输入框。TUI 鼠标模式下 ↑↓ 操作输入历史/光标，滚轮及 PgUp/PgDn 滚动输出。
    # F2 原生模式下终端可把滚轮转换为 ↑↓，因此该模式专门把 ↑↓ 路由到输出滚动。
    # Tab 选折叠块、Enter 展开/收起、y/Y 复制；浏览时直接打字仍会回到输入框。
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

    @kb.add("c-o", filter=~viewing_archive)
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

    @kb.add("f3", eager=True)
    def open_archive(event):
        if state["archive"]:
            close_archive()
            return
        message = model.message_of(state["sel"])
        if message is None:
            message = next((msg for msg in reversed(model.messages) if msg.output_path), None)
        if message is None or not message.output_path:
            _flash("此结果没有输出存档；已截掉且未保存的旧内容无法恢复")
            return
        archive_state.update(path=message.output_path, page=None, previous=[], loading=False)
        archive_state["generation"] += 1
        state["archive"] = True
        event.app.layout.focus(archive_area)
        load_archive(0, [])

    @kb.add("y", filter=viewing_archive, eager=True)
    def copy_archive_page(event):
        if archive_state["page"]:
            _copy(archive_state["page"]["text"], "本页输出")

    @kb.add("f5", filter=viewing_archive, eager=True)
    def previous_archive_page(event):
        previous = archive_state["previous"]
        if previous:
            load_archive(previous[-1], previous[:-1])

    @kb.add("f6", filter=viewing_archive, eager=True)
    def next_archive_page(event):
        page = archive_state["page"]
        if page and not page["eof"]:
            load_archive(page["next_offset"], archive_state["previous"] + [page["offset"]])

    @kb.add("escape", filter=viewing_archive, eager=True)
    @kb.add("c-c", filter=viewing_archive, eager=True)
    @kb.add("c-d", filter=viewing_archive, eager=True)
    def exit_archive(event):
        close_archive()

    @kb.add("f1")
    def _toggle_help(event):
        state["help"] = not state["help"]
        _invalidate()

    @kb.add("f2", filter=~viewing_archive)
    def _mouse_toggle(event):
        state["mouse"] = not state["mouse"]
        drag.update(anchor=None, end=None, active=False, moved=False, edge=0)
        _alt_scroll(not state["mouse"])
        if not state["mouse"]:
            state["sel"] = None
            event.app.layout.focus(output_window)
        _flash("已接管鼠标：点击展开、滚轮滚动、拖选自动复制（F2 切到终端原生选择）"
               if state["mouse"] else "终端原生选择：↑↓/PgUp/PgDn 翻页；支持备用滚动的终端也可用滚轮（F2 切回）")

    # 滚动
    def _page() -> int:
        info = output_window.render_info
        return max(1, (info.window_height if info else 10) - 2)

    @kb.add("pageup")
    def _pgup(event):
        if state["archive"]:
            archive_area.buffer.cursor_up(count=_page())
        else:
            output_window.scroll_by(-_page())
        _invalidate()

    @kb.add("pagedown")
    def _pgdn(event):
        if state["archive"]:
            archive_area.buffer.cursor_down(count=_page())
        else:
            output_window.scroll_by(_page())
        _invalidate()

    # 默认 TUI 模式保持 ↑↓ 回输入框；下面的原生模式绑定具有更高优先级。
    @kb.add("up", filter=in_out)
    def _up_to_input(event):
        _to_input(event)
        input_buffer.auto_up()

    @kb.add("down", filter=in_out)
    def _down_to_input(event):
        _to_input(event)
        input_buffer.auto_down()

    # Native mouse selection disables mouse reports. Terminals supporting
    # DECSET 1007 translate the wheel to arrows; route these to output scrolling,
    # even after typing a draft, rather than unexpectedly changing input history.
    native_mouse = Condition(lambda: not state["mouse"] and not state["archive"])

    @kb.add("up", filter=native_mouse)
    def _native_up(event):
        output_window.scroll_by(-1)
        _invalidate()

    @kb.add("down", filter=native_mouse)
    def _native_down(event):
        output_window.scroll_by(1)
        _invalidate()

    @kb.add("home", filter=in_out)
    @kb.add("c-home")
    def _top(event):
        if state["archive"]:
            archive_area.buffer.cursor_position = 0
            return
        output_window.scroll_to(0)
        _invalidate()

    @kb.add("end", filter=in_out)
    @kb.add("c-end")
    def _bottom(event):
        if state["archive"]:
            archive_area.buffer.cursor_position = len(archive_area.buffer.text)
            return
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
            ConditionalContainer(output_window, filter=~viewing_archive),
            archive_window,
            help_window,
            rule_window,
            input_window,
            hint_window,
        ]),
        floats=[Float(xcursor=True, ycursor=True,
                      content=CompletionsMenu(max_height=10, scroll_offset=1))],
    )
    style = Style.from_dict({
        "title": "#808080",
        "title.name": "#5fafff bold",
        "title.ok": "#5faf87",
        "title.busy": "#d7af5f",
        "title.warn": "#d7af5f",
        "rule": "#585858",
        "rule.label": "#808080",
        "hint": "#808080",
        "hint.flash": "#d7af5f bold",
        "completion-menu": "",
        "completion-menu.completion.current": "reverse",
        "completion-menu.meta.completion": "#808080",
        "completion-menu.meta.completion.current": "reverse",
        "scrollbar.background": "",
        "scrollbar.button": "#585858",
    })

    try:
        app = Application(
            layout=Layout(body, focused_element=input_window),   # 一进来就聚焦输入框
            key_bindings=kb, style=style, full_screen=True,
            mouse_support=Condition(lambda: state["mouse"]),
            min_redraw_interval=1 / 30,
        )
    except Exception as exc:
        raise TuiUnavailable(str(exc)) from exc
    app_ref.append(app)

    def _on_change() -> None:
        # 新内容只在 follow 时贴底（_ScrollWindow._scroll 处理）；上滚回看时不打扰。
        app.invalidate()

    async def _ticker():
        # 拖到边缘时 30Hz 滚动；转圈仍为 10Hz，空闲不重绘。
        last_frame = None
        while True:
            await _asyncio.sleep(1 / 30)
            if drag["active"] and drag["edge"]:
                output_window.scroll_by(drag["edge"])
                top, height = _visible_top_height()
                last = max(0, len(_rows()["rows"]) - 1)
                row = max(0, top) if drag["edge"] < 0 else min(last, top + height - 1)
                drag["end"] = (row, 0 if drag["edge"] < 0 else 10 ** 6)
                app.invalidate()
            frame = int(time.monotonic() * 10)
            if (_busy() or state["flash"] or state["exit_armed"]) and frame != last_frame:
                last_frame = frame
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
        # 默认由 TUI 接管鼠标；F2 原生模式才打开备用滚动，并专门路由 ↑↓。
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
