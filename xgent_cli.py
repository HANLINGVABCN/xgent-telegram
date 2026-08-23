"""XGent CLI（本地终端）可执行入口。

不需要 HTTP 服务器、不需要密码认证——本地终端场景下"能跑这个进程"本身就
是权限凭证，和 xgent_server.py 里 run_web_only_main() 的定位不同（那个是
给远程浏览器访问设计的，需要密码墙；这里是本地直接跑的进程，没有网络
暴露面）。

这是"验证 process_conversation 抽象接口是否真正解耦 Telegram"这项任务的
第三个客户端样本：Telegram（真实平台）、Web（鸭子类型垫片，web_bridge.py）
之后，这里接入一个心智模型完全不同的客户端——没有消息气泡、没有 inline
keyboard、没有"编辑历史消息"能力的纯文本终端。

用法：
    xgent                 （install.sh 注册的命令）
    python xgent_cli.py   （直接从源码目录跑）

交互方式：
  - 直接输入文字：走 process_conversation，等价于 Telegram/Web 里发一条
    普通消息。
  - 输入 /命令（如 /providers）：路由到对应的 cmd_* 处理函数，与 Telegram
    端共用同一批 handler。敲下 `/` 就会列出全部命令，Tab 补全/继续筛选；
    只敲 `/` 回车会打出带编号的命令面板，接着输编号即可执行。
  - 菜单以编号显示，输入编号即可触发，等价于点击 inline keyboard 按钮。
  - 正在往状态机里输入内容时（添加记忆、批量加黑名单、自定义超时秒数等），
    裸编号会让给状态机当正文；这时用 :1 这样的写法强制点按钮。这条歧义是
    CLI 特有的：Telegram/Web 的"点击"和"打字"是两个独立通道，终端只有一个
    输入通道，必须有一种显式写法把二者分开。
  - AI 正在回答时按 Ctrl+C：等价于点 Telegram 上那颗"停止回答"按钮（终端里
    没法在一轮对话进行中再输入编号，Ctrl+C 才是终端原生的中断语义）。
  - 输入 exit / quit 退出，或在空闲时按 Ctrl+C / Ctrl+D。
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import io
import logging
import os
import queue
import signal
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, List, Optional, Sequence

from xgent_app.bootstrap import (
    load_sections as _load_sections,
    migrate_legacy_runtime_paths as _migrate_legacy_runtime_paths,
)
from xgent_app.cli_bridge import (
    build_cli_callback_objects,
    build_cli_command_objects,
    build_cli_conversation_objects,
    close_relay,
    configure_relay,
    dismiss_menu,
    get_last_menu_labels,
    get_last_menu_options,
    get_screen,
    menu_is_top,
    relay_user_message,
)
from xgent_app.cli_render import display_width
from xgent_app.cli_palette import (
    SlashPalette,
    interactive_supported as palette_supported,
)

_MIGRATED_RUNTIME_PATHS = _migrate_legacy_runtime_paths()
_ns: dict = {"__file__": __file__}


def _load_sections_quietly(namespace: dict):
    """加载 section，并挡住加载期打到终端的日志。

    core.setup_logging() 在 section 执行过程中就给 root logger 挂上了写
    sys.stdout 的 StreamHandler，随后各 section 一边加载一边打 INFO（加载
    提示词、初始化数据库…）。等 main() 再去摘 handler 已经晚了，那十来行
    已经糊在用户还没看到欢迎界面的屏幕上。

    这里在加载期间把 stdout 换成一个丢弃缓冲：StreamHandler 在构造时就抓住
    了当时的 stdout 对象，所以它此后一直写进这个缓冲，正好省得再去摘。
    加载失败时把缓冲原样吐出来——诊断信息一个字都不能吞。
    XGENT_CLI_DEBUG=1 时完全不拦，方便排查启动问题。
    """
    if os.environ.get("XGENT_CLI_DEBUG"):
        return _load_sections(namespace)
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            return _load_sections(namespace)
    except Exception:
        sys.stdout.write(buffer.getvalue())
        raise


_SECTION_FILES = _load_sections_quietly(_ns)

BotConfig = _ns["BotConfig"]
BotState = _ns["BotState"]
MessageType = _ns["MessageType"]
UserDataManager = _ns["UserDataManager"]
BotMemoryDB = _ns["BotMemoryDB"]
GlobalRecorder = _ns["GlobalRecorder"]
process_conversation = _ns["process_conversation"]
handle_button_click = _ns["handle_button_click"]
handle_text_message = _ns["handle_text_message"]
logger = _ns["logger"]

SCREEN = get_screen()
PALETTE = SCREEN.palette

HISTORY_FILE = Path.home() / ".xgent_cli_history"

# 本次 CLI 会话的标识。服务端按它区分不同 CLI 会话的中继流——每个会话一个
# MirrorBot，各自维护"CLI 的 message_id -> 真实 Telegram message_id"映射。
_CLI_SESSION_ID = f"cli-{os.getpid()}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------
# 启动期整理
# --------------------------------------------------------------------------

def _quiet_console_logging() -> None:
    """把日志从终端撤掉，只留文件。

    core.setup_logging() 给 root logger 挂了一个写 sys.stdout 的
    StreamHandler——这对服务端进程是对的（pm2 收集 stdout），但在交互式
    终端里它会把每一条 INFO 直接打进用户正在读的界面中间，界面被冲得七零
    八落。文件 handler（xgent_server.log）保留不动，出问题还能翻日志；
    需要在终端看实时日志时设 XGENT_CLI_DEBUG=1。
    """
    if os.environ.get("XGENT_CLI_DEBUG"):
        return
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            root.removeHandler(handler)


# --------------------------------------------------------------------------
# 终端状态兜底
# --------------------------------------------------------------------------
# 读输入的守护线程在退出那一刻可能还阻塞在 readline 里，而 readline 进
# readline() 时会改 termios（关回显、进 cbreak）。正常返回时它自己会还原，
# 被进程退出打断则不会——留给用户一个不回显的 shell。这里在启动时抄一份
# 原始 termios，退出前无条件写回去，代价是两次系统调用。
_ORIGINAL_TERMIOS: Optional[tuple] = None


def _remember_terminal_state() -> None:
    global _ORIGINAL_TERMIOS
    try:
        import termios

        fd = sys.stdin.fileno()
        _ORIGINAL_TERMIOS = (fd, termios.tcgetattr(fd))
    except Exception:
        # 非 POSIX、非 tty（管道/重定向）都会落到这里，本来就没有要还原的东西。
        _ORIGINAL_TERMIOS = None


def _restore_terminal_state() -> None:
    if not _ORIGINAL_TERMIOS:
        return
    try:
        import termios

        fd, attributes = _ORIGINAL_TERMIOS
        termios.tcsetattr(fd, termios.TCSADRAIN, attributes)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 命令表
# --------------------------------------------------------------------------
# 直接复用 idle.py 已经建好的 _WEB_COMMAND_MAP，而不是在这里再手抄一份
# "命令名 -> cmd_* 函数"的表。main.py 注册 Telegram CommandHandler、idle.py
# 给 Web 建映射，如果 CLI 再抄第三份，三处就只能靠口头约定同步——加一个新
# 命令时必然有人忘掉其中一处。
_CHINESE_ALIASES = {
    # main.py:93-96 用 MessageHandler(filters.Regex(r"^/(?:黑名单|blacklist)…"))
    # 单独注册了这个中文别名，它不在 CommandHandler 列表里，所以也不在
    # _WEB_COMMAND_MAP 里——Web 端和早先的 CLI 都漏了它。
    "黑名单": "blacklist",
}

# CLI 专属命令：只在终端里有意义，不进 Telegram/Web 的 _WEB_COMMAND_MAP
# （那两端没有"本地终端"这个概念，getchat 拉的是本地屏幕输出，不是发消息）。
# 和 cmd_* handler 走同一套接口（update, context）->协程，这样才能复用
# _command_names/_resolve_command/_describe_command 这一整套 Tab 补全、
# `/` 面板、/help 列表逻辑，不用另开一条特判分支。
async def _cmd_getchat(update: Any, context: Any) -> None:
    """/getchat [N]：拉取 Telegram/Web 端的跨端对话（默认 50 条，上限 500）。

    历史上这个命令叫 /sync（还留了 /pull 别名），改名是因为"sync"暗示会
    自动同步，但这里只是单次按需拉取——命令名容易让人误解成"开启同步"。
    """
    args = list(getattr(context, "args", None) or [])
    limit = int(args[0]) if args and args[0].isdigit() else 50
    await _pull_cross_client_history(max(1, min(limit, 500)))


_CLI_LOCAL_COMMANDS = {
    "getchat": _cmd_getchat,
}


def _command_map() -> dict:
    ensure = _ns.get("_ensure_web_command_map")
    if callable(ensure):
        try:
            ensure()
        except Exception:
            logger.debug("构建命令表失败", exc_info=True)
    table = dict(_ns.get("_WEB_COMMAND_MAP") or {})
    table.update(_CLI_LOCAL_COMMANDS)
    return table


def _command_names() -> List[str]:
    return sorted(_command_map())


def _resolve_command(name: str):
    table = _command_map()
    key = _CHINESE_ALIASES.get(name, name)
    return table.get(key)


def _describe_command(name: str) -> str:
    """命令的一句话说明。

    说明文字来自 lifecycle.py 的 TELEGRAM_COMMAND_DESCRIPTIONS——Telegram
    的 /命令 菜单用的就是它，CLI 读同一张表，不在这里抄第二份。
    """
    describe = _ns.get("command_description")
    if not callable(describe):
        return ""
    try:
        return describe(_CHINESE_ALIASES.get(name, name)) or ""
    except Exception:
        return ""


def _matching_commands(prefix: str) -> List[str]:
    prefix = prefix.lower()
    return [name for name in _command_names() if name.startswith(prefix)]


# --------------------------------------------------------------------------
# 界面片段
# --------------------------------------------------------------------------

def _code_version() -> str:
    """当前代码的版本标识：git 短哈希 -> install.sh 写的 .xgent-version -> dev。

    标在 banner 上，一眼就能确认跑的是不是刚拉的新版——排查"功能没生效"
    类问题时，先看版本号再查逻辑。部署目录不是 git 检出（zip 解压部署）
    时 git 取不到，install.sh 会把哈希写进 .xgent-version 兜底。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        import subprocess

        result = subprocess.run(
            ["git", "-C", here, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()[:10]
    except Exception:
        pass
    try:
        version_file = os.path.join(here, ".xgent-version")
        if os.path.isfile(version_file):
            with open(version_file, encoding="utf-8") as handle:
                stamped = handle.read().strip()
            if stamped:
                return stamped[:10]
    except Exception:
        pass
    return "dev"


def _banner() -> None:
    pal = PALETTE
    width = min(SCREEN.width, 72)
    title = "XGent CLI"

    def _box_row(content: str = "") -> str:
        """一行框：│ 内容（按显示宽度补齐，中文按 2 列算）│。"""
        filler = max(1, width - 2 - 1 - display_width(content) - 1)
        return (
            f"{pal.paint('│', pal.muted)} {content}"
            f"{' ' * filler}{pal.paint('│', pal.muted)}"
        )

    SCREEN.print_plain("")
    SCREEN.print_plain(pal.paint("╭" + "─" * (width - 2) + "╮", pal.muted))
    SCREEN.print_plain(_box_row(
        f"{pal.paint('◆', pal.ai, pal.bold)} {pal.paint(title, pal.bold)}"
        f"  {pal.paint('本地终端客户端', pal.muted)}"
        f"  {pal.paint(f'v{_code_version()}', pal.muted)}"
    ))
    SCREEN.print_plain(_box_row())
    hints = [
        ("直接输入文字", "与 AI 对话（自动镜像到 Telegram）"),
        ("/", "列出全部命令，Tab 继续补全"),
        ("/getchat", "拉取 Telegram/Web 端的跨端对话"),
        ("数字", "选择菜单项，输入内容时用 :数字"),
        ("Ctrl+C", "红❯取消输入 / 黄❯关闭菜单 / 绿❯退出"),
    ]
    label_width = max(display_width(label) for label, _ in hints)
    for label, desc in hints:
        pad = " " * (label_width - display_width(label))
        SCREEN.print_plain(_box_row(
            f"{pal.paint(label, pal.accent)}{pad}  {pal.paint(desc, pal.muted)}"
        ))
    SCREEN.print_plain(pal.paint("╰" + "─" * (width - 2) + "╯", pal.muted))
    SCREEN.print_plain("")


def _command_rows(names: Sequence[str], numbered: bool = False) -> List[str]:
    """命令清单 -> 终端行。/help、`/` 面板、Tab 补全列表共用这一份排版。"""
    pal = PALETTE
    if not names:
        return []
    label_width = max(display_width(f"/{name}") for name in names)
    rows: List[str] = []
    for index, name in enumerate(names, start=1):
        label = f"/{name}"
        pad = " " * (label_width - display_width(label))
        prefix = f"{pal.paint(f'{index:>2}', pal.accent, pal.bold)} " if numbered else "   "
        row = f"  {prefix}{pal.paint(label, pal.ai)}{pad}"
        description = _describe_command(name)
        if description:
            row += f"  {pal.paint(description, pal.muted)}"
        rows.append(row)
    return rows


def _palette_rows(names: Sequence[str], selected: int) -> List[str]:
    """`/` 下拉面板的候选行：选中那条加箭头 + 淡蓝高亮。

    复用 _command_rows 的排版（对齐宽度、命令说明），只在前面换一个标记位——
    /help 的清单和面板里的候选长得一样，用户不用学两套。选中行用淡蓝
    （pal.user）加粗：和青绿的 AI 输出、灰色的说明拉开层次，扫一眼就知道
    回车会执行哪条。
    """
    pal = PALETTE
    if not names:
        return []
    label_width = max(display_width(f"/{name}") for name in names)
    rows: List[str] = []
    for index, name in enumerate(names):
        label = f"/{name}"
        pad = " " * (label_width - display_width(label))
        description = _describe_command(name)
        if index == selected:
            body = f" ❯ {label}{pad}"
            if description:
                body += f"  {description}"
            rows.append(pal.paint(body, pal.user, pal.bold))
        else:
            body = f"   {pal.paint(label, pal.ai)}{pad}"
            if description:
                body += f"  {pal.paint(description, pal.muted)}"
            rows.append(body)
    return rows


def _input_state_active() -> bool:
    """对话核心的状态机是否正等着用户输入（API Key、轮数、密码这类）。"""
    try:
        return UserDataManager.get('state') != BotState.IDLE
    except Exception:
        return False


def _prompt_mode() -> str:
    """当前提示符档位：'input' 数值输入 > 'menu' 菜单导航 > 'idle' 空闲。

    终端只有一个输入通道，用户分不清"这行字会进状态机/点按钮还是发给 AI"，
    颜色就是这个模式的显式信号；Ctrl+C 的语义跟着档位走：
    红=取消输入、黄=关闭菜单、绿=退出 CLI。
    """
    if _input_state_active():
        return "input"
    if menu_is_top():
        return "menu"
    return "idle"


def _prompt_color_name() -> str:
    return {"input": "err", "menu": "warn", "idle": "ai"}[_prompt_mode()]


def _prompt_plain() -> str:
    """给命令面板用的提示符：带颜色，但不带 readline 的 \\001..\\002 标记。"""
    pal = PALETTE
    if not pal.enabled:
        return "❯ "
    color = getattr(pal, _prompt_color_name())
    return f"{color}{pal.bold}❯{pal.reset} "


def _palette_enabled() -> bool:
    return palette_supported()


def _history_entries() -> List[str]:
    """把 readline 的历史交给面板，两边翻到的是同一份记录。"""
    if _readline is None:
        return []
    try:
        return [
            _readline.get_history_item(i)
            for i in range(1, _readline.get_current_history_length() + 1)
        ]
    except Exception:
        return []


def _remember_history(line: str) -> None:
    """面板不走 readline，得手动把行喂回去，退出时才存得进历史文件。"""
    if not line.strip() or _readline is None:
        return
    try:
        length = _readline.get_current_history_length()
        if length and _readline.get_history_item(length) == line:
            return
        _readline.add_history(line)
    except Exception:
        logger.debug("写入 CLI 历史失败", exc_info=True)


def _print_help() -> None:
    pal = PALETTE
    SCREEN.print_plain("")
    SCREEN.print_plain(pal.paint("可用命令", pal.bold, pal.ai))
    for row in _command_rows(_command_names()):
        SCREEN.print_plain(row)
    SCREEN.print_plain("")
    SCREEN.print_plain(pal.paint("  /黑名单 是 /blacklist 的中文别名（与 Telegram 端一致）", pal.muted))
    SCREEN.print_plain(pal.paint("  /getchat [N] 可指定拉取条数（默认 50，上限 500）", pal.muted))
    SCREEN.print_plain(pal.paint("  exit / quit 退出", pal.muted))
    SCREEN.print_plain("")


def _show_current_menu() -> None:
    labels = get_last_menu_labels()
    if not labels:
        SCREEN.notice("当前没有可选菜单。", "warn")
        return
    renderer = SCREEN.renderer()
    lines = renderer.render_buttons([(label, "") for label in labels])
    SCREEN.print_block(lines, message_id=None)


# --------------------------------------------------------------------------
# 命令面板（`/` 回车）
# --------------------------------------------------------------------------
# 一次性状态：面板刚打出来的**下一行**输入，裸编号解释成"选第 N 条命令"。
# 只活一轮，所以不会和按钮菜单的编号长期打架——用户刚看完命令面板就敲数字，
# 意图是唯一的。
_palette: List[str] = []


def _show_command_palette(names: Sequence[str], title: str = "可用命令") -> None:
    global _palette
    _palette = list(names)
    pal = PALETTE
    SCREEN.print_plain("")
    SCREEN.print_plain(pal.paint(title, pal.bold, pal.ai))
    for row in _command_rows(_palette, numbered=True):
        SCREEN.print_plain(row)
    SCREEN.print_plain(pal.paint("  输入编号执行，或继续输入 /命令；Tab 可补全。", pal.muted))
    SCREEN.print_plain("")


def _take_palette() -> List[str]:
    """取走并清空一次性面板状态。"""
    global _palette
    names, _palette = _palette, []
    return names


# --------------------------------------------------------------------------
# readline：行编辑、历史、`/` 命令补全
# --------------------------------------------------------------------------
_readline: Any = None
_completion_matches: List[str] = []


def _setup_readline() -> None:
    """开启行编辑、历史记录与 `/` 命令补全。

    没有 readline 时（Windows 上标准库不带）静默跳过：input() 自身仍然可用，
    只是没有方向键翻历史和 Tab 补全，`/` 回车打面板的那条路依然通。
    """
    global _readline
    try:
        import readline
    except Exception:
        return
    _readline = readline

    try:
        if HISTORY_FILE.exists():
            readline.read_history_file(str(HISTORY_FILE))
        readline.set_history_length(1000)
        atexit.register(_save_history)
    except Exception:
        logger.debug("CLI 历史记录初始化失败", exc_info=True)

    _setup_completion(readline)


def _is_libedit(readline_module: Any) -> bool:
    """macOS 自带的是 libedit 伪装成 readline，键位绑定语法完全不同。"""
    return "libedit" in (getattr(readline_module, "__doc__", "") or "")


def _setup_completion(readline_module: Any) -> None:
    libedit = _is_libedit(readline_module)
    try:
        readline_module.set_completer(_command_completer)
        # 只按空白切词，`/` 留在词里：默认分隔符含 `/`，会把 "/providers"
        # 切成空前缀，补全出来的是全部命令而不是按 /pro 过滤后的那几条。
        readline_module.set_completer_delims(" \t\n")
    except Exception:
        logger.debug("CLI 补全初始化失败", exc_info=True)
        return

    try:
        readline_module.set_completion_display_matches_hook(_display_command_matches)
    except Exception:
        # 老版本 / libedit 没有这个钩子，退化成 readline 自带的分栏列表。
        logger.debug("CLI 补全列表钩子不可用", exc_info=True)

    def bind(*lines: str) -> None:
        for line in lines:
            try:
                readline_module.parse_and_bind(line)
            except Exception:
                logger.debug("readline 绑定失败: %s", line, exc_info=True)

    if libedit:
        bind("bind ^I rl_complete")
        return

    bind(
        "tab: complete",
        # 一次 Tab 直接列出候选，而不是先响一声再等第二次 Tab。
        "set show-all-if-ambiguous on",
        # 默认超过 100 条会先问一句 "Display all N possibilities?"，
        # 命令面板要的是直接出列表。
        "set completion-query-items 500",
        # 补全无候选时不要响铃：句子中间打 `/` 是正常输入，不该被"嘟"一声。
        "set bell-style none",
    )

    if os.environ.get("XGENT_CLI_NO_SLASH_HINT"):
        return
    # 敲下 `/` 就自动列出命令，不用再按 Tab——用户要的是 codex / claude code
    # 那种"打出斜杠就弹提示"的手感。readline 没有"插入自己再补全"的内置函数，
    # 只能绑一个宏：\C-q(quoted-insert) 把随后的 `/` 原样插入（关键：走
    # quoted-insert 才不会让 `/` 再次触发本绑定而无限递归），\C-i 就是 Tab。
    # 用 $if mode=emacs 圈起来：vi 模式下 `/` 是搜索，抢掉会毁掉 vi 用户的肌肉记忆。
    bind("$if mode=emacs", r'"/": "\C-q/\C-i"', "$endif")


def _command_completer(text: str, state: int) -> Optional[str]:
    """Tab 补全：只在行首那个词上补 /命令。

    句子中间的路径（"看看 /home/hanling"）不该弹出命令列表，所以用
    get_begidx() 卡住"补全词必须从第 0 列开始"。
    """
    global _completion_matches
    try:
        if state == 0:
            _completion_matches = []
            if not text.startswith("/"):
                return None
            try:
                if _readline is not None and _readline.get_begidx() != 0:
                    return None
            except Exception:
                pass
            prefix = text[1:].lower()
            _completion_matches = [
                f"/{name}" for name in _command_names() if name.startswith(prefix)
            ]
        return _completion_matches[state]
    except IndexError:
        return None
    except Exception:
        logger.debug("CLI 补全失败", exc_info=True)
        return None


def _display_command_matches(substitution: str, matches: Sequence[str],
                             longest_match_length: int) -> None:
    """替换 readline 自带的分栏候选列表，改成带说明的命令清单。

    这个钩子在读输入的那个线程里被 readline 回调，主线程此刻正 await 着，
    stdout 没有第二个写入方，直接写即可。写完必须让 readline 重画提示符和
    当前行——rl_forced_update_display()，也就是 readline 自己列完候选后做的
    同一件事。
    """
    try:
        names = [str(item).lstrip("/") for item in matches]
        names = [name for name in names if name]
        rows = _command_rows(names)
        if not rows:
            return
        sys.stdout.write("\n" + "\n".join(rows) + "\n")
        sys.stdout.flush()
        SCREEN.invalidate()
        try:
            _readline.redisplay()
            sys.stdout.flush()
        except Exception:
            logger.debug("readline 重画失败", exc_info=True)
    except Exception:
        logger.debug("CLI 补全列表渲染失败", exc_info=True)


def _save_history() -> None:
    try:
        import readline

        readline.write_history_file(str(HISTORY_FILE))
    except Exception:
        pass


# --------------------------------------------------------------------------
# 跨端同步：CLI -> Telegram / Web
# --------------------------------------------------------------------------
# 目标：在 CLI 里对话，Telegram 和网页看到的东西**和在 Telegram 里直接对话
# 完全一样**——带停止按钮的占位提示、Agent 每轮的状态行、工具/命令返回卡片、
# 流式编辑、token 用量行，一条不少；唯一的差异是用户自己那句话前面带一个
# "🖥 [CLI]" 来源标识。
#
# 做法是在 **bot 方法层**扇出：对话核心在本进程里跑，它对 CliBot 的每一次调用
# （send_message / edit_message_text / delete_message …）除了画到终端，还按顺序
# 写进 cli_relay_ops 表；服务端的回放器读出来重放到 MirrorBot 上，后者同时打
# 真实 Telegram 和网页 SSE。这正是 Telegram↔网页天然一致的那套机制，CLI 只是
# 接到同一条路上。见 cli_bridge._CliRelay 与 idle._replay_relay_op。
#
# 早先是服务端事后读 global_messages、按消息类型白名单把行重新编成文本再发
# Telegram。那条路必然丢东西：占位消息、中间轮次、命令返回、Agent 状态行都不是
# "一条落库的文本"，重编不出来；丢了占位消息还得另造一条假的"生成中"提示去
# 弥补。改成回放操作流之后那些补丁全部删掉了。
#
# 也不要退回"CLI 进程自己发 Telegram"：那是裸连接逐条 POST Bot API，每条重新
# TCP+TLS 握手，网络差时逐条拖到 30 秒超时（"CLI→bot 同步极慢甚至不同步"的
# 根因），而且发完即忘，CLI 一退未发完的直接被砍（吞消息）。服务端那条 PTB
# 连接池是健康的，交给它。
# 设 XGENT_CLI_NO_TG_MIRROR=1 关掉同步。


# --------------------------------------------------------------------------
# 跨端同步：拉取（/getchat）
# --------------------------------------------------------------------------
# 反方向（Telegram/Web -> CLI）不自动同步：终端里正打着字，别处一条消息
# 突然插进来会把输入行冲乱。按需拉取——/getchat 把数据库里的跨端历史渲染
# 出来，看完继续聊，新消息不会自己冒出来。

def _render_history_rows(rows: Sequence[dict]) -> None:
    """把 get_display_history 的行渲染到终端。独立成函数方便单测。"""
    renderer = SCREEN.renderer()
    pal = PALETTE
    for row in rows:
        role = str(row.get("role") or "")
        content = str(row.get("content") or "")
        msg_type = row.get("msg_type")
        if not content.strip():
            continue
        if role == "user":
            lines = renderer.render_message(
                content, (), None, title="User", marker="❯", style="user",
            )
            SCREEN.print_block(lines, message_id=None)
        elif role == "assistant":
            if msg_type == MessageType.AI_REPLY:
                convert = _ns.get("markdown_to_telegram_html")
                if callable(convert):
                    try:
                        content = convert(content)
                    except Exception:
                        pass
                lines = renderer.render_message(content, (), "HTML")
            else:
                # AGENT_RESULT/媒体回执存库时已是 Telegram HTML
                lines = renderer.render_message(content, (), "HTML")
            SCREEN.print_block(lines, message_id=None)
        else:
            compact = content if len(content) <= 160 else content[:157] + "…"
            SCREEN.notice(compact, "info")


async def _pull_cross_client_history(limit: int) -> None:
    db = await BotMemoryDB.get_instance()
    rows = await db.get_display_history(limit)
    pal = PALETTE
    SCREEN.print_plain("")
    if not rows:
        SCREEN.notice("还没有任何跨端记录。", "info")
        return
    SCREEN.print_plain(
        pal.paint(f"── 跨端历史 · 最近 {len(rows)} 条（/getchat N 可加大，默认 50）──", pal.muted)
    )
    _render_history_rows(rows)
    SCREEN.print_plain(
        pal.paint("── 拉取结束。新消息不会自动出现，需要时再 /getchat ──", pal.muted)
    )
    SCREEN.print_plain("")


def _echo_user_line(text: str) -> None:
    """命令/编号这类输入的回显：单行、user 色。"""
    pal = PALETTE
    SCREEN.print_plain(f"{pal.paint('❯', pal.user, pal.bold)} {pal.paint(text, pal.user)}")


def _print_user_block(text: str) -> None:
    """对话消息的用户块：和 AI 块同款排版（❯ User + 正文，user 色）。

    面板提交后不再自带回显，这里把用户的话作为正式消息块打进回卷——
    AI 回复再怎么原地重绘刷屏，用户消息都在上面留着，和 Web/Telegram
    的左右分栏一个意思。
    """
    lines = SCREEN.renderer().render_message(
        text, (), None, title="User", marker="❯", style="user",
    )
    SCREEN.print_block(lines, message_id=None)


def _echo_submitted(text: str, *, conversation: bool) -> None:
    """按消息类型补回显（面板路径提交时不自带回显）。

    input() 回退路径系统已回显（last_read_native_echo），不重复打印。
    """
    if getattr(_READER, "last_read_native_echo", False):
        return
    if conversation and not _input_state_active():
        _print_user_block(text)
    else:
        _echo_user_line(text)


# --------------------------------------------------------------------------
# 一轮处理
# --------------------------------------------------------------------------

def _report_failure(what: str) -> None:
    logger.exception("CLI %s 失败", what)
    SCREEN.notice(f"{what}失败，详情见 xgent_server.log（或设 XGENT_CLI_DEBUG=1 看实时日志）。", "err")


async def _run_conversation(text: str) -> None:
    """跑一轮完整对话。对照 idle.py 的 _web_run_conversation，但没有
    outbox/turn_end 帧这些 SSE 概念——CLI 是同步等待打印，跑完就是跑完。
    """
    update, context, _bot = build_cli_conversation_objects(BotConfig.AUTHORIZED_USER_ID)

    # 配置状态（设置密码/端口/提示词/Key 等）：走 Telegram 同款状态机，
    # 不当 AI 对话——对照 idle.py 的同一处理。
    state = UserDataManager.get('state')
    if state != BotState.IDLE:
        try:
            update.message.text = text
        except Exception:
            pass
        try:
            await handle_text_message(update, context)
        except Exception:
            _report_failure("状态处理")
        return

    try:
        # 对话文本带 origin=cli-chat 标记：标记这是 CLI 的对话文本（区别于
        # 状态机输入），跨端历史据此识别来源。
        await GlobalRecorder.record_user_message(
            text, MessageType.USER_TEXT, BotConfig.AUTHORIZED_USER_ID,
            metadata={'origin': 'cli-chat'},
        )
        # 把用户自己的这句话送到另外两端。**这是全流程里唯一带来源标识的
        # 地方**（Telegram/网页显示成 "🖥 [CLI]" 加原话）——接下来
        # process_conversation 产生的每一条消息都由 CliBot 逐个中继过去，
        # 与在 Telegram 里直接对话逐条一致，不加任何标记、不加任何额外文案。
        relay_user_message(text)
        await process_conversation(update, context, text)
    except Exception:
        _report_failure("对话")


async def _run_command(command: str) -> None:
    """路由 /命令 到对应的 cmd_* 处理函数。对照 idle.py 的 _web_handle_command。"""
    name = command.strip().split(" ", 1)[0].lstrip("/").split("@", 1)[0].lower()
    handler = _resolve_command(name)
    update, context, _bot = build_cli_command_objects(BotConfig.AUTHORIZED_USER_ID, command)
    try:
        if handler is None:
            SCREEN.notice(f"未知命令 /{name}，当作普通对话发给 AI（/help 看命令列表）。", "warn")
            await GlobalRecorder.record_user_message(
                command, MessageType.USER_TEXT, BotConfig.AUTHORIZED_USER_ID,
                metadata={'origin': 'cli-chat'},
            )
            relay_user_message(command)
            await process_conversation(update, context, command)
        else:
            await handler(update, context)
    except Exception:
        _report_failure(f"命令 /{name}")


async def _run_callback(callback_data: str) -> None:
    """按编号触发的按钮点击等价操作。对照 idle.py 的 _web_handle_callback。"""
    update, context, _bot = build_cli_callback_objects(
        BotConfig.AUTHORIZED_USER_ID, callback_data, 0,
    )
    try:
        await handle_button_click(update, context)
    except Exception:
        _report_failure("按钮")


# --------------------------------------------------------------------------
# 读输入：守护线程 + 事件循环
# --------------------------------------------------------------------------
# 哨兵对象，区分四种"没读到正文"的情况。用独立对象而不是空串：空串是
# **用户直接按回车**的合法输入，早先和 EOF 混为一谈，于是在提示符上按一下
# 回车就会静默退出 CLI。
_EOF = object()     # Ctrl+D / stdin 关闭
_EXIT = object()    # 空闲时按了 Ctrl+C
_CANCEL = object()  # 状态输入中按了 Ctrl+C：取消输入状态，不退出 CLI
_MENU_DISMISS = object()  # 菜单导航中按了 Ctrl+C：关闭菜单，不退出 CLI

# Ctrl+C 取消的跨线程信号。信号处理器在主线程里 set 它，命令面板的等键
# 轮询（SlashPalette.abort_check）看到后抛 KeyboardInterrupt——cbreak 模式
# 下 Ctrl+C 走 SIGINT 而不是输入字节，阻塞中的 os.read 自己醒不过来。
_cancel_event = threading.Event()


class _StdinReader:
    """在守护线程里做阻塞式 input()，把结果交回事件循环。

    为什么不用 loop.run_in_executor(None, input)：默认执行器的工作线程是
    **非守护**线程。用户在提示符上按 Ctrl+C 时它还阻塞在 input() 里，而且
    永远不会返回，于是收尾路上连着两道都要等它——
      1. asyncio.run() 的 loop.shutdown_default_executor()；
      2. 解释器退出时 threading._shutdown() 里的 _python_exit() → t.join()。
    表现就是用户报的那个 bug：打印了"再见"却退不出去，再按一次 Ctrl+C 才
    退，还甩出一段 concurrent.futures/threading 的 traceback。守护线程不
    参与这两道等待，进程说走就走。
    """

    def __init__(self) -> None:
        self._requests: "queue.Queue[tuple]" = queue.Queue()
        self._lock = threading.Lock()
        self._pending: Optional[tuple] = None
        self._thread: Optional[threading.Thread] = None
        # 读取代次号。取消输入状态后，还阻塞在 input() 里的旧读取会在
        # **下一次**读取开始后才返回，代次号对不上就丢弃，否则用户按掉
        # 取消之后随手补敲的那个键会被当成新输入发给 AI。
        self._epoch = 0
        # 最近一次读取是否已由终端/系统回显（input() 路径会）。
        # 主循环据此决定要不要补打回显，避免同一条消息显示两遍。
        self.last_read_native_echo = False

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._serve, name="xgent-cli-stdin", daemon=True,
        )
        self._thread.start()

    def _serve(self) -> None:
        while True:
            prompt, epoch = self._requests.get()
            try:
                value: Any = self._read_one(prompt)
            except EOFError:
                value = _EOF
            except KeyboardInterrupt:
                # 面板的 abort 轮询（状态输入中的 Ctrl+C）从这条路进来。
                # 兑现成 _CANCEL；主循环会再校验一次状态，误触发也只会
                # 被当成空行丢弃，不会把 "cancel" 发给 AI。
                value = _CANCEL
            except Exception:
                logger.debug("CLI 读取输入失败", exc_info=True)
                value = _EOF
            self._resolve(value, epoch)

    def _read_one(self, prompt: str) -> str:
        """读一行。能画面板就画，不能就退回 input()。

        面板要自己算光标列，所以拿的是没有 \\001..\\002 包裹的干净提示符——
        那对标记是给 readline 看的，写进裸终端会变成两个不可见的垃圾字节，
        把列数算歪。
        """
        if not _palette_enabled():
            self.last_read_native_echo = True  # input() 自带终端回显
            return input(prompt)
        pal = PALETTE
        self.last_read_native_echo = False
        try:
            line = SlashPalette(
                _prompt_plain(),
                _matching_commands,
                _palette_rows,
                history=_history_entries(),
                abort_check=_cancel_event.is_set,
                echo_ansi=pal.user if pal.enabled else "",
                echo_on_submit=False,
            ).read_line()
        except (EOFError, KeyboardInterrupt):
            raise
        except Exception:
            # 面板画崩了不能把 CLI 带走：退回 input()，下一轮还会再试。
            logger.debug("CLI 命令面板异常，退回 input()", exc_info=True)
            self.last_read_native_echo = True
            return input(prompt)
        _remember_history(line)
        return line

    def _resolve(self, value: Any, epoch: Optional[int] = None) -> None:
        """把结果投递回事件循环。先摘 pending，保证只兑现一次。

        epoch 不是 None 时只兑现同代次的读取——见 __init__ 里代次号的说明。
        """
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is None:
            return
        future, loop, pending_epoch = pending
        if epoch is not None and epoch != pending_epoch:
            return  # 迟到的旧读取：那一轮已经被 Ctrl+C 兑现掉了

        def _apply() -> None:
            if not future.done():
                future.set_result(value)

        try:
            loop.call_soon_threadsafe(_apply)
        except RuntimeError:
            # 循环已经关了，没人再等这个结果。
            pass

    async def read(self, prompt: str) -> Any:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._lock:
            self._epoch += 1
            epoch = self._epoch
            self._pending = (future, loop, epoch)
        self._ensure_thread()
        self._requests.put((prompt, epoch))
        return await future

    def request_exit(self) -> bool:
        """空闲时的 Ctrl+C：把正等着的那次读取兑现成"退出"。

        返回 False 表示当前没有人在等输入（比如刚好在两次读取之间），
        调用方该退回到抛 KeyboardInterrupt 的老路。
        """
        return self._resolve_current(_EXIT)

    def request_cancel(self) -> bool:
        """状态输入中的 Ctrl+C：把正等着的那次读取兑现成"取消输入状态"。"""
        return self._resolve_current(_CANCEL)

    def request_menu_dismiss(self) -> bool:
        """菜单导航中的 Ctrl+C：把正等着的那次读取兑现成"关闭菜单"。"""
        return self._resolve_current(_MENU_DISMISS)

    def _resolve_current(self, sentinel: Any) -> bool:
        with self._lock:
            has_pending = self._pending is not None
        if not has_pending:
            return False
        self._resolve(sentinel)
        return True


_READER = _StdinReader()


def _prompt_text() -> str:
    """提示符。

    ANSI 序列必须用 \\001..\\002 包起来：readline 靠提示符的显示宽度算光标
    位置，把颜色转义的字节数也算进去的话，方向键翻历史时行内容会画错位。

    配置状态机等待输入时（设置 Key/轮数/密码等）整根变红，菜单导航中黄色，
    空闲青绿——终端只有一个输入通道，用户分不清"这行字会进状态机、点按钮
    还是发给 AI"，颜色就是这个模式的显式信号；Ctrl+C 语义跟着颜色走。
    """
    pal = PALETTE
    if not pal.enabled:
        return "❯ "
    color = getattr(pal, _prompt_color_name())
    return f"\001{color}{pal.bold}\002❯\001{pal.reset}\002 "


# --------------------------------------------------------------------------
# 停止（Ctrl+C）
# --------------------------------------------------------------------------

_turn_active = False


def _request_stop() -> bool:
    """把 Ctrl+C 翻译成"点了停止按钮"。

    读的是共享命名空间里**当前活着**的那个 _stop_generation_event，而不是
    get_or_create_stop_event()：process_conversation 每轮开始新建、结束置
    None（messages.py:1627/1640），凭空造一个只会设到一个没人等的事件上。
    这与 callbacks.py:11-24 处理 act_stop_generation 的写法保持一致。
    """
    event = _ns.get("_stop_generation_event")
    if event is None or event.is_set():
        return False
    try:
        asyncio.get_running_loop().call_soon_threadsafe(event.set)
    except RuntimeError:
        event.set()
    return True


def _install_sigint_handler() -> None:
    def handler(_signum, _frame):
        if _turn_active:
            if _request_stop():
                SCREEN.notice("已请求停止，正在收尾…（再按一次 Ctrl+C 强制退出）", "warn")
                return
            raise KeyboardInterrupt
        # 红 ❯（状态输入中）：Ctrl+C = 取消输入状态。/start 里的数值输入、
        # API Key、轮数这类状态机在 Telegram 上有 "发送 cancel" 这条官方
        # 出口，终端的原生等价物就是 Ctrl+C。先叫醒阻塞在等键里的面板
        # （abort 轮询），再兑现等待中的读取；两条路谁先到都只兑现一次。
        if _input_state_active():
            _cancel_event.set()
            if _READER.request_cancel():
                return
            _cancel_event.clear()
        # 黄 ❯（菜单导航中）：Ctrl+C = 关闭菜单。用户在 /start 一层层菜单里
        # 时并没有被困住（随时可以打字聊 AI），但"按 Ctrl+C 想退出菜单却
        # 直接退出了 CLI"才是符合直觉的反例——菜单场景拦截一次退出。
        if menu_is_top():
            _cancel_event.set()
            if _READER.request_menu_dismiss():
                return
            _cancel_event.clear()
        # 绿 ❯（空闲）：兑现正在等待的那次读取，让主循环走正常的收尾路径。
        # 直接抛 KeyboardInterrupt 会打断事件循环本身，收尾要靠 asyncio.run()
        # 的异常路径，那条路上还得等一堆线程——正是这个 bug 的来源。
        if _READER.request_exit():
            return
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):
        # 非主线程或平台不支持：退化成默认行为（Ctrl+C 直接中断）。
        logger.debug("无法安装 SIGINT 处理器", exc_info=True)


