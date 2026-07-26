from __future__ import annotations

import logging

from mini_claude.core.config import McpServerConfig
from mini_claude.core.mcp.client import McpClient
from mini_claude.core.mcp.tool import McpTool
from mini_claude.core.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

# manages all mcp servers lifecycle, start, registration, tool discover, close
class McpServerManager:
    def __init__(self) -> None:
        self._clients: dict[str, McpClient] = {}
        self._tools: list[McpTool] = []

    #connect to all mcp servers, and register its tools
    async def start_all(self, servers: list[McpServerConfig]) -> None:
        for cfg in servers:
            try:
                client = await self._connect(cfg)
                tool_defs = await client.list_tools()
                for tool in tool_defs:
                    self._tools.append(McpTool(client, cfg.name, tool))
                self._clients[cfg.name] = client
                log.info(
                    "mcp: server '%s' connected, %d tool(s) discovered",
                    cfg.name, len(tool_defs),
                )
            except Exception:
                log.exception("mcp: server '%s' failed to start, skipping", cfg.name)

    # registor all discovered tool to ToolRegistry
    def registor_tool(self, tool_registry: ToolRegistry):
        for tool in self._tools:
            tool_registry.register(tool)

    # return all discovered tool
    def get_tools(self) -> list[McpTool]:
        return list(self._tools)

    # disconnect all mcp connection 
    async def stop_all(self) -> None:
        for name, client in self._clients.items():
            try:
                await client.close()
                log.info("mcp: server '%s' closed", name)
            except Exception:
                log.warning("mcp: error closing server '%s'", name)
        self._clients.clear()

    # connect to an mcp server
    async def _connect(self, mcp_config: McpServerConfig) -> McpClient:
        client = McpClient()
        if mcp_config.transport == "stdio":
            if not mcp_config.command:
                raise ValueError(f"mcp server '{mcp_config.name}': stdio transport requires 'command'")
            await client.connect_stdio(mcp_config.command, mcp_config.args, mcp_config.env or None)
        elif mcp_config.transport == "tcp":
            await client.connect_tcp(mcp_config.host, mcp_config.port)
        else:
            raise ValueError(f"mcp server '{mcp_config.name}': unknown transport '{mcp_config.transport}'")
        return client
