"""Dispatch and execute dependency-light Agent protocols.

Only executor selection, exception normalization, and result normalization live
here.  Telegram delivery, persistence, and Agent-loop control remain separate.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from xgent_app.agent_results import (
    AgentOperationResult,
    failed_result,
    normalize_edit_result,
    normalize_grep_result,
    normalize_read_result,
    normalize_run_result,
)


STANDARD_PROTOCOL_TYPES = frozenset({"read", "edit", "grep", "run"})


async def dispatch_standard_protocol(
    block: Mapping[str, Any],
    *,
    executor: Any,
    provider_api_format: str,
    stop_event_factory: Callable[[], Any],
    logger: Any,
) -> Optional[AgentOperationResult]:
    """Execute one standard protocol or return ``None`` when unsupported."""
    block_type = str(block.get("type") or "")
    if block_type not in STANDARD_PROTOCOL_TYPES:
        return None

    if block_type == "read":
        if block.get("path"):
            read_target = block["path"]
            try:
                raw_result = await executor.read_file_ranged(read_target)
            except Exception as exc:
                logger.error(f"Agent读取路径失败: {read_target} ({exc})")
                notice = f"[read结果] 读取失败: {read_target}。错误: {str(exc)[:200]}"
                raw_result = {
                    "notice": notice,
                    "message": {"role": "user", "content": notice},
                }
        else:
            read_path = block["body"]
            try:
                _, range_part = executor._split_read_range(read_path)
                if range_part:
                    raw_result = await executor.read_file_ranged(read_path)
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
            raw_result = await executor.edit_file(block["body"])
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

    raw_result = await executor.run_command(block["body"], stop_event_factory())
    return normalize_run_result(raw_result)
