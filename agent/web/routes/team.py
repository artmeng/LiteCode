from __future__ import annotations

from aiohttp import web

from ..state import WebUIState


def register(app: web.Application, state: WebUIState) -> None:
    service = state.team_service
    app.router.add_get("/api/team", service.get_team)
    app.router.add_post("/api/team/members", service.post_team_member)
    app.router.add_get("/api/team/members/{name}", service.get_team_member)
    app.router.add_post("/api/team/messages", service.post_team_message)
    app.router.add_post("/api/team/members/{name}/wake", service.post_team_wake)
    app.router.add_post("/api/team/members/{name}/shutdown", service.post_team_shutdown)
