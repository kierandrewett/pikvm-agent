from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import Implementation
from PIL import Image

from pikvm_agent.harness.agent_models import ComputerObservation, RunSnapshot
from pikvm_agent.harness.agent_store import InMemoryRunStore
from pikvm_agent.harness.api import create_harness_app
from pikvm_agent.harness.direct_calls import (
    DirectCallBegin,
    DirectCallCoordinator,
    DirectCallFinish,
)
from pikvm_agent.harness.mcp_visibility import (
    DirectCallReporter,
    VisibleFastMCP,
)


TEST_ACCESS_TOKEN = "test-harness-token-0123456789abcdef"
TEST_AGENT_TOKEN = "test-agent-token-0123456789abcdef"
TEST_OBSERVER_TOKEN = "test-observer-token-0123456789abcdef"


class NoopHarness:
    async def create(self, _task: str) -> RunSnapshot:
        raise AssertionError("managed runs are outside this test")


class NoopModels:
    def health(self) -> dict[str, dict[str, object]]:
        return {}


class RecordingComputer:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def refresh(self, *, session_id: str) -> ComputerObservation:
        return ComputerObservation(session_id=session_id, status="running")

    async def resolve_approval(
        self, *, session_id: str, approval_id: str, decision: dict[str, Any]
    ) -> ComputerObservation:
        self.calls.append(("approval", session_id, approval_id, decision))
        return ComputerObservation(session_id=session_id, status="running")

    async def abort(
        self, *, session_id: str, reason: str
    ) -> ComputerObservation:
        self.calls.append(("abort", session_id, reason))
        return ComputerObservation(session_id=session_id, status="aborted")


class PreviewComputer(RecordingComputer):
    def __init__(self, image_path: Path) -> None:
        super().__init__()
        self.image_path = image_path

    async def refresh(self, *, session_id: str) -> ComputerObservation:
        return ComputerObservation(
            session_id=session_id,
            status="running",
            frame_id=17,
            world_version=17,
            control_epoch=2,
            width=1280,
            height=720,
            image_path=str(self.image_path),
        )


