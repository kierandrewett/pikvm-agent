from __future__ import annotations

from pathlib import Path
import asyncio
from typing import Any

import httpx
import pytest

from pikvm_agent.harness.agent_models import (
    ComputerObservation,
    MediaFileEvidence,
    MediaTransaction,
    MediaTransactionState,
    PendingAction,
    RunModelRoute,
    RunSnapshot,
    RunStatus,
    utc_now,
)
from pikvm_agent.harness.agent_store import InMemoryRunStore
from pikvm_agent.harness.api import (
    ShutdownSafeStreamingResponse,
    _sse_event,
    _visible_run,
    create_harness_app,
)

TEST_ACCESS_TOKEN = "test-harness-token-0123456789abcdef"
TEST_AGENT_TOKEN = "test-agent-token-000123456789abcdef"
TEST_OBSERVER_TOKEN = "test-observer-token-0123456789abc"


class StubHarness:
    def __init__(self, store: InMemoryRunStore, frame: Path) -> None:
        self.store = store
        self.frame = frame
        self.calls: list[tuple[str, Any]] = []

    async def create(
        self,
        task: str,
        *,
        caller: dict[str, Any] | None = None,
        model_provider: str | None = None,
        model_route: RunModelRoute | None = None,
    ) -> RunSnapshot:
        run = RunSnapshot(
            run_id="run_1",
            task=task,
            status=RunStatus.RUNNING,
            model_provider=model_provider,
            model_route=model_route,
            caller=dict(caller or {}),
            session_id="s_1",
            observation=ComputerObservation(
                session_id="s_1",
                status="paused",
                frame_id=1,
                world_version=3,
                control_epoch=1,
                image_path=str(self.frame),
            ),
        )
        run.record("computer.opened", frame_id=1)
        await self.store.save(run)
        return run

    async def continue_run(self, run_id: str) -> RunSnapshot:
        self.calls.append(("continue", run_id))
        run = await self.store.get(run_id)
        run.record(
            "action.attempted",
            tool="pikvm_run_burst",
            arguments={"actions": [{"type": "key", "keys": ["CTRL", "P"]}]},
        )
        run.status = RunStatus.PAUSED
        await self.store.save(run)
        return run

    async def resolve_approval(
        self, run_id: str, approval_id: str, decision: dict[str, Any]
    ) -> RunSnapshot:
        self.calls.append(("approval", run_id, approval_id, decision))
        return await self.store.get(run_id)

    async def pause(self, run_id: str, reason: str) -> RunSnapshot:
        self.calls.append(("pause", run_id, reason))
        run = await self.store.get(run_id)
        run.status = RunStatus.PAUSED
        await self.store.save(run)
        return run

    async def steer(self, run_id: str, instruction: str) -> RunSnapshot:
        self.calls.append(("steer", run_id, instruction))
        run = await self.store.get(run_id)
        run.operator_guidance.append(instruction)
        run.status = RunStatus.PAUSED
        run.record("run.steered", instruction=instruction)
        await self.store.save(run)
        return run

    async def abort(self, run_id: str, reason: str) -> RunSnapshot:
        self.calls.append(("abort", run_id, reason))
        run = await self.store.get(run_id)
        run.status = RunStatus.ABORTED
        await self.store.save(run)
        return run


class StubModels:
    def health(self) -> dict[str, dict[str, object]]:
        return {
            "fast-oauth": {
                "calls": 2,
                "successes": 2,
                "failures": 0,
                "last_latency_ms": 81,
            }
        }


class RoutedStubModels:
    def health(self) -> dict[str, dict[str, object]]:
        return {
            "strong": {
                "ready": True,
                "routes": [
                    {"role": "reasoner", "position": 1},
                    {"role": "verifier", "position": 1},
                ],
            },
            "fast": {
                "ready": True,
                "routes": [{"role": "controller", "position": 1}],
            },
            "backup": {
                "ready": True,
                "routes": [
                    {"role": "reasoner", "position": 2},
                    {"role": "controller", "position": 2},
                    {"role": "verifier", "position": 2},
                ],
            },
            "offline": {
                "ready": False,
                "routes": [{"role": "controller", "position": 3}],
            },
        }


class StubLiveFrames:
    async def get(self, session_id: str) -> Any:
        assert session_id == "s_1"
        return type(
            "LiveFrame",
            (),
            {
                "data": b"live-frame",
                "media_type": "image/jpeg",
                "captured_at": "2026-07-24T18:00:00Z",
                "width": 1280,
                "height": 800,
            },
    )()


class RejectedLiveFrames:
    async def get(self, session_id: str) -> Any:
        assert session_id == "s_1"
        raise ValueError("preview exceeded resource envelope")


@pytest.mark.asyncio
async def test_agent_created_run_preserves_managed_client_identity(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    app = create_harness_app(
        harness=StubHarness(store, frame),  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        agent_token=TEST_AGENT_TOKEN,
        allowed_origins={"http://harness"},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_AGENT_TOKEN}"},
    ) as client:
        response = await client.post(
            "/api/runs",
            json={
                "task": "Open a document",
                "auto_start": False,
                "source_client": "codex-cli",
            },
        )

    assert response.status_code == 200
    assert response.json()["origin"] == "managed"
    assert response.json()["caller"] == {
        "interface": "managed_mcp",
        "label": "codex-cli",
    }
    assert (await store.get("run_1")).caller == response.json()["caller"]


