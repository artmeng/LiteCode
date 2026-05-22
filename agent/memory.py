"""agent/memory.py

三层记忆存储：原始历史（history）/ 日期情景（episode）/ 长期记忆（MEMORY.local.md）。

同时管理中断恢复用的 checkpoint，以及用户档案读写。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .memory_history import HistoryLog


_UTC8 = timezone(timedelta(hours=8))


class MemoryStore:
    """三层记忆存储管理器。

    - 工作记忆：由 AgentRunner 内存 history 列表承载，不涉及本类
    - 情景记忆：memory/YYYY-MM-DD.md，每次压缩时由 Compactor 写入
    - 长期记忆：memory/MEMORY.local.md，每轮注入 system prompt
    - Checkpoint：memory/_checkpoint.json，工具批次完成后原子写入
    - 用户档案：templates/USER.local.md，每轮注入 system prompt
    """

    def __init__(self, memory_dir: Path, user_file: Path, memory_template: Path | None = None):
        self.memory_dir = memory_dir
        self.memory_file = memory_dir / "MEMORY.local.md"      # 长期记忆文件
        self.history_file = memory_dir / "history.jsonl"       # 热对话日志
        self.checkpoint_file = memory_dir / "_checkpoint.json"  # 中断恢复快照
        self.user_file = user_file
        self.memory_template = memory_template
        self._ensure()  # 确保目录和文件存在
        self.history_log = HistoryLog(self.memory_dir, self.history_file)

    def _ensure(self) -> None:
        """确保 memory/ 目录以及必要文件存在；处理旧版 MEMORY.md 升级迁移。"""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        # 兼容旧版本：将 MEMORY.md 重命名为 MEMORY.local.md
        legacy_memory = self.memory_dir / "MEMORY.md"
        if not self.memory_file.exists() and legacy_memory.exists():
            legacy_memory.replace(self.memory_file)
        if not self.memory_file.exists():
            # 优先从模板复制，否则写入默认内容
            if self.memory_template and self.memory_template.exists():
                self.memory_file.write_text(self.memory_template.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                self.memory_file.write_text("# 长期记忆\n\n此文件常驻上下文，记录核心目标、当前任务与关键事实。\n", encoding="utf-8")
        if not self.history_file.exists():
            self.history_file.write_text("")

    # ── 原始层：history.jsonl append-only 日志 ─────────────
    def append_history(self, role: str, content: Any, *, extra: dict[str, Any] | None = None) -> None:
        """向热日志追加一条记录；role 可为 user / assistant / model_call / tool_result 等。"""
        row: dict[str, Any] = {
            "ts": datetime.now(_UTC8).isoformat(timespec="seconds"),
            "role": role,
            "content": content if isinstance(content, str) else _json_safe(content),
        }
        if extra:  # 将 extra 字段合并进 row，但不覆盖基础字段
            for k, v in _json_safe(extra).items():
                if k not in row:
                    row[k] = v
        self.history_log.append(row)

    # ── 中期层（按日历日 UTC+8）────────────────────────────
    def today_episode_path(self) -> Path:
        date = datetime.now(_UTC8).strftime("%Y-%m-%d")
        return self.memory_dir / f"{date}.md"

    def read_today_episode(self) -> str:
        p = self.today_episode_path()
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def append_episode(self, content: str) -> None:
        p = self.today_episode_path()
        existing = p.read_text(encoding="utf-8") if p.exists() else f"# {p.stem} 情景记忆\n"
        new_text = existing.rstrip() + "\n\n" + content.strip() + "\n"
        p.write_text(new_text, encoding="utf-8")

    # ── 长期层 ──────────────────────────────────────────────
    def read_memory(self) -> str:
        return self.memory_file.read_text(encoding="utf-8") if self.memory_file.exists() else ""

    def write_memory(self, content: str) -> None:
        self.memory_file.write_text(content.strip() + "\n", encoding="utf-8")

    # ── 归档标记 ────────────────────────────────────────────
    def append_compact_marker(self, active_history: list[dict[str, Any]] | None = None) -> None:
        """写入压缩标记，并将已压缩原始行归档。

        active_history=None 时只写入 compact_event 行（兼容旧次项行为）；
        传入活跃行时触发热日志重写 + 冷归档。
        """
        if active_history is None:
            row = {"ts": datetime.now(_UTC8).isoformat(timespec="seconds"), "type": "compact_event"}
            self.history_log.append(row)
            return
        self.history_log.compact(active_history)

    def history_stats(self) -> dict[str, Any]:
        return self.history_log.stats()

    def load_unarchived_history(self) -> list:
        """返回最后一个 compact_event 之后的未归档对话条目。"""
        out: list[dict[str, Any]] = []
        for r in self.history_log.load_active_rows():
            if "role" not in r or "content" not in r:
                continue
            if r.get("type") == "model_call":
                continue
            item: dict[str, Any] = {"role": r["role"], "content": r["content"]}
            if isinstance(r.get("turn_id"), str):
                item["turn_id"] = r["turn_id"]
            if isinstance(r.get("attachments"), list):
                item["attachments"] = r["attachments"]
            if isinstance(r.get("displayContent"), str):
                item["displayContent"] = r["displayContent"]
            out.append(item)
        return out

    def load_unarchived_turn_ids(self) -> list[str]:
        """Return distinct turn ids after the latest compact marker."""
        ids: list[str] = []
        seen: set[str] = set()
        for item in self.load_unarchived_history():
            turn_id = item.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id or turn_id in seen:
                continue
            seen.add(turn_id)
            ids.append(turn_id)
        return ids

    # ── 用户偏好 ────────────────────────────────────────────
    def read_user(self) -> str:
        return self.user_file.read_text(encoding="utf-8") if self.user_file.exists() else ""

    def write_user(self, content: str) -> None:
        self.user_file.write_text(content.strip() + "\n", encoding="utf-8")

    # ── 中断恢复 Checkpoint ─────────────────────────────────
    def write_checkpoint(self, history: list[dict[str, Any]]) -> None:
        """原子写入当前 turn 的工作历史快照；失败静默（绝不能影响主流程）。"""
        try:
            payload = {
                "ts": datetime.now(_UTC8).isoformat(timespec="seconds"),
                "history": _json_safe(history),
            }
            tmp = self.checkpoint_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.checkpoint_file)
        except Exception:
            pass

    def read_checkpoint(self) -> list[dict[str, Any]] | None:
        """启动时读回上次未完成 turn 的 history；不存在或损坏返回 None。"""
        if not self.checkpoint_file.exists():
            return None
        try:
            data = json.loads(self.checkpoint_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        history = data.get("history") if isinstance(data, dict) else None
        return history if isinstance(history, list) else None

    def clear_checkpoint(self) -> None:
        """删除 checkpoint；turn 正常落地后调用。"""
        try:
            self.checkpoint_file.unlink(missing_ok=True)
        except Exception:
            pass


def _json_safe(obj: Any) -> Any:
    """Convert anthropic content blocks (or anything) into JSON-serialisable form."""
    try:
        json.dumps(obj, ensure_ascii=False)
        return obj
    except (TypeError, ValueError):
        pass
    if isinstance(obj, list):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return {k: _json_safe(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
    return str(obj)
