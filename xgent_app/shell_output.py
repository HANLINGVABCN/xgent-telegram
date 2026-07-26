"""Shell 结果与上下文输出格式化。

只负责把执行结果变成展示文本或模型上下文，不执行命令、不发送消息。
"""

from typing import Any, Dict, Tuple

from xgent_app.text_utils import clip_middle_text


def build_shell_notice(action_label: str, shell_result: Dict[str, Any],
                       session_id: Any, command: str, output: str) -> str:
    pause_reason = shell_result.get('pause_reason') or ''
    stored_output = format_shell_context_output(output, bool(shell_result.get('running')))
    waited_seconds = shell_result.get('waited_seconds')
    output_chars = shell_result.get('output_chars')
    output_idle_seconds = shell_result.get('output_idle_seconds')
    output_chunks = shell_result.get('output_chunks')
    output_active_seconds = shell_result.get('output_active_seconds')
    recent_output_chunks = shell_result.get('recent_output_chunks')
    recent_output_chars = shell_result.get('recent_output_chars')
    recent_output_span_seconds = shell_result.get('recent_output_span_seconds')
    wait_state = shell_result.get('wait_state') or ''
    wait_state_description = shell_result.get('wait_state_description') or ''
    wait_state_reason = shell_result.get('wait_state_reason') or ''
    wait_state_confidence = shell_result.get('wait_state_confidence') or ''
    elapsed_line = f"本次等待/捕获耗时: {waited_seconds} 秒\n" if waited_seconds is not None else ""
    output_line = ""
    if output_chars is not None or output_idle_seconds is not None or output_chunks is not None:
        output_line = (
            f"输出字符数: {output_chars}\n"
            f"输出块数: {output_chunks}\n"
            f"输出活跃时长: {output_active_seconds} 秒\n"
            f"距今无新输出: {output_idle_seconds} 秒\n"
        )
        if recent_output_chunks is not None or recent_output_chars is not None:
            output_line += (
                f"最近输出块数: {recent_output_chunks}\n"
                f"最近输出字符数: {recent_output_chars}\n"
                f"最近输出跨度: {recent_output_span_seconds} 秒\n"
            )
    state_line = ""
    if wait_state_description or wait_state_reason or wait_state_confidence:
        state_line = (
            f"判定说明: {wait_state_description}\n"
            f"判定依据: {wait_state_reason}\n"
            f"判定置信度: {wait_state_confidence}\n"
        )
    return (
        f"[Agent shell {action_label}]\n"
        f"会话: {session_id}\n"
        f"命令: {command}\n"
        f"长驻预判: {shell_result.get('command_hint_long_running')}\n"
        f"判定状态: {wait_state}\n"
        f"{state_line}"
        f"运行中: {shell_result.get('running')}\n"
        f"暂停原因: {pause_reason}\n"
        f"返回码: {shell_result.get('return_code')}\n"
        f"{elapsed_line}"
        f"{output_line}"
        f"输出:\n{stored_output}"
    )


def get_shell_pause_messages(pause_reason: str) -> Tuple[str, str]:
    mapping = {
        'interactive_prompt': (
            "会话大概率在等待输入；当前输出会交给 AI 继续判断。",
            "⏳ Shell 会话大概率在等待输入，正在交给 AI 继续判断。",
        ),
        'active_output': (
            "会话大概率仍在持续输出；当前输出会交给 AI 继续判断。",
            "⏳ Shell 会话大概率仍在持续输出，正在交给 AI 继续判断。",
        ),
        'output_quiet': (
            "会话输出可能已安静下来，但进程仍在运行；当前输出会交给 AI 继续判断。",
            "⏳ Shell 会话输出可能已安静下来但进程仍在运行，正在交给 AI 继续判断。",
        ),
        'output_stalled': (
            "会话输出疑似停滞一段时间，但进程仍在运行；当前输出会交给 AI 继续判断。",
            "⏳ Shell 会话输出疑似停滞一段时间，正在交给 AI 继续判断。",
        ),
        'silent_running': (
            "会话可能仍在运行，但尚未产生可见输出；当前输出会交给 AI 继续判断。",
            "⏳ Shell 会话可能仍在运行但尚未产生可见输出，正在交给 AI 继续判断。",
        ),
        'long_running_command': (
            "会话看起来大概率是长驻任务；当前输出会交给 AI 继续判断。",
            "⏳ Shell 会话看起来大概率是长驻任务，正在交给 AI 继续判断。",
        ),
        'wait_timeout': (
            "会话超过等待窗口仍可能在运行；当前输出会交给 AI 继续判断。",
            "⏳ Shell 会话超过等待窗口仍可能在运行，正在交给 AI 继续判断。",
        ),
        'read_capture': (
            "已快速读取当前会话输出；结果会交给 AI 继续判断。",
            "⏳ 已快速读取 Shell 会话输出，正在交给 AI 继续判断。",
        ),
        'stopped': (
            "会话已停止；当前结果会交给 AI 继续判断。",
            "⏹️ Shell 会话已停止，正在整理给 AI 的结果。",
        ),
    }
    default = (
        "会话仍在运行；当前输出会交给 AI 继续判断。",
        "⏳ Shell 会话仍在运行，正在交给 AI 继续判断。",
    )
    return mapping.get(pause_reason, default)


def build_run_notice(run_result: Dict[str, Any]) -> str:
    output = str(run_result.get('output') or '(无输出)')
    stored_output = format_shell_context_output(output, running=False)
    return (
        "[Agent run]\n"
        f"命令: {run_result.get('command') or ''}\n"
        f"成功: {run_result.get('success')}\n"
        f"返回码: {run_result.get('return_code')}\n"
        f"超时: {run_result.get('timed_out')}\n"
        f"停止: {run_result.get('stopped')}\n"
        f"耗时: {run_result.get('elapsed_seconds')} 秒\n"
        f"完整输出文件: {run_result.get('output_path')}\n"
        f"完整输出大小: {run_result.get('output_bytes')} bytes\n"
        f"上下文输出:\n{stored_output}"
    )


def format_shell_display_output(output: str, running: bool, limit: int = 1600) -> str:
    if len(output) <= limit:
        return output
    if running:
        return output[-limit:].lstrip() + "\n... (仅显示最新 shell 输出)"
    return clip_middle_text(output, limit, "shell 输出")


def format_shell_context_output(output: str, running: bool, limit: int = 12000) -> str:
    """Trim shell output before storing/feeding it to the model."""
    if len(output) <= limit:
        return output
    if running:
        return (
            f"... (shell 输出过长，已省略开头 {len(output) - limit} 字符，保留最新输出) ...\n"
            f"{output[-limit:].lstrip()}"
        )
    return clip_middle_text(output, limit, "shell 输出")


