from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import MCPServerConfig


_LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class MCPTool:
    exposed_name: str
    llm_name: str
    server_name: str
    raw_name: str
    description: str
    input_schema: dict[str, Any]


_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _normalize_llm_tool_name(name: str) -> str:
    cleaned = _NAME_RE.sub("_", name.strip()).strip("_")
    return cleaned or "tool"


class MCPServerConnection:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None

    async def connect(self) -> None:
        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=self.config.env,
        )
        read_stream, write_stream = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))
        await asyncio.wait_for(session.initialize(), timeout=self.config.startup_timeout_s)
        self._session = session

    async def close(self) -> None:
        await self._stack.aclose()
        self._session = None

    async def list_tools(self) -> list[MCPTool]:
        if self._session is None:
            raise RuntimeError(f"MCP 会话未连接: {self.config.name}")
        tools_result = await self._session.list_tools()
        tools: list[MCPTool] = []
        for tool in tools_result.tools:
            schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {"type": "object", "properties": {}}
            exposed_name = f"{self.config.name}.{tool.name}"
            tools.append(
                MCPTool(
                    exposed_name=exposed_name,
                    llm_name=exposed_name,
                    server_name=self.config.name,
                    raw_name=tool.name,
                    description=tool.description or "",
                    input_schema=schema,
                )
            )
        return tools

    async def call_tool(self, raw_tool_name: str, arguments: dict[str, Any], timeout_s: float = 30.0) -> str:
        if self._session is None:
            raise RuntimeError(f"MCP 会话未连接: {self.config.name}")
        call_result = await asyncio.wait_for(
            self._session.call_tool(raw_tool_name, arguments),
            timeout=timeout_s,
        )
        return _normalize_tool_result(call_result)


class MCPManager:
    def __init__(self, server_configs: list[MCPServerConfig]) -> None:
        self._connections: dict[str, MCPServerConnection] = {
            cfg.name: MCPServerConnection(cfg) for cfg in server_configs
        }
        self._tool_map: dict[str, MCPTool] = {}
        self._tool_name_map: dict[str, str] = {}

    async def __aenter__(self) -> "MCPManager":
        for name, conn in self._connections.items():
            try:
                await conn.connect()
                for tool in await conn.list_tools():
                    if tool.exposed_name in self._tool_map:
                        _LOG.warning("重复工具名，后者将覆盖: %s", tool.exposed_name)
                    base_name = _normalize_llm_tool_name(tool.exposed_name)
                    llm_name = base_name
                    suffix = 2
                    while llm_name in self._tool_name_map and self._tool_name_map[llm_name] != tool.exposed_name:
                        llm_name = f"{base_name}_{suffix}"
                        suffix += 1
                    tool.llm_name = llm_name
                    self._tool_map[tool.exposed_name] = tool
                    self._tool_name_map[tool.llm_name] = tool.exposed_name
                _LOG.info("MCP 服务已连接: %s", name)
            except Exception as exc:
                _LOG.exception("MCP 服务连接失败 %s: %s", name, exc)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        for conn in self._connections.values():
            try:
                await conn.close()
            except Exception:
                _LOG.exception("关闭 MCP 连接失败")

    def tools_for_llm(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.llm_name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in self._tool_map.values()
        ]

    async def call_tool(self, exposed_name: str, arguments: dict[str, Any]) -> str:
        resolved_name = self._tool_name_map.get(exposed_name, exposed_name)
        tool = self._tool_map.get(resolved_name)
        if tool is None:
            return f"工具不存在: {exposed_name}"

        conn = self._connections.get(tool.server_name)
        if conn is None:
            return f"MCP 服务不存在: {tool.server_name}"

        try:
            return await conn.call_tool(tool.raw_name, arguments)
        except asyncio.TimeoutError:
            return f"调用工具超时: {exposed_name}"
        except Exception as exc:
            return f"调用工具失败 {exposed_name}: {exc}"


def _normalize_tool_result(result: Any) -> str:
    if hasattr(result, "content") and isinstance(result.content, list):
        parts: list[str] = []
        for item in result.content:
            text = getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text)
            else:
                try:
                    parts.append(json.dumps(item.model_dump(), ensure_ascii=False))
                except Exception:
                    parts.append(str(item))
        if parts:
            return "\n".join(parts)

    if hasattr(result, "model_dump"):
        return json.dumps(result.model_dump(), ensure_ascii=False)

    return str(result)
