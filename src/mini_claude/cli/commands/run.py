from __future__ import annotations

import asyncio
import json
import time
import sys

from mini_claude.core.config import ClaudeConfig
from pydantic import BaseModel
from mini_claude.core.runner import AgentRunner
from mini_claude.core.transport.socket_client import SocketClient, IpcError
from typing import Any


class StdoutPrinter:
    def __init__(self) -> None:
        self._inline = False  # True while LLM tokens are mid-line
        self._run_start: float = 0.0
    
    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False
            
    async def handle(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")

        if t == "run.started":
            self._run_start = time.monotonic()
            print(f"[run] {event.get("run_id", "")}")

        elif t == "step.started":
            self._ensure_newline()
            print(f"[step {event.get("step")}] planning...")

        elif t == "llm.token":
            print(event.get("token", ""), end="", flush=True)
            self._inline = True

        elif t == "tool.call_started":
            self._ensure_newline()
            params_str = json.dumps(event.get("params", {}), ensure_ascii=False)
            print(f"[tool] {event.get("tool_name", "")} {params_str}")

        elif t == "tool.call_finished":
            print(f"[tool] {event.get("tool_name", "")} ✓  {event.get("elapsed_ms")}ms")

        elif t == "tool.call_failed":
            print(
                f"[tool] {event.get("tool_name", "")} ✗  {event.get("error_message", "")}",
                file=sys.stderr,
            )

        elif t == "step.finished":
            self._ensure_newline()
            print(f"[step {event.get("step")}] done")

        elif t == "run.finished":
            self._ensure_newline()
            elapsed = time.monotonic() - self._run_start
            print(f"[run] {event.get("status", "")}  {event.get("step")} steps  {elapsed:.1f}s")

async def _run_async(goal: str, config: ClaudeConfig) -> int:
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1

    printer = StdoutPrinter()
    finished = asyncio.Event()
    exit_code = 0

    async def on_event(event: dict[str, Any]) -> None:
        nonlocal exit_code
        await printer.handle(event)
        if event.get("type") == "run.finished":
            if event.get("status") != "success":
                exit_code = 1
            finished.set()
    
    client.on_event(on_event)
    loop_task = asyncio.create_task(client.run_event_loop())

    try:
        await client.send_command(
            "event.subscribe",
            {
                "topics": ["run.*", "step.*", "tool.*", "llm.token", "llm.usage"],
                "scope": "global",
            },
        )
        await client.send_command("agent.run", {"goal": goal})
    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        loop_task.cancel()
        await client.close()
        return 1

    await finished.wait()

    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass

    await client.close()
    return exit_code

def cmd_run(goal: str, config: ClaudeConfig) -> None:
    try:
        exit_code = asyncio.run(_run_async(goal, config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)