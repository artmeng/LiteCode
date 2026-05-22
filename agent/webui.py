from __future__ import annotations

import argparse
from pathlib import Path

from aiohttp import web
from loguru import logger

from .web import WebUIState, create_app

__all__ = ["WebUIState", "create_app", "main"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the LiteCode Web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()

    root = Path(__file__).parent.parent
    logger.info(f"LiteCode Web UI: http://{args.host}:{args.port}")
    web.run_app(create_app(root), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
