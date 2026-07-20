from __future__ import annotations

import asyncio
import datetime
import fnmatch
import json
import logging
import signal
import time
from pathlib import Path
from typing import Any
from pydantic import BaseModel

import mini_claude
from mini_claude.core.bus.commands import (
    AgentRunCommand,
    AgentRunResult,
    EventSubscribeCommand,
    EventSubscribeResult,
    PongResult,
)
from mini_claude.core.bus.envelope import EventPushEnvelope
from mini_claude.core.config import ClaudeConfig, get_config
from mini_claude.core.events.bus import EventBus
from mini_claude.core.logging_setup import setup_logging
from mini_claude.core.runner import AgentRunner
from mini_claude.core.runs import events_file, new_run_id
from mini_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster
from mini_claude.core.transport.socket_server import SocketServer, get_connection_writer
from mini_claude.core.trace.writer import TraceRecord, TraceWriter

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class CoreApp:
    def __init__(self) -> None:
        self._start_time = time.monotonic()
        self._bus = EventBus()
        self._broadcaster = IpcEventBroadcaster()
        self._current_run_task: asyncio.Task[None] | None = None
        self._config: ClaudeConfig | None = None
        self._trace: TraceWriter | None = None

    async def _ping_handler(self, params: dict[str, Any]) -> EventSubscribeResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version=mini_claude.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=_now(),
        )

    async def _subscribe_handler(self, params: dict[str, Any]) -> EventSubscribeResult:
        command = EventSubscribeCommand.model_validate(params)
        writer = get_connection_writer()

        replay_count = 0
        if command.replay_from_run is not None:
            replay_count = await self._replay_event(command.replay_from_run, writer, command.topics)

        sub_id = self._broadcaster.subscribe(writer, command.topics, command.scope)
        return EventSubscribeResult(subscription_id=sub_id, replayed_count=replay_count)

    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        assert self._config is not None
        command = AgentRunCommand.model_validate(params)

        if self._current_run_task and not self._current_run_task.done():
            raise RuntimeError("a run is already in progress")

        run_id = new_run_id()
        runner = AgentRunner(self._config, bus=self._bus, trace=self._trace)
        self._current_run_task = asyncio.create_task(runner.run(command.goal, run_id))

        return AgentRunResult(run_id=run_id)
    
    async def _trace_event_handler(self, event: BaseModel) -> None:
        assert self._trace is not None
        event_dict = event.model_dump()
        self._trace.emit(
            TraceRecord(
                ts=_now(),
                direction="CORE→CLIENT",
                layer="event",
                kind="event",
                run_id=event_dict.get("run_id"),
                data=event_dict
            )
        )

    async def _replay_event(self, run_id: str, writer: asyncio.StreamWriter, topics: list[str]) -> int:
        path = events_file(run_id)
        if not path.exists():
            return 0

        count = 0
        for line in path.read_text().splitlines():
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")

            if not any(fnmatch.fnmatch(event_type, topic) for topic in topics):
                continue

            envelope = EventPushEnvelope(event=event)
            writer.write(envelope.model_dump_json().encode + b"\n")
            count += 1

        if count:
            await writer.drain()

        return count

    async def run(self) -> None:
        self._start_time = time.monotonic()
        self._config = get_config()
        setup_logging(self._config)

        if self._config.trace.enable:
            trace_path = Path(self._config.trace.file).expanduser()
            self._trace = TraceWriter(trace_path)
            await self._trace.start()
            self._bus.subscribe(self._trace_event_handler)

        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        self._bus.subscribe(self._broadcaster.handle)

        server = SocketServer(
            self._config.host,
            self._config.port,
            broadcaster=self._broadcaster,
            trace=self._trace
        )

        server.register("core.ping", self._ping_handler)
        server.register("agent.run", self._agent_run_handler)
        server.register("event.subscribe", self._subscribe_handler)


        addr = await server.start()
        logger.info("claude-core %s listening addr=%s", mini_claude.__version__, addr)
        logger.info("config: %s", self._config)

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        loop.add_signal_handler(signal.SIGINT, shutdown.set)
        loop.add_signal_handler(signal.SIGTERM, shutdown.set)

        await shutdown.wait()

        logger.info("shutting down")
        await server.stop()
        if self._trace:
            self._trace.stop()


def run() -> None:
    asyncio.run(CoreApp().run())
