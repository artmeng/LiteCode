from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .models import (
    ControlMode,
    ControlState,
    Interaction,
    InteractionKind,
    InteractionStatus,
    Question,
    now_ts,
)
from .clarification import ClarificationAssessment, ClarificationPolicy
from .policy import ControlPolicy
from .store import ControlStore
from ..permissions import PermissionManager


@dataclass
class ControlResume:
    interaction: dict[str, Any]
    message: str
    event: dict[str, Any]
    resume: bool = True


class ControlManager:
    def __init__(self, root):
        self.store = ControlStore(root)
        self.policy = ControlPolicy(self)
        self.clarification_policy = ClarificationPolicy()
        self.permission_manager = PermissionManager(self)

    @property
    def mode(self) -> str:
        return self.store.load().mode

    def payload(self) -> dict[str, Any]:
        state = self.store.load()
        return state.to_dict()

    def set_mode(self, mode: str) -> dict[str, Any]:
        value = str(mode or "").strip().lower()
        if value in {"on", "plan"}:
            value = ControlMode.PLAN.value
        elif value in {"off", "normal", "ask", "ask_before_edit", "edit_before_ask"}:
            value = ControlMode.ASK_BEFORE_EDIT.value
        elif value in {"auto", "automatic"}:
            value = ControlMode.AUTO.value
        if value not in {item.value for item in ControlMode}:
            raise ValueError("mode must be ask_before_edit, auto or plan")
        state = self.store.load()
        if value == ControlMode.PLAN.value and state.mode != ControlMode.PLAN.value:
            state.previous_mode = state.mode
        elif value != ControlMode.PLAN.value:
            state.previous_mode = None
        state.mode = value
        state.updated_at = now_ts()
        self.store.save(state)
        return self.payload()

    def ensure_no_pending(self) -> None:
        pending = self.store.load().pending
        if pending and pending.status == InteractionStatus.WAITING.value:
            raise ValueError(f"pending interaction already exists: {pending.id}")

    def create_ask(
        self,
        *,
        questions: list[dict[str, Any]],
        context: str = "",
        parent_call_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Interaction:
        self.ensure_no_pending()
        parsed = [Question.from_dict(item) for item in questions]
        interaction = Interaction.ask(
            questions=parsed,
            context=context,
            parent_call_id=parent_call_id,
            meta=meta or {},
        )
        self._set_pending(interaction)
        return interaction

    def create_plan(
        self,
        *,
        title: str,
        summary: str,
        plan_markdown: str,
        assumptions: list[str] | None = None,
        risk_level: str = "medium",
        parent_call_id: str | None = None,
    ) -> Interaction:
        self.ensure_no_pending()
        interaction = Interaction.plan(
            title=title,
            summary=summary,
            plan_markdown=plan_markdown,
            assumptions=assumptions,
            risk_level=risk_level,
            parent_call_id=parent_call_id,
        )
        self._set_pending(interaction)
        return interaction

    def create_plan_from_text(self, text: str) -> Interaction:
        body = str(text or "").strip()
        if not body:
            body = "Plan 模式要求先提交可预览计划。"
        title = _first_heading(body) or "计划预览"
        summary = _plain_summary(body)
        if not _looks_like_plan(body):
            body = "\n".join([
                "# 计划预览",
                "",
                body,
                "",
                "## 验收",
                "- 用户批准后再执行任何写入或高影响操作。",
            ])
        return self.create_plan(
            title=title,
            summary=summary,
            plan_markdown=body,
            assumptions=[],
            risk_level="medium",
        )

    def assess_clarification(self, history: list[dict[str, Any]]) -> ClarificationAssessment:
        return self.clarification_policy.assess(history)

    def should_enforce_plan_final(self) -> bool:
        return self.mode == ControlMode.PLAN.value

    def _set_pending(self, interaction: Interaction) -> None:
        state = self.store.load()
        state.pending = interaction
        state.last_interaction = interaction
        state.updated_at = now_ts()
        self.store.save(state)

    def answer(self, interaction_id: str, answers: dict[str, Any]) -> ControlResume:
        # 校验目标 interaction 存在且处于 ASK pending 状态
        interaction = self._require_pending(interaction_id, InteractionKind.ASK)
        # 将原始答案归一化为统一结构（choice + freeform）
        normalized = self._normalize_answers(interaction, answers)
        # 更新状态为 ANSWERED，并写入归一化答案
        updated = interaction.touch(status=InteractionStatus.ANSWERED.value)
        updated.answers = normalized
        # 将本次答案记录到权限管理器，用于后续同参放行判断
        self.permission_manager.record_answer(updated)
        # 标记 interaction 完成，持久化状态并清除 pending
        self._complete(updated)
        # 构造回注给 runner 的用户消息文本
        message = self._answer_message(updated)
        return ControlResume(
            interaction=updated.to_dict(),
            message=message,
            # 下发 ask_answered 事件，WebUI 用于关闭 AskCard 并展示答案
            event={"event": "ask_answered", "interaction": updated.to_dict()},
        )

    def comment(self, interaction_id: str, comment: str) -> ControlResume:
        interaction = self._require_pending(interaction_id, InteractionKind.PLAN)
        text = str(comment or "").strip()
        if not text:
            raise ValueError("comment is required")
        updated = interaction.touch(status=InteractionStatus.COMMENTED.value)
        updated.comments = [
            *updated.comments,
            {"content": text[:4000], "timestamp": now_ts()},
        ]
        self._complete(updated)
        message = self._comment_message(updated, text)
        return ControlResume(
            interaction=updated.to_dict(),
            message=message,
            event={"event": "plan_comment_added", "interaction": updated.to_dict(), "comment": text},
        )

    def approve(self, interaction_id: str) -> ControlResume:
        interaction = self._require_pending(interaction_id, InteractionKind.PLAN)
        updated = interaction.touch(status=InteractionStatus.APPROVED.value)
        state = self.store.load()
        state.mode = self._restore_mode(state)
        state.previous_mode = None
        state.pending = None
        state.last_interaction = updated
        state.updated_at = now_ts()
        self.store.save(state)
        message = self._approval_message(updated)
        return ControlResume(
            interaction=updated.to_dict(),
            message=message,
            event={"event": "plan_approved", "interaction": updated.to_dict(), "control": self.payload()},
        )

    def cancel(self, interaction_id: str) -> dict[str, Any]:
        pending = self._require_pending(interaction_id)
        updated = pending.touch(status=InteractionStatus.CANCELLED.value)
        if pending.kind == InteractionKind.PLAN.value:
            state = self.store.load()
            state.mode = self._restore_mode(state)
            state.previous_mode = None
            state.pending = None
            state.last_interaction = updated
            state.updated_at = now_ts()
            self.store.save(state)
        else:
            self._complete(updated)
        return {
            "event": "interaction_cancelled",
            "interaction": updated.to_dict(),
            "control": self.payload(),
            "message": self._cancel_message(updated),
        }

    def _complete(self, interaction: Interaction) -> None:
        state = self.store.load()
        state.pending = None
        state.last_interaction = interaction
        state.updated_at = now_ts()
        self.store.save(state)

    def _require_pending(
        self,
        interaction_id: str,
        kind: InteractionKind | None = None,
    ) -> Interaction:
        pending = self.store.load().pending
        if pending is None:
            raise ValueError("no pending interaction")
        if pending.id != str(interaction_id):
            raise ValueError(f"pending interaction mismatch: {pending.id}")
        if kind is not None and pending.kind != kind.value:
            raise ValueError(f"pending interaction is not {kind.value}")
        if pending.status != InteractionStatus.WAITING.value:
            raise ValueError(f"interaction is not waiting: {pending.status}")
        return pending

    def _normalize_answers(self, interaction: Interaction, answers: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(answers, dict):
            raise ValueError("answers must be an object")
        question_ids = {q.id for q in interaction.questions}
        normalized: dict[str, Any] = {}
        for qid, value in answers.items():
            if qid not in question_ids and qid != "_freeform":
                continue
            if isinstance(value, dict):
                normalized[qid] = {
                    "choice": str(value.get("choice") or "").strip(),
                    "freeform": str(value.get("freeform") or "").strip(),
                }
            else:
                normalized[qid] = {"choice": str(value or "").strip(), "freeform": ""}
        if not normalized:
            raise ValueError("at least one answer is required")
        return normalized

    def _answer_message(self, interaction: Interaction) -> str:
        lines = [
            "[CONTROL:ASK_ANSWERED]",
            f"interaction_id: {interaction.id}",
            "用户已回答澄清问题，请结合答案继续推进。",
            "",
        ]
        for question in interaction.questions:
            answer = interaction.answers.get(question.id) or {}
            choice = answer.get("choice") if isinstance(answer, dict) else str(answer)
            freeform = answer.get("freeform") if isinstance(answer, dict) else ""
            lines.append(f"- {question.header}: {question.question}")
            if choice:
                lines.append(f"  answer: {choice}")
            if freeform:
                lines.append(f"  note: {freeform}")
        extra = interaction.answers.get("_freeform")
        if isinstance(extra, dict) and extra.get("freeform"):
            lines.append(f"- additional note: {extra['freeform']}")
        return "\n".join(lines).strip()

    def _comment_message(self, interaction: Interaction, comment: str) -> str:
        return (
            "[CONTROL:PLAN_COMMENT]\n"
            f"interaction_id: {interaction.id}\n"
            "用户对计划提出了评论，请保持 Plan 模式，只修订计划并再次调用 propose_plan。\n\n"
            f"评论：\n{comment.strip()}"
        )

    def _approval_message(self, interaction: Interaction) -> str:
        return (
            "[CONTROL:PLAN_APPROVED]\n"
            f"interaction_id: {interaction.id}\n"
            "用户已批准以下计划。现在切换到执行模式，请按计划实施；执行中如出现新的高影响歧义，可再次 ask_user。\n\n"
            f"# {interaction.title}\n\n{interaction.plan_markdown}"
        )

    def _cancel_message(self, interaction: Interaction) -> str:
        return (
            "[CONTROL:INTERACTION_CANCELLED]\n"
            f"interaction_id: {interaction.id}\n"
            f"kind: {interaction.kind}\n"
            "用户取消了这次等待交互。不要继续等待该问题或计划；后续请以用户的新指令为准。"
        )

    def system_prompt(self) -> str:
        if self.mode == ControlMode.PLAN.value:
            return (
                "# Control Mode: Plan\n\n"
                "- 当前处于 Plan 模式。你必须先通过只读探索理解环境，不允许修改文件、运行命令执行变更、派遣子代理或创建队友。\n"
                "- 若需求存在会影响方案的偏好或取舍，调用 `ask_user` 提问。\n"
                "- 当方案足够明确时，必须调用 `propose_plan` 提交完整计划，等待用户评论或批准。\n"
                "- 用户批准前不要执行计划。\n"
                "- 不允许用普通最终回复替代计划卡；最终必须通过 `propose_plan` 进入 PlanCard。"
            )
        return (
            "# Control Tools\n\n"
            f"- 当前权限模式：{self.mode}。\n"
            "- `ask_before_edit` 模式下，危险、不确定或高影响操作会触发权限审批；低风险读操作和普通编辑可继续执行。\n"
            "- `auto` 模式下，工具层不主动审批，但仍受路径安全、schema 校验和工具自身安全策略约束。\n"
            "- 当用户目标存在高影响歧义且无法通过读文件/搜索等方式确定时，调用 `ask_user` 提出结构化问题。\n"
            "- 高影响歧义包括范围/验收不清的大改动、架构/重构/UI 取舍、提交推送、删除覆盖、发布部署、成本/权限/安全边界。\n"
            "- 可通过只读探索确认的事实先探索；但在写入、高影响操作或最终答复前仍有关键取舍时，必须提问。\n"
            "- 只有在用户显式开启 Plan 模式后，才使用 `propose_plan` 提交等待批准的计划。"
        )

    def tool_definitions(self, registry) -> list[dict]:
        return self.policy.filtered_definitions(registry)

    def is_tool_allowed(self, name: str, registry) -> bool:
        return self.policy.is_tool_allowed(name, registry)

    def assess_permission(self, name: str, arguments: dict[str, Any], registry):
        return self.permission_manager.assess(name, arguments, registry=registry)

    def permission_approval_result(self, decision, *, parent_call_id: str | None = None) -> str:
        return self.permission_manager.require_approval(decision, parent_call_id=parent_call_id)

    @staticmethod
    def _restore_mode(state: ControlState) -> str:
        if state.previous_mode in {ControlMode.ASK_BEFORE_EDIT.value, ControlMode.AUTO.value}:
            return state.previous_mode
        return ControlMode.ASK_BEFORE_EDIT.value

    @staticmethod
    def interaction_event(interaction: Interaction) -> dict[str, Any]:
        event = "ask_request" if interaction.kind == InteractionKind.ASK.value else "plan_draft"
        return {"event": event, "interaction": interaction.to_dict()}

    @staticmethod
    def interaction_from_marker(marker: str) -> dict[str, Any] | None:
        try:
            raw = json.loads(marker)
        except json.JSONDecodeError:
            return None
        interaction = raw.get("interaction") if isinstance(raw, dict) else None
        return interaction if isinstance(interaction, dict) else None


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:160]
    return ""


def _plain_summary(text: str) -> str:
    compact = " ".join(line.strip().lstrip("-*# ") for line in text.splitlines() if line.strip())
    return (compact or "计划待预览。")[:1200]


def _looks_like_plan(text: str) -> bool:
    return bool(
        "##" in text
        or "\n-" in text
        or "\n1." in text
        or "验收" in text
        or "Test Plan" in text
    )
