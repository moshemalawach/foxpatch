"""CLI entry point with argparse."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import AppConfig
from .exceptions import ConfigError
from .logging_config import setup_logging
from .orchestrator import Orchestrator


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="foxpatch",
        description="Automated GitHub issue resolution and PR review orchestrator",
    )
    parser.add_argument(
        "--config", "-c",
        required=True,
        help="Path to YAML config file",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single polling cycle then exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log actions without executing them",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override log level from config",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    try:
        config = AppConfig.from_yaml(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    log_level = args.log_level or config.logging.level
    setup_logging(
        level=log_level,
        file=config.logging.file,
        fmt=config.logging.format,
    )

    orchestrator = Orchestrator(config, dry_run=args.dry_run)
    asyncio.run(orchestrator.start(once=args.once))
