"""Execution boundary for interactive Agent shell protocols."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, TypedDict


SHELL_PROTOCOL_TYPES = frozenset({"shell", "stdin", "shellread", "shellkill"})


class ShellExecution(TypedDict):
    result: dict[str, Any]
    session_id: str
    command: str
    output: str


async def execute_shell_protocol(
    block: Mapping[str, Any],
    *,
    shell_manager: Any,
    executor: Any,
    stop_event_factory: Callable[[], Any],
    stop_requested: Callable[[], bool],
) -> Optional[ShellExecution]:
    """Run one shell protocol while keeping Telegram concerns outside."""
    block_type = str(block.get("type") or "")
    if block_type not in SHELL_PROTOCOL_TYPES:
        return None

    if block_type == "shell":
        shell_result = await shell_manager.start(
            block["body"], stop_event_factory()
        )
    elif block_type == "stdin":
        try:
            macro_steps = executor.parse_stdin_macro(block["body"])
        except Exception as exc:
            shell_result = {
                "success": False,
                "session_id": block.get("path"),
                "output": f"解析 stdin 宏语法失败: {str(exc)[:200]}",
                "return_code": -1,
                "status": "parse_error",
            }
        else:
            shell_result = await shell_manager.send_input(
                block["path"], macro_steps, stop_event_factory()
            )
    elif block_type == "shellread":
        shell_result = await shell_manager.read(
            block["path"], stop_event_factory()
        )
    else:
        shell_result = await shell_manager.kill(block["path"])

    session_id = shell_result.get("session_id") or block.get("path") or "无"
    if stop_requested() and shell_result.get("running") and session_id != "无":
        await shell_manager.kill(str(session_id))
        shell_result["running"] = False
        shell_result["status"] = "stopped"
        shell_result["output"] = (
            shell_result.get("output") or ""
        ) + "\n⏹️ 会话已随当前回合停止而关闭。"

    command = shell_result.get("command") or block.get("body") or ""
    output = shell_result.get("output") or "(无输出)"
    return {
        "result": shell_result,
        "session_id": str(session_id),
        "command": str(command),
        "output": str(output),
    }
