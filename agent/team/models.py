from __future__ import annotations

"""Agent Team 的核心数据结构与名称校验规则。"""

import re
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


SCHEMA_VERSION = 1          # .team/ JSON 文件的格式版本号，升版时递增
LEAD_ACTOR = "lead"         # 主 Agent 的保留身份名
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")  # 队友名称合法性正则
_RESERVED_NAMES = {LEAD_ACTOR, "config", "inbox", "threads", "checkpoints", "cursors"}  # 不允许作队友名称的保留词


class TeamStatus(StrEnum):
    """队友生命周期状态机。

    idle → working（唤醒）→ idle（成功）/ error（失败）
    shutdown：永久退出，不再接收任务
    offline：启动时修正遗留的 working 状态
    """
    IDLE = "idle"
    WORKING = "working"
    OFFLINE = "offline"
    SHUTDOWN = "shutdown"
    ERROR = "error"


def now_ts() -> float:
    """返回当前 Unix 时间戳（秒）。"""
    return time.time()


def new_id(prefix: str) -> str:
    """生成短 ID，格式如 `msg_xxx` / `task_xxx`。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def validate_member_name(name: str) -> str:
    """校验队友名称是否合法，并过滤保留名。"""
    safe = str(name or "").strip()
    if not _NAME_RE.match(safe):
        raise ValueError(
            "member name must match [a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}"
        )
    if safe in _RESERVED_NAMES:
        raise ValueError(f"member name {safe!r} is reserved")
    return safe


def validate_actor_name(name: str) -> str:
    """校验消息参与者名称；允许 `lead`，其余走队友命名规则。"""
    actor = str(name or "").strip()
    if actor == LEAD_ACTOR:
        return actor
    return validate_member_name(actor)


@dataclass
class TeamMember:
    """持久化队友状态快照。"""

    name: str
    role: str
    agent_type: str
    status: str = TeamStatus.IDLE.value
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)
    last_error: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TeamMember":
        """从磁盘/网络字典恢复队友对象，兼容旧字段命名。"""
        name = validate_member_name(str(raw.get("name") or ""))
        status = str(raw.get("status") or TeamStatus.IDLE.value)
        if status not in {item.value for item in TeamStatus}:
            status = TeamStatus.IDLE.value
        return cls(
            name=name,
            role=str(raw.get("role") or ""),
            agent_type=str(raw.get("agent_type") or raw.get("agentType") or ""),
            status=status,
            created_at=float(raw.get("created_at") or raw.get("createdAt") or now_ts()),
            updated_at=float(raw.get("updated_at") or raw.get("updatedAt") or now_ts()),
            last_error=(
                str(raw.get("last_error") or raw.get("lastError"))
                if raw.get("last_error") or raw.get("lastError")
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为稳定的存储结构。"""
        return {
            "name": self.name,
            "role": self.role,
            "agent_type": self.agent_type,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
        }

    def touch(self, *, status: str | None = None, last_error: str | None = None) -> "TeamMember":
        """返回更新后的新对象，并刷新 `updated_at`。"""
        return TeamMember(
            name=self.name,
            role=self.role,
            agent_type=self.agent_type,
            status=status or self.status,
            created_at=self.created_at,
            updated_at=now_ts(),
            last_error=last_error,
        )


@dataclass
class TeamMessage:
    """Team inbox 中的单条消息记录。"""

    id: str
    type: str
    from_actor: str
    to: str
    content: str
    timestamp: float = field(default_factory=now_ts)
    task_id: str | None = None
    in_reply_to: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        from_actor: str,
        to: str,
        content: str,
        type: str = "message",
        task_id: str | None = None,
        in_reply_to: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> "TeamMessage":
        """创建新消息并自动分配 ID。"""
        return cls(
            id=new_id("msg"),
            type=type,
            from_actor=validate_actor_name(from_actor),
            to=validate_actor_name(to),
            content=str(content or ""),
            task_id=task_id,
            in_reply_to=in_reply_to,
            meta=meta or {},
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TeamMessage":
        """从 JSON 行恢复消息对象，兼容 `from` / `from_actor`。"""
        meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
        return cls(
            id=str(raw.get("id") or new_id("msg")),
            type=str(raw.get("type") or "message"),
            from_actor=validate_actor_name(str(raw.get("from") or raw.get("from_actor") or "")),
            to=validate_actor_name(str(raw.get("to") or "")),
            content=str(raw.get("content") or ""),
            timestamp=float(raw.get("timestamp") or now_ts()),
            task_id=str(raw.get("task_id")) if raw.get("task_id") else None,
            in_reply_to=str(raw.get("in_reply_to")) if raw.get("in_reply_to") else None,
            meta=meta,
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为 inbox 使用的 JSON 结构。"""
        return {
            "id": self.id,
            "type": self.type,
            "from": self.from_actor,
            "to": self.to,
            "content": self.content,
            "timestamp": self.timestamp,
            "task_id": self.task_id,
            "in_reply_to": self.in_reply_to,
            "meta": self.meta,
        }
