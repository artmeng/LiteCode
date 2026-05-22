from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from ..state import WebUIState


class TeamService:
    def __init__(self, state: WebUIState):
        self.state = state

    async def get_team(self, request: web.Request) -> web.Response:
        return self.state._json(self.team())

    async def get_team_member(self, request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        try:
            return self.state._json(self.state.loop.team_manager.member_payload(name))
        except ValueError as exc:
            return self.state._json({"error": str(exc)}, status=404)

    async def post_team_member(self, request: web.Request) -> web.Response:
        body = await self.state._body(request)
        name = str(body.get("name") or "")
        role = str(body.get("role") or "")
        task = body.get("task")
        agent_type = body.get("agent_type")
        if not name or not role:
            raise web.HTTPBadRequest(reason="'name' and 'role' are required")

        result = await self._run_team_call(
            self.state.loop.team_manager.spawn_teammate,
            name=name,
            role=role,
            task=str(task) if task else None,
            agent_type=str(agent_type) if agent_type else None,
        )
        return self.state._json({"result": result, "team": self.team()})

    async def post_team_message(self, request: web.Request) -> web.Response:
        body = await self.state._body(request)
        to = str(body.get("to") or "")
        content = str(body.get("content") or "")
        wake = bool(body.get("wake", True))
        if not to or not content:
            raise web.HTTPBadRequest(reason="'to' and 'content' are required")

        result = await self._run_team_call(
            self.state.loop.team_manager.send_message,
            to=to,
            content=content,
            wake=wake,
        )
        return self.state._json({"result": result, "team": self.team()})

    async def post_team_wake(self, request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        result = await self._run_team_call(
            self.state.loop.team_manager.wake_teammate,
            name,
            purpose="manual wake",
        )
        return self.state._json({"result": result, "team": self.team()})

    async def post_team_shutdown(self, request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        result = await self._run_team_call(
            self.state.loop.team_manager.shutdown_teammate,
            name=name,
        )
        return self.state._json({"result": result, "team": self.team()})

    def team(self) -> dict[str, Any]:
        return self.state.loop.team_manager.payload()

    async def _run_team_call(self, fn, *args, **kwargs):
        async def emit(event: dict[str, Any]) -> None:
            await self.state._broadcast_event(event)

        loop = asyncio.get_running_loop()
        return await asyncio.to_thread(
            fn,
            *args,
            emit=emit,
            loop=loop,
            **kwargs,
        )
