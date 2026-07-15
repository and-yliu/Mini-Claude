from __future__ import annotations

import asyncio
import json
import sys
import time

import mini_claude
from mini_claude.core.bus.commands import PongResult
from mini_claude.core.bus.envelope import JsonRpcError, JsonRpcSuccess
from mini_claude.core.config import ClaudeConfig

def cmd_ping(config: ClaudeConfig) -> None:
    try:
        asyncio.run(_ping(config))
    except(ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        sys.exit(1)


async def _ping(config: ClaudeConfig) -> None:
    t0 = time.monotonic()
    reader, writer = await asyncio.open_connection(host=config.host, port=config.port)

    req = {
        "jsonrpc": "2.0",
        "id": "cli-1",
        "method": "core.ping",
        "params": {"client": f"cli/{mini_claude.__version__}"},
    }

    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()

    line = await asyncio.wait_for(reader.readline(), timeout=10.0)
    latency_ms = int((time.monotonic() - t0) * 1000)

    writer.close()
    await writer.wait_closed()

    raw = json.loads(line)

    if "error" in raw:
        err = JsonRpcError.model_validate(raw)
        print(f"error: {err.error.code} {err.error.message}", file=sys.stderr)
        sys.exit(1)

    resp = JsonRpcSuccess.model_validate(raw)
    result = PongResult.model_validate(resp.result)
    print(f"pong server={result.server_version} uptime={result.uptime_ms}ms latency={latency_ms}ms")
