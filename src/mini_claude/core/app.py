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
    SessionCreateCommand,
    SessionCreateResult,
    SessionCloseCommand,
    SessionCloseResult,
    SessionSendMessageCommand,
    SessionSendMessageResult,
    SessionGetHistoryCommand,
    SessionGetHistoryResult,
    PermissionRespondCommand,
    PermissionRespondResult,
    SessionCompactCommand,
    SessionCompactResult
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
from mini_claude.core.session.manager import SessionManager, SessionStore, Session
from mini_claude.core.permissions.manager import PermissionManager, load_policy_file
from mini_claude.core.llm.provider import AnthropicProvider
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class CoreApp:
    def __init__(self) -> None:
        self._start_time = time.monotonic()
        self._bus = EventBus()
        self._broadcaster = IpcEventBroadcaster()
        self._running_runs: set[asyncio.Task[Any]] = set()
        self._config: ClaudeConfig | None = None
        self._trace: TraceWriter | None = None
        self._sessions: SessionManager | None = None
        self._permission_manager: PermissionManager | None = None

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

    async def _session_create_handler(self, params: dict[str, Any]) -> SessionCreateResult:
        assert self._sessions is not None
        command = SessionCreateCommand.model_validate(params)

        session: Session = await self._sessions.create(command.mode, command.title)

        return SessionCreateResult(session_id=session.id, status=session.status)
    
    async def _session_message_handler(self, params: dict[str, Any]) -> SessionSendMessageResult:
        assert self._sessions is not None
        command = SessionSendMessageCommand.model_validate(params)
        run_id = await self._sessions.send_message(sid=command.session_id, content=command.content)
        return SessionSendMessageResult(run_id=run_id)

    async def _session_history_handler(self, params: dict[str, Any]) -> SessionGetHistoryResult:
        assert self._sessions is not None
        command = SessionGetHistoryCommand.model_validate(params)
        history = await self._sessions.list_history(command.session_id)
        return SessionGetHistoryResult(messages=history)
    
    async def _session_close_handler(self, params: dict[str, Any]) -> SessionCloseResult:
        assert self._sessions is not None
        command = SessionCloseCommand.model_validate(params)
        await self._sessions.close(sid=command.session_id)
        return SessionCloseResult(status="closed")

    async def _permission_respond_handler(self, params: dict[str, Any]) -> PermissionRespondResult:
        command = PermissionRespondCommand.model_validate(params)
        logger.info("permission.respond received tool_use_id=%s decision=%s", command.tool_use_id, command.decision)

        if self._permission_manager is None:
            logger.error("permission.respond: PermissionManager not initialized")
            return PermissionRespondResult()
        self._permission_manager.response(command.tool_use_id, command.decision)
        return PermissionRespondResult()
    
    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        assert self._config is not None
        command = AgentRunCommand.model_validate(params)

        session = await self._sessions.create("one_shot", command.goal[:40])

        run_id = new_run_id()
        run_task = asyncio.create_task(self._sessions.send_message(session.id, session.title, run_id=run_id))
        self._running_runs.add(run_task)
        run_task.add_done_callback(self._running_runs.discard)

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

    async def _session_compact_handler(self, params: dict[str, Any]) -> SessionCompactResult:
        assert self._sessions is not None
        cmd = SessionCompactCommand.model_validate(params)
        result = await self._sessions.compact(cmd.session_id, cmd.focus)
        return result

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

        policy_file = Path("~/.mini/policy.toml").expanduser()
        self._permission_manager = PermissionManager(policy_file=policy_file, timeout_s=self._config.permission.timeout_s)
        logger.info(
            "permission manager: timeout_s=%.1f  persistent=%d entries",
            self._config.permission.timeout_s,
            len(load_policy_file(policy_file)),
        )

        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        self._bus.subscribe(self._broadcaster.handle)
        sessions_root = Path("~/.mini/sessions").expanduser()
        store = SessionStore(sessions_root)
        compact_provider = AnthropicProvider(model=self._config.llm.default_model)
        self._sessions = SessionManager(
            store, 
            runner_factory= lambda: AgentRunner(self._config, bus=self._bus, trace=self._trace, permission_manager=self._permission_manager),
            bus=self._bus,
            provider=compact_provider
        )

        server = SocketServer(
            self._config.host,
            self._config.port,
            broadcaster=self._broadcaster,
            trace=self._trace
        )

        server.register("core.ping", self._ping_handler)
        server.register("agent.run", self._agent_run_handler)
        server.register("event.subscribe", self._subscribe_handler)
        server.register("session.create", self._session_create_handler)
        server.register("session.send_message", self._session_message_handler)
        server.register("session.get_history", self._session_history_handler)
        server.register("session.close", self._session_close_handler)
        server.register("permission.respond", self._permission_respond_handler)
        server.register("session.compact", self._session_compact_handler)


        addr = await server.start()
        logger.info("claude-core %s listening addr=%s", mini_claude.__version__, addr)
        logger.info("config: %s", self._config)

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        loop.add_signal_handler(signal.SIGINT, shutdown.set)
        loop.add_signal_handler(signal.SIGTERM, shutdown.set)

        await shutdown.wait()

        logger.info("shutting down")
        for run_task in list(self._running_runs):
            run_task.cancel()
        if self._running_runs:
            await asyncio.gather(*self._running_runs, return_exceptions=True)
        await server.stop()
        if self._trace:
            await self._trace.stop()


def run() -> None:
    asyncio.run(CoreApp().run())
