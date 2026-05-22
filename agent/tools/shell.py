from __future__ import annotations
import re
import subprocess
from pathlib import Path

from loguru import logger

from .base import Tool, tool_parameters
from .schema import StringSchema, tool_parameters_schema

# 危险命令模式（regex）——匹配即拒绝
_DENY_PATTERNS = (
    re.compile(r"\brm\s+-rf\s+/"),
    re.compile(r"\bmkfs\."),
    re.compile(r"\bdd\s+if="),
    re.compile(r":\s*\(\)\s*\{"),
    re.compile(r">\s*/dev/sda"),
    re.compile(r">\s*/dev/nvme"),
    re.compile(r"\bcurl\b"),
    re.compile(r"\bwget\b"),
    re.compile(r"\bpython3?\s+-c\b"),
    re.compile(r"\|.*\bsh\b"),
    re.compile(r"\|.*\bbash\b"),
)


@tool_parameters(tool_parameters_schema(
    command=StringSchema("要执行的 shell 命令"),
))
class RunCommand(Tool):
    name = "run_command"
    description = "在终端执行一条 shell 命令并返回输出"
    exclusive = True

    def __init__(self, workspace: Path | None = None):
        self._workspace = workspace

    def execute(self, command: str) -> str:
        logger.info(f"[执行命令]: {command}")

        for pattern in _DENY_PATTERNS:
            if pattern.search(command):
                logger.warning(f"Blocked dangerous command: {command[:120]}")
                return f"Error: command refused by safety policy (matches dangerous pattern: {pattern.pattern!r})"

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(self._workspace) if self._workspace else None,
            )
            output = (result.stdout or result.stderr).strip()
            logger.info(f"[命令输出]: {output[:500]}")
            return output
        except subprocess.TimeoutExpired:
            logger.warning(f"Command timed out (>120s): {command[:80]}")
            return "Error: command timed out after 120 seconds"
