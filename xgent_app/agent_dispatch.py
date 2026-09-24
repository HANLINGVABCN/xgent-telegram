"""Dispatch and execute dependency-light Agent protocols.

Only executor selection, exception normalization, and result normalization live
here.  Telegram delivery, persistence, and Agent-loop control remain separate.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

import asyncio
import shlex

from xgent_app.agent_results import (
    AgentOperationResult,
    failed_result,
    normalize_edit_result,
    normalize_fetch_result,
    normalize_grep_result,
    normalize_intel_result,
    normalize_read_result,
    normalize_run_result,
    normalize_search_result,
)
from xgent_app.agent_search import run_fetch, run_search
from xgent_app.code_intel import INTEL_SUBCOMMANDS, run_intel


STANDARD_PROTOCOL_TYPES = frozenset({"read", "edit", "grep", "run", "search", "fetch", "intel"})

# intel 结果回灌上限：分析输出通常很短，但 map/find-ref 在大仓库可能偏大。
# 截断到这个字符数，避免单次回灌吃掉过多上下文预算；完整信息可再跑更精确的子命令。
_INTEL_OUTPUT_LIMIT = 60000


async def dispatch_standard_protocol(
    block: Mapping[str, Any],
    *,
    executor: Any,
    provider_api_format: str,
    stop_event_factory: Callable[[], Any],
    logger: Any,
    search_api_key: Optional[str] = None,
) -> Optional[AgentOperationResult]:
    """Execute one standard protocol or return ``None`` when unsupported."""
    block_type = str(block.get("type") or "")
    if block_type not in STANDARD_PROTOCOL_TYPES:
        return None

    if block_type == "read":
        # read-x 只在正文写路径（围栏行 read-x:/path 写法已在解析层取消），
        # 故 block['path'] 恒为空——不再保留那条死分支。
        read_path = block["body"]
        try:
            _, range_part = executor._split_read_range(read_path)
            if range_part:
                raw_result = await executor.read_file_ranged(read_path, provider_api_format)
            else:
                raw_result = await executor.read_path_for_model(
                    read_path, provider_api_format
                )
        except Exception as exc:
            logger.error(f"Agent读取路径失败: {read_path} ({exc})")
            notice = f"[read结果] 读取失败: {read_path}。错误: {str(exc)[:200]}"
            raw_result = {
                "notice": notice,
                "message": {"role": "user", "content": notice},
            }
        return normalize_read_result(raw_result)

    if block_type == "edit":
        try:
            raw_result = await executor.edit_file(block["body"], explicit_path=block.get("path", ""))
        except Exception as exc:
            logger.error(f"Agent edit 执行异常: {exc}")
            raw_result = failed_result(
                "edit", f"[edit结果] 执行异常: {str(exc)[:200]}"
            )
        return normalize_edit_result(raw_result)

    if block_type == "grep":
        try:
            raw_result = await executor.grep_search(block["body"])
        except Exception as exc:
            logger.error(f"Agent grep 执行异常: {exc}")
            raw_result = failed_result(
                "grep", f"[grep结果] 执行异常: {str(exc)[:200]}"
            )
        return normalize_grep_result(raw_result)

    if block_type == "search":
        try:
            raw_result = await run_search(block["body"], search_api_key)
        except Exception as exc:
            logger.error(f"Agent search 执行异常: {exc}")
            raw_result = failed_result(
                "search", f"[search结果] 执行异常: {str(exc)[:200]}"
            )
        return normalize_search_result(raw_result)

    if block_type == "fetch":
        try:
            raw_result = await run_fetch(block["body"], search_api_key)
        except Exception as exc:
            logger.error(f"Agent fetch 执行异常: {exc}")
            raw_result = failed_result(
                "fetch", f"[fetch结果] 执行异常: {str(exc)[:200]}"
            )
        return normalize_fetch_result(raw_result)

    if block_type == "intel":
        raw_body = str(block.get("body") or "").strip()
        try:
            argv = shlex.split(raw_body)
        except ValueError as exc:
            return failed_result(
                "intel", f"[intel结果] 参数解析失败（引号不匹配？）: {str(exc)[:200]}"
            )
        if not argv:
            return failed_result(
                "intel",
                "[intel结果] 缺少子命令。可用: " + ", ".join(sorted(INTEL_SUBCOMMANDS)),
            )
        if argv[0] not in INTEL_SUBCOMMANDS:
            return failed_result(
                "intel",
                f"[intel结果] 不支持的子命令 '{argv[0]}'。可用: "
                + ", ".join(sorted(INTEL_SUBCOMMANDS)),
            )
        try:
            # run_intel 是同步 CPU 活（AST 解析、git 调用），丢线程池防阻塞事件循环。
            output = await asyncio.to_thread(run_intel, argv)
        except Exception as exc:
            logger.error(f"Agent intel 执行异常: {exc}")
            return failed_result(
                "intel", f"[intel结果] 执行异常: {str(exc)[:200]}"
            )
        if len(output) > _INTEL_OUTPUT_LIMIT:
            output = (
                output[:_INTEL_OUTPUT_LIMIT]
                + f"\n\n[intel 输出过长，已截断至 {_INTEL_OUTPUT_LIMIT} 字符；"
                "可用更精确的子命令（如 context <符号> / outline <文件>）缩小范围]"
            )
        return normalize_intel_result(
            {"success": True, "output": f"[intel结果] {argv[0]}\n{output}"}
        )

    try:
        raw_result = await executor.run_command(block["body"], stop_event_factory())
    except Exception as exc:
        logger.error(f"Agent run 执行异常: {exc}")
        raw_result = failed_result(
            "run", f"[run结果] 执行异常: {str(exc)[:200]}"
        )
    return normalize_run_result(raw_result)
