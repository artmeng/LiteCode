from __future__ import annotations

import json
import time
from pathlib import Path
from threading import RLock
from typing import Any


class RuntimeEventStore:
    """Append-only durable event log for reconstructing the WebUI chat timeline."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.runtime_dir = self.root / "memory" / "runtime"
        self.events_file = self.runtime_dir / "events.jsonl"
        self._lock = RLock()
        self._latest_seq = 0
        self._ensure()
        self._latest_seq = self._scan_latest_seq()

    @property
    def latest_seq(self) -> int:
        return self._latest_seq

    def append(self, event: dict[str, Any], *, turn_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._latest_seq += 1
            payload = _json_safe(dict(event))
            payload["seq"] = self._latest_seq
            payload.setdefault("ts", time.time())
            if turn_id and not payload.get("turn_id"):
                payload["turn_id"] = turn_id
            with self.events_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return payload

    def replay_after(self, seq: int, *, limit: int | None = None) -> list[dict[str, Any]]:
        out = [event for event in self._iter_events() if int(event.get("seq") or 0) > seq]
        if limit and len(out) > limit:
            return out[-limit:]
        return out

    def recent(self, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        events = list(self._iter_events())
        return events[-limit:]

    def events_for_turns(self, turn_ids: list[str], *, limit: int | None = None) -> list[dict[str, Any]]:
        wanted = {str(item) for item in turn_ids if item}
        if not wanted:
            return []
        out = [
            event
            for event in self._iter_events()
            if str(event.get("turn_id") or "") in wanted
        ]
        if limit and len(out) > limit:
            return out[-limit:]
        return out

    def stats(self, *, active_turn_ids: list[str] | None = None) -> dict[str, Any]:
        events = list(self._iter_events())
        active = {str(item) for item in active_turn_ids or [] if item}
        active_events = [
            event for event in events
            if active and str(event.get("turn_id") or "") in active
        ]
        latest_ts = max((float(event.get("ts") or 0) for event in events), default=0)
        return {
            "path": str(self.events_file.relative_to(self.root)),
            "bytes": self.events_file.stat().st_size if self.events_file.exists() else 0,
            "events": len(events),
            "latestSeq": self._latest_seq,
            "latestTs": latest_ts or None,
            "activeTurnEvents": len(active_events),
            "activeTurns": len(active),
        }

    def _ensure(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if not self.events_file.exists():
            self.events_file.write_text("", encoding="utf-8")

    def _scan_latest_seq(self) -> int:
        latest = 0
        for event in self._iter_events():
            latest = max(latest, int(event.get("seq") or 0))
        return latest

    def _iter_events(self):
        try:
            with self.events_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(raw, dict) and isinstance(raw.get("event"), str):
                        yield raw
        except OSError:
            return


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        pass
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return {
            key: _json_safe(item)
            for key, item in value.__dict__.items()
            if not str(key).startswith("_")
        }
    return str(value)
