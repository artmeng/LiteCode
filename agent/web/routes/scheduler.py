from __future__ import annotations

from aiohttp import web

from ..state import WebUIState


def register(app: web.Application, state: WebUIState) -> None:
    service = state.scheduler_web_service
    app.router.add_get("/api/scheduler", service.get_scheduler)
    app.router.add_post("/api/scheduler/jobs", service.post_scheduler_job)
    app.router.add_patch("/api/scheduler/jobs/{id}", service.patch_scheduler_job)
    app.router.add_post("/api/scheduler/jobs/{id}/run", service.post_scheduler_run)
    app.router.add_post("/api/scheduler/jobs/{id}/pause", service.post_scheduler_pause)
    app.router.add_post("/api/scheduler/jobs/{id}/resume", service.post_scheduler_resume)
    app.router.add_delete("/api/scheduler/jobs/{id}", service.delete_scheduler_job)
