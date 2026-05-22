from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from agent.memory import MemoryStore
from agent.memory_history import HistoryLog


def _archive_rows(memory_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted((memory_dir / "history_archive").glob("*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                rows.append(json.loads(line))
    return rows


def test_legacy_history_migration_archives_before_last_marker(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    history_file = memory_dir / "history.jsonl"
    legacy = [
        {"ts": "2026-05-01T10:00:00+08:00", "role": "user", "content": "old"},
        {"ts": "2026-05-01T10:01:00+08:00", "type": "compact_event"},
        {"ts": "2026-05-02T10:00:00+08:00", "role": "user", "content": "active"},
        {"ts": "2026-05-02T10:01:00+08:00", "role": "assistant", "content": "reply"},
    ]
    history_file.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in legacy) + "\n", encoding="utf-8")

    log = HistoryLog(memory_dir, history_file)
    active = log.load_active_rows()
    archived = _archive_rows(memory_dir)

    assert (memory_dir / "history.legacy-backup.jsonl").exists()
    assert [row.get("content") for row in active] == ["active", "reply"]
    assert len(archived) == 2
    assert len(active) + len(archived) == len(legacy)
    assert log.stats()["archive_files"] == 1


def test_compact_rewrites_hot_history_and_preserves_archive(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    log = HistoryLog(memory_dir, memory_dir / "history.jsonl")
    log.append({"role": "user", "content": "old", "turn_id": "turn_old"})
    log.append({"role": "assistant", "content": "old reply", "turn_id": "turn_old"})
    log.append({"role": "user", "content": "new", "turn_id": "turn_new"})
    log.append({"role": "assistant", "content": "new reply", "turn_id": "turn_new"})

    log.compact([
        {"role": "user", "content": "new", "turn_id": "turn_new"},
        {"role": "assistant", "content": "new reply", "turn_id": "turn_new"},
    ])

    active = log.load_active_rows()
    archived = _archive_rows(memory_dir)

    assert [row["content"] for row in active] == ["new", "new reply"]
    assert [row.get("content") for row in archived if row.get("role")] == ["old", "old reply"]
    assert any(row.get("type") == "compact_event" for row in archived)
    assert log.stats()["active_lines"] == 2


def test_memory_store_load_unarchived_reads_only_hot_log(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path / "memory", tmp_path / "USER.local.md")
    memory.append_history("user", "old", extra={"turn_id": "turn_old"})
    memory.append_history("assistant", "old reply", extra={"turn_id": "turn_old"})
    memory.append_history("user", "new", extra={"turn_id": "turn_new"})
    memory.append_compact_marker(active_history=[
        {"role": "user", "content": "new", "turn_id": "turn_new"},
    ])

    loaded = memory.load_unarchived_history()

    assert loaded == [{"role": "user", "content": "new", "turn_id": "turn_new"}]
    assert memory.history_stats()["archive_files"] == 1


def test_compact_archive_failure_keeps_hot_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    memory_dir = tmp_path / "memory"
    log = HistoryLog(memory_dir, memory_dir / "history.jsonl")
    log.append({"role": "user", "content": "keep"})
    before = (memory_dir / "history.jsonl").read_text(encoding="utf-8")

    def fail(_rows):
        raise OSError("archive failed")

    monkeypatch.setattr(log, "_append_archive", fail)
    with pytest.raises(OSError):
        log.compact([])

    assert (memory_dir / "history.jsonl").read_text(encoding="utf-8") == before
