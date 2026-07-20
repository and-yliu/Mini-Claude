from __future__ import annotations

import argparse
import sys

from mini_claude.cli.commands.ping import cmd_ping
from mini_claude.cli.commands.run import cmd_run
from mini_claude.cli.commands.version import cmd_version
from mini_claude.cli.commands.trace import cmd_trace
from mini_claude.core.config import get_config
from mini_claude.core.logging_setup import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="miniClaude", description="Mini-Claude CLI")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(name="ping", help="Ping the core daemon")

    run_parser = subparsers.add_parser(name="run", help="Run an agent task")
    run_parser.add_argument("--goal", required=True, help="Goal for the agent to accomplish")

    trace_parser = subparsers.add_parser(name="trace", help="View system trace log")
    trace_parser.add_argument("run_id", nargs="?", default=None, help="Filter by run ID")
    trace_parser.add_argument("--layer", choices=["ipc", "event", "llm"], help="Filter by layer")
    trace_parser.add_argument("--direction", help="Filter by direction (e.g. CORE→LLM)")
    trace_parser.add_argument("--raw", action="store_true", help="Output raw NDJSON")
    trace_parser.add_argument("--follow", "-f", action="store_true", help="Follow new records")

    args = parser.parse_args()

    if args.version:
        cmd_version()
        return

    config = get_config()
    setup_logging(config)

    if args.command == "ping":
        cmd_ping(config)
    elif args.command == "run":
        cmd_run(args.goal, config)
    elif args.command == "trace":
        cmd_trace(
            args.run_id,
            config,
            layer=args.layer,
            direction=args.direction,
            raw=args.raw,
            follow=args.follow,
        )
    else:
        parser.print_help()
        sys.exit(1)