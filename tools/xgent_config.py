"""install.sh 读写 Web 配置的唯一入口。

install.sh 里曾经有五段内联 Python 各自抄了一遍下面 _load() 的内容——同一段
15 行 bootstrap、同一段"把加载期日志丢进黑洞"的注释，改一处就得记得改另外四处。
这里收成一个带子命令的脚本，shell 侧只留一个 run_config_py 包装。

约定：只有真正要回给 shell 的结果才 print 到 stdout，其余一律 stderr。
shell 用 $(...) 取结果，stdout 混进日志就是脏值。
"""

import asyncio
import contextlib
import io
import os
import socket
import sys
import time
from typing import Any, Dict


def _probe_port(raw: str) -> int:
    """127.0.0.1 的这个端口上有没有人在听。0=在听 1=确定没人听 2=没探成。

    刻意不走 _load()：这里只要 stdlib 的 socket，用不着数据库和 section，也就没
    机会让加载期日志污染 stdout（见下面 _load 的注释），顺带省掉一两秒。

    为什么用连接而不是 ss / netstat：Android 不给普通应用（含 Termux）读
    /proc/net/tcp，那两个工具在那儿要么 Permission denied、要么只吐一行表头，
    于是"查不了"会被误读成"没监听"。连接是内核层面的事，那条限制管不着它——
    connect 拿到 ECONNREFUSED 才是"确实没人在听"的铁证，而这个证据在 Android
    上同样成立。ECONNREFUSED 单独接、其余 OSError 一律归 2：按 errno 判，不去
    嗅错误文案，也就不吃语言环境的亏。

    只探 127.0.0.1：Web 服务固定绑 DEFAULT_WEB_HOST，不会只监听 ::1。
    """
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        return 2
    if not 1 <= port <= 65535:
        return 2

    # 连不上时重试两次。刚重启完会有零点几秒的空窗：进程已经在跑、端口还没 bind
    # 上，那正是黄色误报最爱的地方。
    for attempt in range(3):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return 0
        except ConnectionRefusedError:
            pass
        except OSError:
            return 2
        if attempt < 2:
            time.sleep(0.2)
    return 1


def _load() -> Dict[str, Any]:
    """执行全部 section，返回它们共享的命名空间。

    load_sections() 会触发 core.py 顶层的加载日志（"加载提示词: ..." 等）直接写
    stdout。调用方靠 stdout 取结果，噪音混进去会让 shell 变量捕获到脏值——这条
    bug 在人工测试里现过形：端口检查捕获出一整段日志 + 数字粘在一起。所以整个
    加载期都套上 redirect_stdout。
    """
    sys.path.insert(0, os.path.abspath("."))
    # 仅 Web 模式下 .env 里可能还没有真实 ID，而 section 加载期会读它。
    os.environ.setdefault("AUTHORIZED_USER_ID", "1")

    from xgent_app.bootstrap import load_sections

    ns: Dict[str, Any] = {"__file__": os.path.abspath("xgent_server.py")}
    with contextlib.redirect_stdout(io.StringIO()):
        load_sections(ns)
    return ns


async def _close(ns: Dict[str, Any]) -> None:
    """显式关掉数据库连接。

    aiosqlite 起的是非 daemon 工作线程，不关的话这种一次性脚本跑完不会退出，
    调用方就卡在 $(...) 上等一个永远不来的 EOF。
    """
    await (await ns["BotMemoryDB"].get_instance()).close()


async def _get_web_state(ns: Dict[str, Any]) -> None:
    await ns["UserDataManager"].init()
    digest = await ns["read_web_password_hash"]()
    port = ns["normalize_web_port"](
        ns["UserDataManager"].get("web_port", ns["DEFAULT_WEB_PORT"])
    )
    enabled = ns["normalize_bool"](
        ns["UserDataManager"].get("web_enabled", False), False
    )
    print(f"password={'yes' if digest else 'no'}")
    print(f"port={port}")
    print(f"enabled={'yes' if enabled else 'no'}")


async def _set_password(ns: Dict[str, Any]) -> None:
    await ns["UserDataManager"].init()
    # 密码走环境变量而不是 argv：argv 在同机任何用户的 ps 里都看得见。
    await ns["persist_web_password"](os.environ.get("XGENT_WEB_PASSWORD", ""))
    print("ok")


async def _get_port(ns: Dict[str, Any]) -> None:
    await ns["UserDataManager"].init()
    print(ns["normalize_web_port"](
        ns["UserDataManager"].get("web_port", ns["DEFAULT_WEB_PORT"])
    ))


async def _set_port(ns: Dict[str, Any], raw: str) -> int:
    await ns["UserDataManager"].init()
    try:
        port = ns["parse_web_port"](raw)
    except ValueError:
        print(f"端口必须是 {ns['MIN_WEB_PORT']}-{ns['MAX_WEB_PORT']} 之间的整数")
        return 1
    ns["UserDataManager"].set("web_port", port)
    await ns["UserDataManager"].save_config("web_port", port)
    print(port)
    return 0


async def _set_web_enabled(ns: Dict[str, Any], want: bool) -> None:
    await ns["UserDataManager"].init()
    ns["UserDataManager"].set("web_enabled", want)
    await ns["UserDataManager"].save_config("web_enabled", want)


async def _dispatch(command: str, args: list) -> int:
    ns = _load()
    try:
        if command == "get-web-state":
            await _get_web_state(ns)
        elif command == "set-password":
            await _set_password(ns)
        elif command == "get-port":
            await _get_port(ns)
        elif command == "set-port":
            return await _set_port(ns, args[0] if args else "")
        elif command == "set-web-enabled":
            await _set_web_enabled(ns, bool(args) and args[0] == "1")
        else:
            print(f"未知子命令: {command}", file=sys.stderr)
            return 2
        return 0
    finally:
        await _close(ns)


def main(argv: list) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        print(
            "用法: xgent_config.py "
            "{get-web-state|set-password|get-port|set-port <值>|set-web-enabled 0|1"
            "|probe-port <端口>}",
            file=sys.stderr,
        )
        return 2
    # probe-port 不读配置、不碰数据库，走一条不经过 _load() 的短路径。
    if argv[0] == "probe-port":
        return _probe_port(argv[1] if len(argv) > 1 else "")
    return asyncio.run(_dispatch(argv[0], argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
