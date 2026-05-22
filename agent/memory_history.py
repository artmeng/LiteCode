"""对话历史的热/冷分层存储。

- 热存储：history.jsonl，append-only，记录当前活跃段
- 冷存储：history_archive/YYYY-MM.jsonl.gz，已压缩原始行的 gzip 归档
- 索引：history_index.json，记录全局 seq 进度与归档统计
"""
from __future__ import annotations

import gzip
import json
import shutil
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4


_UTC8 = timezone(timedelta(hours=8))
_INDEX_VERSION = 1


class HistoryLog:
    """管理 history.jsonl 热日志与 history_archive/ 冷归档的分层存储器。

    核心职责：
    - append：向热日志追加新消息行，自动分配递增 seq
    - compact：将不再活跃的旧行归档至月度 .jsonl.gz，原子重写热日志
    - load_active_rows：读取热日志中的非 compact_event 行，用于重启恢复
    """

    def __init__(self, memory_dir: Path, history_file: Path):
        self.memory_dir = Path(memory_dir)
        self.history_file = Path(history_file)                            # 热日志文件
        self.archive_dir = self.memory_dir / "history_archive"            # 冷归档目录
        self.index_file = self.memory_dir / "history_index.json"          # 全局 seq 索引与统计
        self.legacy_backup = self.memory_dir / "history.legacy-backup.jsonl"  # 旧版迁移备份
        self._lock = RLock()   # 多线程写入保护
        self._ensure()

    def append(self, row: dict[str, Any]) -> dict[str, Any]:
        """向热日志追加一行，自动补全 seq / archived / ts 字段。"""
        with self._lock:
            index = self._load_index()
            payload = dict(row)
            payload.setdefault("seq", int(index.get("latest_seq") or 0) + 1)  # 全局递增序号
            payload.setdefault("archived", False)
            payload.setdefault("ts", datetime.now(_UTC8).isoformat(timespec="seconds"))
            with self.history_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            index["latest_seq"] = max(int(index.get("latest_seq") or 0), int(payload.get("seq") or 0))
            self._write_index(self._stats_from_index(index))
            return payload

    def compact(self, active_messages: list[dict[str, Any]]) -> None:
        """将热日志中不再活跃的旧行归档，并原子重写热日志只保留活跃行。

        active_messages 是 Compactor 保留的最近 K 条消息，用于比对哪些行仍然活跃。
        归档时末尾插入一条 compact_event 标记行，标识本次压缩边界。
        """
        with self._lock:
            hot_rows = self._read_hot_rows()
            # 构造压缩边界标记行，归档时追加在末尾
            marker = {
                "seq": self._next_seq(hot_rows),
                "ts": datetime.now(_UTC8).isoformat(timespec="seconds"),
                "type": "compact_event",
                "archived": True,
            }
            active_rows = self._active_rows_from_messages(active_messages, hot_rows)
            archived_rows = self._rows_to_archive(hot_rows, active_rows)
            archived_rows.append(marker)  # 边界标记一并写入冷归档
            if archived_rows:
                self._append_archive(archived_rows)
            self._rewrite_hot(active_rows)  # 原子重写热日志，只保留活跃行
            index = self._load_index()
            index["latest_seq"] = max(int(index.get("latest_seq") or 0), int(marker["seq"]))
            index["last_archive_at"] = marker["ts"]
            self._write_index(self._stats_from_index(index))

    def load_active_rows(self) -> list[dict[str, Any]]:
        """读取热日志中所有非 compact_event 行，用于重启后恢复 self.history。"""
        with self._lock:
            return [
                row for row in self._read_hot_rows()
                if row.get("type") != "compact_event"  # 跳过压缩边界标记行
            ]

    def stats(self) -> dict[str, Any]:
        """返回热日志与冷归档的统计信息（行数、字节数、归档文件列表等）。"""
        with self._lock:
            return self._stats_from_index(self._load_index())

    def _ensure(self) -> None:
        """确保目录与文件存在；首次运行（无 index）时触发旧版迁移。"""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        if not self.history_file.exists():
            self.history_file.write_text("", encoding="utf-8")
        if not self.index_file.exists():
            self._migrate_legacy_history()  # 首次启动或旧版升级：扫描热日志重建索引
        else:
            self._write_index(self._stats_from_index(self._load_index()))  # 刷新统计

    def _migrate_legacy_history(self) -> None:
        """将旧版 history.jsonl（无索引）迁移到热/冷分层格式。

        以最后一个 compact_event 为边界：之前的行归入冷归档，之后的行保留为热日志活跃段。
        原始文件备份为 history.legacy-backup.jsonl。
        """
        rows = self._read_hot_rows(assign_seq=True)  # 为无 seq 的旧行补分配序号
        if self.history_file.exists() and not self.legacy_backup.exists():
            shutil.copyfile(self.history_file, self.legacy_backup)  # 备份旧文件
        # 找到最后一个压缩边界标记，以此划分冷/热边界
        last_marker = -1
        for i, row in enumerate(rows):
            if row.get("type") == "compact_event":
                last_marker = i
        archived = rows[:last_marker + 1] if last_marker >= 0 else []
        active = rows[last_marker + 1:] if last_marker >= 0 else rows
        for row in archived:
            row["archived"] = True
        for row in active:
            row["archived"] = False
        if archived:
            self._append_archive(archived)  # 旧段写入冷归档
        self._rewrite_hot(active)           # 活跃段重写热日志
        latest = max((int(row.get("seq") or 0) for row in rows), default=0)
        self._write_index(self._stats_from_index({
            "version": _INDEX_VERSION,
            "latest_seq": latest,
            "migrated_at": datetime.now(_UTC8).isoformat(timespec="seconds"),
            "last_archive_at": archived[-1].get("ts") if archived else None,
        }))

    def _read_hot_rows(self, *, assign_seq: bool = False) -> list[dict[str, Any]]:
        """逐行解析热日志，跳过空行与 JSON 格式异常行。

        assign_seq=True 时为无 seq 字段的旧行自动分配递增序号（迁移场景使用）。
        """
        rows: list[dict[str, Any]] = []
        latest = 0
        try:
            with self.history_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # 损坏行直接跳过，不中断读取
                    if not isinstance(row, dict):
                        continue
                    if assign_seq and not isinstance(row.get("seq"), int):
                        latest += 1
                        row["seq"] = latest  # 旧行补 seq
                    else:
                        latest = max(latest, int(row.get("seq") or 0))
                    row.setdefault("archived", False)
                    rows.append(row)
        except OSError:
            return []
        return rows

    def _active_rows_from_messages(
        self,
        messages: list[dict[str, Any]],
        hot_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """从保留的活跃消息列表中匹配对应热日志行，构建活跃行集合。

        优先复用热日志中已有的同签名行（保留原始 seq / ts），
        匹配不到时为新消息生成新行（压缩后 Compactor 重组场景）。
        仅处理 user / assistant 角色，工具消息不落热日志。
        """
        # 按签名建立热日志行的查找表，支持同签名多条（同内容重复消息）
        hot_by_signature: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in hot_rows:
            hot_by_signature.setdefault(self._signature(row), []).append(row)

        active: list[dict[str, Any]] = []
        next_seq = self._next_seq(hot_rows) - 1
        for msg in messages:
            role = str(msg.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            if "content" not in msg:
                continue
            base = {
                "role": role,
                "content": msg.get("content"),
            }
            for key in ("turn_id", "attachments", "displayContent"):
                if key in msg:
                    base[key] = msg[key]
            signature = self._signature(base)
            existing = hot_by_signature.get(signature, [])
            if existing:
                # 复用热日志中已有行，保留原 seq / ts
                row = dict(existing.pop(0))
                row["archived"] = False
            else:
                # 热日志无对应行，生成新行（Compactor 重组后的新消息）
                next_seq += 1
                row = {
                    "seq": next_seq,
                    "ts": datetime.now(_UTC8).isoformat(timespec="seconds"),
                    "archived": False,
                    **_json_safe(base),
                }
            active.append(row)
        return active

    def _rows_to_archive(
        self,
        hot_rows: list[dict[str, Any]],
        active_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """计算热日志中需要归档的行：热日志有但活跃集合中没有的行。

        使用计数器处理同签名多条的情况，避免误归档重复消息。
        """
        # 统计活跃集合中每个签名出现的次数
        active_counts = Counter(self._signature(row) for row in active_rows)
        archived: list[dict[str, Any]] = []
        for row in hot_rows:
            sig = self._signature(row)
            if active_counts[sig] > 0:
                active_counts[sig] -= 1  # 消耗一个活跃配额，保留此行
                continue
            archived_row = dict(row)
            archived_row["archived"] = True
            archived.append(archived_row)
        return archived

    def _append_archive(self, rows: list[dict[str, Any]]) -> None:
        """将行按月份分组，gzip append 写入对应的 YYYY-MM.jsonl.gz 归档文件。"""
        # 按 ts 字段中的年月分组
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            month = self._archive_month(row)
            grouped.setdefault(month, []).append(row)
        for month, items in grouped.items():
            path = self.archive_dir / f"{month}.jsonl.gz"
            with gzip.open(path, "at", encoding="utf-8") as f:  # 追加模式，不覆盖旧归档
                for row in items:
                    f.write(json.dumps(_json_safe(row), ensure_ascii=False) + "\n")

    def _rewrite_hot(self, rows: list[dict[str, Any]]) -> None:
        """原子重写热日志：先写临时文件，再 rename 替换，避免写入中途崩溃导致文件损坏。"""
        for row in rows:
            row["archived"] = False
        # 使用随机临时文件名避免并发写冲突
        tmp = self.history_file.with_name(f".{self.history_file.name}.{uuid4().hex}.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(_json_safe(row), ensure_ascii=False) + "\n")
        tmp.replace(self.history_file)  # 原子 rename，POSIX 保证

    def _stats_from_index(self, index: dict[str, Any]) -> dict[str, Any]:
        """计算并返回完整的索引统计快照，用于写入 history_index.json 与对外暴露 stats()。"""
        hot_rows = self._read_hot_rows()
        archive_files = sorted(self.archive_dir.glob("*.jsonl.gz"))
        archives = [
            {
                "path": str(path.relative_to(self.memory_dir.parent)),
                "bytes": path.stat().st_size,
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime, _UTC8).isoformat(timespec="seconds"),
            }
            for path in archive_files
        ]
        hot_bytes = self.history_file.stat().st_size if self.history_file.exists() else 0
        archive_bytes = sum(item["bytes"] for item in archives)
        return {
            "version": _INDEX_VERSION,
            "latest_seq": int(index.get("latest_seq") or self._next_seq(hot_rows) - 1),
            "active_lines": len(hot_rows),
            "active_bytes": hot_bytes,
            "archive_files": len(archives),
            "archive_bytes": archive_bytes,
            "archives": archives,
            "last_archive_at": index.get("last_archive_at"),
            "migrated_at": index.get("migrated_at"),
            "hot_limit_lines": 2000,                              # 热日志行数软上限
            "hot_limit_bytes": 5 * 1024 * 1024,                  # 热日志字节软上限（5 MB）
            "needs_rotation": hot_bytes > 5 * 1024 * 1024 or len(hot_rows) > 2000,  # 是否需要轮转
        }

    def _load_index(self) -> dict[str, Any]:
        """读取 history_index.json，文件损坏或不存在时返回从热日志推断的最小索引。"""
        try:
            raw = json.loads(self.index_file.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError):
            return {"version": _INDEX_VERSION, "latest_seq": self._next_seq(self._read_hot_rows()) - 1}
        return raw if isinstance(raw, dict) else {"version": _INDEX_VERSION}

    def _write_index(self, index: dict[str, Any]) -> None:
        """原子写入 history_index.json，先写临时文件再 rename。"""
        payload = dict(index)
        payload["version"] = _INDEX_VERSION
        tmp = self.index_file.with_name(f".{self.index_file.name}.{uuid4().hex}.tmp")
        tmp.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.index_file)

    @staticmethod
    def _signature(row: dict[str, Any]) -> tuple[str, str, str]:
        """生成行的匹配签名 (role, turn_id, content_json)，用于热日志行与活跃消息的对应比对。"""
        role = str(row.get("role") or "")
        turn_id = str(row.get("turn_id") or "")
        content = json.dumps(_json_safe(row.get("content")), ensure_ascii=False, sort_keys=True)
        return role, turn_id, content

    @staticmethod
    def _next_seq(rows: list[dict[str, Any]]) -> int:
        """返回当前行列表最大 seq + 1，用于为新行分配递增序号。"""
        return max((int(row.get("seq") or 0) for row in rows), default=0) + 1

    @staticmethod
    def _archive_month(row: dict[str, Any]) -> str:
        """从行的 ts 字段中提取 YYYY-MM，用于决定写入哪个月度归档文件。"""
        ts = str(row.get("ts") or "")
        if len(ts) >= 7 and ts[4:5] == "-" and ts[7:8] in {"", "T", "-"}:
            return ts[:7]
        return datetime.now(_UTC8).strftime("%Y-%m")  # ts 异常时降级为当月


def _json_safe(obj: Any) -> Any:
    """递归将对象转换为 JSON 可序列化形式。

    优先直接序列化，失败时按类型降级处理：list/dict 递归、Pydantic model_dump、
    普通对象取 __dict__，最终兜底转为字符串。
    """
    try:
        json.dumps(obj, ensure_ascii=False)
        return obj
    except (TypeError, ValueError):
        pass
    if isinstance(obj, list):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if hasattr(obj, "model_dump"):       # Pydantic BaseModel
        return obj.model_dump()
    if hasattr(obj, "__dict__"):         # 普通对象
        return {k: _json_safe(v) for k, v in obj.__dict__.items() if not k.startswith("_")}
    return str(obj)                      # 兜底转字符串
