"""统一 Agent 轮次状态与来源描述。

该模块只生成展示文本和不可变来源信息，不发送 Telegram、不访问数据库。
普通协议续轮与 Trigger 唤醒共用同一套轮数，但通过来源和阶段区分。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional


@dataclass(frozen=True)
class AgentTurnOrigin:
    """标识一次 Agent 轮次由谁触发。"""

    kind: str = "user"
    task_id: Optional[str] = None
    run_id: Optional[str] = None

    @classmethod
    def user(cls) -> "AgentTurnOrigin":
        return cls(kind="user")

    @classmethod
    def trigger(cls, task_id: str, run_id: str) -> "AgentTurnOrigin":
        return cls(kind="trigger", task_id=str(task_id), run_id=str(run_id))

    @classmethod
    def ask(cls) -> "AgentTurnOrigin":
        return cls(kind="ask")

    @property
    def is_trigger(self) -> bool:
        return self.kind == "trigger"

    def label(self) -> str:
        if self.is_trigger:
            task_id = self.task_id or "未知任务"
            return f"后台任务 {task_id}"
        if self.kind == "ask":
            return "表单回答"
        return ""


def compact_agent_round_status(text: str) -> Optional[str]:
    """Display one completed round as a small count, without a success banner.

    Progress and unsuccessful terminal states are deliberately not projected
    into a completed round. Existing durable status text remains unchanged.
    """
    text = str(text).strip()
    compact = re.fullmatch(r'Agent 第 (\d+) 轮(?: · (\d+) 个操作)?', text)
    if compact:
        return text
    completed = re.match(r'^✅\s*Agent\s*第\s*(\d+)\s*轮(.*)$', text)
    if not completed or not any(word in completed[2] for word in ('已完成', '本轮处理完成')):
        return None
    if any(word in completed[2] for word in ('停止', '失败', '超过', '超限')):
        return None
    count = re.search(r'(\d+)\s*个操作已完成', completed[2])
    return f'Agent 第 {int(completed[1])} 轮' + (f' · {int(count[1])} 个操作' if count else '')


def build_agent_round_status(
    iteration: int,
    phase: str,
    *,
    origin: Optional[AgentTurnOrigin] = None,
    operation_count: Optional[int] = None,
    next_iteration: Optional[int] = None,
    max_iterations: Optional[int] = None,
) -> str:
    """生成统一 Agent 状态文本。

    ``iteration`` 始终使用全局 Agent 轮数；``origin`` 只改变来源说明，
    不改变计数规则。
    """
    iteration = max(0, int(iteration))
    origin = origin or AgentTurnOrigin.user()
    source = origin.label()
    source_prefix = f" · {source}" if source else ""

    if phase == "running":
        detail = (
            f"{max(0, int(operation_count))} 个操作进行中"
            if operation_count is not None
            else "操作进行中"
        )
        return f"Agent 第 {iteration} 轮{source_prefix} · {detail}"

    if phase == "waiting_ai":
        if origin.is_trigger:
            return f"🔔 Agent 第 {iteration} 轮 · {source or '后台任务'} 已返回，正在继续处理"
        return f"🧠 Agent 第 {iteration} 轮 · 操作结果已返回，正在继续处理"

    if phase == "continued":
        target = next_iteration if next_iteration is not None else iteration + 1
        return f"✅ Agent 第 {iteration} 轮 · 已完成，进入第 {int(target)} 轮"

    if phase == "completed":
        detail = (
            f"{max(0, int(operation_count))} 个操作已完成"
            if operation_count is not None
            else "本轮处理完成"
        )
        return f"✅ Agent 第 {iteration} 轮{source_prefix} · {detail}"

    if phase == "stopped":
        return f"⏹️ Agent 第 {iteration} 轮{source_prefix} · 已停止"

    if phase == "limit":
        limit = int(max_iterations) if max_iterations is not None else iteration
        return f"⛔ Agent 第 {iteration} 轮{source_prefix} · 已超过最大 {limit} 轮，结果已记录但不会继续调用 AI"

    if phase == "failed":
        return f"⚠️ Agent 第 {iteration} 轮{source_prefix} · 处理失败，结果已保留"

    raise ValueError(f"未知 Agent 状态阶段: {phase}")


__all__ = ["AgentTurnOrigin", "build_agent_round_status", "compact_agent_round_status"]
