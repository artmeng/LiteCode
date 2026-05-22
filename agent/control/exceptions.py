from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnPaused(Exception):
    interaction: dict[str, Any]
    tool_messages: list[dict[str, Any]] = field(default_factory=list)

    def __str__(self) -> str:
        kind = self.interaction.get("kind") or "interaction"
        ident = self.interaction.get("id") or "unknown"
        return f"turn paused for {kind}: {ident}"
