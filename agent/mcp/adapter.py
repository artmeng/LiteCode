from __future__ import annotations

from typing import Any

from ..tools.base import Tool
from .connection import MCPConnection


class MCPToolAdapter(Tool):
    """将 MCP 服务器上的工具包装为 emperor-agent 的 Tool 子类。

    名称格式: mcp_{server_name}_{tool_name}
    这样可避免不同 MCP 服务器的工具名冲突，也便于 registry 区分 builtin/mcp。
    """

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        parameters_schema: dict[str, Any],
        connection: MCPConnection,
        read_only: bool = False,
        exclusive: bool = False,
    ) -> None:
        self._server_name = server_name
        self._tool_name = tool_name
        self._description = description
        self._parameters_schema = parameters_schema
        self._connection = connection
        self._read_only = read_only
        self._exclusive = exclusive

    @property
    def name(self) -> str:
        return f"mcp_{self._server_name}_{self._tool_name}"

    @property
    def description(self) -> str:
        return f"[MCP:{self._server_name}] {self._description}"

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters_schema

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def exclusive(self) -> bool:
        return self._exclusive

    def execute(self, **kwargs: Any) -> str:
        """同步接口：内部通过 run_sync 调用异步方法。

        AgentRunner._run_tool 已通过 asyncio.to_thread 在线程中执行，
        因此这里可以直接用 run_sync()。
        """
        from ..providers.base import run_sync
        return run_sync(self._connection.call_tool(self._tool_name, kwargs))