@pytest.mark.asyncio
async def test_direct_mcp_call_is_visible_before_hid_and_keeps_its_result(
    tmp_path: Path,
) -> None:
    store = InMemoryRunStore()
    direct_calls = DirectCallCoordinator(
        store=store,
        computer=RecordingComputer(),  # type: ignore[arg-type]
    )
    app = create_harness_app(
        harness=NoopHarness(),  # type: ignore[arg-type]
        store=store,
        models=NoopModels(),
        access_token=TEST_ACCESS_TOKEN,
        observer_token=TEST_OBSERVER_TOKEN,
        allowed_origins={"http://harness"},
        direct_calls=direct_calls,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        started = await client.post(
            "/api/direct/calls/begin",
            json={
                "call_id": "call-open-1",
                "tool": "pikvm_open",
                "arguments": {"label": "Inspect Windows settings"},
                "caller": {
                    "name": "claude-code",
                    "version": "2.1.0",
                    "provider": "anthropic-oauth",
                    "model": "opus-4.8",
                },
            },
        )
        assert started.status_code == 200
        decision = started.json()
        assert decision["allowed"] is True

        before_hid = (
            await client.get(f"/api/runs/{decision['run_id']}")
        ).json()
        assert before_hid["origin"] == "direct_mcp"
        assert before_hid["caller"]["name"] == "claude-code"
        assert before_hid["events"][-1]["kind"] == "action.attempted"
        assert before_hid["events"][-1]["data"]["arguments"] == {
            "label": "Inspect Windows settings"
        }

        finished = await client.post(
            "/api/direct/calls/finish",
            json={
                "call_id": "call-open-1",
                "status": "completed",
                "latency_ms": 412,
                "result": {
                    "session_id": "session-direct-1",
                    "status": "running",
                    "frame_id": 7,
                    "world_version": 11,
                    "control_epoch": 2,
                    "width": 1280,
                    "height": 720,
                },
            },
        )
        assert finished.status_code == 200
        assert finished.json() == {
            "ok": True,
            "run_id": decision["run_id"],
            "status": "running",
            "cursor": 3,
        }
        visible = (
            await client.get(f"/api/runs/{decision['run_id']}")
        ).json()

    assert visible["run_id"] == decision["run_id"]
    assert visible["session_id"] == "session-direct-1"
    assert visible["observation"]["frame_id"] == 7
    assert visible["events"][-1]["kind"] == "action.completed"
    assert visible["events"][-1]["data"]["latency_ms"] == 412


@pytest.mark.asyncio
async def test_direct_click_retains_a_crop_capable_pre_action_frame(
    tmp_path: Path,
) -> None:
    frame_path = tmp_path / "direct-before.png"
    Image.new("RGB", (1280, 720), "#172033").save(frame_path)
    store = InMemoryRunStore()
    direct_calls = DirectCallCoordinator(
        store=store,
        computer=PreviewComputer(frame_path),
    )
    app = create_harness_app(
        harness=NoopHarness(),  # type: ignore[arg-type]
        store=store,
        models=NoopModels(),
        access_token=TEST_ACCESS_TOKEN,
        observer_token=TEST_OBSERVER_TOKEN,
        allowed_origins={"http://harness"},
        direct_calls=direct_calls,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        opened = (
            await client.post(
                "/api/direct/calls/begin",
                json={
                    "call_id": "open-preview",
                    "tool": "pikvm_open",
                    "arguments": {"label": "Direct click preview"},
                },
            )
        ).json()
        await client.post(
            "/api/direct/calls/finish",
            json={
                "call_id": "open-preview",
                "run_id": opened["run_id"],
                "status": "completed",
                "result": {
                    "session_id": "session-preview",
                    "status": "running",
                },
            },
        )
        click = await client.post(
            "/api/direct/calls/begin",
            json={
                "call_id": "direct-click",
                "tool": "pikvm_click",
                "arguments": {
                    "session_id": "session-preview",
                    "x": 412,
                    "y": 286,
                    "based_on_world_version": 17,
                    "based_on_control_epoch": 2,
                    "idempotency_key": "preview:direct:click",
                },
            },
        )
        visible = (
            await client.get(f"/api/runs/{opened['run_id']}")
        ).json()
        preview = await client.get(
            f"/api/runs/{opened['run_id']}"
            "/verification-images/1/click-target",
            params={
                "x": 412,
                "y": 286,
                "screen_width": 1280,
                "screen_height": 720,
            },
        )

    assert click.status_code == 200
    assert click.json()["allowed"] is True
    assert visible["verification_images"] == [
        {
            "revision": 1,
            "action_index": 0,
            "kind": "pre_action",
            "before_frame_id": 17,
            "after_frame_id": None,
        }
    ]
    assert [event["kind"] for event in visible["events"]][-2:] == [
        "action.pre_action_evidence_captured",
        "action.attempted",
    ]
    assert preview.status_code == 200
    assert preview.headers["x-pikvm-evidence-mode"] == "click-target"
    assert preview.headers["content-type"] == "image/png"


@pytest.mark.asyncio
async def test_target_identity_change_blocks_future_direct_hid() -> None:
    store = InMemoryRunStore()
    coordinator = DirectCallCoordinator(
        store=store,
        computer=RecordingComputer(),  # type: ignore[arg-type]
    )
    opened = await coordinator.begin(
        DirectCallBegin(
            call_id="open-machine-a",
            tool="pikvm_open",
            arguments={"label": "Direct session"},
        )
    )
    await coordinator.finish(
        DirectCallFinish(
            call_id="open-machine-a",
            run_id=opened.run_id,
            status="completed",
            result={
                "session_id": "session-target-continuity",
                "status": "running",
                "machine": {
                    "alias": "Machine A",
                    "fingerprint": "target:aaaaaaaaaaaaaaaa",
                },
            },
        )
    )
    screenshot = await coordinator.begin(
        DirectCallBegin(
            call_id="observe-machine-b",
            tool="pikvm_screenshot",
            arguments={"session_id": "session-target-continuity"},
        )
    )
    changed = await coordinator.finish(
        DirectCallFinish(
            call_id="observe-machine-b",
            run_id=screenshot.run_id,
            status="completed",
            result={
                "session_id": "session-target-continuity",
                "status": "running",
                "machine": {
                    "alias": "Machine B",
                    "fingerprint": "target:bbbbbbbbbbbbbbbb",
                },
            },
        )
    )
    burst = await coordinator.begin(
        DirectCallBegin(
            call_id="must-not-execute",
            tool="pikvm_run_burst",
            arguments={"session_id": "session-target-continuity"},
        )
    )

    assert changed.status.value == "blocked"
    assert changed.error == "target identity changed during direct MCP session"
    assert changed.events[-1].kind == "target.identity_changed"
    assert changed.events[-1].data["previous_fingerprint"] == (
        "target:aaaaaaaaaaaaaaaa"
    )
    assert changed.events[-1].data["current_fingerprint"] == (
        "target:bbbbbbbbbbbbbbbb"
    )
    assert burst.allowed is False
    assert burst.reason == "target identity changed during direct MCP session"


@pytest.mark.asyncio
async def test_operator_pause_blocks_direct_hid_but_keeps_observation_available(
    tmp_path: Path,
) -> None:
    store = InMemoryRunStore()
    direct_calls = DirectCallCoordinator(
        store=store,
        computer=RecordingComputer(),  # type: ignore[arg-type]
    )
    app = create_harness_app(
        harness=NoopHarness(),  # type: ignore[arg-type]
        store=store,
        models=NoopModels(),
        access_token=TEST_ACCESS_TOKEN,
        observer_token=TEST_OBSERVER_TOKEN,
        allowed_origins={"http://harness"},
        direct_calls=direct_calls,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        opened = (
            await client.post(
                "/api/direct/calls/begin",
                json={
                    "call_id": "open",
                    "tool": "pikvm_open",
                    "arguments": {"label": "Direct session"},
                },
            )
        ).json()
        await client.post(
            "/api/direct/calls/finish",
            json={
                "call_id": "open",
                "status": "completed",
                "result": {
                    "session_id": "session-direct-2",
                    "status": "running",
                    "frame_id": 1,
                    "world_version": 1,
                    "control_epoch": 0,
                },
            },
        )
        paused = await client.post(
            f"/api/runs/{opened['run_id']}/pause",
            json={"reason": "operator is inspecting the screen"},
        )
        blocked = await client.post(
            "/api/direct/calls/begin",
            json={
                "call_id": "blocked-hid",
                "tool": "pikvm_run_burst",
                "arguments": {
                    "session_id": "session-direct-2",
                    "actions": [{"type": "key", "keys": ["ENTER"]}],
                },
            },
        )
        observed = await client.post(
            "/api/direct/calls/begin",
            json={
                "call_id": "read-only",
                "tool": "pikvm_screenshot",
                "arguments": {"session_id": "session-direct-2"},
            },
        )

    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    assert blocked.status_code == 200
    assert blocked.json() == {
        "allowed": False,
        "run_id": opened["run_id"],
        "reason": "operator paused direct MCP actions",
    }
    assert observed.status_code == 200
    assert observed.json()["allowed"] is True


@pytest.mark.asyncio
async def test_direct_model_cannot_approve_its_own_consequential_action(
    tmp_path: Path,
) -> None:
    store = InMemoryRunStore()
    computer = RecordingComputer()
    direct_calls = DirectCallCoordinator(
        store=store,
        computer=computer,  # type: ignore[arg-type]
    )
    app = create_harness_app(
        harness=NoopHarness(),  # type: ignore[arg-type]
        store=store,
        models=NoopModels(),
        access_token=TEST_ACCESS_TOKEN,
        observer_token=TEST_OBSERVER_TOKEN,
        allowed_origins={"http://harness"},
        direct_calls=direct_calls,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        opened = (
            await client.post(
                "/api/direct/calls/begin",
                json={
                    "call_id": "open",
                    "tool": "pikvm_open",
                    "arguments": {"label": "Send a Teams message"},
                },
            )
        ).json()
        await client.post(
            "/api/direct/calls/finish",
            json={
                "call_id": "open",
                "status": "completed",
                "result": {
                    "session_id": "session-direct-3",
                    "status": "running",
                },
            },
        )
        await client.post(
            "/api/direct/calls/begin",
            json={
                "call_id": "send",
                "tool": "pikvm_run_burst",
                "arguments": {
                    "session_id": "session-direct-3",
                    "actions": [{"type": "key", "keys": ["ENTER"]}],
                },
            },
        )
        held_response = await client.post(
            "/api/direct/calls/finish",
            json={
                "call_id": "send",
                "status": "completed",
                "result": {
                    "session_id": "session-direct-3",
                    "status": "needs_approval",
                    "approval_request": {
                        "approval_id": "approval-send-1",
                        "risk": "external_side_effect",
                        "reason": "This will send a Teams message.",
                    },
                },
            },
        )
        assert held_response.status_code == 200
        held = (
            await client.get(f"/api/runs/{opened['run_id']}")
        ).json()
        self_approval = (
            await client.post(
                "/api/direct/calls/begin",
                json={
                    "call_id": "model-approval",
                    "tool": "pikvm_resolve_approval",
                    "arguments": {
                        "session_id": "session-direct-3",
                        "approval_id": "approval-send-1",
                        "decision": {"type": "approve"},
                    },
                },
            )
        ).json()
        operator_approval = await client.post(
            f"/api/runs/{opened['run_id']}/approvals/approval-send-1",
            json={"type": "approve", "reason": "I reviewed the exact request"},
            headers={
                "x-pikvm-approval-intent": "approval-send-1",
                "origin": "http://harness",
            },
        )

    assert held["status"] == "needs_approval"
    assert held["events"][-1]["kind"] == "approval.required"
    assert held["events"][-1]["data"]["approval_id"] == "approval-send-1"
    assert self_approval == {
        "allowed": False,
        "run_id": opened["run_id"],
        "reason": "approval decisions must come from the operator console",
    }
    assert operator_approval.status_code == 200
    assert operator_approval.json()["status"] == "running"
    assert operator_approval.json()["pending_approval"] is None
    assert computer.calls == [
        (
            "approval",
            "session-direct-3",
            "approval-send-1",
            {
                "type": "approve",
                "reason": "I reviewed the exact request",
            },
        )
    ]


@pytest.mark.asyncio
async def test_visible_mcp_wraps_the_real_tool_call_with_harness_events(
    tmp_path: Path,
) -> None:
    store = InMemoryRunStore()
    direct_calls = DirectCallCoordinator(
        store=store,
        computer=RecordingComputer(),  # type: ignore[arg-type]
    )
    app = create_harness_app(
        harness=NoopHarness(),  # type: ignore[arg-type]
        store=store,
        models=NoopModels(),
        access_token=TEST_ACCESS_TOKEN,
        observer_token=TEST_OBSERVER_TOKEN,
        allowed_origins={"http://harness"},
        direct_calls=direct_calls,
    )
    transport = httpx.ASGITransport(app=app)
    reporter = DirectCallReporter(
        base_url="http://harness",
        observer_token=TEST_OBSERVER_TOKEN,
        mode="guarded",
        transport=transport,
        caller={
            "name": "codex-cli",
            "version": "0.92.0",
            "provider": "openai-oauth",
            "model": "gpt-5",
        },
    )
    mcp = VisibleFastMCP(
        "instrumented-test",
        json_response=True,
        reporter_factory=lambda: reporter,
    )
    execution_order: list[str] = []

    @mcp.tool()
    async def pikvm_open(label: str) -> dict[str, Any]:
        runs = await store.list()
        assert runs[0].events[-1].kind == "action.attempted"
        execution_order.append("tool")
        return {
            "session_id": "visible-session-1",
            "status": "running",
            "frame_id": 3,
            "world_version": 5,
            "control_epoch": 0,
        }

    result = await mcp.call_tool(
        "pikvm_open", {"label": "Inspect a remote app"}
    )
    runs = await store.list()
    run = runs[0]

    assert execution_order == ["tool"]
    assert run.caller["name"] == "codex-cli"
    assert [event.kind for event in run.events] == [
        "run.created",
        "action.attempted",
        "action.completed",
    ]
    assert run.session_id == "visible-session-1"
    assert run.observation is not None
    assert run.observation.frame_id == 3
    assert result


@pytest.mark.asyncio
async def test_direct_visibility_wraps_the_actual_mcp_protocol_dispatch() -> None:
    store = InMemoryRunStore()
    direct_calls = DirectCallCoordinator(
        store=store,
        computer=RecordingComputer(),  # type: ignore[arg-type]
    )
    app = create_harness_app(
        harness=NoopHarness(),  # type: ignore[arg-type]
        store=store,
        models=NoopModels(),
        access_token=TEST_ACCESS_TOKEN,
        observer_token=TEST_OBSERVER_TOKEN,
        allowed_origins={"http://harness"},
        direct_calls=direct_calls,
    )
    reporter = DirectCallReporter(
        base_url="http://harness",
        observer_token=TEST_OBSERVER_TOKEN,
        mode="guarded",
        transport=httpx.ASGITransport(app=app),
        caller={"provider": "openai-oauth", "model": "declared-model"},
    )
    server = VisibleFastMCP(
        "protocol-boundary-test",
        json_response=True,
        reporter_factory=lambda: reporter,
    )
    tool_executed = False

    @server.tool()
    async def pikvm_open(label: str) -> dict[str, Any]:
        nonlocal tool_executed
        tool_executed = True
        return {
            "session_id": "protocol-session",
            "status": "running",
            "label": label,
        }

    client_to_server_send, client_to_server_receive = (
        anyio.create_memory_object_stream(10)
    )
    server_to_client_send, server_to_client_receive = (
        anyio.create_memory_object_stream(10)
    )
    server_task = asyncio.create_task(
        server._mcp_server.run(  # noqa: SLF001 - protocol integration seam
            client_to_server_receive,
            server_to_client_send,
            server._mcp_server.create_initialization_options(),  # noqa: SLF001
        )
    )
    try:
        async with asyncio.timeout(3):
            async with ClientSession(
                server_to_client_receive,
                client_to_server_send,
                client_info=Implementation(
                    name="codex-protocol-client",
                    version="1.2.3",
                ),
            ) as session:
                await session.initialize()
                result = await session.call_tool(
                    "pikvm_open",
                    {"label": "Protocol-dispatched task"},
                )
                assert result.isError is False
    finally:
        server_task.cancel()
        await asyncio.gather(server_task, return_exceptions=True)

    runs = await store.list()
    assert tool_executed is True
    assert len(runs) == 1
    assert runs[0].caller["name"] == "codex-protocol-client"
    assert runs[0].caller["version"] == "1.2.3"
    assert [event.kind for event in runs[0].events] == [
        "run.created",
        "action.attempted",
        "action.completed",
    ]


@pytest.mark.asyncio
async def test_guarded_visibility_fails_closed_before_the_tool_body() -> None:
    async def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("harness offline", request=request)

    reporter = DirectCallReporter(
        base_url="http://harness",
        observer_token=TEST_OBSERVER_TOKEN,
        mode="guarded",
        transport=httpx.MockTransport(unavailable),
    )
    mcp = VisibleFastMCP(
        "guarded-test",
        reporter_factory=lambda: reporter,
    )
    executed = False

    @mcp.tool()
    async def pikvm_run_burst(session_id: str) -> dict[str, str]:
        nonlocal executed
        executed = True
        return {"session_id": session_id, "status": "running"}

    with pytest.raises(
        ToolError,
        match="preflight unavailable; direct MCP action was not executed",
    ):
        await mcp.call_tool(
            "pikvm_run_burst", {"session_id": "session-guarded"}
        )

    assert executed is False


@pytest.mark.asyncio
async def test_missing_visibility_configuration_fails_closed_before_tool_body() -> None:
    mcp = VisibleFastMCP(
        "missing-visibility-test",
        reporter_factory=lambda: None,
    )
    executed = False

    @mcp.tool()
    async def pikvm_run_burst(session_id: str) -> dict[str, str]:
        nonlocal executed
        executed = True
        return {"session_id": session_id, "status": "running"}

    with pytest.raises(
        ToolError,
        match="operator visibility is not configured; tool was not executed",
    ):
        await mcp.call_tool(
            "pikvm_run_burst", {"session_id": "session-unobserved"}
        )

    assert executed is False


@pytest.mark.asyncio
async def test_private_harness_child_can_explicitly_use_unobserved_tools() -> None:
    mcp = VisibleFastMCP(
        "private-harness-child-test",
        reporter_factory=lambda: None,
        allow_unobserved=True,
    )

    @mcp.tool()
    async def pikvm_screenshot(session_id: str) -> dict[str, str]:
        return {"session_id": session_id, "status": "running"}

    result = await mcp.call_tool(
        "pikvm_screenshot", {"session_id": "session-managed"}
    )

    assert result


@pytest.mark.asyncio
async def test_observe_only_visibility_never_breaks_an_existing_tool_call() -> None:
    async def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("harness offline", request=request)

    reporter = DirectCallReporter(
        base_url="http://harness",
        observer_token=TEST_OBSERVER_TOKEN,
        mode="observe",
        transport=httpx.MockTransport(unavailable),
    )
    mcp = VisibleFastMCP(
        "observe-test",
        json_response=True,
        reporter_factory=lambda: reporter,
    )

    @mcp.tool()
    async def pikvm_screenshot(session_id: str) -> dict[str, str]:
        return {"session_id": session_id, "status": "running"}

    result = await mcp.call_tool(
        "pikvm_screenshot", {"session_id": "session-observe"}
    )

    assert result


@pytest.mark.asyncio
async def test_observe_only_visibility_still_blocks_action_when_preflight_is_down() -> None:
    async def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("harness offline", request=request)

    reporter = DirectCallReporter(
        base_url="http://harness",
        observer_token=TEST_OBSERVER_TOKEN,
        mode="observe",
        transport=httpx.MockTransport(unavailable),
    )
    mcp = VisibleFastMCP(
        "observe-action-test",
        reporter_factory=lambda: reporter,
    )
    executed = False

    @mcp.tool()
    async def pikvm_run_burst(session_id: str) -> dict[str, str]:
        nonlocal executed
        executed = True
        return {"session_id": session_id, "status": "running"}

    with pytest.raises(
        ToolError,
        match="preflight unavailable; direct MCP action was not executed",
    ):
        await mcp.call_tool(
            "pikvm_run_burst",
            {"session_id": "session-observe-action"},
        )

    assert executed is False


@pytest.mark.asyncio
async def test_emergency_stop_aborts_a_direct_session_and_latches_the_gate(
    tmp_path: Path,
) -> None:
    store = InMemoryRunStore()
    computer = RecordingComputer()
    direct_calls = DirectCallCoordinator(
        store=store,
        computer=computer,  # type: ignore[arg-type]
    )
    app = create_harness_app(
        harness=NoopHarness(),  # type: ignore[arg-type]
        store=store,
        models=NoopModels(),
        access_token=TEST_ACCESS_TOKEN,
        observer_token=TEST_OBSERVER_TOKEN,
        allowed_origins={"http://harness"},
        direct_calls=direct_calls,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        opened = (
            await client.post(
                "/api/direct/calls/begin",
                json={
                    "call_id": "open-stop",
                    "tool": "pikvm_open",
                    "arguments": {"label": "Direct stop test"},
                },
            )
        ).json()
        await client.post(
            "/api/direct/calls/finish",
            json={
                "call_id": "open-stop",
                "status": "completed",
                "result": {
                    "session_id": "session-direct-stop",
                    "status": "running",
                },
            },
        )
        stopped = await client.post(
            f"/api/runs/{opened['run_id']}/abort",
            json={"reason": "unexpected pointer movement"},
        )
        retry = (
            await client.post(
                "/api/direct/calls/begin",
                json={
                    "call_id": "after-stop",
                    "tool": "pikvm_run_burst",
                    "arguments": {
                        "session_id": "session-direct-stop",
                        "actions": [{"type": "click", "x": 10, "y": 10}],
                    },
                },
            )
        ).json()

    assert stopped.status_code == 200
    assert stopped.json()["status"] == "aborted"
    assert computer.calls == [
        ("abort", "session-direct-stop", "unexpected pointer movement")
    ]
    assert retry == {
        "allowed": False,
        "run_id": opened["run_id"],
        "reason": "direct MCP session was stopped by the operator",
    }


@pytest.mark.asyncio
async def test_direct_secret_text_is_redacted_from_every_visible_api_shape() -> None:
    store = InMemoryRunStore()
    direct_calls = DirectCallCoordinator(
        store=store,
        computer=RecordingComputer(),  # type: ignore[arg-type]
    )
    app = create_harness_app(
        harness=NoopHarness(),  # type: ignore[arg-type]
        store=store,
        models=NoopModels(),
        access_token=TEST_ACCESS_TOKEN,
        observer_token=TEST_OBSERVER_TOKEN,
        allowed_origins={"http://harness"},
        direct_calls=direct_calls,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        started = (
            await client.post(
                "/api/direct/calls/begin",
                json={
                    "call_id": "secret-input",
                    "tool": "pikvm_type_text",
                    "arguments": {
                        "session_id": "session-secret",
                        "text": "correct horse battery staple",
                        "secret": True,
                    },
                },
            )
        ).json()
        durable = await store.get(started["run_id"])
        run = (
            await client.get(f"/api/runs/{started['run_id']}")
        ).json()
        events = (
            await client.get(
                f"/api/runs/{started['run_id']}/events"
            )
        ).json()

    assert "correct horse battery staple" not in durable.model_dump_json()
    assert "correct horse battery staple" not in str(run)
    assert "correct horse battery staple" not in str(events)
    visible_arguments = run["events"][-1]["data"]["arguments"]
    assert visible_arguments["text"] == "••••••••"
    assert visible_arguments["redacted"] is True


@pytest.mark.asyncio
async def test_model_side_observer_token_has_ingest_scope_only() -> None:
    store = InMemoryRunStore()
    direct_calls = DirectCallCoordinator(
        store=store,
        computer=RecordingComputer(),  # type: ignore[arg-type]
    )
    app = create_harness_app(
        harness=NoopHarness(),  # type: ignore[arg-type]
        store=store,
        models=NoopModels(),
        access_token=TEST_ACCESS_TOKEN,
        observer_token=TEST_OBSERVER_TOKEN,
        allowed_origins={"http://harness"},
        direct_calls=direct_calls,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_OBSERVER_TOKEN}"},
    ) as client:
        direct_health = await client.get("/api/direct/health")
        agent_health = await client.get("/api/agent/health")
        begin = await client.post(
            "/api/direct/calls/begin",
            json={
                "call_id": "observer-scope",
                "tool": "pikvm_open",
                "arguments": {"label": "scope test"},
            },
        )
        providers = await client.get("/api/providers")
        runs = await client.get("/api/runs")
        approval = await client.post(
            "/api/runs/unknown/approvals/unknown",
            json={"type": "approve"},
            headers={
                "origin": "http://harness",
                "x-pikvm-approval-intent": "unknown",
            },
        )

    assert direct_health.status_code == 200
    assert agent_health.status_code == 401
    assert begin.status_code == 200
    assert providers.status_code == 401
    assert runs.status_code == 401
    assert approval.status_code == 401


@pytest.mark.asyncio
async def test_high_level_agent_token_cannot_resolve_approval_or_read_providers(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()

    class AgentSurfaceHarness:
        async def create(self, task: str) -> RunSnapshot:
            run = RunSnapshot(
                run_id="agent-run", task=task, status="paused"
            )
            await store.save(run)
            return run

        async def pause(self, run_id: str, reason: str) -> RunSnapshot:
            return await store.get(run_id)

        async def abort(self, run_id: str, reason: str) -> RunSnapshot:
            return await store.get(run_id)

    app = create_harness_app(
        harness=AgentSurfaceHarness(),  # type: ignore[arg-type]
        store=store,
        models=NoopModels(),
        access_token=TEST_ACCESS_TOKEN,
        agent_token=TEST_AGENT_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_AGENT_TOKEN}"},
    ) as client:
        created = await client.post(
            "/api/runs", json={"task": "agent-scoped task", "auto_start": False}
        )
        visible = await client.get("/api/runs/agent-run")
        agent_health = await client.get("/api/agent/health")
        paused = await client.post(
            "/api/runs/agent-run/pause", json={"reason": "inspect"}
        )
        providers = await client.get("/api/providers")
        approval = await client.post(
            "/api/runs/agent-run/approvals/a_1",
            json={"type": "approve"},
            headers={
                "origin": "http://harness",
                "x-pikvm-approval-intent": "a_1",
            },
        )

    assert created.status_code == 200
    assert visible.status_code == 200
    assert agent_health.status_code == 200
    assert agent_health.json()["scope"] == "managed-harness-control"
    assert paused.status_code == 200
    assert providers.status_code == 401
    assert approval.status_code == 401


@pytest.mark.asyncio
async def test_direct_failure_persists_a_class_not_tool_controlled_prose() -> None:
    store = InMemoryRunStore()
    direct_calls = DirectCallCoordinator(
        store=store,
        computer=RecordingComputer(),  # type: ignore[arg-type]
    )
    started = await direct_calls.begin(
        DirectCallBegin(
            call_id="failed-secret-call",
            tool="pikvm_type_text",
            arguments={
                "session_id": "session-secret-error",
                "text": "secret value from failed tool",
                "secret": True,
            },
        )
    )

    run = await direct_calls.finish(
        DirectCallFinish(
            call_id="failed-secret-call",
            run_id=started.run_id,
            status="failed",
            latency_ms=287,
            error="Tool exploded while typing secret value from failed tool",
        )
    )

    assert run.error == "direct MCP tool failed"
    assert run.events[-1].data["tool"] == "pikvm_type_text"
    assert run.events[-1].data["latency_ms"] == 287
    assert "secret value from failed tool" not in run.model_dump_json()


@pytest.mark.asyncio
async def test_completed_direct_tool_call_keeps_the_session_running() -> None:
    store = InMemoryRunStore()
    direct_calls = DirectCallCoordinator(
        store=store,
        computer=RecordingComputer(),  # type: ignore[arg-type]
    )

    opened = await direct_calls.begin(
        DirectCallBegin(
            call_id="open-for-burst",
            tool="pikvm_open",
            arguments={"label": "Direct lifecycle test"},
        )
    )
    await direct_calls.finish(
        DirectCallFinish(
            call_id="open-for-burst",
            status="completed",
            result={
                "session_id": "session-lifecycle",
                "status": "running",
            },
        )
    )
    first_burst = await direct_calls.begin(
        DirectCallBegin(
            call_id="first-burst",
            tool="pikvm_run_burst",
            arguments={"session_id": "session-lifecycle", "actions": []},
        )
    )
    run = await direct_calls.finish(
        DirectCallFinish(
            call_id="first-burst",
            status="completed",
            result={
                "session_id": "session-lifecycle",
                "status": "completed",
            },
        )
    )
    second_burst = await direct_calls.begin(
        DirectCallBegin(
            call_id="second-burst",
            tool="pikvm_run_burst",
            arguments={"session_id": "session-lifecycle", "actions": []},
        )
    )

    assert opened.allowed is True
    assert first_burst.allowed is True
    assert run.status.value == "running"
    assert second_burst.allowed is True


@pytest.mark.asyncio
async def test_direct_call_finish_recovers_from_a_coordinator_restart() -> None:
    store = InMemoryRunStore()
    computer = RecordingComputer()
    first_coordinator = DirectCallCoordinator(
        store=store,
        computer=computer,  # type: ignore[arg-type]
    )
    started = await first_coordinator.begin(
        DirectCallBegin(
            call_id="survives-restart",
            tool="pikvm_open",
            arguments={"label": "Restart recovery"},
        )
    )
    restarted_coordinator = DirectCallCoordinator(
        store=store,
        computer=computer,  # type: ignore[arg-type]
    )

    run = await restarted_coordinator.finish(
        DirectCallFinish(
            call_id="survives-restart",
            run_id=started.run_id,
            status="completed",
            result={
                "session_id": "session-after-restart",
                "status": "running",
            },
        )
    )

    assert run.run_id == started.run_id
    assert run.session_id == "session-after-restart"
    assert run.events[-1].kind == "action.completed"
