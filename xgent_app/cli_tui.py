"""CLI 全屏 TUI 前端（prompt_toolkit）：历史消息里的长代码块**原地可展开**。

为什么要它
----------
Agent 模式下 AI 回复几乎全是 `*-x` 协议块，`hide_protocol_blocks` 把每块折成
`<blockquote expandable>`。Telegram/网页能点开，但追加式滚屏的 CLI 只能一次性
把整块代码摊在屏幕上——想「看一眼头、需要时再展开」做不到。全屏 TUI 用一个
可滚动的输出区 + 每条消息的折叠态模型解决它：折叠块只显示 header + 前几行，
鼠标点击（或 Ctrl+E/Ctrl+R 全展开/全收起）在**原地**展开成全文。

架构：只换绘制半边，中继/落库逐字节不变
--------------------------------------
关键约束（见 quirky-mixing-dawn.md）：折叠只改**显示**，写进 DB / 执行 /
全局镜像的必须是原始文本。CLI 的 relay(`_RELAY.emit`) 与落库都在 `CliBot`
方法边界、位于绘制上游、拿的是 raw 文本。所以本模块**完全不碰 cli_bridge**：
`PtScreen` 实现与 `cli_render.TerminalScreen` **一模一样的接口**
（print_block/update_block/print_plain/notice/invalidate/renderer/palette/
width/height），经 `set_screen()` 挂上后，`CliBot` 照旧调 `self.screen.*`，
方法签名、`_RELAY.emit`、`_register_menu` 一字不改。TUI 拿到的是 `CliBot`
已经渲染好的 ANSI 行，折叠在**行**这一层做（识别 `│ ` 代码条的连续段），
不需要把原始 HTML 再往下游塞。

opt-in + 兜底
-------------
全屏交互在本无 TTY 的环境里无法验证，所以严格 opt-in：只有
``XGENT_CLI_TUI`` 为真、prompt_toolkit 可导入、stdin/stdout 都是 TTY、且没设
``XGENT_CLI_NO_TUI`` 时才启用（``tui_enabled``）。默认仍走久经验证的
legacy 行式渲染器（cli_render.TerminalScreen + cli_palette.SlashPalette），
它也是 pt 不可用/启动失败时的兜底。纯逻辑（分段/折叠/模型 upsert）全部可在
无 TTY 下单测；pt Application 那层保持薄。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence, Tuple

from .cli_render import MessageRenderer, Palette, content_width, terminal_size

# 代码条连续段超过这么多行才值得折叠——短代码全展开更省事，不值得为几行加个
# 「点击展开」的仪式。
_COLLAPSE_MIN = 8
# 折叠态显示前几行代码，给足「这块大概是什么」的线索。
_PREVIEW_LINES = 3

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# 折叠标记行的目标：(message_id, block_index)。None = 不是热点行。
ToggleTarget = Optional[Tuple[int, int]]


def _strip_ansi(line: str) -> str:
    return _ANSI_RE.sub("", line)


def _is_code_bar(line: str) -> bool:
    """这一行是不是 `_render_pre` 画出来的代码条（`│ …`）。

    折叠块的正文经 render_folded_html 变成 `<pre>`，`MessageRenderer._render_pre`
    把每行前面加一根 `│` 竖条（可能再带缩进）。识别连续的竖条行 = 找到一个
    可折叠的代码段——这是 TUI 折叠唯一依赖的渲染契约。
    """
    return _strip_ansi(line).lstrip().startswith("│")


@dataclass
class Block:
    """一条消息里的一段：要么是散文（不折叠），要么是一段代码条（可折叠）。"""

    lines: List[str]
    collapsible: bool
    expanded: bool = False


@dataclass
class TuiMessage:
    message_id: Optional[int]
    blocks: List[Block]
    leading_blank: bool = True


def segment_lines(lines: Sequence[str], collapse_min: int = _COLLAPSE_MIN) -> List[Block]:
    """把渲染好的 ANSI 行切成 [散文块 | 可折叠代码块] 序列。

    连续的代码条行凑成一段；够长（> collapse_min）才标记为可折叠，否则并进
    相邻散文原样显示——短代码折叠只是徒增点击。散文行（含折叠块的 header，
    如「🔧 run · 42 行」）永远可见。
    """
    blocks: List[Block] = []
    prose: List[str] = []

    def _flush_prose() -> None:
        if prose:
            blocks.append(Block(list(prose), collapsible=False))
            prose.clear()

    i, n = 0, len(lines)
    while i < n:
        if _is_code_bar(lines[i]):
            j = i
            while j < n and _is_code_bar(lines[j]):
                j += 1
            run = list(lines[i:j])
            if len(run) > collapse_min:
                _flush_prose()
                blocks.append(Block(run, collapsible=True))
            else:
                prose.extend(run)
            i = j
        else:
            prose.append(lines[i])
            i += 1
    _flush_prose()
    return blocks


class MessageModel:
    """全屏 TUI 的消息模型：以 message_id 为键的全量消息 + 每块折叠态。

    对照 legacy `TerminalScreen` 只记「最后一块」，这里记全部——所以历史消息
    也能原地编辑（update_block 按 id 命中任意消息）和原地展开，正是 TUI 要的。
    折叠态跨流式编辑按块序号保持（长块流式静止时不会一边刷新一边把用户
    展开的段收回去）。
    """

    def __init__(self, palette: Optional[Palette] = None,
                 preview_lines: int = _PREVIEW_LINES,
                 collapse_min: int = _COLLAPSE_MIN) -> None:
        self.palette = palette or Palette(False)
        self.preview_lines = preview_lines
        self.collapse_min = collapse_min
        self.messages: List[TuiMessage] = []
        self._index: dict = {}
        self.revision = 0  # 每次变更自增，pt 层据此决定要不要重画

    def _touch(self) -> None:
        self.revision += 1

    def _segment(self, lines: Sequence[str]) -> List[Block]:
        return segment_lines(lines, self.collapse_min)

    def upsert(self, message_id: Optional[int], lines: Sequence[str],
               leading_blank: bool = True) -> None:
        """新增或原地替换一条消息。message_id 命中已有消息则替换其内容，
        并**按块序号保留折叠态**；否则追加。message_id 为 None 一律追加。"""
        blocks = self._segment(lines)
        if message_id is not None and message_id in self._index:
            old = self._index[message_id]
            for idx, blk in enumerate(blocks):
                if idx < len(old.blocks) and old.blocks[idx].collapsible and blk.collapsible:
                    blk.expanded = old.blocks[idx].expanded
            old.blocks = blocks
            old.leading_blank = leading_blank
        else:
            msg = TuiMessage(message_id, blocks, leading_blank)
            self.messages.append(msg)
            if message_id is not None:
                self._index[message_id] = msg
        self._touch()

    def has(self, message_id: int) -> bool:
        return message_id in self._index

    def remove(self, message_id: int) -> bool:
        msg = self._index.pop(message_id, None)
        if msg is None:
            return False
        try:
            self.messages.remove(msg)
        except ValueError:
            pass
        self._touch()
        return True

    def toggle(self, message_id: int, block_idx: int) -> bool:
        msg = self._index.get(message_id)
        if msg is None or not (0 <= block_idx < len(msg.blocks)):
            return False
        blk = msg.blocks[block_idx]
        if not blk.collapsible:
            return False
        blk.expanded = not blk.expanded
        self._touch()
        return True

    def set_all_expanded(self, expanded: bool) -> None:
        for msg in self.messages:
            for blk in msg.blocks:
                if blk.collapsible:
                    blk.expanded = expanded
        self._touch()

    def foldable_targets(self) -> List[ToggleTarget]:
        """按渲染顺序列出所有可折叠块的 (message_id, block_idx)，供键盘浏览态
        （Tab 进入、↑/↓ 选中、Enter 原地展开）定位目标。message_id 为 None 的
        消息（散文/notice）不参与。"""
        out: List[ToggleTarget] = []
        for msg in self.messages:
            if msg.message_id is None:
                continue
            for bi, blk in enumerate(msg.blocks):
                if blk.collapsible:
                    out.append((msg.message_id, bi))
        return out

    def _marker(self, collapsed: bool, hidden: int, selected: bool = False) -> str:
        pal = self.palette
        cursor = "▶ " if selected else "  "
        if collapsed:
            text = f"{cursor}⊕ 展开 {hidden} 行代码 · Enter / 点击 / Ctrl+E"
        else:
            text = f"{cursor}⊖ 收起代码 · Enter"
        if not pal.enabled:
            return text
        return pal.paint(text, pal.accent, pal.bold) if selected else pal.paint(text, pal.muted)

    def render_rows(self, selected: ToggleTarget = None) -> List[Tuple[str, ToggleTarget]]:
        """摊平成 (行文本, 折叠热点) 列表，供 pt 输出区逐行渲染。

        折叠块的可见行（预览行 + 标记行）都带上 (message_id, block_idx) 热点，
        点在块上任意一行都能展开；展开态在末尾补一行「收起」标记。``selected``
        命中的块，标记行高亮（▶ + 橙色），标出键盘浏览态下 Enter 将作用的目标。
        """
        rows: List[Tuple[str, ToggleTarget]] = []
        for mi, msg in enumerate(self.messages):
            if mi > 0 and msg.leading_blank:
                rows.append(("", None))
            for bi, blk in enumerate(msg.blocks):
                target: ToggleTarget = (msg.message_id, bi) if (
                    blk.collapsible and msg.message_id is not None) else None
                is_sel = target is not None and target == selected
                if blk.collapsible and not blk.expanded:
                    preview = blk.lines[:self.preview_lines]
                    hidden = len(blk.lines) - len(preview)
                    for ln in preview:
                        rows.append((ln, target))
                    if hidden > 0:
                        rows.append((self._marker(True, hidden, is_sel), target))
                else:
                    for ln in blk.lines:
                        rows.append((ln, target))
                    if blk.collapsible:
                        rows.append((self._marker(False, 0, is_sel), target))
        return rows


class PtScreen:
    """与 `cli_render.TerminalScreen` 同接口的屏幕，但写进 `MessageModel`
    而不是直接往 stdout 打 ANSI。

    `CliBot` 与 xgent_cli 的辅助函数照旧调 print_block/update_block/
    print_plain/notice/invalidate/renderer/palette/width/height——它们不知道
    自己在跟一个 pt 后端说话。这正是「只换绘制半边、中继/落库不动」的落点：
    换掉的只是 `_shared_screen` 这一个对象。
    """

    def __init__(self, palette: Optional[Palette] = None,
                 width: Optional[int] = None) -> None:
        self.palette = palette if palette is not None else Palette(True)
        self._forced_width = width
        self.model = MessageModel(self.palette)
        # pt Application 挂上后设成 app.invalidate；未挂时是空操作，便于单测。
        self.on_change: Callable[[], None] = lambda: None

    # -- 尺寸 ------------------------------------------------------------
    @property
    def width(self) -> int:
        if self._forced_width:
            return self._forced_width
        return terminal_size()[0]

    @property
    def height(self) -> int:
        return terminal_size()[1]

    def renderer(self) -> MessageRenderer:
        return MessageRenderer(self.palette, content_width(self.width))

    # -- 绘制接口（CliBot / xgent_cli 调用） -----------------------------
    def print_block(self, lines: Sequence[str], message_id: Optional[int] = None,
                    leading_blank: bool = True) -> None:
        self.model.upsert(message_id, list(lines), leading_blank)
        self.on_change()

    def update_block(self, lines: Sequence[str], message_id: int) -> bool:
        """原地重绘。TUI 记全量消息，任意历史消息都能命中——命中即成功
        （legacy 只能改「最后一块」，这里是它相对 legacy 的增益）。"""
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
        # 模型按 id 全量保存，update_block 永远能命中目标——不像 legacy 需要
        # 「最后一块」失效标记。这里只需请求一次重画。
        self.on_change()


class TuiUnavailable(RuntimeError):
    """pt 不可用或 App 启动失败。调用方据此回退 legacy 行式渲染器。"""


@dataclass
class TuiHooks:
    """xgent_cli 注入给 TUI 的回调。TUI 只换绘制/输入半边，路由/一轮处理仍归
    xgent_cli——这些回调就是那道接缝，同时也让 run_tui 可以脱离 xgent_cli
    单测（塞假回调）。"""

    dispatch: Callable[[str], Any]           # async (text) -> bool（True 表示退出）
    banner: Callable[[], None] = lambda: None
    prompt_text: Callable[[], str] = lambda: "❯ "
    command_names: Callable[[], Sequence[str]] = lambda: ()
    describe_command: Callable[[str], str] = lambda _n: ""
    turn_active: Callable[[], bool] = lambda: False
    request_stop: Callable[[], bool] = lambda: False
    remember_history: Callable[[str], None] = lambda _line: None
    history_file: Optional[str] = None


def _env_truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _pt_available() -> bool:
    try:
        import prompt_toolkit  # noqa: F401
        return True
    except Exception:
        return False


def tui_enabled() -> bool:
    """该不该启用全屏 TUI。严格 opt-in：显式开关 + pt 可用 + 双向 TTY。"""
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
    """`/` 前缀补全候选：整行以 / 开头且还没打空格时，按前缀筛命令。

    返回 [(补全文本, 说明), ...]。纯函数，便于单测；pt 的 Completer 在
    run_tui 里包一层薄壳调它。
    """
    if not text.startswith("/") or " " in text:
        return []
    prefix = text[1:].lower()
    out: List[Tuple[str, str]] = []
    for name in names:
        if str(name).lower().startswith(prefix):
            out.append(("/" + name, describe(name)))
    return out


async def run_tui(screen: "PtScreen", hooks: TuiHooks) -> None:
    """跑全屏 pt App：输出区渲染 `screen.model`、输入区带 slash 补全/历史。

    只在 `tui_enabled()` 为真时被 xgent_cli 调用。pt 导入或建 App 失败一律抛
    `TuiUnavailable`，调用方据此回退 legacy。dispatch/一轮处理仍归 hooks，
    本函数只管画屏与收键——「只换绘制/输入半边」的落点。
    """
    try:
        import asyncio as _asyncio

        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.filters import Condition, has_focus
        from prompt_toolkit.formatted_text import ANSI, to_formatted_text
        from prompt_toolkit.history import FileHistory, InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import HSplit, Window
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.dimension import D
        from prompt_toolkit.layout.margins import ScrollbarMargin
        from prompt_toolkit.mouse_events import MouseEventType
        from prompt_toolkit.styles import Style
    except Exception as exc:  # pt 不在/装坏 → 让 xgent_cli 回退 legacy
        raise TuiUnavailable(str(exc)) from exc

    app_ref: List[Any] = []  # 延迟持有 app，供鼠标热点/on_change 回调引用
    # 键盘浏览态：sel["target"] = 当前选中的可折叠块 (message_id, block_idx)，
    # None 表示未进入浏览态。Tab 进入并选中首块，↑/↓ 换块，Enter 原地展开/收起，
    # Esc/Tab 退出回输入框。与鼠标点击、Ctrl+E/Ctrl+R 并存，互不干扰。
    sel: dict = {"target": None}

    def _handler_for(target: ToggleTarget):
        def _mouse(mouse_event):  # 点在折叠块任意可见行 → 原地展开/收起
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                if target is not None:
                    mid, bi = target
                    if mid is not None and screen.model.toggle(mid, bi) and app_ref:
                        app_ref[0].invalidate()
                return None
            return NotImplemented
        return _mouse

    def _fragments():
        rows = screen.model.render_rows(sel["target"])
        out: List[Any] = []
        for text, target in rows:
            pieces = to_formatted_text(ANSI(text)) if text else [("", "")]
            if target is not None:
                handler = _handler_for(target)
                pieces = [(style, t, handler) for style, t, *_ in pieces]
            out.extend(pieces)
            out.append(("", "\n"))
        if out and out[-1] == ("", "\n"):
            out.pop()
        return out
    # __RUN_TUI_TAIL__

    class _SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            names = list(hooks.command_names() or ())
            for full, meta in slash_completions(text, names, hooks.describe_command):
                yield Completion(full, start_position=-len(text),
                                 display=full, display_meta=meta)

    try:
        history = (FileHistory(hooks.history_file)
                   if hooks.history_file else InMemoryHistory())
    except Exception:
        history = InMemoryHistory()

    input_buffer = Buffer(history=history, completer=_SlashCompleter(),
                          complete_while_typing=True, multiline=False)

    def _prefix(line_number, wrap_count):
        if wrap_count:
            return []
        try:
            return to_formatted_text(ANSI(hooks.prompt_text()))
        except Exception:
            return [("", "> ")]

    def _hint():
        if sel["target"] is not None:
            return [("class:hint",
                     " 浏览折叠块：↑/↓ 选块 · Enter 原地展开/收起 · "
                     "Esc/Tab 回输入框 · Ctrl+E 全展开 / Ctrl+R 全收起 ")]
        return [("class:hint",
                 " Enter 发送 · Tab 浏览折叠块（Enter 展开）· 点代码块 展开 · "
                 "PgUp/PgDn 滚动 · Ctrl+C 停止·退出 ")]

    output_window = Window(
        content=FormattedTextControl(_fragments, focusable=True, show_cursor=False),
        wrap_lines=True, right_margins=[ScrollbarMargin(display_arrows=True)])
    input_window = Window(
        content=BufferControl(buffer=input_buffer),
        height=D(min=1, max=6), get_line_prefix=_prefix, wrap_lines=True)
    sep = Window(height=1, char="─", style="class:sep")
    hint = Window(content=FormattedTextControl(_hint), height=1, style="class:hint")
    # __RUN_TUI_KB__

    kb = KeyBindings()
    busy = {"active": False}  # 一轮进行中，闸住新提交（与 turn_active 双保险）

    def _scroll_to_selected() -> None:
        # 让选中块的首行进视口：在 render_rows 里找到它的行号，把 vertical_scroll
        # 摆到「首行靠近顶部」。wrap_lines 会再自适应，近似即可。
        tgt = sel["target"]
        if tgt is None:
            return
        for idx, (_txt, rtgt) in enumerate(screen.model.render_rows(tgt)):
            if rtgt == tgt:
                output_window.vertical_scroll = max(0, idx - 2)
                return

    def _select_index(i: int) -> bool:
        targets = screen.model.foldable_targets()
        if not targets:
            sel["target"] = None
            return False
        i = max(0, min(i, len(targets) - 1))
        sel["target"] = targets[i]
        _scroll_to_selected()
        return True

    def _current_index() -> int:
        try:
            return screen.model.foldable_targets().index(sel["target"])
        except ValueError:
            return -1

    @kb.add("enter", filter=has_focus(input_window))
    def _submit(event):
        if busy["active"] or hooks.turn_active():
            return  # 生成中：保留草稿，不吞、不新起一轮
        text = input_buffer.text
        if not text.strip():
            input_buffer.reset()
            return
        input_buffer.append_to_history()
        try:
            hooks.remember_history(text)
        except Exception:
            pass
        input_buffer.reset()
        busy["active"] = True

        async def _go():
            should_exit = False
            try:
                should_exit = bool(await hooks.dispatch(text))
            except Exception as exc:  # 一轮内部异常不该掀翻整个 TUI
                try:
                    screen.notice(f"处理出错：{exc}", "err")
                except Exception:
                    pass
            finally:
                busy["active"] = False
                event.app.invalidate()
            if should_exit:
                event.app.exit()

        _asyncio.ensure_future(_go())

    @kb.add("c-c")
    def _interrupt(event):
        # 生成中 → 协作停止（保 _request_stop 语义）；空闲 → 退出。
        if hooks.turn_active():
            try:
                hooks.request_stop()
            except Exception:
                pass
            return
        event.app.exit()

    @kb.add("c-d")
    def _eof(event):
        if not input_buffer.text:
            event.app.exit()

    @kb.add("c-e")
    def _expand_all(event):
        screen.model.set_all_expanded(True)
        event.app.invalidate()

    @kb.add("c-r")
    def _collapse_all(event):
        screen.model.set_all_expanded(False)
        event.app.invalidate()

    # -- 键盘浏览折叠块（spec ①「CLI Enter 原地展开」）---------------------
    # 空输入时 Tab 进入浏览态并选中首个可折叠块；有输入时 Tab 留给补全，不拦。
    @kb.add("tab", filter=has_focus(input_window) & Condition(
        lambda: not input_buffer.text.strip()))
    def _enter_browse(event):
        if _select_index(0):
            event.app.layout.focus(output_window)
        event.app.invalidate()

    # 浏览态：↑/↓ 换块、Enter 原地展开/收起、Tab/Esc 回输入框。
    @kb.add("down", filter=has_focus(output_window))
    def _sel_next(event):
        _select_index(_current_index() + 1)
        event.app.invalidate()

    @kb.add("up", filter=has_focus(output_window))
    def _sel_prev(event):
        _select_index(_current_index() - 1)
        event.app.invalidate()

    @kb.add("enter", filter=has_focus(output_window))
    def _toggle_selected(event):
        tgt = sel["target"]
        if tgt is not None and tgt[0] is not None:
            screen.model.toggle(tgt[0], tgt[1])
        event.app.invalidate()

    @kb.add("tab", filter=has_focus(output_window))
    @kb.add("escape", filter=has_focus(output_window))
    def _leave_browse(event):
        sel["target"] = None
        event.app.layout.focus(input_window)
        event.app.invalidate()

    def _scroll_step(fallback: int = 10) -> int:
        info = output_window.render_info
        return info.window_height if info else fallback

    @kb.add("pageup")
    def _pgup(event):
        # 手动上滚：改 vertical_scroll 后必须 invalidate，否则屏幕不重绘、
        # 看着就是「翻不动」。离开底部后 _on_change 会据 render_info 自动
        # 停止跟随尾部（不再把用户拽回底）。
        output_window.vertical_scroll = max(
            0, output_window.vertical_scroll - _scroll_step())
        event.app.invalidate()

    @kb.add("pagedown")
    def _pgdn(event):
        output_window.vertical_scroll += _scroll_step()
        event.app.invalidate()

    @kb.add("home")
    def _scroll_top(event):
        output_window.vertical_scroll = 0
        event.app.invalidate()

    @kb.add("end")
    def _scroll_bottom(event):
        output_window.vertical_scroll = 10 ** 9  # pt 渲染时夹回底部
        event.app.invalidate()
    # __RUN_TUI_APP__

    style = Style.from_dict({"sep": "#444444", "hint": "#888888"})
    layout = Layout(HSplit([output_window, sep, input_window, hint]),
                    focused_element=input_window)

    try:
        app = Application(layout=layout, key_bindings=kb, style=style,
                          full_screen=True, mouse_support=True)
    except Exception as exc:
        raise TuiUnavailable(str(exc)) from exc
    app_ref.append(app)

    def _at_bottom() -> bool:
        # 依据「上一帧」的 render_info 判断用户此刻是否停在底部。首帧未渲染
        # （render_info=None）视为在底 → 初始跟随。pt 渲染后会把 vertical_scroll
        # 夹到合法上限，所以这里读到的是夹过的真实值，判断得准。
        info = output_window.render_info
        if info is None:
            return True
        top = max(0, info.content_height - info.window_height)
        return output_window.vertical_scroll >= top - 1

    def _on_change() -> None:
        # 只有用户还停在底部时才跟随尾部跳到底；一旦手动上滚（PgUp/Home/
        # 滚轮）离开底部，流式帧就不再把他拽回去，能安心回看历史。滚回底部
        # 后（PgDn/End 或滚到底）自动恢复跟随。
        if _at_bottom():
            output_window.vertical_scroll = 10 ** 9
        app.invalidate()

    screen.on_change = _on_change
    try:
        hooks.banner()
    except Exception:
        pass
    try:
        await app.run_async()
    finally:
        screen.on_change = lambda: None