async def _run_turn(coro) -> None:
    """跑一轮，期间允许 Ctrl+C 转成停止请求而不是杀掉进程。"""
    global _turn_active
    _turn_active = True
    task = asyncio.ensure_future(coro)
    try:
        while True:
            try:
                await asyncio.shield(task)
                return
            except KeyboardInterrupt:
                # handler 里已经发过停止请求，这里继续等这一轮自己收尾。
                if not _request_stop():
                    task.cancel()
                    raise
    finally:
        _turn_active = False


# --------------------------------------------------------------------------
# 输入循环
# --------------------------------------------------------------------------

async def _init_runtime() -> None:
    await UserDataManager.init()
    await BotMemoryDB.get_instance()
    # 启动跨端中继：从这里起，对话核心在 CLI 里对 bot 的每一次调用都会被
    # 服务端原样回放到 Telegram 和网页。必须在 BotMemoryDB 之后——中继线程
    # 自己开一条 sqlite 连接，建表要等主库初始化完，免得两边同时建。
    configure_relay(
        BotConfig.DB_FILE,
        BotConfig.AUTHORIZED_USER_ID,
        _CLI_SESSION_ID,
        enabled=not os.environ.get("XGENT_CLI_NO_TG_MIRROR"),
    )


async def _shutdown_runtime() -> None:
    # 先排空中继再关库：本轮最后几条操作还在队列里，直接退出就是"最后几条
    # 消息没同步"（历史上 create_task 发完即忘留下的老毛病）。
    try:
        close_relay()
    except Exception:
        logger.exception("CLI 关闭跨端中继失败")
    try:
        db = await BotMemoryDB.get_instance()
        await db.close()
    except Exception:
        logger.exception("CLI 关闭数据库失败")


