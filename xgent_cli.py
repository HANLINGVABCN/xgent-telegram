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
    端共用同一批 handler。输入 /help 看完整命令列表。
  - 菜单以编号显示，输入编号即可触发，等价于点击 inline keyboard 按钮。
  - 正在往状态机里输入内容时（添加记忆、批量加黑名单、自定义超时秒数等），
    裸编号会让给状态机当正文；这时用 :1 这样的写法强制点按钮。这条歧义是
    CLI 特有的：Telegram/Web 的"点击"和"打字"是两个独立通道，终端只有一个
    输入通道，必须有一种显式写法把二者分开。
  - AI 正在回答时按 Ctrl+C：等价于点"❌ 停止回答"按钮（终端里没法在一轮
    对话进行中再输入编号，Ctrl+C 才是终端原生的中断语义）。
  - 输入 exit / quit 退出，或在空闲时按 Ctrl+C / Ctrl+D。
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import functools
import io
import logging
import os
import signal
import sys
from pathlib import Path

from xgent_app.bootstrap import (
    load_sections as _load_sections,
    migrate_legacy_runtime_paths as _migrate_legacy_runtime_paths,
)
from xgent_app.cli_bridge import (
    build_cli_callback_objects,
    build_cli_command_objects,
    build_cli_conversation_objects,
    get_last_menu_labels,
    get_last_menu_options,
    get_screen,
)
from xgent_app.cli_render import display_width

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


def _setup_readline() -> None:
    """开启行编辑与历史记录。

    没有 readline 时（Windows 上标准库不带）静默跳过：input() 自身仍然可用，
    只是没有上下方向键翻历史。为此装不上第三方依赖不值得。
    """
    try:
        import readline  # noqa: F401  (导入即生效)
    except Exception:
        return
    try:
        if HISTORY_FILE.exists():
            readline.read_history_file(str(HISTORY_FILE))
        readline.set_history_length(1000)
        atexit.register(_save_history)
    except Exception:
        logger.debug("CLI 历史记录初始化失败", exc_info=True)


def _save_history() -> None:
    try:
        import readline

        readline.write_history_file(str(HISTORY_FILE))
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


def _command_map() -> dict:
    ensure = _ns.get("_ensure_web_command_map")
    if callable(ensure):
        try:
            ensure()
        except Exception:
            logger.debug("构建命令表失败", exc_info=True)
    return dict(_ns.get("_WEB_COMMAND_MAP") or {})


def _resolve_command(name: str):
    table = _command_map()
    key = _CHINESE_ALIASES.get(name, name)
    return table.get(key)


# --------------------------------------------------------------------------
# 界面片段
# --------------------------------------------------------------------------

def _banner() -> None:
    pal = PALETTE
    width = min(SCREEN.width, 72)
    title = "XGent CLI"
    SCREEN.print_plain("")
    SCREEN.print_plain(pal.paint("─" * width, pal.muted))
    SCREEN.print_plain(
        f"{pal.paint('◆', pal.ai, pal.bold)} {pal.paint(title, pal.bold)}"
        f"  {pal.paint('本地终端客户端', pal.muted)}"
    )
    SCREEN.print_plain(pal.paint("─" * width, pal.muted))
    hints = [
        ("直接输入文字", "与 AI 对话"),
        ("/help", "查看全部命令"),
        ("数字", "选择菜单项，输入内容时用 :数字"),
        ("Ctrl+C", "回答中断，空闲时退出"),
    ]
    label_width = max(display_width(label) for label, _ in hints)
    for label, desc in hints:
        pad = " " * (label_width - display_width(label))
        SCREEN.print_plain(
            f"  {pal.paint(label, pal.accent)}{pad}  {pal.paint(desc, pal.muted)}"
        )
    SCREEN.print_plain("")


def _print_help() -> None:
    pal = PALETTE
    table = _command_map()
    SCREEN.print_plain("")
    SCREEN.print_plain(pal.paint("可用命令", pal.bold, pal.ai))
    names = sorted(table)
    width = max((display_width(n) for n in names), default=0) + 2
    columns = max(1, min(3, SCREEN.width // (width + 4)))
    for index in range(0, len(names), columns):
        row = names[index:index + columns]
        cells = []
        for name in row:
            cell = f"/{name}"
            cells.append(cell + " " * max(0, width + 2 - display_width(cell)))
        SCREEN.print_plain("  " + "".join(cells).rstrip())
    SCREEN.print_plain("")
    SCREEN.print_plain(pal.paint("  /黑名单 是 /blacklist 的中文别名（与 Telegram 端一致）", pal.muted))
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
        await GlobalRecorder.record_user_message(
            text, MessageType.USER_TEXT, BotConfig.AUTHORIZED_USER_ID
        )
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
            )
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
        if _turn_active and _request_stop():
            SCREEN.notice("已请求停止，正在收尾…", "warn")
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


async def _shutdown_runtime() -> None:
    try:
        db = await BotMemoryDB.get_instance()
        await db.close()
    except Exception:
        logger.exception("CLI 关闭数据库失败")


def _read_input(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


async def _read_line_async(prompt: str) -> str:
    """在不阻塞事件循环的前提下读一行输入。

    prompt 交给 input() 自己打印，而不是先 print 再空 input()——readline
    需要知道提示符宽度才能正确重绘行内容，否则按方向键翻历史会把提示符
    一起吃掉。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(_read_input, prompt))


def _prompt_text() -> str:
    pal = PALETTE
    return f"\n{pal.paint('❯', pal.ai, pal.bold)} "


async def _main_loop() -> None:
    _banner()

    while True:
        SCREEN.invalidate()  # 提示符一打出来，屏幕最后一块就不再是消息块
        try:
            line = await _read_line_async(_prompt_text())
        except (KeyboardInterrupt, EOFError):
            break
        if not line:
            # input() 在 EOF（Ctrl+D）时返回空串。
            break
        text = line.strip()
        if not text:
            continue
        low = text.lower()
        if low in ("exit", "quit"):
            break
        if low in ("/help", "help", "?", "/?"):
            _print_help()
            continue
        if low in ("/menu", "menu"):
            _show_current_menu()
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
            await _run_turn(_run_command(text))
        else:
            await _run_turn(_run_conversation(text))


def main() -> None:
    _quiet_console_logging()
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
    SCREEN.print_plain("")
    SCREEN.notice("再见。", "ok")


if __name__ == "__main__":
    main()
