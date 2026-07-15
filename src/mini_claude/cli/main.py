from __future__ import annotations

import argparse
import sys

from mini_claude.cli.commands.ping import cmd_ping
from mini_claude.cli.commands.version import cmd_version
from mini_claude.core.config import get_config
from mini_claude.core.logging_setup import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="miniClaude", description="Mini-Claude CLI")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(name="ping", help="Ping the core daemon")

    args = parser.parse_args()

    if args.version:
        cmd_version()
        return

    if args.command == "ping":
        config = get_config()
        setup_logging(config)
        cmd_ping(config)
    else:
        parser.print_help()
        sys.exit(1)