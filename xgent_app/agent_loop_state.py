"""Small state object for one Agent operation round."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional


# 允许 over 协议生效的 block type。
#
# 入选判据是"失败时 success 位真的会翻成 False"，而不是"看着像纯副作用"：
#   run                 success 严格等于 返回码 0 且没超时、没被停
#   edit                未找到 / 匹配不唯一 / 文件不存在 都是显式 success: False
#   shellkill           会话不存在时 _get() 抛 KeyError，被 except 兜成 success: False
#   file / file_base64  失败走执行分支自己的 except，在那里显式否决
#
# read 和 stdin / shellread 刻意不在名单里：read 的异常分支返回的 dict 根本没有
# success 键，_normalize 默认补 True；shell 系的 _format_result 恒返回 True。
# 拿它们当判据等于把失败当成功放过去——那正是 over 唯一不能犯的错。
OVER_ELIGIBLE_TYPES = frozenset({"run", "edit", "shellkill", "file", "file_base64"})


@dataclass
class AgentRoundState:
    """Coordinate continuation context and stop/pause state for one round."""

    continuation_messages: List[Dict[str, Any]] = field(default_factory=list)
    should_continue: bool = False
    pause_message: Optional[str] = None
    # 模型在 over-x 块里预写的收尾语。None 表示这一轮没有 over 块。
    over_text: Optional[str] = None
    # over 已被否决：出现了白名单外的协议，或白名单里的某个操作失败了。
    over_blocked: bool = False

    def add_context(self, message: Mapping[str, Any]) -> None:
        self.continuation_messages.append(dict(message))

    @property
    def has_context(self) -> bool:
        return bool(self.continuation_messages)

    def note_over_eligibility(self, block_type: str) -> None:
        """白名单外的协议一出现就否决 over。over 块自己不算。"""
        if block_type != "over" and block_type not in OVER_ELIGIBLE_TYPES:
            self.over_blocked = True

    def apply_over(self) -> bool:
        """over 生效就丢掉这一轮的回灌载荷。

        丢掉的只是"立刻再拿这个结果问一次模型"的载荷。结果本身早在各分支的
        persist_* 里落进了 global_messages，下一轮用户消息时模型照样读得到——
        所以这不是把结果藏起来，只是不为它单独再烧一轮。

        载荷一空，plan_agent_round_transition 就落到 has_context 为假那条既有
        分支：正常收尾、不再调模型。
        """
        if self.over_text is None or self.over_blocked:
            return False
        self.continuation_messages.clear()
        return True
