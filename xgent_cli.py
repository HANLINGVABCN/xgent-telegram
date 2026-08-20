"""XGent CLI（本地终端）可执行入口。

不需要 HTTP 服务器、不需要密码认证——本地终端场景下"能跑这个进程"本身就
是权限凭证，和 xgent_server.py 里 run_web_only_main() 的定位不同（那个是
给远程浏览器访问设计的，需要密码墙；这里是本地直接跑的进程，没有网络
暴露面）。

这是"验证 process_conversation 抽象接口是否真正解耦 Telegram"这项任务的
第三个客户端样本：Telegram（真实平台）、Web（鸭子类型垫片，web_bridge.py）
之后，这里接入一个心智模型完全不同的客户端——没有消息气泡、没有 inline
keyboard、没有"编辑历史消息"能力的纯文本终端。能不能顺利套进
web_bridge.py 已经验证过的接口形状，就是这次任务要证明或证伪的东西。

用法：
    python xgent_cli.py

交互方式：
  - 直接输入文字：走 process_conversation，等价于 Telegram/Web 里发一条
    普通消息。
  - 输入 /命令（如 /providers）：路由到对应的 cmd_* 处理函数，等价于
    Telegram 里的斜杠命令。
  - 菜单以编号列表形式显示按钮（[1] xxx / [2] xxx），输入编号即可触发，
    等价于点击 inline keyboard 按钮——这一步验证的是 callbacks.py 的
    路由是否真的只依赖 callback_data 字符串匹配，不依赖 Telegram 消息
    对象的其它属性。
  - 输入 exit 或 quit 退出，或 Ctrl+C / Ctrl+D。
"""

from __future__ import annotations

import asyncio
import sys

from xgent_app.bootstrap import (
    load_sections as _load_sections,
    migrate_legacy_runtime_paths as _migrate_legacy_runtime_paths,
)
from xgent_app.cli_bridge import (
    build_cli_callback_objects,
    build_cli_command_objects,
    build_cli_conversation_objects,
    get_last_menu_options,
)

_MIGRATED_RUNTIME_PATHS = _migrate_legacy_runtime_paths()
_ns: dict = {"__file__": __file__}
_SECTION_FILES = _load_sections(_ns)

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

# CLI 可用的 /命令 -> cmd_* 处理函数映射。与 idle.py 的 _WEB_COMMAND_MAP
# 同一套模式：按函数名从共享命名空间里取出 handler，不是重新实现一遍
# 命令逻辑——这正是要验证的地方：如果命令路由真的平台无关，这里应该
# 能直接复用同一批处理函数，不需要为 CLI 另写一份。
_CLI_COMMAND_PAIRS = [
    ("start", "cmd_start"),
    ("config", "cmd_settings_menu"),
    ("update", "cmd_update_system"),
    ("restart", "cmd_restart_system"),
    ("providers", "cmd_providers_menu"),
    ("provider_config", "cmd_provider_config"),
    ("models", "cmd_models_menu"),
    ("chat_model", "cmd_chat_model_menu"),
    ("media_model", "cmd_media_model_menu"),
    ("prompts", "cmd_prompts_menu"),
    ("clear_memory", "cmd_delete_chat"),
    ("depth", "cmd_depth_menu"),
    ("params", "cmd_timeout_menu"),
    ("thinking", "cmd_thinking_menu"),
    ("web", "cmd_web_menu"),
    ("agent", "cmd_toggle_agent"),
    ("blacklist", "cmd_blacklist_menu"),
    ("stream", "cmd_toggle_stream"),
    ("skills", "cmd_skills_menu"),
    ("status", "cmd_show_info"),
    ("export", "cmd_export_all"),
    ("stats", "cmd_token_stats"),
    ("show_chat_info", "cmd_show_info"),
]
_CLI_COMMAND_MAP = {
    name: _ns[fn_name] for name, fn_name in _CLI_COMMAND_PAIRS if fn_name in _ns
}

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
            logger.exception("CLI 状态处理失败")
            print("⚠️ 处理失败，详情已写入日志。")
        return

    try:
        await GlobalRecorder.record_user_message(
            text, MessageType.USER_TEXT, BotConfig.AUTHORIZED_USER_ID
        )
        await process_conversation(update, context, text)
    except Exception:
        logger.exception("CLI 对话失败")
        print("⚠️ 处理失败，详情已写入日志。")


async def _run_command(command: str) -> None:
    """路由 /命令 到对应的 cmd_* 处理函数。对照 idle.py 的 _web_handle_command。"""
    name = command.strip().split(" ", 1)[0].lstrip("/").split("@", 1)[0].lower()
    handler = _CLI_COMMAND_MAP.get(name)
    update, context, _bot = build_cli_command_objects(BotConfig.AUTHORIZED_USER_ID, command)
    try:
        if handler is None:
            print(f"未知命令：{command}（当作普通对话处理）")
            await GlobalRecorder.record_user_message(
                command, MessageType.USER_TEXT, BotConfig.AUTHORIZED_USER_ID,
            )
            await process_conversation(update, context, command)
        else:
            await handler(update, context)
    except Exception:
        logger.exception("CLI 命令失败: %s", command)
        print("⚠️ 处理失败，详情已写入日志。")


async def _run_callback(callback_data: str) -> None:
    """按编号触发的按钮点击等价操作。对照 idle.py 的 _web_handle_callback。"""
    update, context, _bot = build_cli_callback_objects(
        BotConfig.AUTHORIZED_USER_ID, callback_data, 0,
    )
    try:
        await handle_button_click(update, context)
    except Exception:
        logger.exception("CLI 回调失败: %s", callback_data)
        print("⚠️ 处理失败，详情已写入日志。")


async def _init_runtime() -> None:
    await UserDataManager.init()
    await BotMemoryDB.get_instance()


async def _shutdown_runtime() -> None:
    try:
        db = await BotMemoryDB.get_instance()
        await db.close()
    except Exception:
        logger.exception("CLI 关闭数据库失败")


async def _read_line_async() -> str:
    """在不阻塞事件循环的前提下读一行 stdin。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, sys.stdin.readline)


async def _main_loop() -> None:
    print("=" * 50)
    print("XGent CLI (本地终端) ready.")
    print("直接输入文字对话；/命令 触发命令；输入 exit 退出。")
    print("=" * 50)

    while True:
        print("\n> ", end="", flush=True)
        try:
            line = await _read_line_async()
        except (KeyboardInterrupt, EOFError):
            break
        if not line:
            # readline 在 EOF 时返回空字符串（Ctrl+D）。
            break
        text = line.strip()
        if not text:
            continue
        if text.lower() in ("exit", "quit"):
            break

        if text.isdigit():
            options = get_last_menu_options()
            if options:
                idx = int(text) - 1
                if 0 <= idx < len(options):
                    await _run_callback(options[idx])
                    continue
                print(f"⚠️ 编号超出范围（当前菜单共 {len(options)} 项）。")
                continue

        if text.startswith("/"):
            await _run_command(text)
        else:
            await _run_conversation(text)


def main() -> None:
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
    print("\n再见。")


if __name__ == "__main__":
    main()
