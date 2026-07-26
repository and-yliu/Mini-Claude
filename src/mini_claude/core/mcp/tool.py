from __future__ import annotations
from typing import Any

from mini_claude.core.mcp.client import McpClient, McpServerUnavailableError, McpToolDef, McpToolError
from mini_claude.core.tools.base import BaseTool, ToolResult


# make mcp tool like regular tool in tool registry
class McpTool(BaseTool):
    params_model = None

    def __init__(self, client: McpClient, server_name: str, tool_def: McpToolDef):
        self._client = client
        self._server_name = server_name
        self.name = f"{server_name}__{tool_def.name}"
        self.description = tool_def.description or f"MCP tool from {server_name}"
        self.input_schema: dict[str, Any] = (
            tool_def.input_schema or {"type": "object", "properties": {}}
        )
        self._tool_def = tool_def

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        try:
            content = await self._client.tool_call(self._tool_def.name, params)
            return ToolResult(content=content)
        except McpServerUnavailableError as exc:
            return ToolResult(
                content=f"mcp server '{self._server_name}' unavailable: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        except McpToolError as exc:
            return ToolResult(
                content=f"mcp tool '{self.name}' error: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        except Exception as exc:
            return ToolResult(
                content=f"mcp tool '{self.name}' unexpected error: {exc}",
                is_error=True,
                error_type="runtime_error",
            )