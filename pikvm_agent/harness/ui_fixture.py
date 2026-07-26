"""Deterministic no-machine fixture for operator-console browser audits."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI

from pikvm_agent.harness.agent_models import (
    ComputerObservation,
    ControllerDecision,
    PendingAction,
    PlanDecision,
    RunSnapshot,
    RunStatus,
)
from pikvm_agent.harness.agent_store import InMemoryRunStore
from pikvm_agent.harness.api import create_harness_app
from pikvm_agent.harness.provider_support import provider_support

FIXTURE_RUN_ID = "operator-ui-audit"


@dataclass(frozen=True)
class FixtureFrame:
    data: bytes
    media_type: str
    captured_at: str
    width: int
    height: int


class FixtureLiveFrames:
    """Generate a changing SVG preview without opening a computer target."""

    def __init__(self) -> None:
        self.sequence = 0

    async def get(self, session_id: str) -> FixtureFrame:
        if session_id != "synthetic-session":
            raise KeyError(session_id)
        self.sequence += 1
        now = datetime.now(UTC).isoformat()
        svg = f"""\
<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">
  <rect width="1280" height="720" fill="#0b0d12"/>
  <rect x="72" y="64" width="1136" height="592" rx="12" fill="#161a23"
        stroke="#2b3447"/>
  <text x="120" y="145" fill="#e8edf5" font-family="system-ui"
        font-size="34">Synthetic operator-console audit</text>
  <text x="120" y="205" fill="#9aa6bd" font-family="monospace"
        font-size="22">No VNC or PiKVM target is connected</text>
  <rect x="120" y="270" width="720" height="58" rx="6" fill="#1c2230"/>
  <text x="148" y="307" fill="#7ee29a" font-family="monospace"
        font-size="20">frame {self.sequence:06d} · {now}</text>
  <rect x="120" y="374" width="910" height="24" rx="4" fill="#242c3d"/>
  <rect x="120" y="374" width="{220 + self.sequence % 700}" height="24"
        rx="4" fill="#3b82f6"/>
  <text x="120" y="470" fill="#f0c869" font-family="system-ui"
        font-size="24">Exact actions, provider state, and approvals remain visible.</text>
