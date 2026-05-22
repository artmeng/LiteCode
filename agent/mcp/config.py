from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ServerConfig:
    name: str
    transport: str
    enabled: bool
    command: str | None = None
    args: tuple[str, ...] = field(default_factory=tuple)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    tool_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPConfig:
    servers: dict[str, ServerConfig]
    defaults: dict[str, Any]


DEFAULT_MCP_CONFIG: dict[str, Any] = {
    "servers": {},
    "defaults": {
        "read_only": False,
        "exclusive": False,
    },
}

_MCP_CONFIG_FILE = "mcp_config.json"

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    """递归替换字符串中的 ${ENV_VAR} 为环境变量值。"""
    if isinstance(value, str):
        def _repl(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(0))
        return _ENV_RE.sub(_repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_mcp_config(root: Path) -> MCPConfig:
    path = root / _MCP_CONFIG_FILE
    raw = copy.deepcopy(DEFAULT_MCP_CONFIG)
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
        loaded = _expand_env(loaded)
        _deep_merge(raw, loaded)
    return _parse_config(raw)


def _parse_config(raw: dict[str, Any]) -> MCPConfig:
    servers_raw = raw.get("servers", {})
    servers: dict[str, ServerConfig] = {}
    for name, cfg in servers_raw.items():
        if not isinstance(cfg, dict):
            continue
        transport = str(cfg.get("transport", "stdio"))
        servers[name] = ServerConfig(
            name=name,
            transport=transport,
            enabled=bool(cfg.get("enabled", True)),
            command=str(cfg.get("command", "")) or None,
            args=tuple(cfg.get("args", [])),
            env=dict(cfg.get("env", {})),
            url=str(cfg.get("url", "")) or None,
            headers=dict(cfg.get("headers", {})),
            tool_overrides=dict(cfg.get("tool_overrides", {})),
        )
    return MCPConfig(servers=servers, defaults=raw.get("defaults", DEFAULT_MCP_CONFIG["defaults"]))


def save_mcp_config(root: Path, raw: dict[str, Any]) -> None:
    """保存 MCP 配置到 mcp_config.json，先验证结构再写入。"""
    path = root / _MCP_CONFIG_FILE
    # 基础验证
    if not isinstance(raw.get("servers"), dict):
        raise ValueError("mcp_config: 'servers' must be an object")
    if not isinstance(raw.get("defaults"), dict):
        raw["defaults"] = DEFAULT_MCP_CONFIG["defaults"]
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
    return target
