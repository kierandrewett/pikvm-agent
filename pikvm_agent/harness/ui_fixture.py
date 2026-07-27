"""Deterministic no-machine fixture for chat-workspace browser audits."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from PIL import Image, ImageDraw

from pikvm_agent.harness.agent_models import (
    ComputerObservation,
    ConversationMessage,
    ControllerDecision,
    PendingAction,
    PlanDecision,
    RunModelRoute,
    RunSnapshot,
    RunStatus,
    VerificationImageArtifact,
)
from pikvm_agent.harness.agent_store import InMemoryRunStore
from pikvm_agent.harness.api import create_harness_app
from pikvm_agent.harness.provider_support import provider_support
from pikvm_agent.harness.provider_connections import (
    ProviderConnectionConflict,
    ProviderConnectionRequest,
    ProviderConnectionResult,
)

FIXTURE_RUN_ID = "chat-ui-audit"
GENERIC_TOOL_TASK = "Audit a concise generic tool receipt"


def _write_fixture_evidence(path: Path) -> None:
    image = Image.new("RGB", (1_280, 392), "#0c0d10")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 639, 391), fill="#171a22")
    draw.rectangle((640, 0, 1_279, 391), fill="#171a22")
    draw.text((20, 16), "BEFORE", fill="#f1f2f5")
    draw.text((660, 16), "AFTER", fill="#f1f2f5")
    draw.text((42, 90), "Synthetic settings", fill="#a8abb5")
    draw.text((682, 90), "Synthetic settings", fill="#a8abb5")
    draw.rounded_rectangle((42, 140, 590, 232), 8, fill="#22232a")
    draw.rounded_rectangle((682, 140, 1_230, 232), 8, fill="#22232a")
    draw.text((70, 176), "Computer-use evidence", fill="#f1f2f5")
    draw.text((710, 176), "Computer-use evidence", fill="#f1f2f5")
    draw.ellipse((520, 167, 556, 203), fill="#6b6e78")
    draw.ellipse((1_160, 167, 1_196, 203), fill="#78d69b")
    draw.text((42, 300), "No machine input", fill="#e7bd67")
    draw.text((682, 300), "Observed visual change", fill="#78d69b")
    image.save(path, format="PNG", optimize=True)


def _write_direct_fixture_evidence(path: Path) -> None:
    image = Image.new("RGB", (1_280, 720), "#10141d")
    draw = ImageDraw.Draw(image)
    draw.text((36, 38), "DIRECT PRE-ACTION SCREEN", fill="#f1f2f5")
    draw.text((36, 72), "Synthetic fixture · no machine input", fill="#e7bd67")
    draw.rounded_rectangle((300, 220, 560, 350), 12, fill="#283246")
    draw.text((350, 275), "Clicked control", fill="#f1f2f5")
    image.save(path, format="PNG", optimize=True)


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
        font-size="34">Synthetic chat-workspace audit</text>
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
    def __init__(self) -> None:
        self._providers: dict[str, dict[str, object]] = {
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
                "conformance_p95_latency_ms": 13_480,
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
                "conformance_p95_latency_ms": 820,
            },
        }

    def health(self) -> dict[str, dict[str, object]]:
        return {
            name: dict(health)
            for name, health in self._providers.items()
        }

    def add(self, name: str, health: dict[str, object]) -> None:
        if name in self._providers:
            raise ProviderConnectionConflict(
                f"provider alias already configured: {name}"
            )
        self._providers[name] = dict(health)


class FixtureProviderConnections:
    """Secret-free connection simulator for target-free browser QA."""

    def __init__(self, models: FixtureModels) -> None:
        self._models = models

    async def connect(
        self,
        request: ProviderConnectionRequest,
    ) -> ProviderConnectionResult:
        cli_owned = request.kind in {
            "codex_cli",
            "claude_cli",
            "gemini_cli",
        }
        auth_mode = "saved_cli_login" if cli_owned else "api_key_env"
        support = provider_support(request.kind)
        ready = cli_owned
        self._models.add(
            request.alias,
            {
                "kind": request.kind,
                "configured_model": request.model,
                "auth_mode": auth_mode,
                **support.readiness_metadata(auth_mode),
                "ready": ready,
                "readiness_error": (
                    None if ready else "credential-env-missing"
                ),
                "routes": [],
                "calls": 0,
                "successes": 0,
                "failures": 0,
            },
        )
        return ProviderConnectionResult(
            provider=request.alias,
            configured_model=request.model,
            kind=request.kind,
            ready=ready,
            credential_owner=support.auth_owner(auth_mode),
            readiness_error=(
                None if ready else "credential-env-missing"
            ),
        )


class FixtureHarness:
    """Interactive no-machine harness for exercising the complete chat flow."""

    def __init__(self, store: InMemoryRunStore) -> None:
        self.store = store

    async def create(
        self,
        task: str,
        *,
        caller: dict[str, Any] | None = None,
        model_provider: str | None = None,
        model_route: RunModelRoute | None = None,
    ) -> RunSnapshot:
        run = build_fixture_run(
            64,
            task=task,
            run_id=f"fixture-{uuid4()}",
            model_provider=model_provider,
            model_route=model_route,
        )
        run.caller = dict(caller or {})
        run.record(
            "fixture.task_received",
            model_provider=model_provider,
            model_route=(
                model_route.model_dump(mode="json", exclude_none=True)
                if model_route is not None
                else None
            ),
        )
        await self.store.save(run)
        return run

    async def continue_run(self, run_id: str) -> RunSnapshot:
        run = await self.store.get(run_id)
        run.status = RunStatus.COMPLETED
        run.pending_action = None
        run.active_activity = None
        run.record(
            "verification.completed",
            summary="Synthetic chat flow completed without a machine target.",
        )
        run.record("run.completed", synthetic=True)
        await self.store.save(run)
        return run

    async def pause(self, run_id: str, reason: str) -> RunSnapshot:
        run = await self.store.get(run_id)
        run.status = RunStatus.PAUSED
        run.record("run.paused", reason=reason)
        await self.store.save(run)
        return run

    async def steer(self, run_id: str, instruction: str) -> RunSnapshot:
        run = await self.store.get(run_id)
        run.operator_guidance.append(instruction)
        run.status = RunStatus.PAUSED
        run.record("run.steered", instruction=instruction)
        await self.store.save(run)
        return run

    async def resolve_approval(
        self,
        run_id: str,
        approval_id: str,
        decision: dict[str, Any],
    ) -> RunSnapshot:
        run = await self.store.get(run_id)
        pending = run.pending_approval or {}
        if pending.get("approval_id") != approval_id:
            raise ValueError("approval_id does not match the synthetic request")
        decision_type = str(decision.get("type") or "")
        run.pending_approval = None
        run.active_activity = None
        run.record(
            "approval.resolved",
            approval_id=approval_id,
            decision=decision_type,
            synthetic=True,
        )
        if decision_type == "approve":
            run.record(
                "action.completed",
                call_id="fixture-send-call",
                frame_id=2,
                world_version=2,
                synthetic=True,
            )
            run.status = RunStatus.COMPLETED
            run.record(
                "run.completed",
                summary="Synthetic approval flow completed; no input was sent.",
            )
        else:
            run.status = RunStatus.REJECTED
            run.record(
                "action.refused_by_operator",
                call_id="fixture-send-call",
                synthetic=True,
            )
        await self.store.save(run)
        return run

    async def abort(self, run_id: str, reason: str) -> RunSnapshot:
        run = await self.store.get(run_id)
        run.status = RunStatus.ABORTED
        run.record("run.aborted", reason=reason)
        await self.store.save(run)
        return run


def _fixture_model_name(provider_name: str) -> str:
    return (
        "opus"
        if provider_name == "claude-account"
        else "fast-controller-fixture"
    )


def _fixture_observation() -> ComputerObservation:
    return ComputerObservation(
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
    )


def _new_fixture_run(
    *,
    run_id: str,
    task: str,
    model_provider: str | None,
    model_route: RunModelRoute | None,
    plan: PlanDecision,
    pending_action: PendingAction | None,
    last_controller: ControllerDecision | None,
    next_action_index: int,
) -> RunSnapshot:
    return RunSnapshot(
        run_id=run_id,
        task=task,
        status=RunStatus.RUNNING,
        model_provider=model_provider,
        model_route=model_route,
        session_id="synthetic-session",
        plan=plan,
        observation=_fixture_observation(),
        pending_action=pending_action,
        last_controller=last_controller,
        next_action_index=next_action_index,
    )


def build_fixture_run(
    prefill_events: int = 1_200,
    *,
    task: str | None = None,
    run_id: str = FIXTURE_RUN_ID,
    model_provider: str | None = None,
    model_route: RunModelRoute | None = None,
) -> RunSnapshot:
    if prefill_events < 32:
        raise ValueError("prefill_events must be at least 32")
    provider_name = (
        model_route.controller[0]
        if model_route is not None and model_route.controller
        else model_provider or "fast-controller"
    )
    model_name = _fixture_model_name(provider_name)
    run = _new_fixture_run(
        run_id=run_id,
        task=task
        or (
            "Audit a long provider name, exact MCP arguments, sustained frame "
            "updates, a 1,200-event timeline, and 200% browser reflow"
        ),
        model_provider=model_provider,
        model_route=model_route,
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
        fixture_actions = (
            [
                {
                    "type": "type_text",
                    "text": "Quarterly review draft",
                    "context": "field",
                }
            ]
            if cycle % 3 == 1
            else [{"type": "click", "x": 840, "y": 420}]
        )
        input_receipts = (
            [
                {
                    "index": 0,
                    "type": "type_text",
                    "status": "verified_exact",
                    "verdict": "match",
                    "proof_state": "exact_ocr_readback",
                    "observed_text": "Quarterly review draft",
                    "observed_text_redacted": False,
                    "issued_characters": 22,
                    "requested_characters": 22,
                    "observed_characters": 22,
                    "correction_count": 0,
                    "delivery_retries": 0,
                    "used_fast_path": False,
                    "summary": "Typed and verified the target field.",
                    "edit_distance": 0,
                    "focus_evidence": "read_back_verified",
                    "requested_sha256": hashlib.sha256(
                        b"Quarterly review draft"
                    ).hexdigest(),
                    "issued_prefix_sha256": hashlib.sha256(
                        b"Quarterly review draft"
                    ).hexdigest(),
                    "readback_sha256": hashlib.sha256(
                        b"Quarterly review draft"
                    ).hexdigest(),
                    "exact_readback_sha256_match": True,
                }
            ]
            if cycle % 3 == 1
            else []
        )
        for kind, data in (
            (
                "model.provider_started",
                {
                    "role": "controller",
                    "provider": provider_name,
                    "attempt": 1,
                    "route_index": 0,
                },
            ),
            (
                "model.provider_completed",
                {
                    "role": "controller",
                    "provider": provider_name,
                    "model": model_name,
                    "attempt": 1,
                    "latency_ms": 740,
                },
            ),
            (
                "model.completed",
                {
                    "role": "controller",
                    "provider": provider_name,
                    "model": model_name,
                    "latency_ms": 740,
                    "intent": "Inspect the next bounded target",
                },
            ),
            (
                "action.checkpointed",
                {
                    "index": index,
                    "intent": "Inspect the next bounded target",
                    "actions": fixture_actions,
                },
            ),
            (
                "action.attempted",
                {
                    "index": index,
                    "attempt": 1,
                    "tool": "pikvm_run_burst",
                    "arguments": {
                        "actions": fixture_actions,
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
                    **(
                        {"input_receipts": input_receipts}
                        if input_receipts
                        else {}
                    ),
                },
            ),
            (
                "verification.evidence_captured",
                {
                    "revision": cycle + 1,
                    "action_index": index,
                    "before_frame_id": cycle + 1,
                    "after_frame_id": cycle + 2,
                    "synthetic": True,
                },
            ),
            (
                "model.completed",
                {
                    "role": "verifier",
                    "provider": "claude-account",
                    "model": "opus",
                    "verdict": "verified",
                    "summary": "The synthetic target changed as expected.",
                    "latency_ms": 12_120,
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
        provider=provider_name,
        attempt=1,
        route_index=0,
    )
    return run


def build_approval_fixture_run() -> RunSnapshot:
    """Build a visible approval case without connecting to a real computer."""

    run = _new_fixture_run(
        task="Review a Teams message before the final send input",
        run_id="approval-ui-audit",
        model_provider="claude-account",
        model_route=None,
        plan=PlanDecision(
            summary="Prepare the message, but stop before the irreversible input.",
            steps=[
                "Type the exact message into the compose box.",
                "Hold the Enter key behind a visible approval.",
                "Continue only if the operator allows this one action.",
            ],
            success_criteria=[
                "The proposed text and send input are visible before approval.",
                "No external message is sent by this synthetic fixture.",
            ],
            constraints=[
                "Fixture only: do not connect to Teams, VNC, PiKVM, or any API."
            ],
        ),
        pending_action=None,
        last_controller=None,
        next_action_index=1,
    )
    run.record(
        "model.provider_started",
        role="controller",
        provider="claude-account",
        model="opus",
        attempt=1,
        route_index=0,
    )
    run.record(
        "model.provider_completed",
        role="controller",
        provider="claude-account",
        model="opus",
        attempt=1,
        latency_ms=12_120,
    )
    run.record(
        "model.completed",
        role="controller",
        provider="claude-account",
        model="opus",
        intent="Type the message, then request approval before Enter",
    )
    arguments = {
        "actions": [
            {
                "type": "type_text",
                "text": "Quarterly figures are attached for your review.",
            },
            {"type": "key", "keys": ["ENTER"]},
        ],
        "based_on_world_version": 1,
        "based_on_control_epoch": 1,
        "idempotency_key": "fixture:approval:teams-send",
    }
    run.pending_action = PendingAction(
        index=1,
        intent="Prepare an external message and stop before sending",
        actions=list(arguments["actions"]),
        expected_evidence=[
            "The exact message remains visible and Enter is not pressed."
        ],
        based_on_world_version=1,
        based_on_control_epoch=1,
        idempotency_key="fixture:approval:teams-send",
        attempts=1,
    )
    run.record(
        "action.checkpointed",
        index=run.next_action_index,
        intent="Prepare an external message and stop before sending",
        actions=arguments["actions"],
    )
    run.record(
        "action.attempted",
        index=run.next_action_index,
        attempt=1,
        call_id="fixture-send-call",
        tool="pikvm_run_burst",
        arguments=arguments,
    )
    run.pending_approval = {
        "kind": "direct_burst",
        "approval_id": "fixture-send-approval",
        "session_id": "synthetic-session",
        "risk": "external_side_effect",
        "reason": "Pressing Enter may send the Teams message immediately.",
        "allowed_decisions": ["approve", "reject"],
    }
    run.status = RunStatus.NEEDS_APPROVAL
    run.record(
        "approval.required",
        approval_id="fixture-send-approval",
        risk="external_side_effect",
        request=run.pending_approval,
        synthetic=True,
    )
    return run


def build_direct_fixture_run(
    evidence_path: Path | None = None,
) -> RunSnapshot:
    """Build a guarded-direct trace whose outer client owns the action loop."""

    run = RunSnapshot(
        run_id="direct-ui-audit",
        task="Inspect a direct Claude computer-control trace",
        status=RunStatus.RUNNING,
        origin="direct_mcp",
        caller={
            "interface": "direct_mcp",
            "label": "claude-cli",
            "provider": "anthropic-oauth",
            "model": "opus",
        },
        session_id="synthetic-session",
        observation=_fixture_observation(),
    )
    run.record("run.created", origin="direct_mcp", caller=run.caller)
    if evidence_path is not None:
        run.latest_verification_image_path = str(evidence_path)
        run.latest_verification_image_revision = 1
        run.verification_images = [
            VerificationImageArtifact(
                revision=1,
                action_index=0,
                kind="pre_action",
                before_frame_id=1,
                path=str(evidence_path),
            )
        ]
        run.record(
            "action.pre_action_evidence_captured",
            call_id="fixture-direct-click",
            tool="pikvm_run_burst",
            revision=1,
            action_index=0,
            evidence_kind="pre_action",
            before_frame_id=1,
            source="harness",
            synthetic=True,
        )
    run.record(
        "action.attempted",
        call_id="fixture-direct-click",
        tool="pikvm_run_burst",
        caller=run.caller,
        arguments={
            "actions": [
                {
                    "type": "click",
                    "x": 412,
                    "y": 286,
                    "button": "left",
                }
            ],
            "based_on_world_version": 1,
            "based_on_control_epoch": 1,
            "idempotency_key": "fixture:direct:click",
        },
    )
    run.record(
        "action.completed_unverified",
        call_id="fixture-direct-click",
        tool="pikvm_run_burst",
        status="completed",
        effect_state="unverified",
        caller=run.caller,
        latency_ms=84,
        frame_id=2,
        world_version=2,
    )
    run.status = RunStatus.PAUSED
    run.record(
        "run.paused",
        reason="Synthetic direct trace paused for operator inspection.",
    )
    return run


def build_assistant_handoff_fixture_run() -> RunSnapshot:
    """Build an attributed chat-to-computer hand-off without target contact."""

    run = RunSnapshot(
        run_id="assistant-handoff-ui-audit",
        task="Inspect the connected screen without changing it",
        mode="computer",
        status=RunStatus.PAUSED,
        origin="managed",
        model_route=RunModelRoute(
            reasoner=["claude-account"],
            controller=["fast-controller"],
            verifier=["claude-account"],
        ),
        conversation=[
            {
                "message_id": "fixture-handoff-user",
                "role": "user",
                "content": "Inspect the connected screen without changing it",
                "event_cursor": 0,
            }
        ],
    )
    run.record(
        "model.completed",
        role="assistant",
        provider="claude-account",
        model="opus",
        latency_ms=5_188,
        outcome="computer",
        synthetic=True,
    )
    run.record(
        "assistant.computer_handoff",
        call_id="fixture-computer-handoff",
        tool="computer_start_task",
        arguments={"task": "Inspect the connected screen without changing it"},
        selected_by={
            "provider": "claude-account",
            "model": "opus",
            "latency_ms": 5_188,
        },
        synthetic=True,
    )
    run.conversation.append(
        ConversationMessage(
            message_id="fixture-handoff-assistant",
            role="assistant",
            content="I’ll inspect the managed computer without making changes.",
            event_cursor=run.event_cursor,
        )
    )
    run.record(
        "assistant.computer_handoff_started",
        call_id="fixture-computer-handoff",
        tool="computer_start_task",
        session_id=None,
        selected_by={
            "provider": "claude-account",
            "model": "opus",
            "latency_ms": 5_188,
        },
        synthetic=True,
    )
    run.error = "Synthetic fixture held before computer target contact"
    run.record(
        "run.paused",
        reason=run.error,
        synthetic=True,
    )
    return run


def build_generic_tool_fixture_run() -> RunSnapshot:
    """Build a completed generic tool call with production-shaped MCP output."""

    run = RunSnapshot(
        run_id="generic-tool-ui-audit",
        task=GENERIC_TOOL_TASK,
        mode="assistant",
        status=RunStatus.COMPLETED,
        origin="managed",
        conversation=[],
    )
    run.record("run.created", mode="assistant", synthetic=True)
    run.conversation.append(
        ConversationMessage(
            message_id="fixture-tool-user",
            role="user",
            content=GENERIC_TOOL_TASK,
            event_cursor=run.event_cursor,
        )
    )
    arguments = {
        "query": "python.org latest stable Python release download",
        "max_results": 5,
    }
    selected_by = {
        "provider": "claude-account",
        "model": "opus",
        "latency_ms": 5_300,
    }
    run.record(
        "tool.started",
        call_id="fixture-web-search",
        tool="web.search_text",
        arguments=arguments,
        selected_by=selected_by,
        synthetic=True,
    )
    results = [
        {
            "title": f"Synthetic Python result {index}",
            "href": f"https://www.python.org/synthetic/{index}",
        }
        for index in range(1, 6)
    ]
    run.record(
        "tool.completed",
        call_id="fixture-web-search",
        tool="web.search_text",
        content=json.dumps(
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(results[0]),
                    }
                ],
                "structured_content": {"result": results},
            },
            separators=(",", ":"),
        ),
        synthetic=True,
    )
    run.conversation.append(
        ConversationMessage(
            message_id="fixture-tool-assistant",
            role="assistant",
            content=(
                "The synthetic search returned five results without contacting "
                "an external service."
            ),
            event_cursor=run.event_cursor,
        )
    )
    run.record("run.completed", synthetic=True)
    return run


def advance_fixture_run(
    run: RunSnapshot,
    tick: int,
    evidence_path: Path,
) -> None:
    provider_name = (
        run.model_route.controller[0]
        if run.model_route is not None and run.model_route.controller
        else run.model_provider or "fast-controller"
    )
    model_name = _fixture_model_name(provider_name)
    activity = run.active_activity
    if activity is not None and activity.kind == "model":
        run.record(
            "model.provider_completed",
            role=activity.role,
            provider=activity.provider,
            model=model_name,
            attempt=activity.attempt,
            latency_ms=720 + tick % 120,
        )
        run.record(
            "model.completed",
            role="controller",
            provider=provider_name,
            model=model_name,
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
    run.latest_verification_image_path = str(evidence_path)
    run.latest_verification_image_revision += 1
    evidence = VerificationImageArtifact(
        revision=run.latest_verification_image_revision,
        action_index=run.next_action_index,
        before_frame_id=max(1, tick + 1),
        after_frame_id=tick + 2,
        path=str(evidence_path),
    )
    run.verification_images = [*run.verification_images[-63:], evidence]
    run.record(
        "verification.evidence_captured",
        revision=evidence.revision,
        action_index=evidence.action_index,
        before_frame_id=evidence.before_frame_id,
        after_frame_id=evidence.after_frame_id,
        synthetic=True,
    )
    run.record(
        "model.completed",
        role="verifier",
        provider="claude-account",
        model="opus",
        verdict="verified",
        summary="The synthetic target changed as expected.",
        latency_ms=12_120,
    )
    run.next_action_index += 1
    if run.observation is not None:
        run.observation.frame_id = tick + 2
        run.observation.world_version = tick + 2
    run.record(
        "model.provider_started",
        role="controller",
        provider=provider_name,
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
    approval_run = build_approval_fixture_run()
    handoff_run = build_assistant_handoff_fixture_run()
    generic_tool_run = build_generic_tool_fixture_run()
    evidence_dir = TemporaryDirectory(prefix="pikvm-ui-fixture-")
    evidence_path = Path(evidence_dir.name) / "before-after.png"
    direct_evidence_path = Path(evidence_dir.name) / "direct-before.png"
    _write_fixture_evidence(evidence_path)
    _write_direct_fixture_evidence(direct_evidence_path)
    direct_run = build_direct_fixture_run(direct_evidence_path)
    evidence_events = [
        event
        for event in run.events
        if event.kind == "verification.evidence_captured"
    ][-64:]
    latest_evidence = evidence_events[-1]
    run.latest_verification_image_path = str(evidence_path)
    run.latest_verification_image_revision = int(
        latest_evidence.data["revision"]
    )
    run.verification_images = [
        VerificationImageArtifact(
            revision=int(event.data["revision"]),
            action_index=int(event.data["action_index"]),
            before_frame_id=int(event.data["before_frame_id"]),
            after_frame_id=int(event.data["after_frame_id"]),
            path=str(evidence_path),
        )
        for event in evidence_events
    ]

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await store.save(run)
        await store.save(approval_run)
        await store.save(handoff_run)
        await store.save(generic_tool_run)
        await store.save(direct_run)

        async def produce() -> None:
            tick = 0
            while True:
                await asyncio.sleep(event_interval_ms / 1_000)
                tick += 1
                advance_fixture_run(run, tick, evidence_path)
                await store.save(run)

        producer = asyncio.create_task(produce())
        try:
            yield
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            evidence_dir.cleanup()

    models = FixtureModels()
    app = create_harness_app(
        harness=FixtureHarness(store),  # type: ignore[arg-type]
        store=store,
        models=models,
        access_token=access_token,
        allowed_origins={origin},
        live_frames=FixtureLiveFrames(),
        external_driver=False,
        lifespan=lifespan,
        provider_connections=FixtureProviderConnections(models),
        managed_mcp_name="Managed PiKVM MCP",
        computer_name="Synthetic audit target",
    )
    app.state.synthetic_fixture = True
    app.state.synthetic_store = store
    app.state.synthetic_run = run
    app.state.synthetic_approval_run = approval_run
    app.state.synthetic_handoff_run = handoff_run
    app.state.synthetic_generic_tool_run = generic_tool_run
    app.state.synthetic_direct_run = direct_run
    return app
