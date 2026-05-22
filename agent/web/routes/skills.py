from __future__ import annotations

from aiohttp import web

from ..state import WebUIState


def register(app: web.Application, state: WebUIState) -> None:
    app.router.add_get("/api/tools", state.get_tools)
    app.router.add_get("/api/skills", state.get_skills)
    app.router.add_get("/api/skill", state.get_skill)
    app.router.add_post("/api/skill", state.post_skill)
    app.router.add_delete("/api/skill", state.delete_skill)
    app.router.add_post("/api/skills/import", state.import_skills)
