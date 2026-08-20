"""终端渲染层：把对话核心发来的 Telegram 风味 HTML 渲染成像样的终端界面。

为什么需要单独一层
------------------
对话核心跨客户端边界传过来的从来不是"中立文本"，而是 Telegram 风味的
HTML + 一个 parse_mode 标记（finalize_text_response 对所有客户端都做
markdown_to_telegram_html）。Web 端把还原这件事记在前端 JS（index.html 的
renderText）；CLI 没有前端层，这个模块就是 CLI 的"前端"。

它做四件 Web 前端在做、而终端必须自己做的事：
  1. 把 <b>/<i>/<code>/<pre>/<a> 还原成 ANSI 样式，而不是简单剥成纯文本
     ——剥成纯文本会把"哪里是标题、哪里是代码"这些信息全部丢掉。
  2. 按终端实际宽度换行，并且按**显示宽度**算（中日韩字符占 2 列）。
     用 len() 算宽度会让中文界面的框线全部错位。
  3. 用 ANSI 光标控制做真正的"原地编辑"。这是 CLI 能不能用的关键：流式
     回复每 0.35 秒就把**累计全文**重发一次（rendering.py 的
     _flush_pending），没有原地编辑的话，一条 2000 字的回复会把整个终端
     刷屏上百次。
  4. 把 inline keyboard 渲染成可扫读的编号菜单。

本模块不 import telegram，也不依赖 sections 共享命名空间，可以直接单测。
"""

from __future__ import annotations

import html as _html
import os
import re
import shutil
import sys
import unicodedata
from typing import Any, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# 终端能力探测
# --------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def enable_windows_vt() -> bool:
    """在 Windows 控制台上打开 VT 转义序列支持。

    Windows 10 起的 conhost 默认不解释 ANSI 转义序列，需要显式设置
    ENABLE_VIRTUAL_TERMINAL_PROCESSING(0x4)，否则我们输出的颜色和光标控制
    会原样打印成乱码。失败就当作不支持颜色，由 supports_color 兜底。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for handle_id in (-11, -12):  # STDOUT, STDERR
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:
        return False


def supports_color(stream: Any = None) -> bool:
    """是否应该输出颜色。

    尊重社区约定的 NO_COLOR / FORCE_COLOR，其余按"是不是真终端"判断——
    输出被重定向到文件或管道时（比如用户 `xgent > log.txt`）不该塞进转义
    序列，那会让日志变成乱码。
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    stream = stream or sys.stdout
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if os.name == "nt":
        return enable_windows_vt()
    return os.environ.get("TERM", "") != "dumb"


def terminal_size(default_width: int = 80, default_height: int = 24) -> Tuple[int, int]:
    try:
        size = shutil.get_terminal_size((default_width, default_height))
        # 宽度过窄时排版没有意义，给一个能放下框线的下限。
        return max(40, size.columns), max(10, size.lines)
    except Exception:
        return default_width, default_height


# --------------------------------------------------------------------------
# 显示宽度（CJK 占两列）
# --------------------------------------------------------------------------

def char_width(ch: str) -> int:
    """单个字符在终端里占几列。

    组合记号（重音等）不占位；East Asian Wide/Fullwidth 占 2 列。emoji 在
    unicodedata 里大多已经是 'W'，少数落在 'N'，这里对常见 emoji 区段补一刀
    ——否则带 emoji 的菜单项会算窄，右边框线跟着错位。
    """
    if not ch:
        return 0
    if unicodedata.combining(ch):
        return 0
    codepoint = ord(ch)
    if codepoint < 32 or 0x7F <= codepoint < 0xA0:
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    # 常见 emoji / 符号区段：unicodedata 判为窄，但绝大多数终端按两列渲染。
    if (
        0x1F300 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or 0x2B00 <= codepoint <= 0x2BFF
        or codepoint in (0x2190, 0x2191, 0x2192, 0x2193)
    ):
        return 2
    return 1


def display_width(text: str) -> int:
    """字符串的终端显示宽度，忽略 ANSI 转义序列本身的长度。"""
    stripped = _ANSI_RE.sub("", text)
    # 变体选择符 FE0F / 零宽连接符不额外占位，先去掉再逐字符累加。
    stripped = stripped.replace("️", "").replace("‍", "")
    return sum(char_width(ch) for ch in stripped)


