from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .models import ControlState, SCHEMA_VERSION


class ControlStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.control_dir = self.root / "memory" / "control"
        self.state_file = self.control_dir / "state.json"
        self._lock = RLock()
        self._ensure()

    def _ensure(self) -> None:
        self.control_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_file.exists():
            self.save(ControlState())

    def load(self) -> ControlState:
        with self._lock:
            try:
                raw = json.loads(self.state_file.read_text(encoding="utf-8") or "{}")
            except (json.JSONDecodeError, OSError):
                return ControlState()
        if not isinstance(raw, dict):
            return ControlState()
        return ControlState.from_dict(raw)

    def save(self, state: ControlState) -> None:
        payload = state.to_dict()
        payload["version"] = int(payload.get("version") or SCHEMA_VERSION)
        with self._lock:
            self._atomic_write_json(self.state_file, payload)

    def _atomic_write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