</svg>"""
        return FixtureFrame(
            data=svg.encode(),
            media_type="image/svg+xml",
            captured_at=now,
            width=1280,
            height=720,
        )


class FixtureModels:
    def health(self) -> dict[str, dict[str, object]]:
        return {
            "claude-account": {
                "kind": "claude_cli",
                "credential": "CLI-owned OAuth",
                "auth_mode": "saved_cli_login",
                **provider_support("claude_cli").readiness_metadata(
                    "saved_cli_login"
                ),
                "credential_source": "claude",
                "configured_model": "opus",
                "routes": [
                    {"role": "reasoner", "position": 1},
                    {"role": "verifier", "position": 1},
                ],
                "ready": True,
                "calls": 83,
                "successes": 82,
                "failures": 1,
                "consecutive_failures": 0,
                "last_latency_ms": 12_840,
                "last_model": "opus",
                "conformance_status": "passed",
                "conformance_created_at": "2026-07-26T12:00:00+00:00",
                "conformance_calls_attempted": 5,
                "conformance_schema_valid": 5,
                "conformance_exact": 5,
                "conformance_median_latency_ms": 12_120,
            },
            "fast-controller": {
                "kind": "openai_responses",
                "credential": "server environment",
                "auth_mode": "api_key_env",
                **provider_support("openai_responses").readiness_metadata(
                    "api_key_env"
                ),
                "configured_model": "fast-controller-fixture",
                "routes": [{"role": "controller", "position": 1}],
                "ready": True,
                "calls": 127,
                "successes": 127,
                "failures": 0,
                "consecutive_failures": 0,
                "last_latency_ms": 740,
                "last_model": "fast-controller-fixture",
                "conformance_status": "degraded",
                "conformance_created_at": "2026-07-26T12:00:00+00:00",
                "conformance_calls_attempted": 5,
                "conformance_schema_valid": 5,
                "conformance_exact": 4,
                "conformance_median_latency_ms": 690,
            },
        }


class FixtureHarness:
    """External-driver placeholder; the synthetic producer owns progression."""

    async def create(self, task: str) -> RunSnapshot:
        raise RuntimeError("the UI audit fixture is externally driven")

    async def continue_run(self, run_id: str) -> RunSnapshot:
        raise RuntimeError("the UI audit fixture is externally driven")

    async def pause(self, run_id: str, reason: str) -> RunSnapshot:
        raise RuntimeError("the UI audit fixture is externally driven")

    async def resolve_approval(
        self,
        run_id: str,
        approval_id: str,
        decision: dict[str, Any],
    ) -> RunSnapshot:
        raise RuntimeError("the UI audit fixture has no real approval target")

    async def abort(self, run_id: str, reason: str) -> RunSnapshot:
        raise RuntimeError("the UI audit fixture has no computer session")


def build_fixture_run(prefill_events: int = 1_200) -> RunSnapshot:
    if prefill_events < 32:
        raise ValueError("prefill_events must be at least 32")
    run = RunSnapshot(
        run_id=FIXTURE_RUN_ID,
        task=(
            "Audit a long provider name, exact MCP arguments, sustained frame "
            "updates, a 1,200-event timeline, and 200% browser reflow"
        ),
        status=RunStatus.RUNNING,
        session_id="synthetic-session",
        plan=PlanDecision(
            summary="Prove that live oversight remains legible under load.",
            steps=[
                "Observe the synthetic machine frame.",
                "Keep the current model and exact action visible.",
                "Verify bounded history, controls, and reconnect state.",
            ],
            success_criteria=[
                "No horizontal page overflow at the target reflow width.",
                "The timeline DOM remains bounded to 500 events.",
                "Frame blobs are replaced without unbounded retention.",
            ],
            constraints=["No VNC, PiKVM, email, chat, or external API access."],
        ),
        observation=ComputerObservation(
            session_id="synthetic-session",
            status="running",
            machine={
                "alias": "Synthetic audit target",
                "fingerprint": "fixture-7d29a4",
                "desktop_layer": "No-machine browser fixture",
            },
            frame_id=1,
            world_version=1,
            control_epoch=1,
            width=1280,
            height=720,
        ),
        pending_action=PendingAction(
            index=127,
            intent="Open the selected settings row without committing a change",
            actions=[{"type": "click", "x": 1012, "y": 642, "button": "left"}],
            expected_evidence=["The row becomes selected; no dialog is submitted."],
            based_on_world_version=1,
            based_on_control_epoch=1,
            idempotency_key="fixture:action:127:4e1f96f3",
            attempts=1,
        ),
        last_controller=ControllerDecision(
            outcome="act",
            intent="Open the selected settings row without committing a change",
            actions=[{"type": "click", "x": 1012, "y": 642, "button": "left"}],
            expected_evidence=["The row becomes selected."],
        ),
        next_action_index=127,
    )
    run.model_budget.provider_attempts = 37
    run.model_budget.provider_attempt_limit = 500
    run.model_budget.committed_cost_microusd = 184_250
    run.model_budget.max_cost_microusd = 2_000_000
    run.model_budget.pricing_version = "synthetic-fixture-v1"
    cycle = 0
    while run.event_cursor < prefill_events - 1:
        index = cycle % 127
        for kind, data in (
            (
                "model.provider_started",
                {
                    "role": "controller",
                    "provider": "fast-controller",
                    "attempt": 1,
                    "route_index": 0,
                },
            ),
            (
                "model.provider_completed",
                {
                    "role": "controller",
                    "provider": "fast-controller",
                    "model": "fast-controller-fixture",
                    "attempt": 1,
                    "latency_ms": 740,
                },
            ),
            (
                "model.completed",
                {
                    "role": "controller",
                    "provider": "fast-controller",
                    "model": "fast-controller-fixture",
                    "latency_ms": 740,
                    "intent": "Inspect the next bounded target",
                },
            ),
            (
                "action.checkpointed",
                {
                    "index": index,
                    "intent": "Inspect the next bounded target",
                    "actions": [{"type": "click", "x": 840, "y": 420}],
                },
            ),
            (
                "action.attempted",
                {
                    "index": index,
                    "attempt": 1,
                    "tool": "pikvm_run_burst",
                    "arguments": {
                        "actions": [{"type": "click", "x": 840, "y": 420}],
                        "based_on_world_version": cycle + 1,
                        "based_on_control_epoch": 1,
                    },
                },
            ),
            (
                "action.completed",
                {
                    "index": index,
                    "frame_id": cycle + 2,
                    "world_version": cycle + 2,
                },
            ),
            (
                "verification.completed",
                {
                    "verdict": "verified",
                    "summary": "The synthetic target changed as expected.",
                },
            ),
        ):
            if run.event_cursor >= prefill_events - 1:
                break
            run.record(kind, **data)
        cycle += 1
    run.record(
        "model.provider_started",
        role="controller",
        provider="fast-controller",
        attempt=1,
        route_index=0,
    )
    return run


def advance_fixture_run(run: RunSnapshot, tick: int) -> None:
    activity = run.active_activity
    if activity is not None and activity.kind == "model":
        run.record(
            "model.provider_completed",
            role=activity.role,
            provider=activity.provider,
            model="fast-controller-fixture",
            attempt=activity.attempt,
            latency_ms=720 + tick % 120,
        )
        run.record(
            "model.completed",
            role="controller",
            provider="fast-controller",
            model="fast-controller-fixture",
            latency_ms=720 + tick % 120,
            intent="Inspect one bounded synthetic target",
        )
        run.record(
            "action.checkpointed",
            index=run.next_action_index,
            intent="Inspect one bounded synthetic target",
            actions=[{"type": "click", "x": 800 + tick % 200, "y": 420}],
        )
        run.record(
            "action.attempted",
            index=run.next_action_index,
            attempt=1,
            tool="pikvm_run_burst",
            arguments={
                "actions": [
                    {"type": "click", "x": 800 + tick % 200, "y": 420}
                ],
                "based_on_world_version": (
                    run.observation.world_version if run.observation else 1
                ),
                "based_on_control_epoch": 1,
                "idempotency_key": (
                    f"fixture:action:{run.next_action_index}:{tick:08x}"
                ),
            },
        )
        return
    run.record(
        "action.completed",
        index=run.next_action_index,
        frame_id=tick + 2,
        world_version=tick + 2,
    )
    run.record(
        "verification.completed",
        verdict="verified",
        summary="The synthetic target changed as expected.",
    )
    run.next_action_index += 1
    if run.observation is not None:
        run.observation.frame_id = tick + 2
        run.observation.world_version = tick + 2
    run.record(
        "model.provider_started",
        role="controller",
        provider="fast-controller",
        attempt=1,
        route_index=0,
    )


def build_fixture_app(
    *,
    access_token: str,
    origin: str,
    prefill_events: int = 1_200,
    event_interval_ms: int = 250,
) -> FastAPI:
    if not 50 <= event_interval_ms <= 60_000:
        raise ValueError("event_interval_ms must be between 50 and 60000")
    store = InMemoryRunStore()
    run = build_fixture_run(prefill_events)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await store.save(run)

        async def produce() -> None:
            tick = 0
            while True:
                await asyncio.sleep(event_interval_ms / 1_000)
                tick += 1
                advance_fixture_run(run, tick)
                await store.save(run)

        producer = asyncio.create_task(produce())
        try:
            yield
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    app = create_harness_app(
        harness=FixtureHarness(),  # type: ignore[arg-type]
        store=store,
        models=FixtureModels(),
        access_token=access_token,
        allowed_origins={origin},
        live_frames=FixtureLiveFrames(),
        external_driver=True,
        lifespan=lifespan,
    )
    app.state.synthetic_fixture = True
    app.state.synthetic_store = store
    app.state.synthetic_run = run
    return app
