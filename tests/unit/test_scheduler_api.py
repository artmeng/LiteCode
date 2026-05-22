from __future__ import annotations

import asyncio
from pathlib import Path

from agent.scheduler import SchedulerPayload, SchedulerSchedule, SchedulerService, SchedulerStore
from agent.web.services.scheduler_service import SchedulerWebService


class FakeLoop:
    def __init__(self, tmp_path: Path):
        self.scheduler_service = SchedulerService(SchedulerStore(tmp_path))


class FakeState:
    def __init__(self, tmp_path: Path):
        self.loop = FakeLoop(tmp_path)
        self.events: list[dict] = []

    async def _broadcast_event(self, event: dict) -> None:
        self.events.append(event)


def test_scheduler_payload_lists_status_and_jobs(tmp_path: Path) -> None:
    state = FakeState(tmp_path)
    service = SchedulerWebService(state)
    state.loop.scheduler_service.add_job(
        name="api",
        schedule=SchedulerSchedule(kind="every", every_ms=60_000),
        payload=SchedulerPayload(message="hello"),
    )

    payload = service.scheduler()

    assert payload["status"]["jobs"] == 1
    assert payload["status"]["enabled"] == 1
    assert payload["jobs"][0]["name"] == "api"


def test_scheduler_body_parsing_supports_nested_schedule_and_payload() -> None:
    body = {
        "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Shanghai"},
        "payload": {"kind": "team_wake", "target": "alice", "message": "check"},
    }

    schedule = SchedulerWebService._schedule_from_body(body)
    payload = SchedulerWebService._payload_from_body(body)

    assert schedule.kind == "cron"
    assert schedule.expr == "0 9 * * *"
    assert schedule.tz == "Asia/Shanghai"
    assert payload.kind == "team_wake"
    assert payload.target == "alice"


def test_scheduler_web_service_broadcasts_job_update(tmp_path: Path) -> None:
    state = FakeState(tmp_path)
    service = SchedulerWebService(state)
    job = state.loop.scheduler_service.add_job(
        name="api",
        schedule=SchedulerSchedule(kind="every", every_ms=60_000),
        payload=SchedulerPayload(message="hello"),
    )

    asyncio.run(service._broadcast_job(job, action="created"))

    assert state.events == [{
        "event": "scheduler_job_update",
        "job": job.to_dict(),
        "action": "created",
    }]
