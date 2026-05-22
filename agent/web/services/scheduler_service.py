from __future__ import annotations

from typing import Any

from aiohttp import web

from ...runtime import events as runtime_events
from ...scheduler import SchedulerJob, SchedulerPayload, SchedulerSchedule


class SchedulerWebService:
    def __init__(self, state) -> None:
        self.state = state

    async def get_scheduler(self, request: web.Request) -> web.Response:
        return self.state._json(self.scheduler())

    async def post_scheduler_job(self, request: web.Request) -> web.Response:
        body = await self.state._body(request)
        try:
            job = self.state.loop.scheduler_service.add_job(
                name=str(body.get("name") or "scheduled-job"),
                schedule=self._schedule_from_body(body),
                payload=self._payload_from_body(body),
                delete_after_run=bool(body.get("deleteAfterRun", body.get("delete_after_run", False))),
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(reason=str(exc)) from exc
        await self._broadcast_job(job, action="created")
        return self.state._json({"job": job.to_dict(), "scheduler": self.scheduler()}, status=201)

    async def patch_scheduler_job(self, request: web.Request) -> web.Response:
        body = await self.state._body(request)
        schedule = self._schedule_from_body(body) if "schedule" in body else None
        payload = self._payload_from_body(body) if "payload" in body else None
        try:
            result = self.state.loop.scheduler_service.update_job(
                request.match_info.get("id", ""),
                name=str(body["name"]) if "name" in body else None,
                schedule=schedule,
                payload=payload,
                delete_after_run=(
                    bool(body.get("deleteAfterRun", body.get("delete_after_run")))
                    if "deleteAfterRun" in body or "delete_after_run" in body
                    else None
                ),
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(reason=str(exc)) from exc
        if result == "not_found":
            raise web.HTTPNotFound(reason="scheduler job not found")
        if result == "protected":
            raise web.HTTPForbidden(reason="protected scheduler job cannot be updated")
        await self._broadcast_job(result, action="updated")
        return self.state._json({"job": result.to_dict(), "scheduler": self.scheduler()})

    async def post_scheduler_run(self, request: web.Request) -> web.Response:
        force = request.query.get("force", "true").lower() != "false"
        ok = await self.state.loop.scheduler_service.run_job(
            request.match_info.get("id", ""),
            force=force,
        )
        if not ok:
            raise web.HTTPNotFound(reason="scheduler job not found or disabled")
        return self.state._json({"ok": True, "scheduler": self.scheduler()})

    async def post_scheduler_pause(self, request: web.Request) -> web.Response:
        result = self.state.loop.scheduler_service.enable_job(
            request.match_info.get("id", ""),
            enabled=False,
        )
        if result == "not_found":
            raise web.HTTPNotFound(reason="scheduler job not found")
        await self._broadcast_job(result, action="paused")
        return self.state._json({"job": result.to_dict(), "scheduler": self.scheduler()})

    async def post_scheduler_resume(self, request: web.Request) -> web.Response:
        result = self.state.loop.scheduler_service.enable_job(
            request.match_info.get("id", ""),
            enabled=True,
        )
        if result == "not_found":
            raise web.HTTPNotFound(reason="scheduler job not found")
        await self._broadcast_job(result, action="resumed")
        return self.state._json({"job": result.to_dict(), "scheduler": self.scheduler()})

    async def delete_scheduler_job(self, request: web.Request) -> web.Response:
        result = self.state.loop.scheduler_service.remove_job(request.match_info.get("id", ""))
        if result == "not_found":
            raise web.HTTPNotFound(reason="scheduler job not found")
        if result == "protected":
            raise web.HTTPForbidden(reason="protected scheduler job cannot be deleted")
        await self._broadcast_job(result, action="deleted")
        return self.state._json({"deleted": result.id, "scheduler": self.scheduler()})

    def scheduler(self) -> dict[str, Any]:
        return {
            "status": self.state.loop.scheduler_service.status(),
            "jobs": [job.to_dict() for job in self.state.loop.scheduler_service.list_jobs()],
        }

    async def _broadcast_job(self, job: SchedulerJob, *, action: str) -> None:
        await self.state._broadcast_event(
            runtime_events.scheduler_job_update(job.to_dict(), action=action)
        )

    @staticmethod
    def _schedule_from_body(body: dict[str, Any]) -> SchedulerSchedule:
        raw = body.get("schedule") if isinstance(body.get("schedule"), dict) else body
        return SchedulerSchedule.from_dict({
            "kind": raw.get("kind") or raw.get("scheduleKind"),
            "atMs": raw.get("atMs") or raw.get("at_ms"),
            "everyMs": raw.get("everyMs") or raw.get("every_ms"),
            "expr": raw.get("expr") or raw.get("cronExpr") or raw.get("cron_expr"),
            "tz": raw.get("tz"),
        })

    @staticmethod
    def _payload_from_body(body: dict[str, Any]) -> SchedulerPayload:
        raw = body.get("payload") if isinstance(body.get("payload"), dict) else body
        return SchedulerPayload.from_dict({
            "kind": raw.get("kind") or raw.get("payloadKind") or "agent_turn",
            "message": raw.get("message") or "",
            "target": raw.get("target"),
            "deliver": raw.get("deliver", True),
            "meta": raw.get("meta") if isinstance(raw.get("meta"), dict) else {},
        })
