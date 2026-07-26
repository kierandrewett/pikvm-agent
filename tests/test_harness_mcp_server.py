from __future__ import annotations

from typing import Any

import asyncio
import httpx
import pytest

from pikvm_agent import harness_mcp_server
from pikvm_agent.harness.agent_models import RunSnapshot, RunStatus
from pikvm_agent.harness.agent_store import InMemoryRunStore
from pikvm_agent.harness.api import create_harness_app


AGENT_TOKEN = "managed-mcp-agent-token-0123456789abcdef"
OPERATOR_TOKEN = "managed-mcp-operator-token-0123456789abcd"


class VisibleHarness:
    def __init__(self, store: InMemoryRunStore) -> None:
        self.store = store

    async def create(
        self,
        task: str,
        *,
        caller: dict[str, Any] | None = None,
    ) -> RunSnapshot:
        run = RunSnapshot(
            run_id="managed-visible-run",
            task=task,
            status=RunStatus.RUNNING,
            caller=dict(caller or {}),
            session_id="synthetic-session",
        )
        run.record("run.created")
        run.record("computer.opened", frame_id=1, world_version=1)
        await self.store.save(run)
        return run

    async def continue_run(self, run_id: str) -> RunSnapshot:
        run = await self.store.get(run_id)
        run.record(
            "action.attempted",
            tool="pikvm_run_burst",
            arguments={"actions": [{"type": "key", "keys": ["CTRL", "P"]}]},
        )
        run.record("action.completed", frame_id=2, world_version=2)
        run.status = RunStatus.COMPLETED
        run.record("run.completed")
        await self.store.save(run)
        return run

    async def pause(self, run_id: str, reason: str) -> RunSnapshot:
        run = await self.store.get(run_id)
        run.status = RunStatus.PAUSED
        await self.store.save(run)
        return run

    async def resolve_approval(
        self,
        run_id: str,
        approval_id: str,
        decision: dict[str, Any],
    ) -> RunSnapshot:
        raise AssertionError("model-side approval must not be reachable")

    async def abort(self, run_id: str, reason: str) -> RunSnapshot:
        run = await self.store.get(run_id)
        run.status = RunStatus.ABORTED
        await self.store.save(run)
        return run


class VisibleModels:
    def health(self) -> dict[str, dict[str, object]]:
        return {}


def install_http_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(harness_mcp_server.httpx, "AsyncClient", client)


@pytest.mark.asyncio
async def test_high_level_mcp_exposes_no_raw_hid_or_self_approval_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_request(
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        calls.append(
            {
                "method": method,
                "path": path,
                "body": body,
                "approval_id": approval_id,
            }
        )
        return {"run_id": "run_1", "status": "paused"}

    monkeypatch.setattr(harness_mcp_server, "_request", fake_request)
    monkeypatch.setenv("PIKVM_HARNESS_URL", "http://127.0.0.1:47616")
    monkeypatch.setenv("PIKVM_HARNESS_AGENT_TOKEN", "x" * 32)
    monkeypatch.setenv("PIKVM_MCP_CALLER_LABEL", "codex-cli")

    names = sorted(tool.name for tool in await harness_mcp_server.mcp.list_tools())
    started = await harness_mcp_server.computer_start_task("Open a file")
    paused = await harness_mcp_server.computer_pause("run_1", "inspect")

    assert names == [
        "computer_abort",
        "computer_continue",
        "computer_pause",
        "computer_start_task",
        "computer_status",
    ]
    assert all("click" not in name and "type" not in name for name in names)
    assert started["operator_ui"] == "http://127.0.0.1:47616/app/"
    assert paused["status"] == "paused"
    assert calls[0] == {
        "method": "POST",
        "path": "/api/runs",
        "body": {
            "task": "Open a file",
            "auto_start": True,
            "source_client": "codex-cli",
        },
        "approval_id": None,
    }
    assert calls[-1] == {
        "method": "POST",
        "path": "/api/runs/run_1/pause",
        "body": {"reason": "inspect"},
        "approval_id": None,
    }
    assert all("approval" not in name for name in names)
    assert all("media" not in name and "upload" not in name for name in names)


@pytest.mark.asyncio
async def test_managed_mcp_call_crosses_auth_into_visible_durable_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryRunStore()
    app = create_harness_app(
        harness=VisibleHarness(store),  # type: ignore[arg-type]
        store=store,
        models=VisibleModels(),
        access_token=OPERATOR_TOKEN,
        agent_token=AGENT_TOKEN,
        allowed_origins={"http://harness"},
    )

    async def asgi_request(
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        approval_id: str | None = None,
    ) -> Any:
        assert approval_id is None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://harness",
            headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
        ) as client:
            response = await client.request(method, path, json=body)
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(harness_mcp_server, "_request", asgi_request)
    monkeypatch.setenv("PIKVM_HARNESS_URL", "http://harness")
    monkeypatch.setenv("PIKVM_HARNESS_AGENT_TOKEN", AGENT_TOKEN)
    monkeypatch.setenv("PIKVM_MCP_CALLER_LABEL", "claude-cli")

    started = await harness_mcp_server.computer_start_task(
        "Open a document and inspect it"
    )
    for _ in range(50):
        durable = await store.get(started["run_id"])
        if durable.status is RunStatus.COMPLETED:
            break
        await asyncio.sleep(0)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
        headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
    ) as operator:
        inventory = await operator.get("/api/runs")
        visible = await operator.get(f"/api/runs/{started['run_id']}")

    assert started["operator_ui"] == "http://harness/app/"
    assert inventory.json()[0]["run_id"] == "managed-visible-run"
    assert visible.json()["caller"] == {
        "interface": "managed_mcp",
        "label": "claude-cli",
    }
    assert [event["kind"] for event in visible.json()["events"]] == [
        "run.created",
        "computer.opened",
        "action.attempted",
        "action.completed",
        "run.completed",
    ]


