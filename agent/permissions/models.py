from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class PermissionMode(StrEnum):
    ASK_BEFORE_EDIT = "ask_before_edit"
    AUTO = "auto"
    PLAN = "plan"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    requires_approval: bool = False
    risk: str = RiskLevel.LOW.value
    reason: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] | None = None

    @classmethod
    def allow(cls, *, tool_name: str, arguments: dict[str, Any] | None = None) -> "PermissionDecision":
        return cls(allowed=True, tool_name=tool_name, arguments=arguments or {})

    @classmethod
    def deny(cls, *, tool_name: str, reason: str, arguments: dict[str, Any] | None = None) -> "PermissionDecision":
        return cls(
            allowed=False,
            risk=RiskLevel.HIGH.value,
            reason=reason,
            tool_name=tool_name,
            arguments=arguments or {},
        )

    @classmethod
    def approval(
        cls,
        *,
        tool_name: str,
        reason: str,
        arguments: dict[str, Any] | None = None,
        risk: str = RiskLevel.HIGH.value,
    ) -> "PermissionDecision":
        return cls(
            allowed=False,
            requires_approval=True,
            risk=risk,
            reason=reason,
            tool_name=tool_name,
            arguments=arguments or {},
        )
