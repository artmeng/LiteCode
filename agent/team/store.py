from __future__ import annotations

# Team 子系统本地持久化层
# 负责管理 .team/ 目录下的 config、inbox、thread、checkpoint 和 cursor 文件

import json
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from .models import SCHEMA_VERSION, TeamMember, TeamStatus, validate_actor_name, validate_member_name


class TeamStore:
    """管理 .team/ 下所有持久化文件的读写，是 Team 子系统的唯一存储接口。"""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.team_dir = self.root / ".team"
        self.config_file = self.team_dir / "config.json"
        self.inbox_dir = self.team_dir / "inbox"
        self.threads_dir = self.team_dir / "threads"
        self.checkpoints_dir = self.team_dir / "checkpoints"
        self.cursors_dir = self.team_dir / "cursors"
        self._json_lock = RLock()  # 保护所有 JSON 文件读写的可重入锁
        self._ensure()               # 初始化目录结构和默认配置
        self.mark_stale_working_offline()  # 启动时修正遗留 working 状态

    def _ensure(self) -> None:
        """初始化 .team/ 目录结构；首次运行时写入空的 config.json。"""
        for path in (
            self.team_dir,
            self.inbox_dir,
            self.threads_dir,
            self.checkpoints_dir,
            self.cursors_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if not self.config_file.exists():
            self.save_config({"version": SCHEMA_VERSION, "team_name": "default", "members": []})

    def load_config(self) -> dict[str, Any]:
        """读取并清洗 config.json，确保成员数据可反序列化，返回结构稳定的字典。"""
        with self._json_lock:
            try:
                raw = json.loads(self.config_file.read_text(encoding="utf-8") or "{}")
            except (json.JSONDecodeError, OSError):
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        members = []
        for item in raw.get("members") or []:
            if not isinstance(item, dict):
                continue
            try:
                # 反序列化再序列化，过滤掉格式损坏的成员条目
                members.append(TeamMember.from_dict(item).to_dict())
            except ValueError:
                continue
        return {
            "version": int(raw.get("version") or SCHEMA_VERSION),
            "team_name": str(raw.get("team_name") or raw.get("teamName") or "default"),
            "members": members,
        }

    def save_config(self, config: dict[str, Any]) -> None:
        """将标准化的 config 结构原子写入 config.json。"""
        payload = {
            "version": int(config.get("version") or SCHEMA_VERSION),
            "team_name": str(config.get("team_name") or "default"),
            "members": config.get("members") or [],
        }
        with self._json_lock:
            self._atomic_write_json(self.config_file, payload)

    def list_members(self) -> list[TeamMember]:
        """返回当前所有队友对象的快照列表。"""
        return [TeamMember.from_dict(item) for item in self.load_config().get("members", [])]

    def get_member(self, name: str) -> TeamMember | None:
        """按名称查找单个队友，不存在时返回 None。"""
        safe = validate_member_name(name)
        for member in self.list_members():
            if member.name == safe:
                return member
        return None

    def upsert_member(self, member: TeamMember) -> TeamMember:
        """插入或更新队友记录；按名称匹配，存在则替换，不存在则追加。"""
        validate_member_name(member.name)
        with self._json_lock:
            config = self.load_config()
            members = []
            replaced = False
            for item in config.get("members") or []:
                current = TeamMember.from_dict(item)
                if current.name == member.name:
                    members.append(member.to_dict())  # 用新数据替换同名旧记录
                    replaced = True
                else:
                    members.append(current.to_dict())
            if not replaced:
                members.append(member.to_dict())  # 首次创建，追加到末尾
            config["members"] = members
            self.save_config(config)
        return member

    def update_member(self, name: str, **fields: Any) -> TeamMember:
        """局部更新队友字段（如 status、last_error），返回更新后的对象。"""
        with self._json_lock:
            member = self.get_member(name)
            if member is None:
                raise ValueError(f"unknown teammate: {name}")
            data = member.to_dict()
            data.update(fields)
            updated = TeamMember.from_dict(data)
            return self.upsert_member(updated)

    def mark_stale_working_offline(self) -> None:
        """启动时将遗留 working 状态的队友修正为 offline，避免假死状态残留。"""
        with self._json_lock:
            changed = False
            members = []
            for member in self.list_members():
                if member.status == TeamStatus.WORKING.value:
                    member = member.touch(status=TeamStatus.OFFLINE.value, last_error=None)
                    changed = True
                members.append(member.to_dict())
            if changed:
                config = self.load_config()
                config["members"] = members
                self.save_config(config)

    def inbox_path(self, actor: str) -> Path:
        """返回 actor 的 inbox JSONL 文件路径（append-only 消息总线）。"""
        safe = validate_actor_name(actor)
        return self.inbox_dir / f"{safe}.jsonl"

    def thread_path(self, name: str) -> Path:
        """返回队友 thread（完整对话历史）的文件路径。"""
        safe = validate_member_name(name)
        return self.threads_dir / f"{safe}.json"

    def checkpoint_path(self, name: str) -> Path:
        """返回队友 checkpoint（崩溃恢复点）的文件路径。"""
        safe = validate_member_name(name)
        return self.checkpoints_dir / f"{safe}.json"

    def cursor_path(self, actor: str) -> Path:
        """返回 actor 的 inbox 已读游标文件路径。"""
        safe = validate_actor_name(actor)
        return self.cursors_dir / f"{safe}.json"

    def read_thread(self, name: str) -> list[dict[str, Any]]:
        """读取队友完整对话历史；文件不存在或解析失败时返回空列表。"""
        path = self.thread_path(name)
        if not path.exists():
            return []  # 首次唤醒，历史为空
        with self._json_lock:
            try:
                raw = json.loads(path.read_text(encoding="utf-8") or "{}")
            except (json.JSONDecodeError, OSError):
                return []
        messages = raw.get("messages") if isinstance(raw, dict) else None
        return list(messages) if isinstance(messages, list) else []

    def write_thread(self, name: str, messages: list[dict[str, Any]]) -> None:
        """覆盖写入队友的完整对话历史（每轮执行成功后调用）。"""
        with self._json_lock:
            self._atomic_write_json(
                self.thread_path(name),
                {"version": SCHEMA_VERSION, "member": validate_member_name(name), "messages": messages},
            )

    def read_checkpoint(self, name: str) -> list[dict[str, Any]] | None:
        """读取 checkpoint 中的 messages 列表；不存在时返回 None。"""
        payload = self.read_checkpoint_payload(name)
        if payload is None:
            return None
        return payload["messages"]

    def read_checkpoint_payload(self, name: str) -> dict[str, Any] | None:
        """读取完整 checkpoint 载荷，兼容旧版纯列表格式，并对 cursor/ids 字段做类型兜底。"""
        path = self.checkpoint_path(name)
        if not path.exists():
            return None
        with self._json_lock:
            try:
                raw = json.loads(path.read_text(encoding="utf-8") or "{}")
            except (json.JSONDecodeError, OSError):
                return None
        if isinstance(raw, list):
            # 兼容旧版：checkpoint 直接存 messages 列表，无外层包装
            messages = raw
            raw_payload: dict[str, Any] = {}
        elif isinstance(raw, dict):
            messages = raw.get("messages")
            raw_payload = raw
        else:
            return None
        if not isinstance(messages, list):
            return None

        payload: dict[str, Any] = {
            "version": int(raw_payload.get("version") or SCHEMA_VERSION),
            "member": validate_member_name(name),
            "messages": list(messages),
        }
        # 解析 inbox 游标范围（类型容错，避免因磁盘写坏导致崩溃）
        for key in ("pending_cursor_start", "pending_cursor_end"):
            if key in raw_payload:
                try:
                    payload[key] = max(0, int(raw_payload[key]))
                except (TypeError, ValueError):
                    pass
        # 解析本轮待处理消息 ID 列表
        ids = raw_payload.get("pending_message_ids")
        if isinstance(ids, list):
            payload["pending_message_ids"] = [str(item) for item in ids]
        return payload

    def write_checkpoint(
        self,
        name: str,
        messages: list[dict[str, Any]],
        *,
        pending_cursor_start: int | None = None,
        pending_cursor_end: int | None = None,
        pending_message_ids: list[str] | None = None,
    ) -> None:
        """写入崩溃恢复点：保存当前 history 快照及本轮处理的 inbox 游标范围。"""
        payload: dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "member": validate_member_name(name),
            "messages": messages,
        }
        if pending_cursor_start is not None:
            payload["pending_cursor_start"] = max(0, int(pending_cursor_start))
        if pending_cursor_end is not None:
            payload["pending_cursor_end"] = max(0, int(pending_cursor_end))
        if pending_message_ids is not None:
            payload["pending_message_ids"] = [str(item) for item in pending_message_ids]
        with self._json_lock:
            self._atomic_write_json(self.checkpoint_path(name), payload)

    def clear_checkpoint(self, name: str) -> None:
        """执行成功后删除 checkpoint，避免下次唤醒误判为崩溃恢复。"""
        with self._json_lock:
            self.checkpoint_path(name).unlink(missing_ok=True)

    def read_cursor(self, actor: str) -> int:
        """读取 actor inbox 的已读偏移量；文件不存在或解析异常时回退 0。"""
        path = self.cursor_path(actor)
        if not path.exists():
            return 0  # 首次读取，从头开始
        with self._json_lock:
            try:
                raw = json.loads(path.read_text(encoding="utf-8") or "{}")
            except (json.JSONDecodeError, OSError):
                return 0
        return max(0, int(raw.get("inbox") or 0))

    def write_cursor(self, actor: str, offset: int) -> None:
        """更新 actor inbox 的已读游标到指定偏移量。"""
        with self._json_lock:
            self._atomic_write_json(self.cursor_path(actor), {"inbox": max(0, int(offset))})

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        """以临时文件原子替换的方式写 JSON，防止写入中断导致文件损坏。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        # 用随机后缀生成临时文件，避免并发写入冲突
        tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)  # 原子替换，POSIX 系统保证原子性
        finally:
            tmp.unlink(missing_ok=True)  # 无论成败都清理临时文件
