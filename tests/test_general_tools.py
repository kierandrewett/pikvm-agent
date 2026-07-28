from __future__ import annotations

from types import SimpleNamespace
from contextlib import AsyncExitStack

import pytest

from pikvm_agent.harness.general_tools import (
    McpServerConnection,
    McpToolBroker,
)


def remote_tool(*, read_only_hint: bool) -> SimpleNamespace:
    return SimpleNamespace(
        name="send",
        title="Send message",
        description="Send a message to another person.",
        inputSchema={"type": "object"},
        annotations=SimpleNamespace(
            readOnlyHint=read_only_hint,
            destructiveHint=False,
            openWorldHint=True,
        ),
    )


def test_remote_read_only_hint_cannot_bypass_local_approval_policy() -> None:
    untrusted = McpServerConnection(
        name="teams",
        command="teams-mcp",
        allowed_tools=frozenset({"send"}),
    )
    reviewed = McpServerConnection(
        name="archive",
        command="archive-mcp",
        allowed_tools=frozenset({"send"}),
        read_only_tools=frozenset({"send"}),
    )

    untrusted_descriptor = McpToolBroker._descriptor(  # noqa: SLF001
        untrusted,
        remote_tool(read_only_hint=True),
    )
    reviewed_descriptor = McpToolBroker._descriptor(  # noqa: SLF001
        reviewed,
        remote_tool(read_only_hint=False),
    )

    assert untrusted_descriptor.read_only is False
    assert untrusted_descriptor.requires_approval is True
    assert reviewed_descriptor.read_only is True
    assert reviewed_descriptor.requires_approval is False


def test_mcp_server_connection_is_fail_closed_without_a_safe_namespace() -> None:
    with pytest.raises(ValueError, match="explicit allow-list"):
        McpServerConnection(name="empty", command="empty-mcp")

    with pytest.raises(ValueError, match="server names"):
        McpServerConnection(
            name="bad.name",
            command="bad-mcp",
            allowed_tools=frozenset({"search"}),
        )

    with pytest.raises(ValueError, match="machine-control"):
        McpServerConnection(
            name="raw",
            command="raw-pikvm-mcp",
            allowed_tools=frozenset({"pikvm_run_burst"}),
        )

    with pytest.raises(ValueError, match="daemon capabilities"):
        McpServerConnection(
            name="raw",
            command="wrapper-mcp",
            inherited_env=("PATH", "PIKVM_AGENT_HARNESS_TOKEN"),
            allowed_tools=frozenset({"run_burst"}),
        )


@pytest.mark.asyncio
async def test_one_unavailable_mcp_server_does_not_take_down_other_tools() -> None:
    class IsolatedBroker(McpToolBroker):
        async def _connect_server(self, config):  # type: ignore[no-untyped-def]
            if config.name == "offline":
                raise TimeoutError("secret transport detail")
            stack = AsyncExitStack()
            await stack.__aenter__()
            descriptor = self._descriptor(
                config,
                SimpleNamespace(
                    name="search",
                    title="Search",
                    description="Search sources.",
                    inputSchema={"type": "object"},
                    annotations=None,
                ),
            )
            return stack, SimpleNamespace(
                config=config,
                session=object(),
                tools={"search": descriptor},
            )

    broker = IsolatedBroker(
        [
            McpServerConnection(
                name="offline",
                command="offline-mcp",
                allowed_tools=frozenset({"search"}),
            ),
            McpServerConnection(
                name="working",
                command="working-mcp",
                allowed_tools=frozenset({"search"}),
                read_only_tools=frozenset({"search"}),
            ),
        ]
    )

    await broker.start()
    catalog = await broker.catalog()

    assert [tool.name for tool in catalog] == ["working.search"]
    assert broker.health() == {
        "offline": {
            "ready": False,
            "tools": 0,
            "error": "unavailable",
        },
        "working": {
            "ready": True,
            "tools": 1,
            "error": None,
        },
    }
    assert "secret transport detail" not in str(broker.health())
    await broker.close()


@pytest.mark.asyncio
async def test_packaged_web_mcp_catalog_is_namespaced_and_read_only() -> None:
    broker = McpToolBroker(
        [
            McpServerConnection(
                name="web",
                command="ddgs",
                args=("mcp",),
                allowed_tools=frozenset(
                    {"search_text", "search_news", "extract_content"}
                ),
                read_only_tools=frozenset(
                    {"search_text", "search_news", "extract_content"}
                ),
            )
        ]
    )
    await broker.start()
    try:
        catalog = await broker.catalog()
    finally:
        await broker.close()

    assert [tool.name for tool in catalog] == [
        "web.extract_content",
        "web.search_news",
        "web.search_text",
    ]
    assert all(tool.read_only for tool in catalog)
    assert all(not tool.requires_approval for tool in catalog)
