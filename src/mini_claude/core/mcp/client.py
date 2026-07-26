from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


class McpServerUnavailableError(Exception):
    pass


class McpToolError(Exception):
    """MCP server response error (connection ok, but response fail）"""
    pass

@dataclass
class McpToolDef:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


class McpClient:
    def __init__(self) -> None:
        self._id = 0
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._transport = ""
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None

    _STREAM_LIMIT = 64 * 1024 * 1024  # 64 MB，prevent large response to trigger LimitOverrunError

    # use stdio to connect to MCP server
    async def connect_stdio(self, command: str, args: list[str], env:dict[str, str] | None = None) -> None:
        import os
        merged_env = {**os.environ, **(env or {})}
        self._proc = await asyncio.create_subprocess_exec(
            command, 
            *args, 
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            limit=self._STREAM_LIMIT
        )
        self._reader = self._proc.stdout
        self._writer_proc = self._proc.stdin
        self._transport = "stdio"
        # backend constantly reading stderr, to avoid pipe buffer full 
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._initialize()

    # use tcp to connect to MCP server
    async def connect_tcp(self, host: str, port: int) -> None:
        self._reader, tcp_writer = await asyncio.open_connection(host, port, limit=self._STREAM_LIMIT)
        self._tcp_writer = tcp_writer
        self._transport = "tcp"
        await self._initialize()

    # mcp handshake
    async def _initialize(self) -> None:
        await self._call(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "mini-claude", "version": "0.1"}
            }
        )
        await self._notify("notifications/initialized", {})

    # list tools of MCP server
    async def list_tools(self) -> list[McpToolDef]:
        response = await self._call("tools/list", {})
        tool = []
        for t in response.get("tools", []):
            tool.append(
                McpToolDef(
                    name = t.get("name", ""),
                    description=t.get("description", ""),
                    input_schema=t.get("inputSchema", {})
                )
            )
        return tool

    # call the tool on the MCP server, return all text response
    async def tool_call(self, name: str, arguments: dict[str: Any]) -> str:
        response = await self._call(
            "tools/call", 
            {
                "name": name,
                "arguments": arguments
            }
        )

        parts: list[str] = []
        for part in response.get("content", []):
            if part.get("type") == "text":
                parts.append(str(part["text"]))

        return '\n'.join(parts)

    # background task, reading the stderr 
    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                stderr_line = line.decode(errors="replace").rstrip()
                if stderr_line:
                    log.debug("mcp stderr: %s", stderr_line)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.debug("mcp stderr drain stopped", exc_info=True)

    # close connection
    async def close(self) -> None:
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        if self._transport == "stdio" and self._proc is not None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        elif self._transport == "tcp":
            writer: asyncio.StreamWriter = getattr(self, "_tcp_writer", None)
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    #send JSON-RPC request and wait for response
    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._id += 1
        req_id = self._id
        req_id_str = str(req_id)
        request = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}

        async with self._lock:
            await self._write_line(json.dumps(request))
            while True:
                line = await self._read_line()
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("mcp: ignoring non-JSON line: %r", line[:200])
                    continue

                msg_id = msg.get("id")
                if msg_id is None:
                    log.debug("mcp: received server notification: %s", msg.get("method"))
                    continue

                if str(msg_id) == req_id_str:
                    if "error" in msg:
                        err = msg["error"]
                        raise McpToolError(
                            f"{err.get('message', str(err))} (code={err.get('code')})"
                        )
                    result: dict[str, Any] = msg.get("result", {})
                    return result

    # send JSON-RPC notification, no respsone
    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        notification = {"jsonrpc": "2.0", "method": method, "params": params}
        await self._write_line(json.dumps(notification))

    # write to mcp server a line of json
    async def _write_line(self, line: str) -> None:
        data = (line + "\n").encode()
        if self._transport == "stdio":
            w = self._proc.stdin if self._proc else None
            if w is None:
                raise McpServerUnavailableError("stdio writer unavailable")
            w.write(data)
            await w.drain()
        elif self._transport == "tcp":
            w = getattr(self, "_tcp_writer", None)
            if w is None:
                raise McpServerUnavailableError("tcp writer unavailable")
            w.write(data)
            await w.drain()

    # read from mcp server a line of json
    async def _read_line(self) -> str:
        if self._reader is None:
            raise McpServerUnavailableError("reader unavailable")
        while True:
            try:
                data = await asyncio.wait_for(self._reader.readline(), timeout=30.0)
            except TimeoutError:
                raise McpServerUnavailableError("MCP server read timeout")
            except asyncio.LimitOverrunError as exc:
                raise McpServerUnavailableError(
                    f"MCP response too large (>{self._STREAM_LIMIT // 1024 // 1024}MB): {exc}"
                ) from exc

            if data == b"":
                raise McpServerUnavailableError("MCP server closed connection")

            line = data.decode(errors="replace").strip()
            if line:
                return line
