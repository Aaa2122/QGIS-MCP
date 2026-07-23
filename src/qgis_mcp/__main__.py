from __future__ import annotations

import argparse
import asyncio
import logging

from .server import run_stdio


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server for a live QGIS session")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="WARNING",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()

