from __future__ import annotations

import json

from mini_claude.core.task.manager import TaskManager
from mini_claude.core.tools.base import BaseTool, ToolResult


class TaskGetTool(BaseTool):
    name = "task_get"
    description = "Get full details of a task by its integer ID. Returns the task as JSON."
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "integer",
                "description": "ID of the task to retrieve.",
            },
        },
        "required": ["task_id"],
    }

    # With TaskManager instance for invoke to use
    def __init__(self, task_manager: TaskManager) -> None:
        self._manager = task_manager

    # return ToolResult
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        task_id = int(str(params["task_id"]))
        try:
            task = self._manager.get(task_id)
            return ToolResult(content=json.dumps(task.to_dict(), ensure_ascii=False))
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
