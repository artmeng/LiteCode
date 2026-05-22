from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any

from loguru import logger

from .config import ServerConfig

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.sse import sse_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    ClientSession = None  # type: ignore[misc,assignment]
    StdioServerParameters = None  # type: ignore[misc,assignment]


class MCPConnection(ABC):
    """单个 MCP 服务器的连接抽象。"""

    def __init__(self, server_name: str, config: ServerConfig) -> None:
        self.server_name = server_name
        self.config = config
        self._session: Any = None
        self._client_ctx: Any = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @abstractmethod
    async def _enter_client(self) -> Any:
        """返回异步上下文管理器，其 __aenter__ 产生 (read_stream, write_stream)。"""
        ...

    async def connect(self) -> bool:
        if not MCP_AVAILABLE:
            logger.error(f"[MCP:{self.server_name}] mcp SDK not installed")
            return False
        try:
            self._client_ctx = await self._enter_client().__aenter__()
            read_stream, write_stream = self._client_ctx
            self._session = ClientSession(read_stream, write_stream)
            await self._session.__aenter__()
            await self._session.initialize()
            self._connected = True
            logger.info(f"[MCP:{self.server_name}] connected")
            return True
        except Exception as e:
            logger.warning(f"[MCP:{self.server_name}] connection failed: {e}")
            self._connected = False
            return False

    async def disconnect(self) -> None:
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception as e:
                logger.debug(f"[MCP:{self.server_name}] session cleanup error: {e}")
            self._session = None
        if self._client_ctx is not None:
            try:
                await self._client_ctx.__aexit__(None, None, None)
            except Exception as e:
                logger.debug(f"[MCP:{self.server_name}] client cleanup error: {e}")
            self._client_ctx = None
        self._connected = False
        logger.info(f"[MCP:{self.server_name}] disconnected")

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._session is None or not self._connected:
            return []
        try:
            result = await self._session.list_tools()
            return [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": t.inputSchema,
                }
                for t in result.tools
            ]
        except Exception as e:
            logger.warning(f"[MCP:{self.server_name}] list_tools failed: {e}")
            return []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if self._session is None or not self._connected:
            raise RuntimeError(f"MCP server '{self.server_name}' not connected")
        result = await self._session.call_tool(tool_name, arguments=arguments)
        texts: list[str] = []
        for item in result.content:
            if getattr(item, "type", None) == "text":
                texts.append(item.text)
            else:
                texts.append(str(item))
        output = "\n".join(texts) or "(empty result)"
        if result.isError:
            return f"Error: {output}"
        return output


# 允许传递给 MCP 子进程的环境变量白名单
_SAFE_ENV_KEYS = frozenset(
    {"PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TMPDIR", "TERM", "PWD"}
)


class StdioConnection(MCPConnection):
    """通过子进程 stdio 连接 MCP 服务器。"""

    async def _enter_client(self) -> Any:
        base_env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
        if self.config.env:
            base_env.update(self.config.env)
        params = StdioServerParameters(
            command=self.config.command or "",
            args=list(self.config.args),
            env=base_env or None,
        )
        return stdio_client(params)


class SSEConnection(MCPConnection):
    """通过 SSE HTTP 连接 MCP 服务器。"""

    async def _enter_client(self) -> Any:
        return sse_client(
            url=self.config.url or "",
            headers=self.config.headers,
        )
