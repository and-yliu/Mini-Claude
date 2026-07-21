from __future__ import annotations

import asyncio
import json
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.binding import Binding
from textual.widgets import Label, Static
from textual.widget import Widget

from mini_claude.core.config import ClaudeConfig
from mini_claude.core.transport.socket_client import IpcError, SocketClient

def _preview(s: str, n: int) -> str:
        return s[:n] + "…" if len(s) > n else s


def _params_str(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False)


class LLMStreamBlock(Static):
    DEFAULT_CSS = "LLMStreamBlock { padding: 0 2; color: $text; }"

    def __init__(self) -> None:
        super().__init__("")
        self._text = ""
    
    def append_token(self, token: str):
        self._text += token
        self.update(self._text)

class ToolCallBlock(Widget):
    DEFAULT_CSS = """
    ToolCallBlock { height: auto; padding: 0 0; }
    ToolCallBlock > .detail { display: none; padding: 0 4; color: $text-muted; }
    ToolCallBlock.expanded > .detail { display: block; }
    """

    def __init__(self, tool_name: str, params: dict[str, Any]) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._params = params
        self._params_full = _params_str(params)
        self._output = ""
        self._elapsed_ms = 0
        self._is_error = False
        self._finished = False
    
    def compose(self) -> ComposeResult:
        yield Static(self._summary(), classes="summary")
        yield Static("", classes="detail")

    def _summary(self) -> str:
        params_preview = _preview(self._params_full, 60)
        icon = "[bold yellow]✎[/bold yellow]"
        line = f"  {icon} [bold]{self._tool_name}[/bold]  [dim]{params_preview}[/dim]"
        if self._finished:
            out_pre = _preview(self._output, 50)
            color = "red" if self._is_error else "dim"
            hint = "  [dim]▸ click to expand[/dim]" if len(self._output) > 50 else ""
            line += (
                f"\n  [dim]↳[/dim] [{color}]{out_pre}[/{color}]"
                f"  [dim]{self._elapsed_ms}ms[/dim]{hint}"
            )
        return line

    def set_result(self, output: str, elapsed_ms: int, *, is_error: bool = False) -> None:
        self._output = output
        self._elapsed_ms = elapsed_ms
        self._is_error = is_error
        self._finished = True
        if self.children:
            self.query_one(".summary", Static).update(self._summary())
    
    def on_click(self) -> None:
        if not self._finished:
            return
        if "expanded" in self.classes:
            self.remove_class("expanded")
        else:
            detail = self.query_one(".detail", Static)
            detail.update(
                f"[dim]params:[/dim]\n    {self._params_full}\n"
                f"[dim]output:[/dim]\n    {self._output}\n"
                f"[dim]elapsed:[/dim] {self._elapsed_ms}ms"
            )
            self.add_class("expanded")




