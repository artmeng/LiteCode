from __future__ import annotations

# Team inbox 消息总线
# 基于 JSONL 文件实现 append-only 消息投递与已读游标推进

import json
from threading import Lock, RLock
from typing import Any

from .models import TeamMessage, validate_actor_name
from .store import TeamStore


class MessageBus:
    """基于 JSONL inbox 文件的轻量消息总线。

    每个 actor 对应一个独立的 .jsonl 文件，只追加不删除。
    已读进度通过 cursors/ 中的偏移量记录。
    """

    def __init__(self, store: TeamStore):
        self.store = store
        self._locks: dict[str, RLock] = {}  # 每个 actor 独立一把忽入锁
        self._locks_guard = Lock()           # 保护 _locks 字典本身的多线程安全

    def append(self, message: TeamMessage) -> TeamMessage:
        """将消息追加到目标 actor 的 inbox 文件（按 actor 加锁保证线程安全）。"""
        actor = validate_actor_name(message.to)
        with self._lock_for(actor):
            path = self.store.inbox_path(actor)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                # 每行一条 JSON，不带 BOM，方便逐行读取
                f.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")
        return message

    def send(
        self,
        *,
        from_actor: str,
        to: str,
        content: str,
        type: str = "message",
        task_id: str | None = None,
        in_reply_to: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> TeamMessage:
        """构造并发送一条 TeamMessage，返回已落盘的消息对象。"""
        return self.append(TeamMessage.create(
            from_actor=from_actor,
            to=to,
            content=content,
            type=type,
            task_id=task_id,
            in_reply_to=in_reply_to,
            meta=meta,
        ))

    def read(self, actor: str, *, limit: int = 20, mark_read: bool = True) -> list[TeamMessage]:
        """读取 actor 未读消息，mark_read=True 时自动推进已读游标。"""
        safe = validate_actor_name(actor)
        with self._lock_for(safe):
            messages = self._all_messages_unlocked(safe)
            # cursor 不超过总消息数，防止文件被外部截断后越界
            cursor = min(self.store.read_cursor(safe), len(messages))
            if limit <= 0:
                unread = messages[cursor:]           # limit=0 表示读取全部未读
            else:
                unread = messages[cursor:cursor + limit]
            if mark_read and unread:
                self.store.write_cursor(safe, cursor + len(unread))  # 推进游标
            return unread

    def recent(self, actor: str, *, limit: int = 50) -> list[TeamMessage]:
        """返回 actor inbox 最近 N 条消息，不影响已读游标（仅用于 UI 展示）。"""
        safe = validate_actor_name(actor)
        with self._lock_for(safe):
            messages = self._all_messages_unlocked(safe)
            if limit <= 0:
                return messages
            return messages[-limit:]

    def unread_count(self, actor: str) -> int:
        """返回 actor 当前未读消息数量。"""
        safe = validate_actor_name(actor)
        with self._lock_for(safe):
            messages = self._all_messages_unlocked(safe)
            cursor = min(self.store.read_cursor(safe), len(messages))
            return max(0, len(messages) - cursor)

    def all_messages(self, actor: str) -> list[TeamMessage]:
        """返回 actor inbox 的全部消息（包含已读）。"""
        safe = validate_actor_name(actor)
        with self._lock_for(safe):
            return self._all_messages_unlocked(safe)

    def _all_messages_unlocked(self, actor: str) -> list[TeamMessage]:
        """无锁逐行读取 JSONL 文件并反序列化为消息列表。调用方必须持有该 actor 的锁。"""
        path = self.store.inbox_path(actor)
        if not path.exists():
            return []  # inbox 尚未创建，没有任何消息
        out: list[TeamMessage] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    if isinstance(raw, dict):
                        out.append(TeamMessage.from_dict(raw))
                except (json.JSONDecodeError, ValueError):
                    continue  # 跳过损坏行，不中断读取
        return out

    def _lock_for(self, actor: str) -> RLock:
        """按 actor 名获取对应的可重入锁，避免并发读写同一 inbox 文件。"""
        safe = validate_actor_name(actor)
        with self._locks_guard:  # 保护锁字典本身的并发创建
            if safe not in self._locks:
                self._locks[safe] = RLock()
            return self._locks[safe]
