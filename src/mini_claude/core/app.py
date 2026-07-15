from __future__ import annotations

import asyncio
import datetime
import logging
import signal
import time
from typing import Any

import mini_claude
from mini_claude.core.bus.commands import PongResult
from mini_claude.core.config import get_config
from mini_claude.core.logging_setup import setup_logging
from mini_claude.core.transport.socket_server import SocketServer

logger = logging.getLogger(__name__)

def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()

class CoreApp:
    def __init__(self) -> None:
        self._start_time = time.monotonic()
    
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version = mini_claude.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=_now()
        )

    async def run(self) -> None:
        self._start_time = time.monotonic()
        config = get_config()
        setup_logging(config)

        server = SocketServer(config.host, config.port)
        server.register("core.ping", self._ping_handler)

        addr = await server.start()
        logger.info("claude-core %s listening addr=%s", mini_claude.__version__, addr)
        logger.info("config: %s", config)

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        loop.add_signal_handler(signal.SIGINT, shutdown.set)
        loop.add_signal_handler(signal.SIGTERM, shutdown.set)

        await shutdown.wait()

        logger.info("shutting down")
        await server.stop()

    

def run() -> None:
    asyncio.run(CoreApp().run())
