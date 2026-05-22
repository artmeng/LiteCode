from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


SCHEMA_VERSION = 1
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


class ControlMode(StrEnum):
    ASK_BEFORE_EDIT = "ask_before_edit"
    AUTO = "auto"
    PLAN = "plan"


class InteractionKind(StrEnum):
    ASK = "ask"
    PLAN = "plan"


class InteractionStatus(StrEnum):
    WAITING = "waiting"
    ANSWERED = "answered"
    COMMENTED = "commented"
    APPROVED = "approved"
    CANCELLED = "cancelled"


def now_ts() -> float:
    return time.time()


def new_interaction_id(kind: str) -> str:
    return f"{kind}_{uuid.uuid4().hex[:12]}"


def safe_id(value: str, *, label: str = "id") -> str:
    text = str(value or "").strip()
    if not _SAFE_ID_RE.match(text):
        raise ValueError(f"{label} must match [a-zA-Z0-9][a-zA-Z0-9_.-]{{0,63}}")
    return text


@dataclass
class QuestionOption:
    label: str
    description: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "QuestionOption":
        label = str(raw.get("label") or "").strip()
        if not label:
            raise ValueError("option label is required")
        return cls(label=label[:80], description=str(raw.get("description") or "").strip()[:240])

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "description": self.description}


@dataclass
class Question:
    id: str
    header: str
    question: str
    options: list[QuestionOption]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Question":
        qid = safe_id(str(raw.get("id") or ""), label="question id")
        header = str(raw.get("header") or "").strip()
        text = str(raw.get("question") or "").strip()
        if not header or not text:
            raise ValueError("question header and question are required")
        options_raw = raw.get("options") or []
        if not isinstance(options_raw, list):
            raise ValueError("question options must be an array")
        options = [QuestionOption.from_dict(item) for item in options_raw if isinstance(item, dict)]
        if not 2 <= len(options) <= 4:
            raise ValueError("each question must have 2-4 options")
        return cls(id=qid, header=header[:24], question=text[:400], options=options)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "header": self.header,
            "question": self.question,
            "options": [item.to_dict() for item in self.options],
        }