class MiniClaudeTuiApp(App[None]):
    TITLE = "MiniClaude TUI"
    BINDINGS = [Binding("q", "quit", "Quit")]
    CSS = """
    Screen { background: $background; }
    #header {
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
    }
    #log-view {
        height: 1fr;
    }
    Static.run-header { color: cyan; padding: 1 2 0 2; }
    Static.step-divider { color: $text-muted; padding: 0 2; }
    Static.run-ok { color: green; padding: 0 2 1 2; }
    Static.run-err { color: red; padding: 0 2 1 2; }
    Static.usage { padding: 0 2; }
    Static.log-line { padding: 0 2; }
    """


    # init connection parameters and token buffer
    def __init__(self, host: str, port: int, replay_run_id: str | None = None) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._replay_run_id = replay_run_id
        self._client: SocketClient | None = None
        self._current_llm: LLMStreamBlock | None = None
        self._pending_tool_blocks: dict[str, ToolCallBlock] = {}

    
    # UI: top status bar and rolling log
    def compose(self) -> ComposeResult:
        yield Label("[bold]MiniClaude[/bold]  [dim]connecting...[/dim]", id="header")
        yield VerticalScroll(id="log-view")

    # run on application start
    def on_mount(self) -> None:
        self.run_worker(self._socket_loop(), exclusive=True, name="socket")
    
    # append a additional block to the end and scoll to end
    def _append(self, widget: Widget) -> None:
        log_view = self.query_one("#log-view", VerticalScroll)
        log_view.mount(widget)
        log_view.scroll_end(animate=False)

    # this LLMStreamBlock is done and next token will create a new block
    def _break_llm(self) -> None:
        self._current_llm = None

    async def _socket_loop(self) -> None:
        header = self.query_one("#header", Label)

        while True:
            client = SocketClient(self._host, self._port)
            self._client = None
            try:
                await client.connect()
            except (ConnectionRefusedError, OSError):
                header.update("[bold]MiniClaude[/bold]  [red]not connected — retrying...[/red]")
                await asyncio.sleep(2)
                continue
                
            self._client = client
            header.update(f"[bold]MiniClaude[/bold]  [dim]{self._host}:{self._port}[/dim]")
            loop_task = asyncio.create_task(client.run_event_loop())

            async def on_event(event: dict[str, Any]):
                self._handle_event(event)
            
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
                header.update(f"[bold]MiniClaude[/bold] [red]subscribe error: {e}[/red]")
            finally:
                if not loop_task.done():
                    loop_task.cancel()
                self._client = None
                self._break_llm()
                await client.close()

            header.update("[bold]MiniClaude[/bold]  [dim]disconnected — retrying...[/dim]")
            await asyncio.sleep(2)


    def _handle_event(self, event: dict[str, Any]):
        t = event.get("type", "")

        if t == "llm.token":
            token = event.get("token", "")
            if self._current_llm is None:
                llm_block = LLMStreamBlock()
                self._append(llm_block)
                self._current_llm = llm_block
            self._current_llm.append_token(token)
            return
        
        self._break_llm()

        if t == "run.started":
            run_id = event.get("run_id", "")
            goal = event.get("goal", "")
            self._append(Static(
                f"[bold cyan]▶ run[/bold cyan]  [dim]{run_id}[/dim]\n"
                f"  [dim]goal:[/dim] {goal}",
                classes="run-header",
            ))

        elif t == "step.started":
            step = event.get("step", "")
            self._append(Static(
                f"[dim]── step {step} {'─' * 48}[/dim]",
                classes="step-divider",
            ))

        elif t == "tool.call_started":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            params = event.get("params") or {}
            tc_block = ToolCallBlock(tool_name, params)
            self._pending_tool_blocks[tool_use_id] = tc_block
            self._append(tc_block)

        elif t == "tool.call_finished":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            output = str(event.get("output") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(output, elapsed_ms)
            
        elif t == "tool.call_failed":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            error_message = str(event.get("error_message") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(error_message, elapsed_ms, is_error=True)

        elif t == "run.finished":
            status = event.get("status", "")
            steps = event.get("steps", 0)
            reason = event.get("reason") or ""
            if status == "success":
                self._append(Static(
                    f"[bold green]✓ completed[/bold green]  [dim]{steps} steps[/dim]",
                    classes="run-ok",
                ))
            else:
                detail = f"  [dim]{reason}[/dim]" if reason else ""
                self._append(Static(
                    f"[bold red]✗ failed[/bold red]{detail}  [dim]{steps} steps[/dim]",
                    classes="run-err",
                ))

        elif t == "llm.usage":
            self._append(Static(
                f"[dim]  tokens  "
                f"in={event.get('input_tokens')} "
                f"out={event.get('output_tokens')} "
                f"cache={event.get('cache_read_input_tokens')}[/dim]",
                classes="usage",
            ))

        elif t == "log.line":
            level = event.get("level", "INFO")
            color = "bold red" if level == "ERROR" else ("yellow" if level == "WARNING" else "dim")
            self._append(Static(
                f"[{color}]{level}[/{color}]  "
                f"[dim]{event.get('source', '')}[/dim]  {event.get('message', '')}",
                classes="log-line",
            ))

def run(config: ClaudeConfig, replay_run_id: str | None = None) -> None:
    app = MiniClaudeTuiApp(config.host, config.port, replay_run_id=replay_run_id)
    app.run()