from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mini_claude.core.bus.events import RunFinishedEvent, RunStartedEvent
from mini_claude.core.config import ClaudeConfig
from mini_claude.core.context import ExecutionContext
from mini_claude.core.events.bus import EventBus, EventHandler
from mini_claude.core.events.writer import EventWriter
from mini_claude.core.llm.base import LLMProvider
from mini_claude.core.llm.provider import AnthropicProvider
from mini_claude.core.loop import AgentLoop
from mini_claude.core.mcp.server import McpServerManager
from mini_claude.core.runs import RUNS_DIR, new_run_id, ensure_run_dir
from mini_claude.core.subagent.registry import BackgroundTaskRegistry
from mini_claude.core.subagent.tool import AgentResultTool, SubagentTool
from mini_claude.core.tools.builtin import (
    BashTool,
    ListDirTool,
    ReadFileTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WriteFileTool,
    NoteSaveTool
)
from mini_claude.core.tools.registry import ToolRegistry
from mini_claude.core.trace.writer import TraceWriter, TraceRecord
from mini_claude.core.trace.provider import TracingProvder
from mini_claude.core.task.manager import TaskManager
from mini_claude.core.session.manager import Session, SessionStore
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.memory.loader import load_context_file
from mini_claude.core.compact.compactor import Compactor

def _now(): 
    return datetime.now(UTC).isoformat()

@dataclass
class RunOutcome:
    status: str
    result: str
    reason: str | None

class AgentRunner:
    # assemble all dependency, to execute a full agent run
    def __init__(
        self,
        config: ClaudeConfig,
        *,
        bus: EventBus | None = None,
        provider: LLMProvider | None = None,
        extra_handlers: list[EventHandler] | None = None,
        runs_dir: Path | None = None,
        trace: TraceWriter | None = None,
        permission_manager: PermissionManager | None = None,
        mcp_manager: McpServerManager | None = None
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._runs_dir = runs_dir or RUNS_DIR
        self._trace = trace
        self._permission_manager = permission_manager
        self._task_registry = BackgroundTaskRegistry()
        self._mcp_manager = mcp_manager
    
    def _build_registry(
        self, 
        task_manager: TaskManager, 
        *,
        store: SessionStore | None = None, 
        session: Session | None = None, 
        run_id: str | None = None,
        provider: LLMProvider | None = None,
        bus: EventBus | None = None,
        child_runs_dir: Path | None = None,
        session_id: str = "",
        tool_whitelist: list[str] | None = None,
    ) -> ToolRegistry:
        allowed: set[str] | None = set(tool_whitelist) if tool_whitelist else None

        def _ok(name: str) -> bool:
            return allowed is None or name in allowed

        
        registry = ToolRegistry()

        for t in [ReadFileTool(), BashTool(), WriteFileTool(), ListDirTool()]:
            if _ok(t.name):
                registry.register(t)


        for t in [
            TaskCreateTool(task_manager),
            TaskUpdateTool(task_manager),
            TaskListTool(task_manager),
            TaskGetTool(task_manager),
        ]:
            if _ok(t.name):
                registry.register(t)
        
        if session is not None and store is not None and run_id is not None:
            tool = NoteSaveTool(store, session.id, run_id)
            if _ok(tool.name):
                registry.register(tool)

        if provider is not None and bus is not None and run_id is not None:
            run_dir = child_runs_dir or self._runs_dir
            if _ok("subagent"):
                registry.register(SubagentTool(
                    provider=provider,
                    parent_bus=bus,
                    parent_run_id=run_id,
                    session_id=session_id,
                    permission_manager=self._permission_manager,
                    task_registry=self._task_registry,
                    runs_dir=run_dir,
                    max_steps=self._config.agent.max_steps,
                    depth=0
                ))
            if _ok("agent_result"):
                registry.register(AgentResultTool(self._task_registry))

        if self._mcp_manager is not None:
            for mcp_tool in self._mcp_manager.get_tools():
                if _ok(mcp_tool.name):
                    registry.register(mcp_tool)
        return registry
    
    async def run(self, goal: str, run_id: str | None = None) -> None:
        await self.run_and_capture(goal, run_id=run_id)

    
    # create a complete agent run: generate run_id、connect eventbus、drive AgentLoop
    async def run_and_capture(
        self, 
        goal: str, 
        *,
        run_id: str | None = None, 
        session: Session | None = None, 
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None
    ) -> RunOutcome:
        # create run directory
        run_id = run_id or new_run_id()
        if session is not None and store is not None:
            run_path = store.runs_dir(session.id)
            history = store.read_messages(session.id)
            notes = store.read_notes(session.id)
        else:
            run_path = self._runs_dir
            history = [{"role": "user", "content": goal}]
            notes = ""
        run_path.mkdir(parents=True, exist_ok=True)

        task_manager = TaskManager(run_path / ".task")

        # create eventBus and subscribe all listener
        bus = self._bus or EventBus()
        for h in self._extra_handlers:
            bus.subscribe(h)

        global_context = load_context_file(Path("~/.mini/context.md").expanduser())
        project_context = load_context_file(Path(".mini/context.md"))

        # create working memory
        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            prefill_messages=history,
            session_notes=notes,
            max_steps=self._config.agent.max_steps,
            global_context=global_context,
            project_context=project_context,
            system_prompt_override=system_prompt_override
        )
        prefill_len = len(history)

        # open event file and start the loop
        async with EventWriter(run_path / "events.jsonl") as writer:
            writer.subscribe(bus)
            await bus.publish(RunStartedEvent(run_id=run_id, goal=goal, ts=_now()))

            # get llm provider, tool and agent loop
            provider = self._provider or AnthropicProvider(self._config.llm.default_model)
            if self._trace:
                provider = TracingProvder(
                    provider,
                    self._trace,
                    include_payload=self._config.trace.include_llm_payload
                )
            session_id_str = session.id if session is not None else ""
            child_runs_dir = (
                store.runs_dir(session.id)
                if session is not None and store is not None
                else self._runs_dir
            )
            registry = self._build_registry(
                task_manager, 
                store=store, 
                session=session, 
                run_id=run_id, 
                provider=provider,
                bus=bus,
                child_runs_dir=child_runs_dir,
                session_id=session_id_str,
                tool_whitelist=tool_whitelist
            )

            cancelled = False
            try:
                session_dir = store.session_dir(session.id) if session is not None and store is not None else run_path
                session_id_str = session.id if session is not None else ""
                compactor = Compactor(bus, session_dir, session_id_str)
                
                loop = AgentLoop(
                    provider, registry, bus, 
                    permission_manager=self._permission_manager, 
                    compact_threshold=self._config.compact.auto_threshold,
                    compactor=compactor,
                    session_id=session.id if session is not None else ""
                )
                await loop.run(context)
            except asyncio.CancelledError:
                cancelled = True
                if not context.is_done():
                    context.mark_failed("cancelled")
            except Exception:
                logging.getLogger(__name__).exception(
                    "agent run failed run_id=%s step=%d", run_id, context.step
                )
                if not context.is_done():
                    context.mark_failed("llm-error")

            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status=context.status,
                    reason=context.reason,
                    steps=context.step,
                    ts=_now(),
                )
            )

        if session is not None and store is not None:
            store.append_messages(session.id, context.messages[prefill_len:], run_id=run_id)

        if cancelled:
            raise asyncio.CancelledError()

        return RunOutcome(
            status=context.status,
            result=context.result,
            reason=context.reason,
        )

