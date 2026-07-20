from __future__ import annotations

import asyncio
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
from mini_claude.core.runs import RUNS_DIR, new_run_id, ensure_run_dir
from mini_claude.core.tools.builtin.read_file import ReadFileTool
from mini_claude.core.tools.registry import ToolRegistry
from mini_claude.core.trace.writer import TraceWriter, TraceRecord
from mini_claude.core.trace.provider import TracingProvder

def _now(): 
    return datetime.now(UTC).isoformat()

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
        trace: TraceWriter | None = None
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._runs_dir = runs_dir or RUNS_DIR
        self._trace = trace
    
    # create a complete agent run: generate run_id、connect eventbus、drive AgentLoop
    async def run(self, goal: str, run_id: str | None = None):
        # create run directory
        run_id = run_id or new_run_id()
        run_path = ensure_run_dir(run_id)

        # create eventBus and subscribe all listener
        bus = self._bus or EventBus()
        for h in self._extra_handlers:
            bus.subscribe(h)

        # create working memory
        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
        )

        # open event file and start the loop
        async with EventWriter(run_path / "events.jsonl") as writer:
            writer.subscribe(bus)
            await bus.publish(RunStartedEvent(run_id=run_id, goal=goal, ts=_now()))

            # get llm provider, tool and agent loop
            provider = self._provider or AnthropicProvider(self._config.llm.default_model)
            registry = ToolRegistry()
            registry.register(ReadFileTool())

            if self._trace:
                provider = TracingProvder(
                    provider,
                    self._trace,
                    include_payload=self._config.trace.include_llm_payload
                )
            
            loop = AgentLoop(provider, registry, bus)

            cancelled = False
            try:
                await loop.run(context)
            except asyncio.CancelledError:
                cancelled = True
                if not context.is_done():
                    context.mark_failed("cancelled")

            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status=context.status,
                    reason=context.reason,
                    steps=context.step,
                    ts=_now(),
                )
            )

        if cancelled:
            raise asyncio.CancelledError()

