from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from .adapter import MCPToolAdapter
from .config import MCPConfig, ServerConfig, load_mcp_config
from .connection import MCPConnection, SSEConnection, StdioConnection


class MCPClient:
    """管理所有 MCP 服务器连接，负责发现工具并向 ToolRegistry 注册。

    使用方式：
        client = MCPClient(root_path)
        await client.initialize()   # 连接所有 enabled 服务器
        tools = client.get_tools()  # 获取所有适配后的 Tool 实例
        await client.close()        # 优雅关闭所有连接
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.config: MCPConfig | None = None
        self._connections: dict[str, MCPConnection] = {}
        self._tools: list[MCPToolAdapter] = []
        self._initialized = False

    async def initialize(self) -> None:
        """加载配置并连接所有启用的 MCP 服务器。"""
        if self._initialized:
            return
        self.config = load_mcp_config(self.root)
        defaults = self.config.defaults

        for name, server_cfg in self.config.servers.items():
            if not server_cfg.enabled:
                logger.info(f"[MCP] server '{name}' disabled, skipping")
                continue

            conn = self._create_connection(server_cfg)
            self._connections[name] = conn

            ok = await conn.connect()
            if not ok:
                logger.warning(f"[MCP] server '{name}' connection failed, tools unavailable")
                continue

            tools = await conn.list_tools()
            logger.info(f"[MCP:{name}] discovered {len(tools)} tools")

            for tool_def in tools:
                tool_name = tool_def["name"]
                overrides = server_cfg.tool_overrides.get(tool_name, {})
                # MCP SDK 使用 camelCase (inputSchema)，转换为 snake_case 以兼容现有 schema
                schema = tool_def.get("inputSchema", {})
                adapter = MCPToolAdapter(
                    server_name=name,
                    tool_name=tool_name,
                    description=tool_def.get("description", ""),
                    parameters_schema=schema,
                    connection=conn,
                    read_only=overrides.get("read_only", defaults.get("read_only", False)),
                    exclusive=overrides.get("exclusive", defaults.get("exclusive", False)),
                )
                self._tools.append(adapter)

        self._initialized = True
        total_servers = sum(1 for c in self._connections.values() if c.connected)
        logger.info(
            f"[MCP] total {len(self._tools)} tools from {total_servers} connected servers"
        )

    def _create_connection(self, cfg: ServerConfig) -> MCPConnection:
        if cfg.transport == "sse":
            return SSEConnection(cfg.name, cfg)
        return StdioConnection(cfg.name, cfg)

    def get_tools(self) -> list[MCPToolAdapter]:
        return list(self._tools)

    def get_connection(self, server_name: str) -> MCPConnection | None:
        return self._connections.get(server_name)

    async def close(self) -> None:
        """优雅关闭所有连接。"""
        for name, conn in list(self._connections.items()):
            try:
                await conn.disconnect()
            except Exception as e:
                logger.warning(f"[MCP:{name}] disconnect error: {e}")
        self._connections.clear()
        self._tools.clear()
        self._initialized = False
