"""Visible, fail-closed non-computer tools for the conversational harness.

The assistant sees one small catalogue and invokes one namespaced tool at a
time. Concrete MCP transports stay behind this module; callers never manage
sessions, transport lifetimes, annotations, or result normalisation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel, ConfigDict, Field


class ToolDescriptor(BaseModel):
    """Model-visible capability metadata plus host-owned safety semantics."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=4_000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    read_only: bool = False
    destructive: bool = False
    open_world: bool = False

    @property
    def requires_approval(self) -> bool:
        return not self.read_only


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(max_length=50_000)
    is_error: bool = False


class ToolBroker(Protocol):
    async def catalog(self) -> list[ToolDescriptor]: ...

    def health(self) -> dict[str, dict[str, object]]: ...

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolResult: ...


class EmptyToolBroker:
    async def catalog(self) -> list[ToolDescriptor]:
        return []

    def health(self) -> dict[str, dict[str, object]]:
        return {}

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        del arguments
        raise KeyError(f"unknown assistant tool: {name}")


@dataclass(frozen=True)
class McpServerConnection:
    """Reviewed connection data; secrets are inherited by name, never inline."""

    name: str
    transport: Literal["stdio", "streamable_http"] = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    inherited_env: tuple[str, ...] = ("PATH",)
    url: str | None = None
    header_env: dict[str, str] = field(default_factory=dict)
    allowed_tools: frozenset[str] = frozenset()
    read_only_tools: frozenset[str] = frozenset()
    timeout_s: float = 30.0

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.name):
            raise ValueError(
                "MCP assistant server names use letters, numbers, _ or -"
            )
        if not self.allowed_tools:
            raise ValueError(
                f"MCP tool server {self.name} requires an explicit allow-list"
            )
        if self.read_only_tools - self.allowed_tools:
            raise ValueError(
                f"MCP tool server {self.name} read-only tools must be allowed"
            )


@dataclass
class _McpServerSession:
    config: McpServerConnection
    session: ClientSession
    tools: dict[str, ToolDescriptor]


class McpToolBroker:
    """Persistent MCP adapters with a namespaced, visibility-first interface."""

    def __init__(self, servers: list[McpServerConnection]) -> None:
        self._servers = list(servers)
        names = [server.name for server in self._servers]
        duplicates = sorted(
            name for name in set(names) if names.count(name) > 1
        )
        if duplicates:
            raise ValueError(
                "duplicate MCP tool server: " + ", ".join(duplicates)
            )
        self._started = False
        self._stacks: dict[str, AsyncExitStack] = {}
        self._sessions: dict[str, _McpServerSession] = {}
        self._errors: dict[str, str] = {}

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for config in self._servers:
            try:
                stack, server = await self._connect_server(config)
            except Exception as exc:
                self._errors[config.name] = self._failure_class(exc)
                continue
            self._stacks[config.name] = stack
            self._sessions[config.name] = server
            self._errors.pop(config.name, None)

    async def close(self) -> None:
        stacks = list(reversed(self._stacks.values()))
        self._started = False
        self._stacks.clear()
        self._sessions.clear()
        self._errors.clear()
        for stack in stacks:
            await stack.aclose()

    async def catalog(self) -> list[ToolDescriptor]:
        return sorted(
            (
                descriptor
                for server in self._sessions.values()
                for descriptor in server.tools.values()
            ),
            key=lambda tool: tool.name,
        )

    def health(self) -> dict[str, dict[str, object]]:
        return {
            config.name: {
                "ready": config.name in self._sessions,
                "tools": len(
                    self._sessions[config.name].tools
                    if config.name in self._sessions
                    else {}
                ),
                "error": self._errors.get(config.name),
            }
            for config in self._servers
        }

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        server_name, separator, tool_name = name.partition(".")
        if not separator:
            raise KeyError(f"unknown assistant tool: {name}")
        server = self._sessions.get(server_name)
        if server is None or tool_name not in server.tools:
            raise KeyError(f"unknown assistant tool: {name}")
        result = await server.session.call_tool(
            tool_name,
            arguments=arguments,
            read_timeout_seconds=timedelta(
                seconds=server.config.timeout_s
            ),
        )
        payload = {
            "content": [
                block.model_dump(mode="json", by_alias=True)
                for block in result.content
            ],
            "structured_content": result.structuredContent,
        }
        content = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(content) > 50_000:
            content = content[:49_999] + "…"
        return ToolResult(
            content=content,
            is_error=bool(result.isError),
        )

    @staticmethod
    def _transport(config: McpServerConnection) -> Any:
        if config.transport == "stdio":
            if not config.command:
                raise ValueError(
                    f"MCP tool server {config.name} requires command"
                )
            env = {
                name: os.environ[name]
                for name in config.inherited_env
                if name in os.environ
            }
            command = config.command
            if (
                os.sep not in command
                and shutil.which(command, path=env.get("PATH")) is None
            ):
                sibling = Path(sys.executable).with_name(command)
                if sibling.is_file():
                    command = str(sibling)
            return stdio_client(
                StdioServerParameters(
                    command=command,
                    args=list(config.args),
                    env=env,
                    cwd=config.cwd,
                )
            )
        if not config.url:
            raise ValueError(f"MCP tool server {config.name} requires url")
        headers = {
            header: McpToolBroker._required_environment(
                env_name,
                config.name,
            )
            for header, env_name in config.header_env.items()
        }
        return streamablehttp_client(
            config.url,
            headers=headers,
            timeout=config.timeout_s,
            sse_read_timeout=max(config.timeout_s, 300),
        )

    async def _connect_server(
        self,
        config: McpServerConnection,
    ) -> tuple[AsyncExitStack, _McpServerSession]:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            transport_result = await stack.enter_async_context(
                self._transport(config)
            )
            read, write = transport_result[:2]
            session = await stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(
                        seconds=config.timeout_s
                    ),
                )
            )
            await session.initialize()
            tools: dict[str, ToolDescriptor] = {}
            cursor: str | None = None
            while True:
                page = await session.list_tools(cursor=cursor)
                for tool in page.tools:
                    if tool.name not in config.allowed_tools:
                        continue
                    tools[tool.name] = self._descriptor(config, tool)
                cursor = page.nextCursor
                if not cursor:
                    break
            return stack, _McpServerSession(
                config=config,
                session=session,
                tools=tools,
            )
        except BaseException:
            await stack.aclose()
            raise

    @staticmethod
    def _failure_class(exc: Exception) -> str:
        if isinstance(exc, (TimeoutError, OSError)):
            return "unavailable"
        return "connection-failed"

    @staticmethod
    def _required_environment(name: str, server_name: str) -> str:
        value = os.environ.get(name)
        if value is None:
            raise ValueError(
                f"MCP tool server {server_name} requires environment {name}"
            )
        return value

    @staticmethod
    def _descriptor(
        config: McpServerConnection,
        tool: Any,
    ) -> ToolDescriptor:
        annotations = tool.annotations
        # MCP annotations come from the remote server. They can inform what the
        # operator sees, but they cannot grant auto-execution authority.
        return ToolDescriptor(
            name=f"{config.name}.{tool.name}",
            title=tool.title or tool.name,
            description=tool.description or "",
            input_schema=dict(tool.inputSchema),
            read_only=tool.name in config.read_only_tools,
            destructive=bool(
                annotations is not None
                and annotations.destructiveHint is True
            ),
            open_world=bool(
                annotations is not None
                and annotations.openWorldHint is True
            ),
        )
