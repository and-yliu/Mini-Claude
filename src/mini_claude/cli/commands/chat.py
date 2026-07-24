from __future__ import annotations

import asyncio
import sys
from typing import Any

from mini_claude.core.config import ClaudeConfig
from mini_claude.core.transport.socket_client import IpcError, SocketClient

_DECISION_MAP: dict[str, str] = {
    "y": "allow_once",
    "a": "always_allow",
    "n": "deny_once",
    "d": "always_deny",
}

class ChatPrinter:
    def __init__(self) -> None:
        self._inline = False
        self.pending_permission_id: str | None = None
        self.permission_requested = asyncio.Event()

    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    async def handle(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "llm.token":
            print(event.get("token", ""), end="", flush=True)
            self._inline = True
        elif t == "tool.call_started":
            self._ensure_newline()
            print(f"[tool] {event.get('tool_name', '')}")
        elif t == "session.waiting_for_input":
            self._ensure_newline()
            print("[waiting for input]")
        elif t == "session.closed":
            self._ensure_newline()
            print("session closed.")
        elif t == "permission.requested":
            self._ensure_newline()
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            tool_use_id = str(event.get("tool_use_id", ""))
            print(f"[permission] {tool_name}  {param_preview}")
            print("  y=allow once  a=always allow  n=deny once  d=always deny")
            self.pending_permission_id = tool_use_id
            self.permission_requested.set()

async def _readline(prompt:str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)

async def _chat_async(config: ClaudeConfig) -> int:
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1
    
    printer = ChatPrinter()
    client.on_event(printer.handle)

    loop_task = asyncio.create_task(client.run_event_loop())

    try:
        await client.send_command(
            "event.subscribe",
            {
                "topics": ["session.*", "run.*", "tool.*", "llm.token", "permission.*"],
                "scope": "global",
            },
        )
        created = await client.send_command("session.create", params={"mode": "chat"})
        session_id = str(created["session_id"])
        print(f"[session: {session_id}]")

        run_task: asyncio.Task[dict[str, Any]] | None = None

        while True:
            if run_task is None:
                try:
                    line = await _readline("> ")
                except (EOFError, KeyboardInterrupt):
                    break

                content = line.strip()
                if not content:
                    continue

                run_task = asyncio.create_task(
                    client.send_command(
                        "session.send_message",
                        params={
                            "session_id": session_id,
                            "content": content,
                        },
                    )
                )
                continue

            permission_task = asyncio.create_task(
                printer.permission_requested.wait()
            )
            done, _ = await asyncio.wait(
                (run_task, permission_task),
                return_when=asyncio.FIRST_COMPLETED,
            )

            if run_task in done:
                if not permission_task.done():
                    permission_task.cancel()
                    try:
                        await permission_task
                    except asyncio.CancelledError:
                        pass
                await run_task
                run_task = None
                continue

            while True:
                try:
                    line = await _readline("permission> ")
                except (EOFError, KeyboardInterrupt):
                    return 130

                decision = _DECISION_MAP.get(line.strip().lower())
                if decision is None:
                    print(
                        "  enter y (allow once), a (always allow), "
                        "n (deny once), d (always deny)"
                    )
                    continue

                tool_use_id = printer.pending_permission_id
                if tool_use_id is None:
                    printer.permission_requested.clear()
                    break

                await client.send_command(
                    "permission.respond",
                    params={
                        "tool_use_id": tool_use_id,
                        "decision": decision,
                    },
                )
                printer.pending_permission_id = None
                printer.permission_requested.clear()
                break
        
        await client.send_command("session.close", params={"session_id": session_id})

    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        await client.close()
    
    return 0



def cmd_chat(config: ClaudeConfig) -> None:
    try:
        exit_code = asyncio.run(_chat_async(config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)