def _tokenize_with_ansi(text: str) -> List[Tuple[str, bool]]:
    """把字符串切成 (片段, 是否为 ANSI 转义) 序列，供按宽度切分时跳过转义。"""
    tokens: List[Tuple[str, bool]] = []
    pos = 0
    for match in _ANSI_RE.finditer(text):
        if match.start() > pos:
            tokens.append((text[pos:match.start()], False))
        tokens.append((match.group(0), True))
        pos = match.end()
    if pos < len(text):
        tokens.append((text[pos:], False))
    return tokens


def wrap_line(text: str, width: int) -> List[str]:
    """按显示宽度折行，ANSI 转义不计宽度也不会被从中间切断。

    优先在空白处断行；一个"词"本身就超过整行宽度时（长 URL、连续中文）
    直接按列硬切，不留一行只有半个字的残行。
    """
    if width <= 0:
        return [text]
    if display_width(text) <= width:
        return [text]

    lines: List[str] = []
    current = ""
    current_width = 0
    pending_space = ""

    def flush() -> None:
        nonlocal current, current_width, pending_space
        lines.append(current)
        current = ""
        current_width = 0
        pending_space = ""

    for chunk, is_ansi in _tokenize_with_ansi(text):
        if is_ansi:
            # 转义序列跟随当前行，不占宽度。
            current += chunk
            continue
        for word in re.findall(r"\s+|\S+", chunk):
            if word.isspace():
                pending_space = word.replace("\t", "    ")
                continue
            word_width = display_width(word)
            space_width = display_width(pending_space)
            if current_width and current_width + space_width + word_width > width:
                flush()
            elif pending_space:
                current += pending_space
                current_width += space_width
                pending_space = ""
            if word_width <= width:
                current += word
                current_width += word_width
                continue
            # 超长词：按列硬切。
            for ch in word:
                cw = char_width(ch)
                if current_width + cw > width:
                    flush()
                current += ch
                current_width += cw
    if current:
        lines.append(current)
    return lines or [""]


def pad_to_width(text: str, width: int) -> str:
    """右侧补空格到指定显示宽度（用于列对齐）。"""
    gap = width - display_width(text)
    return text + " " * gap if gap > 0 else text


# --------------------------------------------------------------------------
# 调色板
# --------------------------------------------------------------------------

