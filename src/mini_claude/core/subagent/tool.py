from __future__ import annotations

import asyncio
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mini_claude.core.agent.loader import AgentProfile, AgentProfileLoader
from mini_claude.core.bus.events import SubagentFinishedEvent, SubagentStartedEvent
from mini_claude.core.context import ExecutionContext
from mini_claude.core.events.bus import EventBus
from mini_claude.core.events.writer import EventWriter
from mini_claude.core.llm.base import LLMProvider
from mini_claude.core.loop import AgentLoop
from mini_claude.core.permissions.manager import PermissionManager
from mini_claude.core.runs import new_run_id
from mini_claude.core.subagent.registry import BackgroundTaskRegistry
from mini_claude.core.task.manager import TaskManager
from mini_claude.core.tools.base import BaseTool, ToolResult
from mini_claude.core.tools.builtin import BashTool, ListDirTool, ReadFileTool, WriteFileTool, TaskCreateTool, TaskGetTool, TaskListTool, TaskUpdateTool
from mini_claude.core.tools.registry import ToolRegistry

_profile_loader = AgentProfileLoader()


def _now() -> str:
    return datetime.now(UTC).isoformat()

class SubagentParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    description: str
    prompt: str
    run_in_background: bool = False
    subagent_type: str = ""

class SubagentTool(BaseTool):
    params_model = SubagentParams
    name = "subagent"
    description = (
        "Spawn an isolated sub-agent to handle a self-contained sub-task. "
        "The sub-agent starts with a clean context containing only the provided prompt — "
        "it does not inherit the current conversation history. "
        "Use run_in_background=true to run in parallel; retrieve result later with agent_result."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "3-5 word task description shown in progress display",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "Complete task description including all context the sub-agent needs. "
                    "The sub-agent cannot see the parent conversation, so be explicit."
                ),
            },
            "run_in_background": {
                "type": "boolean",
                "description": "When true, returns immediately with a run_id; use agent_result to poll.",  # noqa: E501
            },
            "subagent_type": {
                "type": "string",
                "description": "Agent role profile (planner/executor/reviewer). Leave empty for default.",  # noqa: E501
            },
        },
        "required": ["description", "prompt"],
    }

    def __init__(
        self, 
        provider: LLMProvider, 
        parent_bus: EventBus,
        parent_run_id: str, 
        session_id: str, 
        permission_manager: PermissionManager | None, 
        task_registry, 
        max_steps: int, 
        runs_dir: Path, 
        depth: int = 0
    ):
        self._provider = provider
        self._parent_bus = parent_bus
        self._parent_run_id = parent_run_id
        self._session_id = session_id
        self._permission_manager = permission_manager
        self._task_registry = task_registry
        self._max_steps = max_steps
        self._runs_dir = runs_dir
        self._depth = depth

    async def invoke(self, params: dict[str, Any]) -> ToolResult:
        p = SubagentParams.model_validate(params)

        if self._depth > 2:
            return ToolResult(
                content="Subagent nesting limit (2) reached; cannot spawn further subagents.",
                is_error=True,
                error_type="runtime_error",
            )

        agent_profile: AgentProfile | None = None
        if p.subagent_type:
            agent_profile = _profile_loader.resolve(p.subagent_type)

        child_run_id = new_run_id()
        child_context = ExecutionContext(
            run_id=child_run_id,
            goal=p.prompt,
            max_steps=self._max_steps,
            system_prompt_override=agent_profile.system_prompt if agent_profile else None
        )

        child_bus = EventBus()

        # connect child_bus and parent_bus
        async def _bridge(event: BaseModel) -> None:
            await self._parent_bus.publish(event)

        child_bus.subscribe(_bridge)

        child_tool_registry = self._build_child_tool_registry(child_bus, child_run_id, agent_profile)
        child_loop = AgentLoop(
            provider=self._provider,
            registry=child_tool_registry,
            bus=child_bus,
            permission_manager=self._permission_manager,
            session_id=self._session_id,
        )

        await self._parent_bus.publish(
            SubagentStartedEvent(
                run_id=child_run_id,
                parent_run_id=self._parent_run_id,
                description=p.description,
                ts=_now(),
            )
        )

        child_run_path = self._runs_dir / child_run_id
        child_run_path.mkdir(parents=True, exist_ok=True)

        if p.run_in_background:
            task: asyncio.Task[None] = asyncio.create_task(
                self._run_background(
                    child_loop, child_context, child_bus, child_run_path, child_run_id
                )
            )
            self._task_registry.register(child_run_id, task, child_context)
            return ToolResult(
                content=(
                    f"Subagent started in background. run_id={child_run_id}. "
                    f"Use agent_result(run_id='{child_run_id}') to retrieve result."
                )
            )

        async with EventWriter(child_run_path / "events.jsonl") as writer:
            writer.subscribe(child_bus)
            await child_loop.run(child_context)

        await self._parent_bus.publish(
            SubagentFinishedEvent(
                run_id=child_run_id,
                parent_run_id=self._parent_run_id,
                status=child_context.status,
                ts=_now(),
            )
        )

        if child_context.status == "success":
            return ToolResult(
                content=child_context.result or "Subagent completed with no text output."
            )
        return ToolResult(
            content=(
                child_context.result
                or f"Subagent failed (status={child_context.status}, reason={child_context.reason})"
            ),
            is_error=True,
            error_type="runtime_error",
        )

    # background coroutine
    async def _run_background(
        self,
        loop: AgentLoop,
        context: ExecutionContext,
        bus: EventBus,
        run_path: Path,
        run_id: str,
    ):
        async with EventWriter(run_path / "events.jsonl") as writer:
            writer.subscribe(bus)
            await loop.run(context)

        await self._parent_bus.publish(
            SubagentFinishedEvent(
                run_id=run_id,
                parent_run_id=self._parent_run_id,
                status=context.status,
                ts=_now(),
            )
        )

    def _build_child_tool_registry(self, bus: EventBus, run_id: str, profile: AgentProfile | None):
        allowed: set[str] | None = set(profile.allowed_tools) if profile and profile.allowed_tools else None

        def _allowed(name: str) -> bool:
            return allowed is None or name in allowed

        registry = ToolRegistry()
        for t in [
            ReadFileTool(),
            BashTool(),
            WriteFileTool(),
            ListDirTool(),
        ]:
            if _allowed(t.name):
                registry.register(t)

        task_manager = TaskManager(self._runs_dir / run_id / ".tasks")
        for t in [
            TaskCreateTool(task_manager),
            TaskUpdateTool(task_manager),
            TaskListTool(task_manager),
            TaskGetTool(task_manager),
        ]:
            if _allowed(t.name):
                registry.register(t)

        if self._depth < 1:
            nested = SubagentTool(
                provider=self._provider,
                parent_bus=bus,
                parent_run_id=run_id,
                permission_manager=self._permission_manager,
                max_steps=self._max_steps,
                task_registry=self._task_registry,
                runs_dir=self._runs_dir,
                session_id=self._session_id,
                depth=self._depth + 1,
            )
            if _allowed("subagent"):
                registry.register(nested)
            if _allowed("agent_result"):
                registry.register(AgentResultTool(self._task_registry))

        return registry

