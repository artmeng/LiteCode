from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.scheduler import (
    SchedulerJob,
    SchedulerPayload,
    SchedulerRunRecord,
    SchedulerSchedule,
    SchedulerStatus,
    SchedulerStore,
    SchedulerStoreCorrupt,
)
from agent.scheduler.store import SchedulerStoreData


def make_job(job_id: str = "job-1", *, name: str = "job") -> SchedulerJob:
    return SchedulerJob.create(
        job_id=job_id,
        name=name,
        schedule=SchedulerSchedule(kind="every", every_ms=60_000),
        payload=SchedulerPayload(kind="agent_turn", message="ping"),
        now=1_700_000_000_000,
    )


def test_store_initializes_scheduler_tree(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path)

    assert (tmp_path / "memory" / "scheduler" / "jobs.json").exists()
    assert store.load().to_dict() == {"version": 1, "jobs": []}


def test_job_roundtrip_supports_camel_case_payload(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path)
    job = make_job()
    job.state.next_run_at_ms = 1_700_000_060_000
    job.state.record_run(
        run_at_ms=1_700_000_000_000,
        status=SchedulerStatus.OK.value,
        duration_ms=12,
    )

    store.upsert_job(job)
    loaded = store.get_job("job-1")

    assert loaded is not None
    assert loaded.id == "job-1"
    assert loaded.schedule.every_ms == 60_000
    assert loaded.payload.message == "ping"
    assert loaded.state.run_history[0].status == "ok"
    raw = json.loads(store.jobs_file.read_text(encoding="utf-8"))
    assert raw["jobs"][0]["schedule"]["everyMs"] == 60_000
    assert raw["jobs"][0]["state"]["runHistory"][0]["durationMs"] == 12


def test_run_history_is_trimmed() -> None:
    state = make_job().state
    for i in range(25):
        state.record_run(run_at_ms=i, status="ok")

    assert len(state.run_history) == 20
    assert state.run_history[0].run_at_ms == 5


def test_upsert_and_remove_job(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path)
    store.upsert_job(make_job("job-1", name="old"))
    store.upsert_job(make_job("job-1", name="new"))

    assert [job.name for job in store.list_jobs()] == ["new"]
    removed = store.remove_job("job-1")
    assert removed is not None
    assert store.list_jobs() == []


def test_action_log_merge_add_update_delete(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path)
    store.append_action("add", job=make_job("job-1", name="first"))
    store.append_action("update", job=make_job("job-1", name="second"))
    store.append_action("add", job=make_job("job-2", name="keep"))
    store.append_action("delete", job_id="job-1")

    jobs = store.load().jobs

    assert [(job.id, job.name) for job in jobs] == [("job-2", "keep")]
    assert store.action_file.read_text(encoding="utf-8") == ""


def test_corrupt_store_is_preserved_and_not_overwritten(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path)
    store.jobs_file.write_text("{not json", encoding="utf-8")

    with pytest.raises(SchedulerStoreCorrupt):
        store.load(allow_last_good=False)

    backups = list(store.scheduler_dir.glob("jobs.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not json"
    assert not store.jobs_file.exists()


def test_corrupt_store_can_return_last_good_snapshot(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path)
    store.upsert_job(make_job("job-1"))
    assert len(store.load().jobs) == 1
    store.jobs_file.write_text("\x00bad", encoding="utf-8")

    snapshot = store.load(allow_last_good=True)

    assert [job.id for job in snapshot.jobs] == ["job-1"]


def test_atomic_write_failure_preserves_previous_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SchedulerStore(tmp_path)
    store.save(SchedulerStoreData(jobs=[make_job("job-1")]))
    original = store.jobs_file.read_bytes()
    real_open = Path.open

    def boom(path: Path, *args, **kwargs):
        if path.name.startswith(".jobs.json.") and path.name.endswith(".tmp"):
            raise OSError("disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", boom)

    with pytest.raises(OSError, match="disk full"):
        store.save(SchedulerStoreData(jobs=[make_job("job-2")]))

    assert store.jobs_file.read_bytes() == original


def test_store_load_skips_invalid_action_lines(tmp_path: Path) -> None:
    store = SchedulerStore(tmp_path)
    store.action_file.write_text(
        "not json\n"
        + json.dumps({"action": "add", "job": make_job("job-1").to_dict()}) + "\n"
        + json.dumps({"action": "delete", "jobId": "../bad"}) + "\n",
        encoding="utf-8",
    )

    assert [job.id for job in store.load().jobs] == ["job-1"]