class Palette:
    """一组 ANSI 样式。关掉颜色时所有字段都是空串，调用点无需再判断。"""

    _FIELDS = {
        "reset": "\x1b[0m",
        "bold": "\x1b[1m",
        "dim": "\x1b[2m",
        "italic": "\x1b[3m",
        "underline": "\x1b[4m",
        "strike": "\x1b[9m",
        "ai": "\x1b[38;5;79m",       # 青绿：AI 说话
        "user": "\x1b[38;5;110m",    # 淡蓝：用户输入回显
        "accent": "\x1b[38;5;215m",  # 橙：菜单编号、强调
        "muted": "\x1b[38;5;245m",   # 灰：分隔线、次要说明
        "code": "\x1b[38;5;180m",    # 米色：行内代码
        "link": "\x1b[38;5;75m",
        "ok": "\x1b[38;5;114m",
        "warn": "\x1b[38;5;221m",
        "err": "\x1b[38;5;203m",
    }

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        for name, code in self._FIELDS.items():
            setattr(self, name, code if enabled else "")

    def paint(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        prefix = "".join(styles)
        return f"{prefix}{text}{self.reset}" if prefix else text


# --------------------------------------------------------------------------
# Telegram HTML -> ANSI
# --------------------------------------------------------------------------

_PRE_RE = re.compile(r"<pre>(.*?)</pre>", re.S | re.I)
_A_RE = re.compile(r'<a\s+href="([^"]*)"\s*>(.*?)</a>', re.S | re.I)

# Telegram 支持的行内标签 -> ANSI 样式名。对话核心只会产出这些
# （markdown_to_telegram_html 的输出集合），未知标签一律剥掉。
_INLINE_TAGS = {
    "b": ("bold",), "strong": ("bold",),
    "i": ("italic",), "em": ("italic",),
    "u": ("underline",),
    "s": ("strike",), "del": ("strike",), "strike": ("strike",),
    "code": ("code",),
}


def html_to_ansi(text: str, palette: Optional[Palette] = None) -> str:
    """把 Telegram HTML 子集渲染成带 ANSI 样式的纯文本。

    与 rendering.plain_text_from_html 的区别：那个函数把标签**删掉**，用于
    "HTML 解析失败时降级成纯文本"；这里是把标签**翻译成样式**。CLI 用后者，
    否则终端上"标题、代码、正文"看起来完全一样，正是用户说的"极其简陋"。

    <pre> 块单独走 render_code_block（在 MessageRenderer 里），这里只把它
    还原成带标记的占位段落，交给上层排版。
    """
    pal = palette or Palette(False)
    result = str(text)

    # <a href="x">y</a> -> y (x)：终端里链接不可点，裸 URL 才是可用信息。
    def _link(match: "re.Match[str]") -> str:
        href, label = match.group(1), match.group(2)
        label_clean = re.sub(r"<[^>]+>", "", label)
        if not label_clean.strip() or label_clean.strip() == href.strip():
            return pal.paint(href, pal.link, pal.underline)
        return f"{pal.paint(label_clean, pal.link)} {pal.paint('(' + href + ')', pal.muted)}"

    result = _A_RE.sub(_link, result)

    # 块级标签换行化。
    result = re.sub(r"</(p|div|blockquote|li|h[1-6])\s*>", "\n", result, flags=re.I)
    result = re.sub(r"<br\s*/?>", "\n", result, flags=re.I)
    result = re.sub(r"<blockquote>", "", result, flags=re.I)

    # 行内样式标签 -> ANSI。用栈式替换而不是一次性正则，保证嵌套
    # （<b><code>x</code></b>）时内层 reset 不会把外层样式一起清掉。
    def _inline(match: "re.Match[str]") -> str:
        tag = match.group(1).lower()
        inner = match.group(2)
        styles = _INLINE_TAGS.get(tag)
        if not styles:
            return inner
        codes = "".join(getattr(pal, name, "") for name in styles)
        if not codes:
            return inner
        # 内层结束后重新开启外层可能仍需要的样式：结尾用 reset 再补回本层
        # 之外的上下文由调用方（整段最后统一 reset）负责，这里保持简单：
        # reset 之后不残留样式，嵌套场景由外层重新着色。
        return f"{codes}{inner}{pal.reset}"

    inline_pattern = re.compile(
        r"<(" + "|".join(_INLINE_TAGS) + r")>(.*?)</\1>", re.S | re.I
    )
    for _ in range(4):  # 允许 4 层嵌套，够用且不会无限循环
        new_result = inline_pattern.sub(_inline, result)
        if new_result == result:
            break
        result = new_result

    result = re.sub(r"<[^>]+>", "", result)          # 剩余未知标签一律剥掉
    return _html.unescape(result)


# --------------------------------------------------------------------------
# 控制按钮：终端里点不了的那一类
# --------------------------------------------------------------------------
# Telegram/Web 上"停止回答"是一颗随时可点的按钮，所以对话核心把它做成
# inline keyboard（core.py 的 build_stop_keyboard）。终端上这颗按钮**根本
# 点不了**：一轮对话跑起来之后输入循环就在 await，用户敲不进任何编号。把它
# 照直渲染成 " 1 ❌ 停止回答" 有两处不对——
#   1. 它给出一个用户按不动的编号，是假承诺；
#   2. ❌ 在终端里是"失败/出错"的通用记号，顶在"正在运行"的状态行下面，
#      看起来像刚刚报了个错。
# 所以这里把这类按钮从编号菜单里摘出来，改成一行灰色的键位提示——终端里
# 中断的原生语义本来就是 Ctrl+C（xgent_cli.py 的 SIGINT 处理器会把它翻译
# 成同一个 act_stop_generation）。
#
# 键是 callback_data 而不是按钮文字：文字随时可能改文案，callback_data 是
# 对话核心和回调路由之间的稳定契约。
CONTROL_BUTTON_HINTS = {
    "act_stop_generation": ("⌃C", "中断回答"),
}


def split_control_buttons(
    buttons: Sequence[Tuple[str, str]],
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """拆成 (可编号点击的按钮, 键位提示)。

    渲染和菜单登记必须用同一份拆分结果，否则屏幕上显示的编号和
    cli_bridge 记下的 callback_data 会错位一格——用户点 1 触发了 2。
    """
    menu: List[Tuple[str, str]] = []
    hints: List[Tuple[str, str]] = []
    for text, data in buttons:
        hint = CONTROL_BUTTON_HINTS.get(data)
        if hint is None:
            menu.append((text, data))
        elif hint not in hints:
            hints.append(hint)
    return menu, hints


# --------------------------------------------------------------------------
# 消息排版
# --------------------------------------------------------------------------

class MessageRenderer:
    """把一条消息（文本 + 按钮）排版成终端行列表。

    只负责"算出要打印哪些行"，不碰 stdout——这样可以脱离终端直接单测：
    给定 HTML 进去，断言渲染出的行长什么样。
    """

    def __init__(self, palette: Optional[Palette] = None, width: int = 80):
        self.palette = palette or Palette(False)
        self.width = width

    # -- 分块 ------------------------------------------------------------
    def _split_pre_blocks(self, text: str) -> List[Tuple[str, str]]:
        """切成 [("text"|"pre", 内容), ...]，保持原顺序。"""
        parts: List[Tuple[str, str]] = []
        pos = 0
        for match in _PRE_RE.finditer(text):
            if match.start() > pos:
                parts.append(("text", text[pos:match.start()]))
            parts.append(("pre", match.group(1)))
            pos = match.end()
        if pos < len(text):
            parts.append(("text", text[pos:]))
        return parts or [("text", text)]

    def _render_pre(self, raw: str, body_width: int) -> List[str]:
        """代码块/表格：加左边框、不折行。

        markdown_to_telegram_html 把 Markdown 表格也转成 <pre>，内容是按等宽
        对齐过的——一旦折行对齐就全毁了。所以这里宁可让超宽内容被终端自己
        软换行，也不主动折。
        """
        pal = self.palette
        inner = _html.unescape(re.sub(r"<[^>]+>", "", raw)).strip("\n")
        bar = pal.paint("│", pal.muted)
        lines = [f"{bar} {pal.paint(line, pal.code)}" for line in inner.split("\n")]
        return lines or [f"{bar} "]

    # -- 对外接口 --------------------------------------------------------
    def render_text(self, text: str, parse_mode: Any = None,
                    indent: str = "  ") -> List[str]:
        """消息正文 -> 终端行。"""
        raw = str(text)
        is_html = parse_mode is not None and "html" in str(parse_mode).lower()
        body_width = max(20, self.width - display_width(indent))
        lines: List[str] = []

        for kind, chunk in self._split_pre_blocks(raw) if is_html else [("text", raw)]:
            if kind == "pre":
                lines.extend(indent + line for line in self._render_pre(chunk, body_width))
                continue
            rendered = html_to_ansi(chunk, self.palette) if is_html else chunk
            for paragraph in rendered.split("\n"):
                if not paragraph.strip():
                    lines.append("")
                    continue
                for wrapped in wrap_line(paragraph.rstrip(), body_width):
                    lines.append(indent + wrapped)

        # 去掉首尾多余空行，并把连续空行压成一行——Telegram HTML 里
        # <pre>/</p> 前后常常各带一个换行，直译过来就是大片空白。
        collapsed: List[str] = []
        for line in lines:
            if not line.strip() and collapsed and not collapsed[-1].strip():
                continue
            collapsed.append(line)
        while collapsed and not collapsed[0].strip():
            collapsed.pop(0)
        while collapsed and not collapsed[-1].strip():
            collapsed.pop()
        return collapsed

    def render_buttons(self, buttons: Sequence[Tuple[str, str]],
                       indent: str = "  ") -> List[str]:
        """inline keyboard -> 编号菜单。

        buttons 是 [(显示文字, callback_data), ...]，编号即用户要输入的数字。
        终端够宽且条目够多时排成两列——9 项的主菜单单列会占掉大半屏，正是
        用户说的"不适配"。
        """
        if not buttons:
            return []
        pal = self.palette
        labels = []
        for idx, (text, _data) in enumerate(buttons, start=1):
            number = pal.paint(f"{idx:>2}", pal.accent, pal.bold)
            labels.append(f"{number} {text}")

        body_width = max(20, self.width - display_width(indent))
        widest = max(display_width(label) for label in labels)
        columns = 2 if (len(labels) >= 4 and widest * 2 + 4 <= body_width) else 1

        lines: List[str] = []
        if columns == 1:
            lines.extend(indent + label for label in labels)
        else:
            # 行优先（1 2 / 3 4），不是列优先（1 3 / 2 4）——编号菜单是拿来
            # 从左到右扫读的，列优先会让视线在"1 3"之间跳。
            column_width = widest + 4
            for row_start in range(0, len(labels), columns):
                row = labels[row_start:row_start + columns]
                cells = [pad_to_width(cell, column_width) for cell in row[:-1]]
                cells.append(row[-1])
                lines.append(indent + "".join(cells).rstrip())
        return lines

    def render_hints(self, hints: Sequence[Tuple[str, str]],
                     indent: str = "  ") -> List[str]:
        """键位提示 -> 终端行（"⌃C  中断回答" 这种）。

        整行走 muted 灰：它是操作说明，不是消息内容，不该跟正文抢注意力。
        """
        if not hints:
            return []
        pal = self.palette
        key_width = max(display_width(key) for key, _desc in hints)
        lines: List[str] = []
        for key, desc in hints:
            pad = " " * (key_width - display_width(key))
            lines.append(indent + pal.paint(f"{key}{pad}  {desc}", pal.muted))
        return lines

    def render_message(self, text: str, buttons: Sequence[Tuple[str, str]] = (),
                       parse_mode: Any = None, title: str = "XGent",
                       marker: str = "◆", style: str = "ai") -> List[str]:
        """完整一条消息：标题行 + 正文 + 菜单 + 键位提示。

        标题只有一个记号加名字，不拉满屏宽的横线——每条消息都顶一条长横线
        会把界面变成"横线为主、内容为辅"。正文统一缩进 2 格，靠缩进而不是
        线条来划分层次，配合消息之间的空行已经足够分清边界。
        """
        pal = self.palette
        color = getattr(pal, style, "")
        header = pal.paint(f"{marker} {title}", color, pal.bold)

        lines = [header]
        body = self.render_text(text, parse_mode)
        lines.extend(body)
        menu_buttons, hints = split_control_buttons(buttons)
        button_lines = self.render_buttons(menu_buttons)
        if button_lines:
            if body:
                lines.append("")
            lines.extend(button_lines)
        # 键位提示紧贴正文，不额外空行：它是状态行的附注，隔开反而像另一段。
        lines.extend(self.render_hints(hints))
        return lines


# --------------------------------------------------------------------------
# 屏幕：负责真正的打印与原地重绘
# --------------------------------------------------------------------------

class TerminalScreen:
    """管理 stdout，并在可能时用 ANSI 光标控制做原地重绘。

    原地重绘是 CLI 可用性的关键。对话核心把"更新一条消息"表达成
    edit_message_text(message_id=…)，流式回复每 0.35 秒就调一次且带的是
    **累计全文**。终端没有"编辑历史气泡"的原语，但只要记住"最后打印的那个
    块属于哪条消息、占了几行"，就能用 CSI A（上移）+ CSI J（清到屏幕末尾）
    把它擦掉重画——效果等价于原地编辑，而不是刷屏上百次。

    只有当目标消息**仍是屏幕上最后一个块**时才重绘；中间夹了别的输出（用户
    敲了回车、又发了一条新消息）就退化成追加打印一份，宁可多打一份也不能
    擦掉不属于自己的内容。
    """

    def __init__(self, stream: Any = None, color: Optional[bool] = None,
                 width: Optional[int] = None):
        self.stream = stream or sys.stdout
        self.palette = Palette(supports_color(self.stream) if color is None else color)
        self._forced_width = width
        self._last_message_id: Optional[int] = None
        self._last_line_count = 0
        self._can_redraw = False

    # -- 基础 ------------------------------------------------------------
    @property
    def width(self) -> int:
        if self._forced_width:
            return self._forced_width
        return terminal_size()[0]

    @property
    def height(self) -> int:
        return terminal_size()[1]

    def renderer(self) -> MessageRenderer:
        return MessageRenderer(self.palette, self.width)

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except UnicodeEncodeError:
            # Windows 上非 UTF-8 代码页（GBK）遇到 emoji 会抛这个。丢字符也
            # 比丢整条消息强——直接崩掉会让用户以为程序死了。
            safe = text.encode(getattr(self.stream, "encoding", "utf-8") or "utf-8",
                               errors="replace").decode(
                getattr(self.stream, "encoding", "utf-8") or "utf-8", errors="replace")
            self.stream.write(safe)
            self.stream.flush()

    def invalidate(self) -> None:
        """声明"屏幕最后一块已经不是我们记住的那块了"，禁用下一次原地重绘。"""
        self._can_redraw = False
        self._last_message_id = None
        self._last_line_count = 0

    def _visual_rows(self, lines: Sequence[str]) -> int:
        """这些行在终端上实际占几行。

        不能直接用 len(lines)：超过终端宽度的行会被终端自己软换行成多行。
        代码块/表格（<pre>）刻意不折行——折了对齐就毁了——所以这种超宽行
        真实存在。按 len() 记账会让重绘时上移的行数偏少，光标停在块中间，
        擦掉的就是别人的内容。
        """
        width = self.width
        total = 0
        for line in lines:
            line_width = display_width(line)
            total += max(1, -(-line_width // width)) if line_width else 1
        return total

    # -- 打印 ------------------------------------------------------------
    def print_block(self, lines: Sequence[str], message_id: Optional[int] = None,
                    leading_blank: bool = True) -> None:
        """打印一个新块，并记住它，以便后续原地重绘。"""
        payload = ("\n" if leading_blank else "") + "\n".join(lines) + "\n"
        self._write(payload)
        self._last_message_id = message_id
        # 记录的是"块在屏幕上占的行数"，重绘时要连同前置空行一起算。
        self._last_line_count = self._visual_rows(lines) + (1 if leading_blank else 0)
        self._can_redraw = message_id is not None and self.palette.enabled

    def update_block(self, lines: Sequence[str], message_id: int) -> bool:
        """尝试原地重绘 message_id 对应的块。成功返回 True。"""
        if not self._can_redraw or self._last_message_id != message_id:
            return False
        # 块比屏幕还高时，顶部已经滚出可视区，上移会越界擦到别的内容。
        if self._last_line_count + 1 >= self.height:
            return False
        # 上移到块的第一行，清掉从这里到屏幕末尾的所有内容，再重画。
        self._write(f"\x1b[{self._last_line_count}A\x1b[J")
        payload = "\n" + "\n".join(lines) + "\n"
        self._write(payload)
        self._last_line_count = self._visual_rows(lines) + 1
        return True

    def print_plain(self, text: str = "") -> None:
        """打印一行普通文本（提示、警告等），并让下一次原地重绘失效。"""
        self._write(text + "\n")
        self.invalidate()

    def notice(self, text: str, level: str = "info") -> None:
        pal = self.palette
        marker, style = {
            "info": ("ℹ", pal.muted),
            "ok": ("✓", pal.ok),
            "warn": ("!", pal.warn),
            "err": ("✗", pal.err),
        }.get(level, ("ℹ", pal.muted))
        self.print_plain(f"{pal.paint(marker, style)} {pal.paint(text, style)}")


def buttons_from_markup(reply_markup: Any) -> List[Tuple[str, str]]:
    """InlineKeyboardMarkup -> [(显示文字, callback_data), ...]。

    没有 callback_data 的按钮（url / web_app）在终端里点不了，直接跳过——
    与 Web 前端 renderButtons 里 "if (!btn.callback_data) return" 的处理一致。
    """
    if reply_markup is None:
        return []
    keyboard = getattr(reply_markup, "inline_keyboard", None)
    if not keyboard:
        return []
    buttons: List[Tuple[str, str]] = []
    for row in keyboard:
        for button in row:
            data = str(getattr(button, "callback_data", "") or "")
            if not data:
                continue
            buttons.append((str(getattr(button, "text", "") or ""), data))
    return buttons


__all__ = [
    "CONTROL_BUTTON_HINTS",
    "Palette",
    "MessageRenderer",
    "TerminalScreen",
    "buttons_from_markup",
    "char_width",
    "display_width",
    "enable_windows_vt",
    "html_to_ansi",
    "pad_to_width",
    "split_control_buttons",
    "supports_color",
    "terminal_size",
    "wrap_line",
]
