"""Centralized loguru configuration for LiteCode."""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def configure(root: Path | None = None) -> None:
    """Configure loguru sinks. Safe to call multiple times."""
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:>7}</level> | <level>{message}</level>",
        level="INFO",
        colorize=True,
    )
    logger.add(
        sys.stderr,
        format="<red>{time:HH:mm:ss}</red> | <level>{level:>7}</level> | <red>{message}</red>",
        level="WARNING",
        colorize=True,
    )
    if root is not None:
        log_dir = root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_dir / "agent_{time:YYYY-MM-DD}.log",
            rotation="10 MB",
            retention="30 days",
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:>7} | {name}:{function}:{line} | {message}",
            backtrace=True,
            diagnose=True,
        )


__all__ = ["logger", "configure"]
