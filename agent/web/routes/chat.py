from __future__ import annotations

from aiohttp import web

from ..state import WebUIState


def register(app: web.Application, state: WebUIState) -> None:
    app.router.add_get("/ws", state.chat_service.ws_handler)
    app.router.add_get("/api/bootstrap", state.bootstrap)
    app.router.add_get("/{tail:.*}", state.static)
