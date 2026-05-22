from __future__ import annotations

from .models import ControlMode


CONTROL_TOOL_NAMES = {"ask_user", "propose_plan"}


class ControlPolicy:
    def __init__(self, manager):
        self.manager = manager

    def is_tool_allowed(self, name: str, registry) -> bool:
        if name == "ask_user":
            return True
        if name == "propose_plan":
            return self.manager.mode == ControlMode.PLAN.value
        if self.manager.mode != ControlMode.PLAN.value:
            return True
        tool = registry.get(name)
        if tool is None:
            return False
        return bool(getattr(tool, "read_only", False))

    def filtered_definitions(self, registry) -> list[dict]:
        definitions = registry.get_definitions()
        if self.manager.mode != ControlMode.PLAN.value:
            return [item for item in definitions if item.get("name") != "propose_plan"]
        return [item for item in definitions if self.is_tool_allowed(str(item.get("name") or ""), registry)]
