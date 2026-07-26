"""Small state object for one Agent operation round."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional


@dataclass
class AgentRoundState:
    """Coordinate continuation context and stop/pause state for one round."""

    continuation_messages: List[Dict[str, Any]] = field(default_factory=list)
    should_continue: bool = False
    pause_message: Optional[str] = None

    def add_context(self, message: Mapping[str, Any]) -> None:
        self.continuation_messages.append(dict(message))

    @property
    def has_context(self) -> bool:
        return bool(self.continuation_messages)
