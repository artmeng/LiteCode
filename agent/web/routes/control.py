from __future__ import annotations

from aiohttp import web

from ..state import WebUIState


def register(app: web.Application, state: WebUIState) -> None:
    app.router.add_get("/api/control", state.get_control)
    app.router.add_post("/api/control/mode", state.post_control_mode)
    app.router.add_post("/api/control/interactions/{id}/cancel", state.post_control_cancel)
