from __future__ import annotations

import json
from typing import Any

from ..control.tools import make_pause_result

from .models import PermissionDecision
from .policy import PermissionPolicy


class PermissionManager:
    """工具执行权限管理器。

    在 runner._run_tool 的第三道门禁处被调用，负责：
    1. 优先查询一次性授权缓存（approved_once / denied_once），
       实现"同参操作只弹一次审批卡"的交互承诺。
    2. 缓存未命中时委托 PermissionPolicy 按当前模式评估风险。
    3. 需要审批时调用 control_manager.create_ask 生成 AskCard，
       暂停 turn 等待用户确认。
    4. 用户回答后由 record_answer 将结果写入缓存，供下一次 assess 消费。
    """

    def __init__(self, control_manager):
        self.control_manager = control_manager  # 用于创建 Ask interaction 和读取当前权限模式
        self.policy = PermissionPolicy()         # 无状态策略层，负责规则匹配
        self._approved_once: set[str] = set()   # 已批准的操作指纹集合（消费一次即移除）
        self._denied_once: set[str] = set()     # 已拒绝的操作指纹集合（消费一次即移除）

    def assess(self, tool_name: str, arguments: dict[str, Any] | None, *, registry=None) -> PermissionDecision:
        """评估工具调用是否允许执行，返回 allow / approval / deny 三态决策。

        执行顺序：
        1. 计算本次调用的指纹（工具名 + 参数的确定性哈希）。
        2. 指纹在 approved_once 中 → 放行并移除（一次性授权消费完毕）。
        3. 指纹在 denied_once 中 → 拒绝并移除（一次性拒绝消费完毕）。
        4. 均未命中 → 交由 PermissionPolicy 按当前模式（auto/plan/ask_before_edit）做规则评估。
        """
        args = arguments or {}
        fingerprint = _fingerprint(tool_name, args)
        # 一次性放行：用户上一次审批"允许"后写入，此处消费
        if fingerprint in self._approved_once:
            self._approved_once.remove(fingerprint)
            return PermissionDecision.allow(tool_name=tool_name, arguments=args)
        # 一次性拒绝：用户上一次审批"拒绝"后写入，此处消费，避免重复弹同一审批卡
        if fingerprint in self._denied_once:
            self._denied_once.remove(fingerprint)
            return PermissionDecision.deny(
                tool_name=tool_name,
                arguments=args,
                reason="user denied this high-risk operation",
            )
        # 缓存均未命中，走策略层规则评估
        return self.policy.assess(tool_name, args, self.control_manager.mode, registry=registry)

    def require_approval(
        self,
        decision: PermissionDecision,
        *,
        parent_call_id: str | None = None,
    ) -> str:
        """为高风险操作创建权限审批 AskCard，暂停当前 turn 等待用户确认。

        通过 control_manager.create_ask 生成一条 ASK interaction，
        其 meta.permission 字段携带指纹、工具名、风险等级和参数，
        供 record_answer 在用户回答后识别并写入授权缓存。
        返回值是占位 tool_result 字符串，写入 history 后触发 TurnPaused。
        """
        interaction = self.control_manager.create_ask(
            questions=[
                {
                    "id": "permission",
                    "header": "权限",
                    "question": f"是否允许执行高风险操作 `{decision.tool_name}`？",
                    "options": [
                        {"label": "允许", "description": "批准本次操作，Agent 可继续执行。"},
                        {"label": "拒绝", "description": "不执行本次操作，让 Agent 改用更安全方案。"},
                    ],
                }
            ],
            context=self._context(decision),
            parent_call_id=parent_call_id,
            # meta.permission 是 record_answer 识别本条 interaction 为权限审批的标志
            meta={
                "permission": {
                    "fingerprint": _fingerprint(decision.tool_name, decision.arguments or {}),
                    "tool_name": decision.tool_name,
                    "risk": decision.risk,
                    "reason": decision.reason,
                    "arguments": decision.arguments or {},
                }
            },
        )
        # 将 interaction 序列化为占位 tool_result，写入 history 后上层抛出 TurnPaused
        return make_pause_result(interaction.to_dict())

    def record_answer(self, interaction) -> None:
        """解析用户对权限审批卡的回答，将结果写入一次性授权缓存。

        由 ControlManager.answer 在用户提交答案后调用。
        只处理 meta.permission 存在的 interaction（即权限审批卡），
        其他类型的 Ask（如 Ask Guard 问题）不做处理。
        """
        # 仅处理携带权限审批元数据的 interaction
        permission = getattr(interaction, "meta", {}).get("permission") if getattr(interaction, "meta", None) else None
        if not isinstance(permission, dict):
            return
        fingerprint = str(permission.get("fingerprint") or "")
        if not fingerprint:
            return
        # 兼容 dict 格式（{choice, freeform}）和纯字符串格式的回答
        answer = interaction.answers.get("permission")
        choice = ""
        if isinstance(answer, dict):
            choice = str(answer.get("choice") or answer.get("freeform") or "")
        else:
            choice = str(answer or "")
        normalized = choice.strip().lower()
        # 用户选择"允许"→ 写入 approved_once，下次同参调用直接放行
        if "允许" in normalized or "approve" in normalized or "allow" in normalized or "yes" == normalized:
            self._approved_once.add(fingerprint)
            self._denied_once.discard(fingerprint)
            return
        # 用户选择"拒绝"→ 写入 denied_once，下次同参调用直接返回拒绝，不再弹卡
        self._denied_once.add(fingerprint)
        self._approved_once.discard(fingerprint)

    @staticmethod
    def _context(decision: PermissionDecision) -> str:
        """构造权限审批卡的上下文说明文本，展示在 AskCard 的详情区域。"""
        return "\n".join([
            "Permission Guard",
            f"risk: {decision.risk}",
            f"reason: {decision.reason}",
            f"tool: {decision.tool_name}",
            "arguments:",
            json.dumps(decision.arguments or {}, ensure_ascii=False, indent=2)[:1600],
        ])


def _fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
    """计算工具调用的确定性指纹（工具名 + 参数 JSON 的拼接字符串）。

    用于一次性授权缓存的键，确保同工具名 + 同参数组合才视为同一操作。
    序列化失败时自动降级为 _json_safe 处理后再序列化。
    """
    try:
        encoded = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = json.dumps(_json_safe(arguments or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{tool_name}:{encoded}"


def _json_safe(value: Any) -> Any:
    """将任意 Python 对象递归转换为可 JSON 序列化的安全类型。

    dict/list 递归处理；基本类型直接返回；其余类型转为字符串兜底。
    """
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
