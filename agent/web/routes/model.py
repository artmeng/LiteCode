from __future__ import annotations

from aiohttp import web

from ..state import WebUIState


def register(app: web.Application, state: WebUIState) -> None:
    app.router.add_get("/api/model-config", state.get_model_config)
    app.router.add_post("/api/model-config", state.post_model_config)
    app.router.add_post("/api/model-test", state.model_test)