class AgentResultParams(BaseModel):
    run_id: str

class AgentResultTool(BaseTool):
    params_model = AgentResultParams
    name = "agent_result"
    description = (
        "Retrieve the result of a background sub-agent previously started with spawn_agent. "
        "Returns 'still running' if the sub-agent has not yet completed."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The run_id returned by spawn_agent(run_in_background=true)",
            },
        },
        "required": ["run_id"],
    }

    def __init__(self, task_registry: BackgroundTaskRegistry) -> None:
        self._task_registry = task_registry

    async def invoke(self, params: dict[str, Any]) -> ToolResult:
        p = AgentResultParams.model_validate(params)

        entry = self._task_registry.get(p.run_id)
        if entry is None:
            return ToolResult(
                content=f"Unknown run_id: {p.run_id}. Only background subagents can be queried.",
                is_error=True,
                error_type="runtime_error",
            )

        task, context = entry
        if not task.done():
            return ToolResult("agent is running")
        if task.cancelled():
            return ToolResult(
                content="Subagent was cancelled.", is_error=True, error_type="runtime_error"
            )

        exc = task.exception()
        if exc is not None:
            return ToolResult(
                content=f"Subagent raised an exception: {exc}",
                is_error=True,
                error_type="runtime_error",
            )

        return ToolResult(content=context.result or "Subagent completed with no text result.")





        

        
