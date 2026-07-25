from __future__ import annotations

from typing import Any

import pytest

from pikvm_agent.harness.mcp_computer import (
    McpComputerDriver,
    harness_child_environment,
)


class FakeToolClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name == "pikvm_open":
            state = {
                "session_id": "s_1",
                "status": "paused",
                "frame_id": 1,
                "world_version": 4,
                "control_epoch": 2,
                "machine": {
                    "alias": "Windows test VM",
                    "fingerprint": "target:12ab34cd56ef",
                    "identity_source": "explicit_machine_id",
                    "desktop_layer": "VNC console",
                },
            }
        elif name == "pikvm_screenshot":
            state = {
                "session_id": "s_1",
                "status": "running",
                "frame_id": 3,
                "world_version": 6,
                "control_epoch": 2,
            }
        elif name == "pikvm_run_burst":
            state = {
                "session_id": "s_1",
                "status": "needs_approval",
                "frame_id": 1,
                "world_version": 4,
                "control_epoch": 2,
                "approval_request": {
                    "approval_id": "a_1",
                    "risk": "communication_send",
                },
            }
        elif name == "pikvm_resolve_approval":
            state = {
                "session_id": "s_1",
                "status": "completed",
                "frame_id": 2,
                "world_version": 5,
                "control_epoch": 2,
            }
        else:
            state = {"session_id": "s_1", "status": "aborted"}
        return {
            "is_error": False,
            "state": state,
            "texts": [],
            "images": [f"/tmp/{name}.jpg"],
        }


def test_harness_child_is_the_only_raw_mcp_process_trusted_to_relay_approval() -> None:
    env = harness_child_environment(
        "http://127.0.0.1:48123",
        inherited={
            "PATH": "/usr/bin",
            "PIKVM_HARNESS_OBSERVER_URL": "http://127.0.0.1:48124",
            "PIKVM_HARNESS_OBSERVER_TOKEN": "observer-secret",
            "PIKVM_HARNESS_OBSERVER_MODE": "guarded",
            "PIKVM_MCP_CALLER_LABEL": "codex-cli",
            "PIKVM_MCP_PROVIDER": "openai-oauth",
            "PIKVM_MCP_MODEL": "gpt-test",
        },
    )

    assert env == {
        "PATH": "/usr/bin",
        "PIKVM_AGENT_DAEMON": "http://127.0.0.1:48123",
        "PIKVM_AGENT_TRUSTED_APPROVAL_CLIENT": "1",
    }


@pytest.mark.asyncio
async def test_mcp_computer_preserves_raw_tool_contract_and_evidence() -> None:
    client = FakeToolClient()
    computer = McpComputerDriver(client)

    opened = await computer.open("send a draft")
    paused = await computer.burst(
        session_id=opened.session_id,
        actions=[{"type": "click", "x": 10, "y": 20}],
        based_on_world_version=opened.world_version,
        based_on_control_epoch=opened.control_epoch,
        idempotency_key="run:action:0:digest",
    )
    approved = await computer.resolve_approval(
        session_id=opened.session_id,
        approval_id="a_1",
        decision={"type": "approve"},
    )

    assert paused.status == "needs_approval"
    assert opened.machine["alias"] == "Windows test VM"
    assert opened.machine["fingerprint"] == "target:12ab34cd56ef"
    assert paused.approval_request == {
        "approval_id": "a_1",
        "risk": "communication_send",
    }
    assert paused.image_path == "/tmp/pikvm_run_burst.jpg"
    assert approved.status == "completed"
    assert client.calls == [
        ("pikvm_open", {"label": "send a draft"}),
        (
            "pikvm_run_burst",
            {
                "session_id": "s_1",
                "actions": [{"type": "click", "x": 10, "y": 20}],
                "based_on_world_version": 4,
                "based_on_control_epoch": 2,
                "idempotency_key": "run:action:0:digest",
            },
        ),
        (
            "pikvm_resolve_approval",
            {
                "session_id": "s_1",
                "approval_id": "a_1",
                "decision": {"type": "approve"},
            },
        ),
    ]


@pytest.mark.asyncio
async def test_harness_leaves_burst_runtime_on_server_auto_budget() -> None:
    client = FakeToolClient()
    computer = McpComputerDriver(client)

    await computer.burst(
        session_id="s_1",
        actions=[
            {"type": "key", "keys": ["CTRL", "A"]},
            {"type": "type_text", "text": "dim screen when inactive"},
        ],
        based_on_world_version=4,
        based_on_control_epoch=2,
        idempotency_key="typing-aware-budget",
    )

    _, arguments = client.calls[-1]
    assert "max_runtime_ms" not in arguments


@pytest.mark.asyncio
async def test_mcp_computer_refreshes_the_existing_session() -> None:
    client = FakeToolClient()
    computer = McpComputerDriver(client)

    refreshed = await computer.refresh(session_id="s_1")

    assert refreshed.world_version == 6
    assert refreshed.image_path == "/tmp/pikvm_screenshot.jpg"
    assert client.calls == [
        ("pikvm_screenshot", {"session_id": "s_1"}),
    ]
