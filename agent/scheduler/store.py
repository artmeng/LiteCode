from __future__ import annotations

import json
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from filelock import FileLock

from .models import SCHEMA_VERSION, SchedulerJob, validate_job_id


class SchedulerStoreCorrupt(RuntimeError):
    pass


@dataclass
class SchedulerStoreData:
    version: int = SCHEMA_VERSION
    jobs: list[SchedulerJob] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SchedulerStoreData":
        jobs = []
        for item in raw.get("jobs") or []:
            if isinstance(item, dict):
                jobs.append(SchedulerJob.from_dict(item))
        return cls(version=int(raw.get("version") or SCHEMA_VERSION), jobs=jobs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": int(self.version or SCHEMA_VERSION),
            "jobs": [job.to_dict() for job in self.jobs],
        }


class SchedulerStore:
    """Durable store for local scheduled jobs.

    The store is intentionally limited to persistence and merge semantics. Timer
    execution lives in the scheduler service so tests can verify each layer in
    isolation.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.scheduler_dir = self.root / "memory" / "scheduler"
        self.jobs_file = self.scheduler_dir / "jobs.json"
        self.action_file = self.scheduler_dir / "action.jsonl"
        self.lock_file = self.scheduler_dir / "scheduler.lock"
        self._json_lock = RLock()
        self._file_lock = FileLock(str(self.lock_file))
        self._last_good: SchedulerStoreData | None = None
        self._ensure()

    def _ensure(self) -> None:
        self.scheduler_dir.mkdir(parents=True, exist_ok=True)
        if not self.jobs_file.exists():
            self._atomic_write_json(self.jobs_file, SchedulerStoreData().to_dict(), fsync=False)

    def load(self, *, merge_actions: bool = True, allow_last_good: bool = True) -> SchedulerStoreData:
        with self._json_lock:
            with self._file_lock:
                try:
                    data = self._read_store()
                except SchedulerStoreCorrupt:
                    if allow_last_good and self._last_good is not None:
                        return self._last_good
                    raise
                if merge_actions:
                    data = self._merge_actions(data)
                    self._last_good = data
                return data

    def save(self, data: SchedulerStoreData, *, fsync: bool = False) -> None:
        with self._json_lock:
            with self._file_lock:
                payload = data.to_dict()
                self._atomic_write_json(self.jobs_file, payload, fsync=fsync)
                self._last_good = SchedulerStoreData.from_dict(payload)

    def list_jobs(self, *, include_disabled: bool = True) -> list[SchedulerJob]:
        jobs = self.load().jobs
        if not include_disabled:
            jobs = [job for job in jobs if job.enabled]
        return sorted(jobs, key=lambda job: job.state.next_run_at_ms or float("inf"))

    def get_job(self, job_id: str) -> SchedulerJob | None:
        safe = validate_job_id(job_id)
        return next((job for job in self.load().jobs if job.id == safe), None)

    def upsert_job(self, job: SchedulerJob) -> SchedulerJob:
        with self._json_lock:
            data = self.load()
            jobs = []
            replaced = False
            for current in data.jobs:
                if current.id == job.id:
                    jobs.append(job)
                    replaced = True
                else:
                    jobs.append(current)
            if not replaced:
                jobs.append(job)
            data.jobs = jobs
            self.save(data)
        return job

    def remove_job(self, job_id: str) -> SchedulerJob | None:
        safe = validate_job_id(job_id)
        with self._json_lock:
            data = self.load()
            removed = next((job for job in data.jobs if job.id == safe), None)
            if removed is None:
                return None
            data.jobs = [job for job in data.jobs if job.id != safe]
            self.save(data)
            return removed

    def append_action(
        self,
        action: Literal["add", "update", "delete"],
        *,
        job: SchedulerJob | None = None,
        job_id: str | None = None,
    ) -> None:
        if action in {"add", "update"} and job is None:
            raise ValueError(f"job is required for action={action}")
        if action == "delete" and not job_id:
            raise ValueError("job_id is required for action=delete")
        payload: dict[str, Any] = {"action": action}
        if job is not None:
            payload["job"] = job.to_dict()
        if job_id:
            payload["jobId"] = validate_job_id(job_id)
        self.scheduler_dir.mkdir(parents=True, exist_ok=True)
        with self._json_lock:
            with self._file_lock:
                with self.action_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _read_store(self) -> SchedulerStoreData:
        try:
            raw = json.loads(self.jobs_file.read_text(encoding="utf-8") or "{}")
            if not isinstance(raw, dict):
                raise ValueError("scheduler store root must be an object")
            data = SchedulerStoreData.from_dict(raw)
            self._last_good = data
            return data
        except Exception as exc:
            backup = self.jobs_file.with_suffix(
                self.jobs_file.suffix + f".corrupt-{int(time.time())}-{uuid4().hex[:8]}"
            )
            with suppress(OSError):
                if self.jobs_file.exists():
                    self.jobs_file.rename(backup)
            raise SchedulerStoreCorrupt(
                f"scheduler store at {self.jobs_file} is corrupt; preserved at {backup}"
            ) from exc

    def _merge_actions(self, data: SchedulerStoreData) -> SchedulerStoreData:
        if not self.action_file.exists():
            return data
        jobs = {job.id: job for job in data.jobs}
        changed = False
        with self.action_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    action = json.loads(line)
                    kind = action.get("action")
                    if kind in {"add", "update"}:
                        job = SchedulerJob.from_dict(action.get("job") or {})
                        jobs[job.id] = job
                        changed = True
                    elif kind == "delete":
                        job_id = validate_job_id(str(action.get("jobId") or action.get("job_id") or ""))
                        if job_id in jobs:
                            jobs.pop(job_id, None)
                            changed = True
                except Exception:
                    continue
        if not changed:
            return data
        merged = SchedulerStoreData(version=data.version, jobs=list(jobs.values()))
        self._atomic_write_json(self.jobs_file, merged.to_dict(), fsync=False)
        self.action_file.write_text("", encoding="utf-8")
        return merged

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any], *, fsync: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                if fsync:
                    f.flush()
                    os.fsync(f.fileno())
            tmp.replace(path)
            if fsync:
                with suppress(PermissionError):
                    fd = os.open(str(path.parent), os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
