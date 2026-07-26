from __future__ import annotations

import asyncio

from mini_claude.core.context import ExecutionContext

# manage subagent task life cycle
class BackgroundTaskRegistry:
    def __init__(self):
        self._tasks: dict[str, tuple[asyncio.Task[None], ExecutionContext]] = {}

    # register a background task and its execution context
    def register(self, run_id: str, task: asyncio.Task[None], context: ExecutionContext):
        self._tasks[run_id] = (task, context)

    # return a background task and its execution context based on the run
    def get(self, run_id: str) -> tuple[asyncio.Task[None], ExecutionContext] | None:
        return self._tasks.get(run_id)

    # return all background task and its execution context, use to do cleanup
    def all(self) -> list[tuple[asyncio.Task[None], ExecutionContext]]:
        return list(self._tasks.values())