@pytest.mark.asyncio
async def test_operator_can_choose_a_configured_byo_model_provider(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    app = create_harness_app(
        harness=StubHarness(store, frame),  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        selected = await client.post(
            "/api/runs",
            json={
                "task": "Create the quarterly workbook",
                "auto_start": False,
                "model_provider": "fast-oauth",
            },
        )
        unknown = await client.post(
            "/api/runs",
            json={
                "task": "Create the quarterly workbook",
                "auto_start": False,
                "model_provider": "not-configured",
            },
        )

    assert selected.status_code == 200
    assert selected.json()["model_provider"] == "fast-oauth"
    assert unknown.status_code == 422
    assert "unknown model provider" in unknown.json()["detail"]


@pytest.mark.asyncio
async def test_operator_can_choose_independent_role_preferences_with_fallback(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    app = create_harness_app(
        harness=StubHarness(store, frame),  # type: ignore[arg-type]
        store=store,
        models=RoutedStubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        selected = await client.post(
            "/api/runs",
            json={
                "task": "Create the quarterly workbook",
                "auto_start": False,
                "model_preferences": {
                    "reasoner": "backup",
                    "controller": "fast",
                    "verifier": "strong",
                },
            },
        )
        unknown = await client.post(
            "/api/runs",
            json={
                "task": "Use an unknown reasoner",
                "auto_start": False,
                "model_preferences": {"reasoner": "not-configured"},
            },
        )
        offline = await client.post(
            "/api/runs",
            json={
                "task": "Use an offline controller",
                "auto_start": False,
                "model_preferences": {"controller": "offline"},
            },
        )
        conflicting = await client.post(
            "/api/runs",
            json={
                "task": "Use an ambiguous route",
                "auto_start": False,
                "model_provider": "fast",
                "model_preferences": {"controller": "fast"},
            },
        )

    assert selected.status_code == 200
    assert selected.json()["model_provider"] is None
    assert selected.json()["model_route"] == {
        "reasoner": ["backup", "strong"],
        "controller": ["fast", "backup"],
        "verifier": ["strong", "backup"],
    }
    durable = await store.get("run_1")
    assert durable.model_route is not None
    assert durable.model_route.controller == ["fast", "backup"]
    assert unknown.status_code == 422
    assert "unknown model provider" in unknown.json()["detail"]
    assert offline.status_code == 409
    assert "model provider is not ready" in offline.json()["detail"]
    assert conflicting.status_code == 422


@pytest.mark.asyncio
async def test_only_operator_can_durably_steer_a_managed_run(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = StubHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        agent_token=TEST_AGENT_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)
    await harness.create("Write the report")

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
    ) as client:
        agent_denied = await client.post(
            "/api/runs/run_1/steer",
            headers={"authorization": f"Bearer {TEST_AGENT_TOKEN}"},
            json={
                "instruction": "Preserve the existing heading",
                "auto_resume": False,
            },
        )
        steered = await client.post(
            "/api/runs/run_1/steer",
            headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
            json={
                "instruction": "Preserve the existing heading",
                "auto_resume": False,
            },
        )

    assert agent_denied.status_code == 401
    assert steered.status_code == 200
    assert steered.json()["operator_guidance"] == [
        "Preserve the existing heading"
    ]
    assert steered.json()["events"][-1]["kind"] == "run.steered"
    assert harness.calls == [
        ("steer", "run_1", "Preserve the existing heading")
    ]


@pytest.mark.asyncio
async def test_managed_harness_cannot_steer_a_direct_mcp_run(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = StubHarness(store, frame)
    await store.save(
        RunSnapshot(
            run_id="direct_1",
            task="Externally controlled edit",
            status=RunStatus.PAUSED,
            origin="direct_mcp",
        )
    )
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        response = await client.post(
            "/api/runs/direct_1/steer",
            json={
                "instruction": "Change the externally owned plan",
                "auto_resume": True,
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "direct MCP runs remain controlled by their external client"
    )
    assert harness.calls == []


class StubMediaApprovals:
    def __init__(self, store: InMemoryRunStore) -> None:
        self.store = store
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def resolve_approval(
        self,
        run_id: str,
        approval_id: str,
        decision: dict[str, Any],
    ) -> RunSnapshot:
        self.calls.append((run_id, approval_id, decision))
        run = await self.store.get_state(run_id)
        run.pending_approval = None
        run.status = RunStatus.PAUSED
        assert run.media_transaction is not None
        run.media_transaction.state = MediaTransactionState.ATTACHED
        run.record(
            "media.attached",
            transaction_id=run.media_transaction.transaction_id,
        )
        await self.store.save(run)
        return run

    async def release(
        self,
        run_id: str,
        reason: str = "virtual media no longer needed",
    ) -> RunSnapshot:
        self.calls.append((run_id, "release", {"reason": reason}))
        run = await self.store.get_state(run_id)
        assert run.media_transaction is not None
        run.media_transaction.state = MediaTransactionState.RELEASED
        run.record(
            "media.released",
            transaction_id=run.media_transaction.transaction_id,
            reason=reason,
        )
        await self.store.save(run)
        return run


@pytest.mark.asyncio
async def test_virtual_media_approval_uses_operator_credential_and_exact_intent(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    lease = utc_now()
    transaction = MediaTransaction(
        transaction_id="media-1",
        state=MediaTransactionState.AWAITING_APPROVAL,
        approval_id="approval-media-1",
        purpose="Open exact workbook",
        session_id="session-lab",
        machine_fingerprint="machine-7",
        control_epoch=4,
        adapter="pikvm",
        media_name="pikvm-abc.iso",
        image_sha256="a" * 64,
        image_bytes=42,
        manifest_sha256="b" * 64,
        files=[
            MediaFileEvidence(
                name="earnings.xlsx",
                size=12,
                sha256="c" * 64,
            )
        ],
        lease_expires_at=lease,
    )
    await store.save(
        RunSnapshot(
            run_id="media-run",
            task="Open workbook",
            status=RunStatus.NEEDS_APPROVAL,
            pending_approval={
                "approval_id": transaction.approval_id,
                "kind": "virtual_media_attach",
            },
            media_transaction=transaction,
        )
    )
    media = StubMediaApprovals(store)
    harness = StubHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        agent_token=TEST_AGENT_TOKEN,
        allowed_origins={"http://harness"},
        media_transactions=media,
    )
    transport = httpx.ASGITransport(app=app)
    endpoint = "/api/runs/media-run/approvals/approval-media-1"
    exact_intent = {"X-PiKVM-Approval-Intent": "approval-media-1"}

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
    ) as client:
        agent_denied = await client.post(
            endpoint,
                headers={
                    "authorization": f"Bearer {TEST_AGENT_TOKEN}",
                    "origin": "http://harness",
                    **exact_intent,
                },
            json={"type": "approve", "reason": "model cannot approve"},
        )
        missing_intent = await client.post(
            endpoint,
            headers={
                "authorization": f"Bearer {TEST_ACCESS_TOKEN}",
                "origin": "http://harness",
            },
            json={"type": "approve", "reason": "generic request"},
        )
        approved = await client.post(
            endpoint,
                headers={
                    "authorization": f"Bearer {TEST_ACCESS_TOKEN}",
                    "origin": "http://harness",
                    **exact_intent,
                },
            json={"type": "approve", "reason": "expected lab workbook"},
        )

    assert agent_denied.status_code == 401
    assert missing_intent.status_code == 409
    assert approved.status_code == 200
    assert approved.json()["media_transaction"]["state"] == "attached"
    assert media.calls == [
        (
            "media-run",
            "approval-media-1",
            {"type": "approve", "reason": "expected lab workbook"},
        )
    ]
    assert harness.calls == []


@pytest.mark.asyncio
async def test_emergency_stop_halts_run_then_releases_attached_media(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    transaction = MediaTransaction(
        transaction_id="media-stop",
        state=MediaTransactionState.ATTACHED,
        approval_id="approval-media-stop",
        purpose="Open exact workbook",
        session_id="session-lab",
        machine_fingerprint="machine-7",
        control_epoch=4,
        adapter="pikvm",
        media_name="pikvm-stop.iso",
        image_sha256="a" * 64,
        image_bytes=42,
        manifest_sha256="b" * 64,
        files=[
            MediaFileEvidence(
                name="earnings.xlsx",
                size=12,
                sha256="c" * 64,
            )
        ],
        lease_expires_at=utc_now(),
        attached_at=utc_now(),
    )
    await store.save(
        RunSnapshot(
            run_id="media-stop-run",
            task="Open workbook",
            status=RunStatus.RUNNING,
            media_transaction=transaction,
        )
    )
    media = StubMediaApprovals(store)
    harness = StubHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
        media_transactions=media,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        stopped = await client.post(
            "/api/runs/media-stop-run/abort",
            json={"reason": "operator emergency stop"},
        )

    assert stopped.status_code == 200
    assert stopped.json()["status"] == "aborted"
    assert stopped.json()["media_transaction"]["state"] == "released"
    assert harness.calls == [
        ("abort", "media-stop-run", "operator emergency stop")
    ]
    assert media.calls == [
        (
            "media-stop-run",
            "release",
            {"reason": "emergency stop: operator emergency stop"},
        )
    ]


@pytest.mark.asyncio
async def test_artifact_acceptance_is_observer_owned_and_visible_in_the_run(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    await store.save(
        RunSnapshot(
            run_id="office_run",
            task="Create the quarterly workbook",
            status=RunStatus.COMPLETED,
        )
    )
    app = create_harness_app(
        harness=StubHarness(store, frame),  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        agent_token=TEST_AGENT_TOKEN,
        observer_token=TEST_OBSERVER_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)
    endpoint = "/api/runs/office_run/artifact-acceptance"

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
    ) as client:
        denied = await client.post(
            endpoint,
            headers={"authorization": f"Bearer {TEST_AGENT_TOKEN}"},
            json={
                "kind": "office_artifact",
                "label": "Quarterly earnings workbook",
                "state": "pending",
            },
        )
        operator_denied = await client.post(
            endpoint,
            headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
            json={
                "kind": "office_artifact",
                "label": "Quarterly earnings workbook",
                "state": "pending",
            },
        )
        observer_headers = {
            "authorization": f"Bearer {TEST_OBSERVER_TOKEN}"
        }
        pending = await client.post(
            endpoint,
            headers=observer_headers,
            json={
                "kind": "office_artifact",
                "label": "Quarterly earnings workbook",
                "state": "pending",
            },
        )
        capturing = await client.post(
            endpoint,
            headers=observer_headers,
            json={
                "kind": "office_artifact",
                "label": "Quarterly earnings workbook",
                "state": "capturing",
            },
        )
        passed = await client.post(
            endpoint,
            headers=observer_headers,
            json={
                "kind": "office_artifact",
                "label": "Quarterly earnings workbook",
                "state": "passed",
                "artifact_format": "xlsx",
                "checks_passed": 24,
                "checks_total": 24,
                "byte_count": 12_345,
                "sha256": "a" * 64,
            },
        )
        rewrite = await client.post(
            endpoint,
            headers=observer_headers,
            json={
                "kind": "office_artifact",
                "label": "Quarterly earnings workbook",
                "state": "failed",
                "error_class": "synthetic-rewrite",
            },
        )
        detail = await client.get(
            "/api/runs/office_run",
            headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
        )
        listed = await client.get(
            "/api/runs",
            headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
        )

    assert denied.status_code == 401
    assert operator_denied.status_code == 401
    assert pending.status_code == 200
    assert capturing.status_code == 200
    assert passed.status_code == 200
    assert rewrite.status_code == 409
    payload = detail.json()
    assert payload["artifact_acceptance"] == {
        "kind": "office_artifact",
        "label": "Quarterly earnings workbook",
        "state": "passed",
        "artifact_format": "xlsx",
        "checks_passed": 24,
        "checks_total": 24,
        "byte_count": 12_345,
        "sha256": "a" * 64,
        "error_class": None,
        "updated_at": payload["artifact_acceptance"]["updated_at"],
    }
    assert [
        event["kind"]
        for event in payload["events"]
        if event["kind"].startswith("artifact.")
    ] == [
        "artifact.pending",
        "artifact.capturing",
        "artifact.passed",
    ]
    assert listed.json()[0]["artifact_acceptance_state"] == "passed"


@pytest.mark.asyncio
async def test_verification_image_is_authenticated_without_exposing_its_path(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    comparison = tmp_path / "before-after.png"
    comparison.write_bytes(b"\x89PNG\r\n\x1a\ncomparison")
    store = InMemoryRunStore()
    run = RunSnapshot(
        run_id="evidence_run",
        task="Inspect the labelled transition",
        status=RunStatus.PAUSED,
        latest_verification_image_path=str(comparison),
        latest_verification_image_revision=3,
    )
    await store.save(run)
    app = create_harness_app(
        harness=StubHarness(store, frame),  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        detail = await client.get("/api/runs/evidence_run")
        evidence = await client.get(
            "/api/runs/evidence_run/verification-image"
        )

    payload = detail.json()
    assert detail.status_code == 200
    assert payload["verification_image_available"] is True
    assert payload["verification_image_revision"] == 3
    assert "latest_verification_image_path" not in payload
    assert str(comparison) not in detail.text
    assert evidence.status_code == 200
    assert evidence.content == comparison.read_bytes()
    assert evidence.headers["content-type"] == "image/png"
    assert evidence.headers["cache-control"] == "no-store"
    assert evidence.headers["x-pikvm-evidence-mode"] == "before-after"


@pytest.mark.asyncio
async def test_verification_image_is_404_until_verifier_evidence_exists(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    await store.save(
        RunSnapshot(
            run_id="no_evidence",
            task="Wait for a verified transition",
            status=RunStatus.PAUSED,
        )
    )
    app = create_harness_app(
        harness=StubHarness(store, frame),  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        detail = await client.get("/api/runs/no_evidence")
        evidence = await client.get(
            "/api/runs/no_evidence/verification-image"
        )

    assert detail.status_code == 200
    assert detail.json()["verification_image_available"] is False
    assert detail.json()["verification_image_revision"] == 0
    assert evidence.status_code == 404


def test_sse_event_exposes_retry_ready_and_heartbeat_contract() -> None:
    ready = _sse_event(
        "stream.ready",
        {"run_id": "run_1", "cursor": 7},
        retry_ms=1_000,
    )
    heartbeat = _sse_event(
        "stream.heartbeat",
        {"run_id": "run_1", "cursor": 8},
    )

    assert ready == (
        "retry: 1000\n"
        "event: stream.ready\n"
        'data: {"run_id":"run_1","cursor":7}\n\n'
    )
    assert heartbeat == (
        "event: stream.heartbeat\n"
        'data: {"run_id":"run_1","cursor":8}\n\n'
    )


def test_visible_run_never_serializes_unbounded_event_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = RunSnapshot(
        run_id="bounded",
        task="Bound the serializer itself",
        status=RunStatus.RUNNING,
    )
    run.model_budget.provider_attempts = 4
    run.model_budget.provider_attempt_limit = 40
    run.model_budget.committed_cost_microusd = 125_000
    run.model_budget.max_cost_microusd = 1_500_000
    run.model_budget.pricing_version = "customer-prices-v1"
    for index in range(600):
        run.record("run.tick", number=index)
    original_model_dump = RunSnapshot.model_dump

    def guarded_model_dump(
        self: RunSnapshot, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        assert kwargs.get("exclude") == {
            "events",
            "latest_verification_image_path",
        }
        return original_model_dump(self, *args, **kwargs)

    monkeypatch.setattr(RunSnapshot, "model_dump", guarded_model_dump)

    visible = _visible_run(run)

    assert visible["event_count"] == 600
    assert len(visible["events"]) == 500
    assert visible["events"][0]["sequence"] == 101
    assert visible["model_budget"] == {
        "provider_attempts": 4,
        "provider_attempt_limit": 40,
        "committed_cost_microusd": 125_000,
        "max_cost_microusd": 1_500_000,
        "pricing_version": "customer-prices-v1",
        "reservations_microusd": {},
        "provider_cost_microusd": {},
        "outstanding_cost_microusd": 0,
    }


def test_visible_run_omits_internal_paths_and_raw_tool_payloads() -> None:
    run = RunSnapshot(
        run_id="safe-observation",
        task="Inspect a direct result",
        status=RunStatus.RUNNING,
        observation=ComputerObservation(
            session_id="session-safe-observation",
            status="running",
            frame_id=4,
            image_path="/private/harness/frames/frame-4.png",
            raw={
                "content_base64": "private-file-payload",
                "image_path": "/private/tool/result.png",
                "status": "running",
            },
        ),
    )

    visible = _visible_run(run)

    assert "image_path" not in visible["observation"]
    assert "raw" not in visible["observation"]
    assert "/private/" not in str(visible)
    assert "private-file-payload" not in str(visible)


@pytest.mark.asyncio
async def test_event_stream_publishes_activity_changes_without_a_status_change(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"\xff\xd8fake-jpeg\xff\xd9")
    store = InMemoryRunStore()
    run = RunSnapshot(
        run_id="stream-run",
        task="Keep current work visible over SSE",
        status=RunStatus.RUNNING,
    )
    run.record(
        "action.attempted",
        tool="pikvm_run_burst",
        call_id="call-1",
        arguments={"actions": [{"type": "key", "keys": ["CTRL", "P"]}]},
    )
    await store.save(run)
    app = create_harness_app(
        harness=StubHarness(store, frame),  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    route = next(
        candidate
        for candidate in app.routes
        if getattr(candidate, "path", "") == "/api/runs/{run_id}/stream"
    )

    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    response = await route.endpoint(  # type: ignore[union-attr]
        ConnectedRequest(),
        "stream-run",
        0,
    )
    iterator = response.body_iterator
    try:
        ready = await anext(iterator)
        attempted = await anext(iterator)
        active = await anext(iterator)

        assert "event: stream.ready" in ready
        assert "event: run.event" in attempted
        assert '"kind":"tool"' in active
        assert '"status":"running"' in active

        changed = await store.get("stream-run")
        changed.record("action.completed", call_id="call-1")
        await store.save(changed)

        completed = await anext(iterator)
        inactive = await anext(iterator)

        assert "event: run.event" in completed
        assert '"active_activity":null' in inactive
        assert '"status":"running"' in inactive
    finally:
        await iterator.aclose()


@pytest.mark.asyncio
async def test_sse_response_treats_server_shutdown_cancellation_as_disconnect() -> None:
    response_started = asyncio.Event()
    never_disconnects = asyncio.Event()

    async def content():
        yield b"event: ready\ndata: {}\n\n"
        await asyncio.Event().wait()

    async def receive() -> dict[str, str]:
        await never_disconnects.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response_started.set()

    response = ShutdownSafeStreamingResponse(
        content(), media_type="text/event-stream"
    )
    task = asyncio.create_task(
        response(
            {
                "type": "http",
                "asgi": {"spec_version": "2.3"},
                "method": "GET",
                "path": "/api/runs/run_1/stream",
                "headers": [],
            },
            receive,
            send,
        )
    )
    await asyncio.wait_for(response_started.wait(), timeout=1)
    task.cancel()

    await asyncio.wait_for(task, timeout=1)
    assert task.cancelled() is False


@pytest.mark.asyncio
async def test_harness_api_exposes_runs_events_frame_controls_and_provider_health(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"\xff\xd8fake-jpeg\xff\xd9")
    store = InMemoryRunStore()
    harness = StubHarness(store, frame)
    app = create_harness_app(
        harness=harness,
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        unauthenticated = await httpx.AsyncClient(
            transport=transport, base_url="http://harness"
        ).get("/api/runs")
        unauthenticated_catalog = await httpx.AsyncClient(
            transport=transport, base_url="http://harness"
        ).get("/api/provider-catalog")
        created = (
            await client.post(
                "/api/runs", json={"task": "Open a file", "auto_start": False}
            )
        ).json()
        listed = (await client.get("/api/runs")).json()
        events = (
            await client.get("/api/runs/run_1/events", params={"after": 0})
        ).json()
        image = await client.get("/api/runs/run_1/frame")
        continued = (await client.post("/api/runs/run_1/continue")).json()
        paused = (
            await client.post(
                "/api/runs/run_1/pause", json={"reason": "inspect state"}
            )
        ).json()
        providers = (await client.get("/api/providers")).json()
        provider_catalog = (await client.get("/api/provider-catalog")).json()
        approval_without_intent = await client.post(
            "/api/runs/run_1/approvals/a_1",
            json={"type": "approve"},
            headers={"origin": "http://harness"},
        )
        approval_without_origin = await client.post(
            "/api/runs/run_1/approvals/a_1",
            json={"type": "approve"},
            headers={"x-pikvm-approval-intent": "a_1"},
        )
        approval_with_intent = await client.post(
            "/api/runs/run_1/approvals/a_1",
            json={"type": "approve"},
            headers={
                "x-pikvm-approval-intent": "a_1",
                "origin": "http://harness",
            },
        )
        aborted = (
            await client.post(
                "/api/runs/run_1/abort", json={"reason": "operator stop"}
            )
        ).json()

    assert created["run_id"] == "run_1"
    assert unauthenticated.status_code == 401
    assert unauthenticated_catalog.status_code == 401
    assert len(provider_catalog) == 10
    assert {
        entry["kind"] for entry in provider_catalog
    } == {
        "subprocess_json",
        "codex_cli",
        "claude_cli",
        "gemini_cli",
        "openai_compatible",
        "openai_responses",
        "azure_openai_responses",
        "anthropic_api",
        "gemini_api",
        "vertex_gemini",
    }
    assert "credential_source" not in repr(provider_catalog)
    assert listed[0]["task"] == "Open a file"
    assert events["events"][0]["kind"] == "computer.opened"
    assert events["cursor"] == 1
    assert image.content == frame.read_bytes()
    assert continued["status"] == "paused"
    assert paused["status"] == "paused"
    assert providers["fast-oauth"]["last_latency_ms"] == 81
    assert approval_without_intent.status_code == 409
    assert approval_without_origin.status_code == 409
    assert "operator UI origin" in approval_without_origin.json()["detail"]
    assert approval_with_intent.status_code == 200
    assert aborted["status"] == "aborted"
    assert ("continue", "run_1") in harness.calls
    assert ("pause", "run_1", "inspect state") in harness.calls
    assert ("abort", "run_1", "operator stop") in harness.calls


@pytest.mark.asyncio
async def test_run_api_bounds_live_payloads_and_paginates_durable_history(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"\xff\xd8fake-jpeg\xff\xd9")
    store = InMemoryRunStore()
    run = RunSnapshot(
        run_id="long_run",
        task="Long-running visibility soak",
        status=RunStatus.RUNNING,
    )
    for index in range(1_200):
        run.record("run.tick", number=index)
    await store.save(run)
    original_get = store.get
    full_snapshot_reads = 0

    async def counted_get(run_id: str) -> RunSnapshot:
        nonlocal full_snapshot_reads
        full_snapshot_reads += 1
        return await original_get(run_id)

    store.get = counted_get  # type: ignore[method-assign]
    app = create_harness_app(
        harness=StubHarness(store, frame),  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        listed_response = await client.get("/api/runs")
        detail = (await client.get("/api/runs/long_run")).json()
        first_page = (
            await client.get(
                "/api/runs/long_run/events",
                params={"after": 0, "limit": 200},
            )
        ).json()
        final_page = (
            await client.get(
                "/api/runs/long_run/events",
                params={"after": 200, "limit": 1_000},
            )
        ).json()
        performance = (
            await client.get("/api/runs/long_run/performance")
        ).json()

    listed = listed_response.json()
    assert listed[0]["event_count"] == 1_200
    assert listed[0]["event_cursor"] == 1_200
    assert "events" not in listed[0]
    assert len(listed_response.content) < 1_024
    assert detail["event_count"] == 1_200
    assert detail["events_truncated"] is True
    assert len(detail["events"]) == 500
    assert detail["events"][0]["sequence"] == 701
    assert detail["events"][-1]["sequence"] == 1_200
    assert first_page["cursor"] == 200
    assert first_page["latest_cursor"] == 1_200
    assert first_page["has_more"] is True
    assert [event["sequence"] for event in first_page["events"]] == list(
        range(1, 201)
    )
    assert final_page["cursor"] == 1_200
    assert final_page["latest_cursor"] == 1_200
    assert final_page["has_more"] is False
    assert len(final_page["events"]) == 1_000
    assert performance["run_id"] == "long_run"
    assert performance["wall_clock_ms"] >= 0
    assert full_snapshot_reads == 1


@pytest.mark.asyncio
async def test_external_benchmark_console_is_observe_approve_stop_only(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = StubHarness(store, frame)
    await harness.create("Externally driven task")
    app = create_harness_app(
        harness=harness,
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
        external_driver=True,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        health = await client.get("/api/health")
        created = await client.post(
            "/api/runs",
            json={"task": "Second task", "auto_start": False},
        )
        continued = await client.post("/api/runs/run_1/continue")
        paused = await client.post(
            "/api/runs/run_1/pause",
            json={"reason": "pause"},
        )
        steered = await client.post(
            "/api/runs/run_1/steer",
            json={"instruction": "change course", "auto_resume": True},
        )
        aborted = await client.post(
            "/api/runs/run_1/abort",
            json={"reason": "operator stop"},
        )

    assert health.json()["control_mode"] == "external_benchmark"
    assert created.status_code == 409
    assert continued.status_code == 409
    assert paused.status_code == 409
    assert steered.status_code == 409
    assert aborted.status_code == 200
    assert ("continue", "run_1") not in harness.calls
    assert ("pause", "run_1", "pause") not in harness.calls
    assert ("steer", "run_1", "change course") not in harness.calls
    assert ("abort", "run_1", "operator stop") in harness.calls


class BlockingHarness(StubHarness):
    def __init__(self, store: InMemoryRunStore, frame: Path) -> None:
        super().__init__(store, frame)
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def continue_run(self, run_id: str) -> RunSnapshot:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_start_schedules_visible_run_without_waiting_for_model(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = BlockingHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
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
            "/api/runs",
            json={"task": "Create the document", "auto_start": False},
        )
        started = await client.post("/api/runs/run_1/start")
        await asyncio.wait_for(harness.started.wait(), timeout=1)
        stopped = await client.post(
            "/api/runs/run_1/abort",
            json={"reason": "test cleanup"},
        )

    assert created.status_code == 200
    assert started.status_code == 200
    assert started.json()["status"] == "running"
    assert stopped.status_code == 200
    assert harness.cancelled.is_set()


class MultiSliceHarness(StubHarness):
    """A task that needs three internal action slices but no human input."""

    def __init__(self, store: InMemoryRunStore, frame: Path) -> None:
        super().__init__(store, frame)
        self.completed = asyncio.Event()

    async def continue_run(self, run_id: str) -> RunSnapshot:
        self.calls.append(("continue", run_id))
        run = await self.store.get(run_id)
        if len(self.calls) < 3:
            run.status = RunStatus.PAUSED
            run.next_action_index += 4
            run.record(
                "run.paused",
                reason="per-call action budget reached",
            )
        else:
            run.status = RunStatus.COMPLETED
            run.record("run.completed", summary="all slices finished")
            self.completed.set()
        await self.store.save(run)
        return run


class SteerResumeHarness(StubHarness):
    def __init__(self, store: InMemoryRunStore, frame: Path) -> None:
        super().__init__(store, frame)
        self.completed = asyncio.Event()

    async def continue_run(self, run_id: str) -> RunSnapshot:
        self.calls.append(("continue", run_id))
        run = await self.store.get(run_id)
        run.status = RunStatus.COMPLETED
        run.record("run.completed", summary="guided plan completed")
        await self.store.save(run)
        self.completed.set()
        return run


@pytest.mark.asyncio
async def test_operator_steering_can_resume_under_harness_ownership(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = SteerResumeHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        await client.post(
            "/api/runs",
            json={"task": "Draft the report", "auto_start": False},
        )
        steered = await client.post(
            "/api/runs/run_1/steer",
            json={
                "instruction": "Keep the existing heading",
                "auto_resume": True,
            },
        )
        await asyncio.wait_for(harness.completed.wait(), timeout=1)
        completed = await client.get("/api/runs/run_1")

    assert steered.status_code == 200
    assert steered.json()["status"] == "paused"
    assert completed.json()["status"] == "completed"
    assert harness.calls == [
        ("steer", "run_1", "Keep the existing heading"),
        ("continue", "run_1"),
    ]


@pytest.mark.asyncio
async def test_auto_started_task_crosses_internal_action_slices_without_client_continue(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = MultiSliceHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        created = await client.post(
            "/api/runs",
            json={"task": "Complete a multi-step Office task", "auto_start": True},
        )
        await asyncio.wait_for(harness.completed.wait(), timeout=1)
        completed = await client.get("/api/runs/run_1")

    assert created.status_code == 200
    assert completed.json()["status"] == "completed"
    assert harness.calls == [
        ("continue", "run_1"),
        ("continue", "run_1"),
        ("continue", "run_1"),
    ]


class IncompleteVerificationHarness(StubHarness):
    def __init__(self, store: InMemoryRunStore, frame: Path) -> None:
        super().__init__(store, frame)
        self.completed = asyncio.Event()

    async def continue_run(self, run_id: str) -> RunSnapshot:
        self.calls.append(("continue", run_id))
        run = await self.store.get(run_id)
        if len(self.calls) == 1:
            run.status = RunStatus.PAUSED
            run.record("run.paused", reason="verifier requires more work")
        else:
            run.status = RunStatus.COMPLETED
            run.record("run.completed", summary="remaining work verified")
            self.completed.set()
        await self.store.save(run)
        return run


@pytest.mark.asyncio
async def test_auto_started_task_retries_when_verifier_says_more_work_remains(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = IncompleteVerificationHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        await client.post(
            "/api/runs",
            json={"task": "Finish every acceptance criterion", "auto_start": True},
        )
        await asyncio.wait_for(harness.completed.wait(), timeout=1)

    assert harness.calls == [
        ("continue", "run_1"),
        ("continue", "run_1"),
    ]


class ReplanningHarness(StubHarness):
    def __init__(self, store: InMemoryRunStore, frame: Path) -> None:
        super().__init__(store, frame)
        self.completed = asyncio.Event()

    async def continue_run(self, run_id: str) -> RunSnapshot:
        self.calls.append(("continue", run_id))
        run = await self.store.get(run_id)
        if len(self.calls) == 1:
            run.status = RunStatus.PAUSED
            run.record(
                "controller.requested_replan",
                reason="the visible application changed",
            )
        else:
            run.status = RunStatus.COMPLETED
            run.record("run.completed", summary="replanned task completed")
            self.completed.set()
        await self.store.save(run)
        return run


@pytest.mark.asyncio
async def test_auto_started_task_owns_safe_replanning_without_client_continue(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = ReplanningHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        await client.post(
            "/api/runs",
            json={"task": "Adapt to the visible application", "auto_start": True},
        )
        await asyncio.wait_for(harness.completed.wait(), timeout=1)

    assert harness.calls == [
        ("continue", "run_1"),
        ("continue", "run_1"),
    ]


class FailedVerificationHarness(StubHarness):
    def __init__(self, store: InMemoryRunStore, frame: Path) -> None:
        super().__init__(store, frame)
        self.completed = asyncio.Event()

    async def continue_run(self, run_id: str) -> RunSnapshot:
        self.calls.append(("continue", run_id))
        run = await self.store.get(run_id)
        if len(self.calls) == 1:
            run.status = RunStatus.PAUSED
            run.error = "the harmless click produced no visible effect"
            run.record(
                "verification.failed",
                summary=run.error,
            )
        else:
            run.status = RunStatus.COMPLETED
            run.error = None
            run.record(
                "run.completed",
                summary="replanned after the harmless visual miss",
            )
            self.completed.set()
        await self.store.save(run)
        return run


@pytest.mark.asyncio
async def test_auto_started_task_replans_after_failed_visual_verification(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = FailedVerificationHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        await client.post(
            "/api/runs",
            json={"task": "Recover from a missed click", "auto_start": True},
        )
        await asyncio.wait_for(harness.completed.wait(), timeout=1)

    assert harness.calls == [
        ("continue", "run_1"),
        ("continue", "run_1"),
    ]


class StaleWorldHarness(StubHarness):
    def __init__(self, store: InMemoryRunStore, frame: Path) -> None:
        super().__init__(store, frame)
        self.completed = asyncio.Event()

    async def continue_run(self, run_id: str) -> RunSnapshot:
        self.calls.append(("continue", run_id))
        run = await self.store.get(run_id)
        if len(self.calls) == 1:
            run.status = RunStatus.PAUSED
            run.record(
                "action.stale_world_refreshed",
                status="stale_world",
                fresh_controller_decision_required=True,
            )
        else:
            run.status = RunStatus.COMPLETED
            run.record("run.completed", summary="fresh plan completed")
            self.completed.set()
        await self.store.save(run)
        return run


@pytest.mark.asyncio
async def test_auto_started_task_replans_after_stale_world_without_human_continue(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = StaleWorldHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        await client.post(
            "/api/runs",
            json={"task": "Act only on a fresh screen", "auto_start": True},
        )
        await asyncio.wait_for(harness.completed.wait(), timeout=1)

    assert harness.calls == [
        ("continue", "run_1"),
        ("continue", "run_1"),
    ]


class LoopingReplanHarness(StubHarness):
    def __init__(self, store: InMemoryRunStore, frame: Path) -> None:
        super().__init__(store, frame)
        self.third_attempt = asyncio.Event()

    async def continue_run(self, run_id: str) -> RunSnapshot:
        self.calls.append(("continue", run_id))
        run = await self.store.get(run_id)
        run.status = RunStatus.PAUSED
        run.record(
            "controller.requested_replan",
            reason="no visible progress",
        )
        await self.store.save(run)
        if len(self.calls) == 3:
            self.third_attempt.set()
        return run


@pytest.mark.asyncio
async def test_autonomous_replanning_stops_at_a_harness_owned_slice_limit(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = LoopingReplanHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
        max_autonomous_resumes=2,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        await client.post(
            "/api/runs",
            json={"task": "Do not spin forever", "auto_start": True},
        )
        await asyncio.wait_for(harness.third_attempt.wait(), timeout=1)
        for _ in range(10):
            latest = await store.get("run_1")
            if latest.events[-1].kind == "run.autonomy_stopped":
                break
            await asyncio.sleep(0)
        stopped = await client.get("/api/runs/run_1")

    assert harness.calls == [
        ("continue", "run_1"),
        ("continue", "run_1"),
        ("continue", "run_1"),
    ]
    assert stopped.json()["status"] == "paused"
    assert stopped.json()["events"][-1]["kind"] == "run.autonomy_stopped"
    assert stopped.json()["events"][-1]["data"]["limit"] == 2


class ApprovedMultiSliceHarness(StubHarness):
    def __init__(self, store: InMemoryRunStore, frame: Path) -> None:
        super().__init__(store, frame)
        self.completed = asyncio.Event()

    async def create(self, task: str) -> RunSnapshot:
        run = await super().create(task)
        run.status = RunStatus.NEEDS_APPROVAL
        run.pending_approval = {
            "approval_id": "approval-exact-1",
            "risk": "communication_send",
        }
        run.record(
            "approval.required",
            approval_id="approval-exact-1",
            risk="communication_send",
        )
        await self.store.save(run)
        return run

    async def resolve_approval(
        self, run_id: str, approval_id: str, decision: dict[str, Any]
    ) -> RunSnapshot:
        self.calls.append(("approval", run_id, approval_id, decision))
        run = await self.store.get(run_id)
        run.pending_approval = None
        run.status = RunStatus.PAUSED
        run.record("run.paused", reason="per-call action budget reached")
        await self.store.save(run)
        return run

    async def continue_run(self, run_id: str) -> RunSnapshot:
        self.calls.append(("continue", run_id))
        run = await self.store.get(run_id)
        run.status = RunStatus.COMPLETED
        run.record("run.completed", summary="approved task completed")
        await self.store.save(run)
        self.completed.set()
        return run


@pytest.mark.asyncio
async def test_exact_human_approval_releases_the_remaining_autonomous_task(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = ApprovedMultiSliceHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        await client.post(
            "/api/runs",
            json={"task": "Complete the approved task", "auto_start": False},
        )
        approved = await client.post(
            "/api/runs/run_1/approvals/approval-exact-1",
            json={"type": "approve", "reason": "reviewed"},
            headers={
                "origin": "http://harness",
                "x-pikvm-approval-intent": "approval-exact-1",
            },
        )
        await asyncio.wait_for(harness.completed.wait(), timeout=1)

    assert approved.status_code == 200
    assert harness.calls == [
        (
            "approval",
            "run_1",
            "approval-exact-1",
            {"type": "approve", "reason": "reviewed"},
        ),
        ("continue", "run_1"),
    ]


class BlockingApprovalHarness(StubHarness):
    def __init__(self, store: InMemoryRunStore, frame: Path) -> None:
        super().__init__(store, frame)
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def create(
        self,
        task: str,
        *,
        caller: dict[str, Any] | None = None,
        model_provider: str | None = None,
    ) -> RunSnapshot:
        run = await super().create(
            task,
            caller=caller,
            model_provider=model_provider,
        )
        run.status = RunStatus.NEEDS_APPROVAL
        run.pending_approval = {
            "kind": "direct_burst",
            "approval_id": "approval-blocking-1",
            "risk": "unknown",
        }
        await self.store.save(run)
        return run

    async def resolve_approval(
        self,
        run_id: str,
        approval_id: str,
        decision: dict[str, Any],
    ) -> RunSnapshot:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_operator_pause_cancels_slow_post_approval_verification(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = BlockingApprovalHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        await client.post(
            "/api/runs",
            json={"task": "Launch the selected app", "auto_start": False},
        )
        approval = asyncio.create_task(
            client.post(
                "/api/runs/run_1/approvals/approval-blocking-1",
                json={"type": "approve", "reason": "reviewed"},
                headers={
                    "origin": "http://harness",
                    "x-pikvm-approval-intent": "approval-blocking-1",
                },
            )
        )
        await asyncio.wait_for(harness.started.wait(), timeout=1)
        paused = await asyncio.wait_for(
            client.post(
                "/api/runs/run_1/pause",
                json={"reason": "operator stop"},
            ),
            timeout=1,
        )
        approval_response = await asyncio.wait_for(approval, timeout=1)

    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    assert approval_response.status_code == 200
    assert harness.cancelled.is_set()


class ConcurrentContinueHarness(StubHarness):
    def __init__(self, store: InMemoryRunStore, frame: Path) -> None:
        super().__init__(store, frame)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def continue_run(self, run_id: str) -> RunSnapshot:
        self.calls.append(("continue", run_id))
        run = await self.store.get(run_id)
        if len(self.calls) == 1:
            self.started.set()
            await self.release.wait()
            run.status = RunStatus.PAUSED
            run.error = "the screen result is ambiguous"
            run.record(
                "verification.uncertain",
                summary="the screen result is ambiguous",
            )
        else:
            run.status = RunStatus.COMPLETED
            run.record("run.completed", summary="pause was bypassed")
        await self.store.save(run)
        return run


@pytest.mark.asyncio
async def test_overlapping_continue_cannot_bypass_a_meaningful_pause(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = ConcurrentContinueHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        await client.post(
            "/api/runs",
            json={"task": "Respect uncertainty", "auto_start": False},
        )
        first = asyncio.create_task(client.post("/api/runs/run_1/continue"))
        await asyncio.wait_for(harness.started.wait(), timeout=1)
        overlapping = asyncio.create_task(
            client.post("/api/runs/run_1/continue")
        )
        await asyncio.sleep(0)
        harness.release.set()
        await asyncio.gather(first, overlapping)
        final = await client.get("/api/runs/run_1")

    assert harness.calls == [("continue", "run_1")]
    assert final.json()["status"] == "paused"
    assert final.json()["events"][-1]["kind"] == "verification.uncertain"


class RestartRecoveryHarness(StubHarness):
    def __init__(self, store: InMemoryRunStore, frame: Path) -> None:
        super().__init__(store, frame)
        self.completed = asyncio.Event()

    async def continue_run(self, run_id: str) -> RunSnapshot:
        self.calls.append(("continue", run_id))
        run = await self.store.get(run_id)
        run.status = RunStatus.COMPLETED
        run.record("run.completed", summary="recovered after restart")
        await self.store.save(run)
        self.completed.set()
        return run


@pytest.mark.asyncio
async def test_process_start_recovers_only_internal_autonomous_yields(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    resumable = RunSnapshot(
        run_id="resume-after-restart",
        task="Keep going",
        status=RunStatus.PAUSED,
    )
    resumable.record("run.paused", reason="per-call action budget reached")
    uncertain = RunSnapshot(
        run_id="wait-for-operator",
        task="Do not guess",
        status=RunStatus.PAUSED,
        error="screen result uncertain",
    )
    uncertain.record(
        "verification.uncertain",
        summary="screen result uncertain",
    )
    await store.save(resumable)
    await store.save(uncertain)
    harness = RestartRecoveryHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(harness.completed.wait(), timeout=1)

    assert harness.calls == [("continue", "resume-after-restart")]
    assert (await store.get("wait-for-operator")).status is RunStatus.PAUSED


@pytest.mark.asyncio
async def test_process_start_recovers_a_slice_scheduled_just_before_crash(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    scheduled = RunSnapshot(
        run_id="scheduled-before-crash",
        task="Keep going after restart",
        status=RunStatus.PAUSED,
    )
    scheduled.record("run.paused", reason="per-call action budget reached")
    scheduled.record(
        "run.autonomous_resume",
        reason="per-call action budget reached",
        source="harness_supervisor",
    )
    await store.save(scheduled)
    harness = RestartRecoveryHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(harness.completed.wait(), timeout=1)

    assert harness.calls == [("continue", "scheduled-before-crash")]


class RestartLoopHarness(StubHarness):
    def __init__(self, store: InMemoryRunStore, frame: Path) -> None:
        super().__init__(store, frame)
        self.attempted = asyncio.Event()

    async def continue_run(self, run_id: str) -> RunSnapshot:
        self.calls.append(("continue", run_id))
        run = await self.store.get(run_id)
        run.status = RunStatus.PAUSED
        run.record("controller.requested_replan", reason="no progress")
        await self.store.save(run)
        self.attempted.set()
        return run


@pytest.mark.asyncio
async def test_restart_does_not_reset_the_durable_autonomous_resume_limit(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    scheduled = RunSnapshot(
        run_id="resume-limit-before-crash",
        task="Keep the safety ceiling",
        status=RunStatus.PAUSED,
    )
    for _ in range(2):
        scheduled.record("run.paused", reason="verifier requires more work")
        scheduled.record(
            "run.autonomous_resume",
            reason="verifier requires more work",
            source="harness_supervisor",
        )
    await store.save(scheduled)
    harness = RestartLoopHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
        max_autonomous_resumes=2,
    )

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(harness.attempted.wait(), timeout=1)
        for _ in range(10):
            latest = await store.get("resume-limit-before-crash")
            if latest.events[-1].kind == "run.autonomy_stopped":
                break
            await asyncio.sleep(0)

    latest = await store.get("resume-limit-before-crash")
    assert harness.calls == [("continue", "resume-limit-before-crash")]
    assert latest.events[-1].kind == "run.autonomy_stopped"
    assert latest.events[-1].data["limit"] == 2


def test_harness_api_uses_the_same_32_character_token_floor_as_launcher(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()

    with pytest.raises(ValueError, match="at least 32 characters"):
        create_harness_app(
            harness=StubHarness(store, frame),  # type: ignore[arg-type]
            store=store,
            models=StubModels(),
            access_token="x" * 31,
            allowed_origins={"http://harness"},
        )


@pytest.mark.asyncio
async def test_operator_pause_cancels_an_inflight_loop_before_marking_paused(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = BlockingHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        await client.post(
            "/api/runs", json={"task": "Wait", "auto_start": True}
        )
        await asyncio.wait_for(harness.started.wait(), timeout=1)
        response = await client.post(
            "/api/runs/run_1/pause", json={"reason": "operator intervention"}
        )

    assert response.status_code == 200
    assert response.json()["status"] == "paused"
    assert harness.cancelled.is_set()


@pytest.mark.asyncio
async def test_operator_steer_cancels_inflight_model_work_before_replanning(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = BlockingHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        await client.post(
            "/api/runs",
            json={"task": "Draft the report", "auto_start": True},
        )
        await asyncio.wait_for(harness.started.wait(), timeout=1)
        response = await client.post(
            "/api/runs/run_1/steer",
            json={
                "instruction": "Use a table instead of prose",
                "auto_resume": False,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "paused"
    assert response.json()["operator_guidance"] == [
        "Use a table instead of prose"
    ]
    assert harness.cancelled.is_set()


@pytest.mark.asyncio
async def test_operator_abort_does_not_turn_cancelled_continue_into_http_500(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    store = InMemoryRunStore()
    harness = BlockingHarness(store, frame)
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        await client.post(
            "/api/runs", json={"task": "Wait", "auto_start": False}
        )
        continuing = asyncio.create_task(
            client.post("/api/runs/run_1/continue")
        )
        await asyncio.wait_for(harness.started.wait(), timeout=1)
        aborted = await client.post(
            "/api/runs/run_1/abort",
            json={"reason": "emergency stop"},
        )
        continued = await asyncio.wait_for(continuing, timeout=1)

    assert aborted.status_code == 200
    assert aborted.json()["status"] == "aborted"
    assert continued.status_code == 200
    assert continued.json()["status"] in {"running", "aborted"}
    assert harness.cancelled.is_set()


@pytest.mark.asyncio
async def test_harness_frame_endpoint_prefers_non_mutating_live_preview(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"checkpoint-frame")
    store = InMemoryRunStore()
    harness = StubHarness(store, frame)
    await harness.create("Observe a machine")
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
        live_frames=StubLiveFrames(),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        response = await client.get("/api/runs/run_1/frame")

    assert response.status_code == 200
    assert response.content == b"live-frame"
    assert response.headers["x-pikvm-frame-mode"] == "live"
    assert response.headers["x-pikvm-captured-at"] == "2026-07-24T18:00:00Z"
    assert response.headers["x-pikvm-width"] == "1280"
    assert response.headers["x-pikvm-height"] == "800"


@pytest.mark.asyncio
async def test_harness_frame_endpoint_falls_back_when_live_preview_is_rejected(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"checkpoint-frame")
    store = InMemoryRunStore()
    harness = StubHarness(store, frame)
    await harness.create("Observe a machine")
    app = create_harness_app(
        harness=harness,  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
        live_frames=RejectedLiveFrames(),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        response = await client.get("/api/runs/run_1/frame")

    assert response.status_code == 200
    assert response.content == b"checkpoint-frame"
    assert response.headers["x-pikvm-frame-mode"] == "checkpoint"
    assert response.headers["x-pikvm-live-capable"] == "true"


@pytest.mark.asyncio
async def test_harness_api_recursively_redacts_secret_action_text(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"\xff\xd8fake-jpeg\xff\xd9")
    store = InMemoryRunStore()
    run = RunSnapshot(
        run_id="secret_run",
        task="Enter a credential",
        status=RunStatus.NEEDS_APPROVAL,
        session_id="secret_session",
        observation=ComputerObservation(
            session_id="secret_session",
            status="needs_approval",
            frame_id=4,
            world_version=7,
            control_epoch=2,
            image_path=str(frame),
            raw={
                "approval_request": {
                    "proposed_action": {
                        "actions": [
                            {
                                "type": "type_text",
                                "text": "raw-secret-value",
                                "secret": True,
                            }
                        ]
                    }
                }
            },
        ),
        pending_action=PendingAction(
            index=0,
            intent="Enter the credential",
            actions=[
                {
                    "type": "type_text",
                    "text": "raw-secret-value",
                    "secret": True,
                }
            ],
            based_on_world_version=7,
            based_on_control_epoch=2,
            idempotency_key="secret_run:action:0:digest",
        ),
        pending_approval={
            "approval_id": "secret_approval",
            "proposed_action": {
                "actions": [
                    {
                        "type": "type_text",
                        "text": "raw-secret-value",
                        "secret": True,
                    }
                ]
            },
        },
    )
    run.record(
        "approval.required",
        request={
            "actions": [
                {
                    "type": "type_text",
                    "text": "raw-secret-value",
                    "secret": True,
                }
            ]
        },
    )
    await store.save(run)
    app = create_harness_app(
        harness=StubHarness(store, frame),  # type: ignore[arg-type]
        store=store,
        models=StubModels(),
        access_token=TEST_ACCESS_TOKEN,
        allowed_origins={"http://harness"},
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://harness",
        headers={"authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
    ) as client:
        detail = await client.get("/api/runs/secret_run")
        listed = await client.get("/api/runs")
        events = await client.get("/api/runs/secret_run/events")

    for response in (detail, events):
        assert response.status_code == 200
        assert "raw-secret-value" not in response.text
        assert "••••••••" in response.text
    assert listed.status_code == 200
    assert "raw-secret-value" not in listed.text