async def _main_loop() -> None:
    _banner()

    while True:
        SCREEN.invalidate()  # 提示符一打出来，屏幕最后一块就不再是消息块
        SCREEN.print_plain("")  # 提示符前留一行；不并进提示符是因为 readline
                                # 处理不好含换行的提示符（重绘会错位）
        line = await _READER.read(_prompt_text())
        if line is _EXIT or line is _EOF:
            break
        if line is _MENU_DISMISS:
            # 菜单导航中按了 Ctrl+C：关闭当前菜单（编号不再生效），提示符
            # 恢复空闲色。再按一次 Ctrl+C 才是退出 CLI。
            _cancel_event.clear()
            if dismiss_menu():
                SCREEN.notice("已关闭菜单导航（编号不再生效）。再按 Ctrl+C 退出 CLI；直接输入文字则与 AI 对话。", "info")
            continue
        if line is _CANCEL:
            # 状态输入中按了 Ctrl+C：喂 "cancel" 给状态机——messages.py 的
            # 官方取消路径会清掉 state 和全部 pending buffer 并回主菜单，
            # 提示符随之恢复绿色。状态已经不在（误触发）就当空行丢弃，
            # 绝不能把 "cancel" 当普通对话发给 AI。
            _cancel_event.clear()
            if _input_state_active():
                await _run_turn(_run_conversation("cancel"))
            continue
        text = str(line).strip()
        if not text:
            continue
        low = text.lower()
        if low in ("exit", "quit"):
            break
        if low in ("/help", "help", "?", "/?"):
            _take_palette()
            _echo_submitted(text, conversation=False)
            _print_help()
            continue
        if low in ("/menu", "menu"):
            _take_palette()
            _echo_submitted(text, conversation=False)
            _show_current_menu()
            continue

        # 命令面板刚打出来时，裸编号先解释成"选第 N 条命令"。面板是一次性的：
        # 取走即失效，所以它不会长期挡住按钮菜单的编号。
        palette = _take_palette()
        if palette and text.isdigit():
            number = int(text)
            if 1 <= number <= len(palette):
                _echo_submitted(f"/{palette[number - 1]}", conversation=False)
                await _run_turn(_run_command(f"/{palette[number - 1]}"))
                continue
            SCREEN.notice(f"编号超出范围，命令面板共 {len(palette)} 项。", "warn")
            continue

        # CLI 把"点按钮"和"发消息"挤进了同一个输入通道，这是 Telegram/Web
        # 结构上没有的歧义——那边点击和打字是两个独立物理通道。两种写法：
        #   ":N"  显式按钮，任何状态下都优先解释为点击第 N 项；
        #   "N"   裸编号，只在 IDLE 时当按钮，否则让给状态机。
        # 两条规则缺一不可：只让编号赢，状态机正等着用户输数字时（自定义超时
        # 秒数/记忆深度/Agent 轮数）真正的输入会被当成编号吃掉；只让状态机赢，
        # SET_MEMORY、SET_COMMAND_BLACKLIST 这类"文本一律进缓冲区、只有按钮
        # 能退出"的状态（callbacks.py:201/468 发提示时带 reply_markup）又会把
        # 用户永久困住。":N" 就是那条任何时候都打得开的出口。
        explicit_pick = text.startswith(":") and text[1:].strip().isdigit()
        bare_pick = text.isdigit() and UserDataManager.get('state') == BotState.IDLE
        if explicit_pick or bare_pick:
            options = get_last_menu_options()
            number = int(text[1:].strip() if explicit_pick else text)
            if options and 1 <= number <= len(options):
                _echo_submitted(text, conversation=False)
                await _run_turn(_run_callback(options[number - 1]))
                continue
            if options:
                SCREEN.notice(f"编号超出范围，当前菜单共 {len(options)} 项（输入 /menu 重看）。", "warn")
                continue
            if explicit_pick:
                SCREEN.notice("当前没有可选的菜单。", "warn")
                continue
            # 裸编号但当前没有菜单：当普通文本处理——在 Telegram 里输入
            # "42" 本来就只是发一条消息。

        if text.startswith("/"):
            routed = _route_command_prefix(text)
            if routed is None:
                continue
            _echo_submitted(routed, conversation=False)
            await _run_turn(_run_command(routed))
        else:
            # 对话消息：打印用户块（❯ User + 正文），消息在回卷里永久可见
            _echo_submitted(text, conversation=True)
            await _run_turn(_run_conversation(text))


