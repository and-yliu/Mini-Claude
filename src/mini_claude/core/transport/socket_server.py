from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from mini_claude.core.bus.envelope import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    HandlerError,
    JsonRpcRequest,
    JsonRpcSuccess,
    make_error,
)
from mini_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster

logger = logging.getLogger(__name__)

type CommandHandler = Callable[[dict[str, Any]], Awaitable[Any]]

# The current writer for each connection-handling coroutine, allowing handlers to access the connection context.
_writer_var: ContextVar[asyncio.StreamWriter] = ContextVar("_writer_var")

def get_connection_writer():
    return _writer_var.get()

def _now() -> str:
    return datetime.now(UTC).isoformat()


_MAX_LINE_BYTES = 64 * 1024 * 1024  # 64 MB per frame


class SocketServer:
    def __init__(
        self,
        host: str,
        port: int,
        broadcaster: IpcEventBroadcaster | None = None
    ) -> None:
        self._host = host
        self._port = port
        self._handlers: dict[str, CommandHandler] = {}
        self._server: asyncio.AbstractServer
        self._broadcaster = broadcaster 

    # Register a command handler by method name
    def register(self, method: str, handler: CommandHandler) -> None:
        self._handlers[method] = handler

    # Start the TCP server; exit if the port is already in use
    async def start(self) -> str:
        try:
            _r, w = await asyncio.open_connection(self._host, self._port)
            w.close()
            await w.wait_closed()
            raise SystemExit(f"core already running at {self._host}:{self._port}")
        except (ConnectionRefusedError, OSError):
            pass

        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self._host,
            port=self._port,
            limit=_MAX_LINE_BYTES,
        )
        return f"{self._host}:{self._port}"

    # Shutdown the server: first disconnect all active connections, then wait for the server to fully close (up to 2 seconds)
    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            pass

    # Handle a single client connection, closing the write stream after completion
    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername", "<unknown>")
        logger.debug("client connected: %s", peer)
        try:
            await self._read_loop(reader, writer)
        finally:
            if self._broadcaster is not None:
                self._broadcaster.unsubscribe(writer)
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except TimeoutError:
                pass
            logger.debug("client disconnected: %s", peer)

    # continuously read newline-delimited JSON lines and dispatch them one by one
    async def _read_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        while True:
            try:
                line = await reader.readline()
            except asyncio.LimitOverrunError:
                await self._send(writer, make_error(None, INVALID_REQUEST, "Request too large"))
                return

            if not line:
                return

            # Each command is executed as a separate task to avoid long-running handlers (e.g., session.send_message)
            # blocking the read loop, allowing concurrent commands like permission.respond to be processed promptly
            asyncio.create_task(self._handle_line(line, writer))

    # Parse a single line of JSON-RPC request and call the corresponding handler, writing the result or error back to the client
    async def _handle_line(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            raw: Any = json.loads(line)
        except json.JSONDecodeError as e:
            await self._send(writer, make_error(None, PARSE_ERROR, f"Parse error: {e}"))
            return

        try:
            req = JsonRpcRequest.model_validate(raw)
        except ValidationError as e:
            await self._send(writer, make_error(None, INVALID_REQUEST, "Invalid Request", str(e)))
            return


        handler = self._handlers.get(req.method)
        if handler is None:
            await self._send(
                writer,
                make_error(req.id, METHOD_NOT_FOUND, f"Method not found: {req.method}"),
            )
            return

        _writer_var.set(writer)
        try:
            result = await handler(req.params)
        except HandlerError as e:
            await self._send(writer, make_error(req.id, e.code, str(e), e.data))
            return
        except ValidationError as e:
            await self._send(
                writer, make_error(req.id, INVALID_PARAMS, "Invalid params", str(e))
            )
            return
        except Exception as e:
            logger.exception("handler %s raised: %s", req.method, e)
            await self._send(writer, make_error(req.id, INTERNAL_ERROR, "Internal error"))
            return

        result_data: Any = result.model_dump() if isinstance(result, BaseModel) else result
        try:
            await self._send(writer, JsonRpcSuccess(id=req.id, result=result_data))
        except (ConnectionResetError, BrokenPipeError, OSError):
            logger.debug("client disconnected before response for %s", req.method)

    # Serialize pydantic message to JSON line and write to stream, then flush buffer
    async def _send(self, writer: asyncio.StreamWriter, msg: BaseModel) -> None:
        writer.write(msg.model_dump_json().encode() + b"\n")
        await writer.drain()