@dataclass
class Interaction:
    id: str
    kind: str
    status: str = InteractionStatus.WAITING.value
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)
    parent_call_id: str | None = None
    context: str = ""
    questions: list[Question] = field(default_factory=list)
    answers: dict[str, Any] = field(default_factory=dict)
    title: str = ""
    summary: str = ""
    plan_markdown: str = ""
    assumptions: list[str] = field(default_factory=list)
    risk_level: str = "medium"
    comments: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ask(
        cls,
        *,
        questions: list[Question],
        context: str = "",
        parent_call_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> "Interaction":
        if not 1 <= len(questions) <= 3:
            raise ValueError("ask_user requires 1-3 questions")
        return cls(
            id=new_interaction_id(InteractionKind.ASK.value),
            kind=InteractionKind.ASK.value,
            questions=questions,
            context=context.strip()[:1000],
            parent_call_id=parent_call_id,
            meta=meta or {},
        )

    @classmethod
    def plan(
        cls,
        *,
        title: str,
        summary: str,
        plan_markdown: str,
        assumptions: list[str] | None = None,
        risk_level: str = "medium",
        parent_call_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> "Interaction":
        title = title.strip()
        summary = summary.strip()
        plan_markdown = plan_markdown.strip()
        if not title or not summary or not plan_markdown:
            raise ValueError("title, summary and plan_markdown are required")
        return cls(
            id=new_interaction_id(InteractionKind.PLAN.value),
            kind=InteractionKind.PLAN.value,
            title=title[:160],
            summary=summary[:1200],
            plan_markdown=plan_markdown,
            assumptions=[str(item).strip()[:300] for item in assumptions or [] if str(item).strip()],
            risk_level=(risk_level or "medium").strip().lower()[:24],
            parent_call_id=parent_call_id,
            meta=meta or {},
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Interaction":
        kind = str(raw.get("kind") or "")
        if kind not in {item.value for item in InteractionKind}:
            raise ValueError(f"unknown interaction kind: {kind}")
        status = str(raw.get("status") or InteractionStatus.WAITING.value)
        if status not in {item.value for item in InteractionStatus}:
            status = InteractionStatus.WAITING.value
        questions = []
        for item in raw.get("questions") or []:
            if isinstance(item, dict):
                questions.append(Question.from_dict(item))
        comments = raw.get("comments") if isinstance(raw.get("comments"), list) else []
        return cls(
            id=safe_id(str(raw.get("id") or new_interaction_id(kind)), label="interaction id"),
            kind=kind,
            status=status,
            created_at=float(raw.get("created_at") or raw.get("createdAt") or now_ts()),
            updated_at=float(raw.get("updated_at") or raw.get("updatedAt") or now_ts()),
            parent_call_id=str(raw.get("parent_call_id") or raw.get("parentCallId") or "") or None,
            context=str(raw.get("context") or ""),
            questions=questions,
            answers=raw.get("answers") if isinstance(raw.get("answers"), dict) else {},
            title=str(raw.get("title") or ""),
            summary=str(raw.get("summary") or ""),
            plan_markdown=str(raw.get("plan_markdown") or raw.get("planMarkdown") or ""),
            assumptions=[str(item) for item in raw.get("assumptions") or []],
            risk_level=str(raw.get("risk_level") or raw.get("riskLevel") or "medium"),
            comments=[item for item in comments if isinstance(item, dict)],
            meta=raw.get("meta") if isinstance(raw.get("meta"), dict) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "parent_call_id": self.parent_call_id,
            "context": self.context,
            "questions": [item.to_dict() for item in self.questions],
            "answers": self.answers,
            "title": self.title,
            "summary": self.summary,
            "plan_markdown": self.plan_markdown,
            "assumptions": list(self.assumptions),
            "risk_level": self.risk_level,
            "comments": list(self.comments),
            "meta": dict(self.meta),
        }

    def touch(self, *, status: str | None = None) -> "Interaction":
        data = self.to_dict()
        if status:
            data["status"] = status
        data["updated_at"] = now_ts()
        return Interaction.from_dict(data)


@dataclass
class ControlState:
    version: int = SCHEMA_VERSION
    mode: str = ControlMode.ASK_BEFORE_EDIT.value
    previous_mode: str | None = None
    pending: Interaction | None = None
    last_interaction: Interaction | None = None
    updated_at: float = field(default_factory=now_ts)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ControlState":
        mode = str(raw.get("mode") or ControlMode.ASK_BEFORE_EDIT.value)
        if mode == "normal":
            mode = ControlMode.ASK_BEFORE_EDIT.value
        if mode not in {item.value for item in ControlMode}:
            mode = ControlMode.ASK_BEFORE_EDIT.value
        previous_mode = str(raw.get("previous_mode") or raw.get("previousMode") or "") or None
        if previous_mode == "normal":
            previous_mode = ControlMode.ASK_BEFORE_EDIT.value
        if previous_mode not in {
            ControlMode.ASK_BEFORE_EDIT.value,
            ControlMode.AUTO.value,
        }:
            previous_mode = None

        def parse_interaction(value: Any) -> Interaction | None:
            if not isinstance(value, dict):
                return None
            try:
                return Interaction.from_dict(value)
            except ValueError:
                return None

        return cls(
            version=int(raw.get("version") or SCHEMA_VERSION),
            mode=mode,
            previous_mode=previous_mode,
            pending=parse_interaction(raw.get("pending")),
            last_interaction=parse_interaction(raw.get("last_interaction") or raw.get("lastInteraction")),
            updated_at=float(raw.get("updated_at") or raw.get("updatedAt") or now_ts()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "mode": self.mode,
            "previous_mode": self.previous_mode,
            "pending": self.pending.to_dict() if self.pending else None,
            "last_interaction": self.last_interaction.to_dict() if self.last_interaction else None,
            "updated_at": self.updated_at,
        }