@pytest.mark.asyncio
async def test_managed_mcp_reports_safe_outage_then_recovers_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError(
                "connection leaked an internal endpoint",
                request=request,
            )
        return httpx.Response(
            200,
            json={"run_id": "durable-run", "status": "completed"},
        )

    install_http_transport(monkeypatch, handler)
    monkeypatch.setenv(
        "PIKVM_HARNESS_URL",
        "http://internal-harness.invalid:47616",
    )
    monkeypatch.setenv(
        "PIKVM_HARNESS_AGENT_TOKEN",
        "secret-agent-token-0123456789abcdef",
    )

    with pytest.raises(RuntimeError) as outage:
        await harness_mcp_server._request(
            "GET",
            "/api/runs/durable-run",
        )

    message = str(outage.value)
    assert message == (
        "managed harness unavailable; keep this task in managed mode and "
        "retry after the operator harness restarts"
    )
    assert "internal-harness" not in message
    assert "secret-agent-token" not in message

    recovered = await harness_mcp_server._request(
        "GET",
        "/api/runs/durable-run",
    )

    assert recovered == {
        "run_id": "durable-run",
        "status": "completed",
    }
    assert attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (
            401,
            "managed harness authorization failed; the operator must repair "
            "the scoped agent credential",
        ),
        (
            403,
            "managed harness authorization failed; the operator must repair "
            "the scoped agent credential",
        ),
        (404, "managed harness run was not found"),
        (409, "managed harness refused the request (HTTP 409)"),
        (
            503,
            "managed harness service failed; retry after the operator service "
            "recovers",
        ),
    ],
)
async def test_managed_mcp_classifies_http_failures_without_body_leakage(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    message: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"detail": "secret internal service description"},
        )

    install_http_transport(monkeypatch, handler)
    monkeypatch.setenv(
        "PIKVM_HARNESS_URL",
        "http://internal-harness.invalid:47616",
    )
    monkeypatch.setenv(
        "PIKVM_HARNESS_AGENT_TOKEN",
        "secret-agent-token-0123456789abcdef",
    )

    with pytest.raises(RuntimeError, match="^" + message.replace(
        "(", r"\("
    ).replace(")", r"\)") + "$") as failure:
        await harness_mcp_server._request(
            "GET",
            "/api/runs/durable-run",
        )

    assert str(failure.value) == message
    assert "secret internal" not in str(failure.value)
    assert "internal-harness" not in str(failure.value)
