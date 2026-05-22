"""Tests for MCP config loading and saving."""

import json
from pathlib import Path

import pytest

from agent.mcp.config import (
    DEFAULT_MCP_CONFIG,
    MCPConfig,
    ServerConfig,
    load_mcp_config,
    save_mcp_config,
)


class TestLoadMcpConfig:
    """Verify MCP config loading behavior."""

    def test_empty_config(self, tmp_path: Path) -> None:
        config = load_mcp_config(tmp_path)
        assert isinstance(config, MCPConfig)
        assert config.servers == {}
        assert config.defaults == DEFAULT_MCP_CONFIG["defaults"]

    def test_load_stdio_server(self, tmp_path: Path) -> None:
        raw = {
            "servers": {
                "fetch": {
                    "transport": "stdio",
                    "command": "uvx",
                    "args": ["mcp-server-fetch"],
                    "enabled": True,
                    "tool_overrides": {"fetch": {"read_only": True}},
                }
            },
            "defaults": {"read_only": False, "exclusive": False},
        }
        (tmp_path / "mcp_config.json").write_text(json.dumps(raw))
        config = load_mcp_config(tmp_path)
        assert "fetch" in config.servers
        server = config.servers["fetch"]
        assert server.transport == "stdio"
        assert server.command == "uvx"
        assert server.args == ("mcp-server-fetch",)
        assert server.enabled is True
        assert server.tool_overrides == {"fetch": {"read_only": True}}

    def test_load_sse_server(self, tmp_path: Path) -> None:
        raw = {
            "servers": {
                "remote": {
                    "transport": "sse",
                    "url": "http://localhost:3001/sse",
                    "headers": {"Authorization": "Bearer token"},
                    "enabled": False,
                }
            },
        }
        (tmp_path / "mcp_config.json").write_text(json.dumps(raw))
        config = load_mcp_config(tmp_path)
        server = config.servers["remote"]
        assert server.transport == "sse"
        assert server.url == "http://localhost:3001/sse"
        assert server.headers == {"Authorization": "Bearer token"}
        assert server.enabled is False

    def test_env_interpolation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_MCP_VAR", "interpolated_value")
        raw = {
            "servers": {
                "test": {
                    "transport": "stdio",
                    "command": "echo",
                    "env": {"MY_VAR": "${TEST_MCP_VAR}"},
                    "enabled": True,
                }
            },
        }
        (tmp_path / "mcp_config.json").write_text(json.dumps(raw))
        config = load_mcp_config(tmp_path)
        assert config.servers["test"].env["MY_VAR"] == "interpolated_value"

    def test_missing_env_fallback(self, tmp_path: Path) -> None:
        raw = {
            "servers": {
                "test": {
                    "transport": "stdio",
                    "command": "echo",
                    "env": {"MY_VAR": "${NONEXISTENT_VAR}"},
                    "enabled": True,
                }
            },
        }
        (tmp_path / "mcp_config.json").write_text(json.dumps(raw))
        config = load_mcp_config(tmp_path)
        assert config.servers["test"].env["MY_VAR"] == "${NONEXISTENT_VAR}"


class TestSaveMcpConfig:
    """Verify MCP config saving behavior."""

    def test_save_and_reload(self, tmp_path: Path) -> None:
        raw = {
            "servers": {
                "filesystem": {
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                    "enabled": True,
                }
            },
            "defaults": {"read_only": True, "exclusive": False},
        }
        save_mcp_config(tmp_path, raw)
        config = load_mcp_config(tmp_path)
        assert "filesystem" in config.servers
        assert config.defaults["read_only"] is True

    def test_save_invalid_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="servers.*must be an object"):
            save_mcp_config(tmp_path, {"defaults": {}})


class TestDefaults:
    """Verify default values are applied correctly."""

    def test_server_defaults_to_enabled(self, tmp_path: Path) -> None:
        raw = {
            "servers": {"test": {"transport": "stdio", "command": "echo"}}
        }
        (tmp_path / "mcp_config.json").write_text(json.dumps(raw))
        config = load_mcp_config(tmp_path)
        assert config.servers["test"].enabled is True

    def test_server_defaults_to_stdio(self, tmp_path: Path) -> None:
        raw = {"servers": {"test": {"command": "echo"}}}
        (tmp_path / "mcp_config.json").write_text(json.dumps(raw))
        config = load_mcp_config(tmp_path)
        assert config.servers["test"].transport == "stdio"
