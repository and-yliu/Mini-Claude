from __future__ import annotations

import asyncio
import json
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Label, RichLog

from mini_claude.core.transport.socket_client import IpcError, SocketClient

class MiniClaudeTuiApp(App[None]):
    TITLE = "MiniClaude TUI"
    BINDINGS = [Binding("q", "quit", "Quit")]
    CSS = """
    Screen { layout: vertical; }
    #status {
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
    }
    #log { height: 1fr; }
    """

    # init connection parameters and token buffer
    def __init__(self, host: str, port: int, replay_run_id: str | None = None) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._replay_run_id = replay_run_id
        self._token_buf = ""
    
    # UI: top status bar and rolling log
    def compose(self) -> ComposeResult:
        yield Label("● connecting...", id="status")
        yield RichLog(id="log", highlight=True, markup=True)

    def on_mount(self) -> None:
        self.run_worker(self._socket_loop(), exclusive=True, name="socket")

    async def _socket_loop(self) -> None:
        log = self.query_one("#log", RichLog)
        status = self.query_one("#status", Label)

        while True:
            client = SocketClient(self._host, self._port)
            try:
                await client.connect()
            except (ConnectionRefusedError, OSError):
                status.update("● not connected — retrying in 2s")
                await asyncio.sleep(2)
                continue
                
            status.update(f"● connected {self._host}:{self._port}")
            loop_task = asyncio.create_task(client.run_event_loop())

            async def on_event(event: dict[str, Any]):
                self._handle_event(event, log)
            
            client.on_event(on_event)

            try:
                params: dict[str, Any] = {
                    "topics": [
                        "run.*", "step.*", "tool.*",
                        "llm.token", "llm.usage", "log.*",
                    ],
                    "scope": "global",
                }
                if self._replay_run_id is not None:
                    params["replay_run_id"] = self._replay_run_id
                await client.send_command("event.subscribe", params)
                await loop_task
            except IpcError as e:
                status.update(f"● subscribe error — {e}")
            finally:
                if not loop_task.done():
                    loop_task.cancel()
                self._flush_tokens(log)
                await client.close()

            status.update("● disconnected — retrying in 2s")
            await asyncio.sleep(2)

    def _flush_tokens(self, log: RichLog) -> None:
        if self._token_buf:
            log.write(self._token_buf)
            self._token_buf = ""

    def _handle_event(self, event: dict[str, Any], log: RichLog):
        t = event.get("type", "")

        if t == "llm.token":
            self._token_buf += event.get("token", "")
        
        self._flush_tokens(log)

        if t == "run.started":
            log.write(
                f"[bold blue]▶ run[/bold blue]  {event.get('run_id', '')}  "
                f"{event.get('goal', '')}"
            )
        elif t == "step.started":
            log.write(f"[bold]  step {event.get('step')}[/bold]  planning...")
        elif t == "tool.call_started":
            params_str = json.dumps(event.get("params", {}), ensure_ascii=False)
            log.write(f"[green]  tool[/green]  {event.get('tool_name', '')}  {params_str}")
        elif t == "tool.call_finished":
            log.write(
                f"[green]  tool[/green]  {event.get('tool_name', '')} "
                f"✓  {event.get('elapsed_ms')}ms"
            )
        elif t == "tool.call_failed":
            log.write(
                f"[red]  tool[/red]  {event.get('tool_name', '')} "
                f"✗  {event.get('error_message', '')}"
            )
        elif t == "step.finished":
            log.write(f"  step {event.get('step')}  done")
        elif t == "run.finished":
            s = event.get("status", "")
            color = "green" if s == "success" else "red"
            log.write(f"[{color}]■ run[/{color}]  {s}  {event.get('steps')} steps")
        elif t == "llm.usage":
            log.write(
                f"[dim]  usage[/dim]  in={event.get('input_tokens')} "
                f"out={event.get('output_tokens')} "
                f"cache_read={event.get('cache_read_input_tokens')}"
            )
        elif t == "log.line":
            level = event.get("level", "INFO")
            log.write(
                f"[dim]{level}[/dim]  {event.get('source', '')}  {event.get('message', '')}"
            )