def _route_command_prefix(text: str) -> Optional[str]:
    """决定 `/…` 这一行到底要执行什么。

    返回要执行的命令行；返回 None 表示"已经在屏幕上给了提示"，主循环该直接
    进入下一轮。

    没装 readline 的终端（Windows）敲不出 Tab 补全，这条路是它唯一的命令
    发现方式；装了 readline 的终端里它同样有用——补全只在打字途中弹，
    回车之后就没了。
    """
    if text == "/":
        _show_command_palette(_command_names())
        return None
    # 带参数的命令（"/stats 7"）交给正常路由，不去猜前缀。
    if " " in text:
        return text
    name = text[1:].lower()
    if _resolve_command(name) is not None:
        return text
    matches = _matching_commands(name)
    if not matches:
        # 一条都不像：保持原行为，交给 _run_command 提示并转发给 AI。
        return text
    if len(matches) == 1:
        # 前缀唯一，直接当成用户打全了——这正是补全的意义。
        SCREEN.notice(f"/{name} → /{matches[0]}", "info")
        return f"/{matches[0]}"
    _show_command_palette(matches, title=f"匹配 /{name} 的命令")
    return None


def main() -> None:
    _quiet_console_logging()
    _remember_terminal_state()
    _setup_readline()
    _install_sigint_handler()

    async def _run():
        await _init_runtime()
        try:
            await _main_loop()
        finally:
            await _shutdown_runtime()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    _restore_terminal_state()
    SCREEN.print_plain("")
    SCREEN.notice("再见。", "ok")


if __name__ == "__main__":
    main()
