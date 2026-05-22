from __future__ import annotations

from aiohttp import web

from ..state import WebUIState


def register(app: web.Application, state: WebUIState) -> None:
    app.router.add_get("/api/config", state.get_config)
    app.router.add_post("/api/config", state.post_config)
    app.router.add_post("/api/attachments", state.upload_attachment)
    app.router.add_get("/api/attachments/{id}/raw", state.attachment_raw)
    app.router.add_get("/api/mcp-config", state.get_mcp_config)
    app.router.add_post("/api/mcp-config", state.post_mcp_config)
