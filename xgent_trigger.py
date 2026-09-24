#!/usr/bin/env python3
"""XGent 后台任务 CLI —— 供 AI 在对话中经 run-x 调用。

设计意图：把原本的 trigger-x 协议块降级成一个普通命令行工具。AI 不再需要学
YAML 协议格式，只用它最熟的 shell：

    trigger add --cmd "systemctl status nginx" --after 30s
    trigger add --cmd "tail -f /var/log/app.log" --when READY --repeat
    trigger show
    trigger kill trg_abc123
    trigger kill-all

执行引擎（调度、条件匹配、并发闸门、投递、重启恢复）完全复用
SelfTriggerManager；本文件只是它的一个瘦输入层。

进程模型：本 CLI 是独立进程、不持有调度器，`register_from_fields` 会检测到
`_scheduler is None` 而只把任务落库；持有调度器的 bot 进程在一个 `_pickup_scan`
周期（约 20 秒）内接管执行，结果经 MirrorBot 投递回对话。

对话归属：从环境变量 XGENT_CHAT_ID / XGENT_CONVERSATION_ID 读取（由
AgentExecutor.run_command 在起子进程时注入）。缺失说明不是在对话里经 run-x 调的，
add 会拒绝（否则任务不知道该把结果投递回哪个对话）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import os
import sys

from xgent_app.bootstrap import (
    load_sections as _load_sections,
    migrate_legacy_runtime_paths as _migrate_legacy_runtime_paths,
)

_migrate_legacy_runtime_paths()
_ns: dict = {"__file__": __file__}


def _load_sections_quietly(namespace: dict):
    """加载 section 并挡住加载期打到终端的日志。

    section 加载时 core.setup_logging() 会给 root logger 挂 stdout handler，随后
    各 section 边加载边打 INFO。CLI 的 stdout 是要回灌给模型的结果通道，不能混进
    这些噪声。加载失败时原样吐出缓冲，诊断信息一个字不吞。
    XGENT_CLI_DEBUG=1 时不拦，方便排查。
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


_load_sections_quietly(_ns)
SelfTriggerManager = _ns["SelfTriggerManager"]


def _require_conversation_context() -> tuple[int, str]:
    """从环境变量取对话上下文；缺失即报错退出。"""
    chat_id_raw = os.environ.get("XGENT_CHAT_ID")
    conversation_id = os.environ.get("XGENT_CONVERSATION_ID")
    if not chat_id_raw or not conversation_id:
        print(
            "❌ 缺少对话上下文（XGENT_CHAT_ID / XGENT_CONVERSATION_ID）。\n"
            "trigger 命令只能由 AI 在对话中经 run-x 调用——这样后台任务结果才知道"
            "投递回哪个对话。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        print(f"❌ XGENT_CHAT_ID 不是合法整数: {chat_id_raw}", file=sys.stderr)
        raise SystemExit(2)
    return chat_id, conversation_id


async def _cmd_add(args: argparse.Namespace) -> str:
    chat_id, conversation_id = _require_conversation_context()
    return await SelfTriggerManager.register_from_fields(
        command=args.cmd,
        chat_id=chat_id,
        conversation_id=conversation_id,
        task=args.task,
        after=args.after,
        at=args.at,
        cron=args.cron,
        when=args.when,
        repeat=args.repeat,
        timezone=args.tz,
        origin_user_text=args.task or args.cmd,
    )


async def _cmd_show(_args: argparse.Namespace) -> str:
    return await SelfTriggerManager.format_active_tasks()


async def _cmd_kill(args: argparse.Namespace) -> str:
    return await SelfTriggerManager.cancel(args.task_id)


async def _cmd_kill_all(_args: argparse.Namespace) -> str:
    count = await SelfTriggerManager.cancel_all()
    return f"✅ 已取消全部触发任务，共 {count} 个"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trigger",
        description="XGent 后台任务调度：延迟/定时/周期/条件监控。命令无时长限制。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="登记一个后台任务")
    p_add.add_argument("--cmd", required=True, help="要执行的 shell 命令")
    p_add.add_argument("--task", default=None, help="一句话任务概述（省略则取命令前 80 字）")
    p_add.add_argument("--after", default=None, help="延迟执行：30s/15m/2h/1d/1w")
    p_add.add_argument("--at", default=None, help="定时执行：YYYY-MM-DD HH:MM[:SS]")
    p_add.add_argument("--cron", default=None, help="周期执行：5 字段 cron，如 '0 * * * *'")
    p_add.add_argument("--when", default=None, help="输出条件：字面量/正则(/re/i)/逻辑(A AND B)")
    p_add.add_argument("--repeat", action="store_true", help="条件命中并投递后自动重启监控（需配 --when）")
    p_add.add_argument("--tz", default=None, help="时区，如 Asia/Shanghai（默认服务端配置）")
    p_add.set_defaults(func=_cmd_add)

    p_show = sub.add_parser("show", help="列出活跃任务")
    p_show.set_defaults(func=_cmd_show)

    p_kill = sub.add_parser("kill", help="取消指定任务")
    p_kill.add_argument("task_id", help="任务 ID，如 trg_abc123")
    p_kill.set_defaults(func=_cmd_kill)

    p_kill_all = sub.add_parser("kill-all", help="取消全部任务")
    p_kill_all.set_defaults(func=_cmd_kill_all)

    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(args.func(args))
    except ValueError as exc:
        # _compute_definition 的校验错误（坏 after/cron、repeat 无 when 等）
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    _rc = main()
    # load_sections 会启动常驻后台线程（纯 Web 模式的服务组件等），非守护线程会
    # 让解释器在 main() 返回后仍挂着不退。CLI 是一次性命令，输出已 flush，直接
    # os._exit 强制退出，避免每次调用都挂到 run-x 超时。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_rc)
