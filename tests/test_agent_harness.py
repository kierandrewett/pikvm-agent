from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

from pikvm_agent.harness.agent import AgentHarness
from pikvm_agent.harness.agent_models import (
    ArtifactAcceptance,
    ArtifactAcceptanceState,
    ComputerObservation,
    ControllerDecision,
    HarnessConfig,
    ModelRequest,
    ModelResponse,
    PendingAction,
    PlanDecision,
    RunModelRoute,
    RunSnapshot,
    RunStatus,
    VerificationDecision,
)
from pikvm_agent.harness.agent_store import InMemoryRunStore
from pikvm_agent.harness.model_budget import (
    ModelBudgetPolicy,
    ProviderCostTerms,
)
from pikvm_agent.harness.model_pool import ModelPool, RoleRoute


class ScriptedProvider:
    name = "scripted"

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.role == "reasoner":
            data = {
                "summary": "Enter the requested text and verify it.",
                "steps": ["Focus the editor", "Type the text", "Verify exact text"],
                "success_criteria": ["The editor contains exactly hello world"],
                "constraints": ["Do not submit or send anything"],
            }
        elif request.role == "controller":
            data = {
                "outcome": "act",
                "intent": "Type the requested text into the already-focused editor.",
                "actions": [{"type": "type_text", "text": "hello world"}],
                "expected_evidence": ["The focused editor shows hello world"],
            }
        else:
            data = {
                "verdict": "complete",
                "summary": "The exact requested text is visible.",
                "evidence": ["Observed hello world in the focused editor"],
                "criteria": [
                    {
                        "criterion_index": 0,
                        "satisfied": True,
                        "evidence": "The editor visibly contains exactly hello world.",
                    }
                ],
            }
        return ModelResponse(provider=self.name, model="scripted-v1", data=data)


def test_artifact_acceptance_pass_requires_complete_host_evidence() -> None:
    acceptance = ArtifactAcceptance(
        kind="office_artifact",
        label="Quarterly earnings workbook",
        state=ArtifactAcceptanceState.PASSED,
        artifact_format="xlsx",
        checks_passed=24,
        checks_total=24,
        byte_count=12_345,
        sha256="a" * 64,
    )

    assert acceptance.state is ArtifactAcceptanceState.PASSED
    with pytest.raises(ValidationError, match="all declared checks"):
        ArtifactAcceptance(
            kind="office_artifact",
            label="Quarterly earnings workbook",
            state=ArtifactAcceptanceState.PASSED,
            artifact_format="xlsx",
            checks_passed=23,
            checks_total=24,
            byte_count=12_345,
            sha256="a" * 64,
        )


def test_durable_model_route_rejects_duplicates_and_ambiguous_legacy_pin() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        RunModelRoute(reasoner=["strong", "strong"])

    with pytest.raises(
        ValidationError,
        match="model_provider and model_route cannot both be selected",
    ):
        RunSnapshot(
            run_id="ambiguous-model-route",
            task="Do the task",
            status=RunStatus.RUNNING,
            model_provider="strong",
            model_route=RunModelRoute(reasoner=["strong", "backup"]),
        )


@pytest.mark.asyncio
async def test_status_and_continuation_use_the_bounded_control_snapshot() -> None:
    class TrackingStore(InMemoryRunStore):
        control_reads = 0
        full_reads = 0

        async def get_control(
            self,
            run_id: str,
            event_limit: int = 1_000,
        ) -> RunSnapshot:
            self.control_reads += 1
            return await super().get_control(run_id, event_limit)

        async def get(self, run_id: str) -> RunSnapshot:
            self.full_reads += 1
            return await super().get(run_id)

    store = TrackingStore()
    run = RunSnapshot(
        run_id="bounded-agent-read",
        task="Do not replay a complete historical timeline",
        status=RunStatus.COMPLETED,
    )
    for index in range(1_200):
        run.record("run.tick", number=index)
    await store.save(run)
    harness = AgentHarness(
        computer=object(),  # type: ignore[arg-type]
        models=object(),  # type: ignore[arg-type]
        store=store,
    )

    status = await harness.status(run.run_id)
    continued = await harness.continue_run(run.run_id)

    assert len(status.events) == 1_000
    assert status.event_cursor == 1_200
    assert continued.status is RunStatus.COMPLETED
    assert store.control_reads == 2
    assert store.full_reads == 0


class TemporarilyUnavailableProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.failures_remaining = 1

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise TimeoutError("provider temporarily unavailable")
        return await super().complete(request)


class ControllerUnavailableProvider(ScriptedProvider):
    name = "controller-unavailable"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.role == "controller":
            raise TimeoutError("controller provider unavailable")
        return await super().complete(request)


class MeteredProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        return response.model_copy(
            update={"usage": {"input_tokens": 10, "output_tokens": 5}}
        )


class InitiallyBlockedProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.controller_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            self.controller_calls += 1
            if self.controller_calls == 1:
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "outcome": "blocked",
                        "intent": "Wait for approval.",
                        "actions": [],
                        "expected_evidence": [],
                        "reason": "Approval has not been recorded.",
                    },
                )
        return await super().complete(request)


class FailingVerifierProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "verifier":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "verdict": "failed",
                    "summary": "Typed text has the wrong letter case.",
                    "evidence": ["Expected uppercase but observed lowercase."],
                },
            )
        return await super().complete(request)


class InvalidThenRepairedControllerProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.controller_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            self.controller_calls += 1
            if self.controller_calls == 1:
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "outcome": "blocked",
                        "intent": "Contradictory model output.",
                        "actions": [{"type": "key", "keys": ["End"]}],
                        "expected_evidence": [],
                        "reason": "",
                    },
                )
        return await super().complete(request)


class StallingControllerProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "verifier":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "verdict": "verified",
                    "summary": "The action was accepted but the task needs more work.",
                    "evidence": ["The editor remains visible."],
                },
            )
        return await super().complete(request)


class RepeatedUngroundedThenKeyboardProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.controller_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            self.controller_calls += 1
            actions = (
                [{"type": "key", "keys": ["META", "M"]}]
                if self.controller_calls == 3
                else [
                    {
                        "type": "click",
                        "x": 704 if self.controller_calls == 2 else 705,
                        "y": 94,
                    }
                ]
            )
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": (
                        "Minimize the visible windows with a safe shortcut."
                        if self.controller_calls == 3
                        else "Click the visible title-bar minimize control."
                    ),
                    "actions": actions,
                    "expected_evidence": ["The obstructing windows are minimized."],
                },
            )
        return await super().complete(request)


class DistinctUngroundedThenKeyboardProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.controller_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            self.controller_calls += 1
            actions_by_call = {
                1: [{"type": "click", "x": 705, "y": 94}],
                2: [{"type": "click", "x": 620, "y": 660}],
                3: [{"type": "key", "keys": ["META", "M"]}],
            }
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": f"Bounded navigation {self.controller_calls}.",
                    "actions": actions_by_call[self.controller_calls],
                    "expected_evidence": ["Word is visible and unobstructed."],
                },
            )
        return await super().complete(request)


class KeyOnlyControllerProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": "Submit the already prepared value.",
                    "actions": [{"type": "key", "keys": ["ENTER"]}],
                    "expected_evidence": ["The prepared value is submitted."],
                },
            )
        return await super().complete(request)


class GlobalShortcutControllerProvider(ScriptedProvider):
    def __init__(self, keys: list[str] | None = None) -> None:
        super().__init__()
        self.keys = keys or ["CTRL", "ALT", "T"]

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": "Open a terminal without depending on window focus.",
                    "actions": [
                        {"type": "key", "keys": self.keys},
                        {"type": "wait_for_change", "timeout_ms": 3000},
                    ],
                    "expected_evidence": [
                        "A terminal window is visible in the foreground."
                    ],
                },
            )
        return await super().complete(request)


class GlobalShortcutSequenceControllerProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": "Exit fullscreen and open the desktop overview.",
                    "actions": [
                        {"type": "key", "keys": ["ESC"]},
                        {"type": "key", "keys": ["META"]},
                    ],
                    "expected_evidence": [
                        "The desktop overview is visible instead of fullscreen video."
                    ],
                },
            )
        return await super().complete(request)


class PointerOnlyControllerProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": "Move the pointer without making task progress.",
                    "actions": [{"type": "move", "x": 450, "y": 100}],
                    "expected_evidence": ["The pointer moved."],
                },
            )
        return await super().complete(request)


class RepeatedFailedSearchProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.controller_calls = 0
        self.verifier_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            self.controller_calls += 1
            actions = (
                [{"type": "click", "x": 120, "y": 80}]
                if self.controller_calls == 2
                else [
                    {
                        "type": "type_text",
                        "text": "dim screen when inactive",
                        "context": "field",
                    }
                ]
            )
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": f"Bounded action {self.controller_calls}.",
                    "actions": actions,
                    "expected_evidence": ["The intended intermediate state is visible."],
                },
            )
        if request.role == "verifier":
            self.requests.append(request)
            self.verifier_calls += 1
            verdict = "failed" if self.verifier_calls == 1 else "verified"
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "verdict": verdict,
                    "summary": (
                        "The search returned no results."
                        if verdict == "failed"
                        else "Focus is visibly established."
                    ),
                    "evidence": ["Visible state inspected."],
                },
            )
        return await super().complete(request)


class ToggleRetryProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.controller_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            self.controller_calls += 1
            x = 522 if self.controller_calls == 1 else 513
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": (
                        "Enable Do Not Disturb using the visible switch."
                        if self.controller_calls == 1
                        else "Retry enabling Do Not Disturb on the same switch."
                    ),
                    "actions": [{"type": "click", "x": x, "y": 302}],
                    "expected_evidence": [
                        "The Do Not Disturb switch is visibly enabled."
                    ],
                },
            )
        if request.role == "verifier":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "verdict": "failed",
                    "summary": "The switch state is visually ambiguous.",
                    "evidence": ["The colour did not settle yet."],
                },
            )
        return await super().complete(request)


class ContradictoryCompletionProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "verifier":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "verdict": "complete",
                    "summary": (
                        "This completes the current navigation action, but the "
                        "overall task has not yet been performed or verified."
                    ),
                    "evidence": ["The settings entry is now visible."],
                    "criteria": [
                        {
                            "criterion_index": 0,
                            "satisfied": False,
                            "evidence": "The final setting has not been changed.",
                        }
                    ],
                },
            )
        return await super().complete(request)


class RejectedDoneProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.role == "reasoner":
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "summary": "Disable the requested setting.",
                    "steps": ["Inspect the setting", "Disable it"],
                    "success_criteria": ["The requested setting is off."],
                    "constraints": ["Preserve unrelated settings."],
                },
            )
        if request.role == "controller":
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "done",
                    "intent": "The visible GUI has no matching control.",
                    "actions": [],
                    "expected_evidence": [],
                },
            )
        return ModelResponse(
            provider=self.name,
            model="scripted-v1",
            data={
                "verdict": "complete",
                "summary": "The requested setting is still on.",
                "evidence": ["No matching GUI control is visible."],
                "criteria": [
                    {
                        "criterion_index": 0,
                        "satisfied": False,
                        "evidence": "The setting has not been disabled.",
                    }
                ],
            },
        )


class FakeComputer:
    def __init__(self) -> None:
        self.bursts: list[dict[str, Any]] = []
        self.aborts: list[dict[str, str]] = []

    async def open(self, label: str) -> ComputerObservation:
        return ComputerObservation(
            session_id="s_1",
            status="paused",
            frame_id=1,
            world_version=7,
            control_epoch=2,
            image_path="/tmp/frame-before.jpg",
        )

    async def burst(
        self,
        *,
        session_id: str,
        actions: list[dict[str, Any]],
        based_on_world_version: int | None,
        based_on_control_epoch: int | None,
        idempotency_key: str,
    ) -> ComputerObservation:
        self.bursts.append(
            {
                "session_id": session_id,
                "actions": actions,
                "based_on_world_version": based_on_world_version,
                "based_on_control_epoch": based_on_control_epoch,
                "idempotency_key": idempotency_key,
            }
        )
        return ComputerObservation(
            session_id=session_id,
            status="completed",
            frame_id=2,
            world_version=8,
            control_epoch=2,
            image_path="/tmp/frame-after.jpg",
        )

    async def refresh(self, *, session_id: str) -> ComputerObservation:
        return ComputerObservation(
            session_id=session_id,
            status="paused",
            frame_id=3,
            world_version=9,
            control_epoch=2,
            image_path="/tmp/frame-refreshed.jpg",
        )

    async def resolve_approval(
        self, *, session_id: str, approval_id: str, decision: dict[str, Any]
    ) -> ComputerObservation:
        raise AssertionError("no approval expected")

    async def abort(self, *, session_id: str, reason: str) -> ComputerObservation:
        self.aborts.append({"session_id": session_id, "reason": reason})
        return ComputerObservation(session_id=session_id, status="aborted")


class ApprovalComputer(FakeComputer):
    def __init__(self) -> None:
        super().__init__()
        self.resolutions: list[dict[str, Any]] = []

    async def burst(self, **kwargs: Any) -> ComputerObservation:
        self.bursts.append(kwargs)
        return ComputerObservation(
            session_id=kwargs["session_id"],
            status="needs_approval",
            frame_id=1,
            world_version=7,
            control_epoch=2,
            approval_request={
                "approval_id": "approval_1",
                "risk": "communication_send",
                "summary": "Send a message",
            },
        )

    async def resolve_approval(
        self, *, session_id: str, approval_id: str, decision: dict[str, Any]
    ) -> ComputerObservation:
        self.resolutions.append(
            {
                "session_id": session_id,
                "approval_id": approval_id,
                "decision": decision,
            }
        )
        return ComputerObservation(
            session_id=session_id,
            status="completed" if decision["type"] == "approve" else "rejected",
            frame_id=2,
            world_version=8,
            control_epoch=2,
        )


class UngroundedNavigationComputer(FakeComputer):
    def __init__(self) -> None:
        super().__init__()
        self.resolutions: list[dict[str, Any]] = []
        self.opens = 0

    async def open(self, label: str) -> ComputerObservation:
        self.opens += 1
        observation = await super().open(label)
        return observation.model_copy(
            update={
                "session_id": f"s_{self.opens}",
                "frame_id": self.opens,
                "world_version": 6 + self.opens,
            }
        )

    async def burst(self, **kwargs: Any) -> ComputerObservation:
        self.bursts.append(kwargs)
        return ComputerObservation(
            session_id=kwargs["session_id"],
            status="needs_approval",
            frame_id=2,
            world_version=7,
            control_epoch=2,
            approval_request={
                "kind": "direct_burst",
                "approval_id": "unknown_click_1",
                "risk": "unknown",
                "reason": (
                    "coordinate click target could not be independently read"
                ),
            },
        )

    async def resolve_approval(
        self, *, session_id: str, approval_id: str, decision: dict[str, Any]
    ) -> ComputerObservation:
        self.resolutions.append(
            {
                "session_id": session_id,
                "approval_id": approval_id,
                "decision": decision,
            }
        )
        return ComputerObservation(
            session_id=session_id,
            status="blocked",
            frame_id=2,
            world_version=7,
            control_epoch=2,
        )


class UngroundedThenKeyboardComputer(UngroundedNavigationComputer):
    async def burst(self, **kwargs: Any) -> ComputerObservation:
        if kwargs["actions"] == [{"type": "key", "keys": ["META", "M"]}]:
            return await FakeComputer.burst(self, **kwargs)
        return await super().burst(**kwargs)


class StaleThenFreshComputer(FakeComputer):
    def __init__(self) -> None:
        super().__init__()
        self.refreshes = 0

    async def burst(self, **kwargs: Any) -> ComputerObservation:
        self.bursts.append(kwargs)
        if len(self.bursts) == 1:
            return ComputerObservation(
                session_id=kwargs["session_id"],
                status="stale_world",
                frame_id=2,
                world_version=8,
                control_epoch=2,
            )
        return ComputerObservation(
            session_id=kwargs["session_id"],
            status="completed",
            frame_id=4,
            world_version=10,
            control_epoch=2,
            image_path="/tmp/frame-after.jpg",
        )

    async def refresh(self, *, session_id: str) -> ComputerObservation:
        self.refreshes += 1
        return ComputerObservation(
            session_id=session_id,
            status="paused",
            frame_id=3,
            world_version=9,
            control_epoch=2,
            image_path="/tmp/frame-refreshed.jpg",
        )


class FlakyComputer(FakeComputer):
    def __init__(self) -> None:
        super().__init__()
        self.keys: list[str] = []

    async def burst(self, **kwargs: Any) -> ComputerObservation:
        self.bursts.append(kwargs)
        self.keys.append(kwargs["idempotency_key"])
        if len(self.bursts) == 1:
            raise TimeoutError("response lost after submission")
        return ComputerObservation(
            session_id=kwargs["session_id"],
            status="completed",
            frame_id=2,
            world_version=8,
            control_epoch=2,
            image_path="/tmp/frame-after.jpg",
        )


class FocusLostComputer(FakeComputer):
    async def burst(self, **kwargs: Any) -> ComputerObservation:
        self.bursts.append(kwargs)
        return ComputerObservation(
            session_id=kwargs["session_id"],
            status="failed",
            frame_id=2,
            world_version=7,
            control_epoch=2,
            error="typed text did not change the screen",
            raw={"reason": "type_unverified"},
        )


class UnverifiedTypingComputer(FakeComputer):
    async def burst(self, **kwargs: Any) -> ComputerObservation:
        self.bursts.append(kwargs)
        return ComputerObservation(
            session_id=kwargs["session_id"],
            status="unverified",
            frame_id=2,
            world_version=8,
            control_epoch=2,
            image_path="/tmp/frame-after.jpg",
            error="OCR could not prove the exact typed text",
            raw={"reason": "type_unverified"},
        )


class InputReceiptComputer(FakeComputer):
    async def burst(self, **kwargs: Any) -> ComputerObservation:
        self.bursts.append(kwargs)
        return ComputerObservation(
            session_id=kwargs["session_id"],
            status="completed",
            frame_id=2,
            world_version=8,
            control_epoch=2,
            image_path="/tmp/frame-after.jpg",
            raw={
                "action_receipts": [
                    {
                        "index": 0,
                        "type": "type_text",
                        "status": "verified_exact",
                        "verdict": "match",
                        "observed_text": "hello world",
                        "observed_text_redacted": False,
                        "issued_characters": 11,
                        "requested_characters": 11,
                        "observed_characters": 11,
                        "correction_count": 1,
                        "delivery_retries": 0,
                        "used_fast_path": False,
                        "summary": "Typed and verified.",
                        "edit_distance": 0,
                        "focus_evidence": "read_back_verified",
                        "requested_sha256": "a" * 64,
                        "issued_prefix_sha256": "a" * 64,
                        "readback_sha256": "a" * 64,
                        "exact_readback_sha256_match": True,
                        "private_path": "/tmp/do-not-expose.png",
                        "unknown": {"nested": "value"},
                    },
                    {
                        "index": 99,
                        "type": "type_text",
                        "status": "verified_exact",
                        "observed_text": "not a submitted action",
                    },
                ]
            },
        )


class ImageComputer(FakeComputer):
    def __init__(self, before: Path, after: Path) -> None:
        super().__init__()
        self.before = before
        self.after = after

    async def open(self, label: str) -> ComputerObservation:
        observation = await super().open(label)
        return observation.model_copy(update={"image_path": str(self.before)})

    async def burst(self, **kwargs: Any) -> ComputerObservation:
        observation = await super().burst(**kwargs)
        return observation.model_copy(update={"image_path": str(self.after)})


class TargetSwitchComputer(FakeComputer):
    async def open(self, label: str) -> ComputerObservation:
        observation = await super().open(label)
        return observation.model_copy(
            update={
                "machine": {
                    "alias": "Machine A",
                    "fingerprint": "target:aaaaaaaaaaaaaaaa",
                }
            }
        )

    async def burst(self, **kwargs: Any) -> ComputerObservation:
        observation = await super().burst(**kwargs)
        return observation.model_copy(
            update={
                "machine": {
                    "alias": "Machine B",
                    "fingerprint": "target:bbbbbbbbbbbbbbbb",
                }
            }
        )


def build_harness(
    provider: ScriptedProvider, computer: FakeComputer
) -> AgentHarness:
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            role: RoleRoute(providers=[provider.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    return AgentHarness(
        computer=computer,
        models=pool,
        store=InMemoryRunStore(),
        config=HarnessConfig(max_actions_per_advance=1),
    )


def test_controller_action_schema_rejects_unknown_hid_and_verification_bypass() -> None:
    with pytest.raises(ValidationError):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Run an invented action",
                "actions": [{"type": "shell", "command": "whoami"}],
                "expected_evidence": [],
                "reason": "",
            }
        )
    with pytest.raises(ValidationError, match="no_verify"):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Bypass read-back",
                "actions": [
                    {
                        "type": "type_text",
                        "text": "hello",
                        "no_verify": True,
                    }
                ],
                "expected_evidence": [],
                "reason": "",
            }
        )


def test_controller_action_schema_rejects_duplicate_pointer_moves() -> None:
    with pytest.raises(ValidationError, match="duplicate consecutive pointer move"):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Preserve focus before typing.",
                "actions": [
                    {"type": "move", "x": 1132, "y": 539},
                    {"type": "move", "x": 1132, "y": 539},
                    {"type": "move", "x": 1132, "y": 539},
                ],
                "expected_evidence": ["Focus remains unchanged."],
            }
        )


def test_controller_action_schema_rejects_pointer_only_wiggle() -> None:
    with pytest.raises(ValidationError, match="multiple pointer-only moves"):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Preserve the current terminal state.",
                "actions": [
                    {"type": "move", "x": 353, "y": 245},
                    {"type": "move", "x": 349, "y": 248},
                    {"type": "move", "x": 353, "y": 245},
                    {"type": "move", "x": 349, "y": 248},
                ],
                "expected_evidence": ["The terminal remains unchanged."],
            }
        )


def test_controller_can_request_one_bounded_spreadsheet_grid() -> None:
    decision = ControllerDecision.model_validate(
        {
            "outcome": "act",
            "intent": "Enter the quarterly table from the verified active cell.",
            "actions": [
                {
                    "type": "spreadsheet_grid",
                    "rows": [["Q1", "124.8"], ["Q2", "132.1"]],
                }
            ],
            "expected_evidence": ["The two spreadsheet rows are visible."],
        }
    )

    assert decision.actions[0].model_dump(mode="json") == {
        "type": "spreadsheet_grid",
        "rows": [["Q1", "124.8"], ["Q2", "132.1"]],
    }


async def test_controller_prompt_limits_grid_entry_to_a_verified_spreadsheet_cell() -> None:
    provider = ScriptedProvider()
    harness = build_harness(provider, FakeComputer())

    await harness.start("Enter a small quarterly table in the workbook.")

    prompt = next(
        request.prompt
        for request in provider.requests
        if request.role == "controller"
    )
    assert "spreadsheet_grid" in prompt
    assert "verified active spreadsheet cell" in prompt
    assert "Never use it in messaging" in prompt
    assert "one reviewed local-file action" in prompt
    assert "Treat recent_verified_actions as durable evidence" in prompt


async def test_controller_prompt_prefers_a_stable_legible_end_state() -> None:
    provider = ScriptedProvider()
    harness = build_harness(provider, FakeComputer())

    await harness.start("Open Calculator and calculate 37 × 19.")

    prompt = next(
        request.prompt
        for request in provider.requests
        if request.role == "controller"
    )
    normalized = " ".join(prompt.split())
    assert "stable, directly legible local end state" in normalized
    assert "complete reversible local operation, not one mouse click" in normalized
    assert "group the full sequence of reversible local inputs" in normalized
    assert "one controller/verifier round trip on each digit" in normalized
    assert "complete expression including the equals key" in normalized
    assert "tiny expression-history text" in normalized
    assert "consequential commit actions" in normalized


async def test_reasoner_prompt_avoids_duplicate_pre_and_post_save_audits() -> None:
    provider = ScriptedProvider()
    harness = build_harness(provider, FakeComputer())

    await harness.start(
        "Create the workbook, save it, reopen it, and verify every required value."
    )

    prompt = next(
        request.prompt
        for request in provider.requests
        if request.role == "reasoner"
    )
    assert "do not plan a complete content audit both before and after saving" in prompt
    assert "perform the requested detailed audit once, after reopening" in prompt
    assert "simultaneously legible in one frame" in prompt
    assert "do not cancel an already-open Save As dialog solely to resume an audit" in prompt
    assert "Treat recent_verified_actions as durable evidence" in prompt


async def test_reasoner_can_plan_a_short_visible_terminal_fallback() -> None:
    provider = ScriptedProvider()
    harness = build_harness(provider, FakeComputer())

    await harness.start("Disable the local dim-screen setting.")

    prompt = next(
        request.prompt
        for request in provider.requests
        if request.role == "reasoner"
    )
    normalized = " ".join(prompt.split())
    assert "on-screen terminal" in normalized
    assert "short, inspectable command" in normalized
    assert "exact GUI control is absent" in normalized
    assert "not a hidden side channel" in normalized
    assert "Never use this fallback for a long script" in normalized
    assert "Do not invent a GUI-only or no-terminal constraint" in normalized
    assert "missing from the visible GUI, replan to that fallback" in normalized
    assert "maximize or widen the terminal" in normalized
    assert "increase its text size" in normalized
    assert "never append a guessed suffix" in normalized
    assert "cancel the draft with Ctrl+C" in normalized


async def test_controller_handles_an_unverified_terminal_draft_without_guessing() -> None:
    provider = ScriptedProvider()
    harness = build_harness(provider, FakeComputer())

    await harness.start("Disable the local dim-screen setting.")

    prompt = next(
        request.prompt
        for request in provider.requests
        if request.role == "controller"
    )
    normalized = " ".join(prompt.split())
    assert "never append guessed missing characters" in normalized
    assert "do not press Enter" in normalized
    assert "cancel the draft with Ctrl+C" in normalized
    assert "visibly clean prompt" in normalized
    assert "long exact terminal draft" in normalized
    assert "separate verified width action" in normalized
    assert "separate verified text-size increase" in normalized
    assert "request a replan instead of blocking" in normalized
    assert "model-invented GUI-only or no-terminal constraint" in normalized


def test_controller_separates_spreadsheet_focus_from_grid_entry() -> None:
    with pytest.raises(
        ValidationError,
        match="separate verified focus action",
    ):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Click the first cell and enter the table.",
                "actions": [
                    {"type": "click", "x": 160, "y": 240},
                    {
                        "type": "spreadsheet_grid",
                        "rows": [["Q1", "124.8"]],
                    },
                ],
                "expected_evidence": ["The row is visible."],
            }
        )


def test_controller_action_schema_rejects_duplicate_click_within_burst() -> None:
    with pytest.raises(ValidationError, match="duplicate pointer activation"):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Focus the terminal once.",
                "actions": [
                    {"type": "move", "x": 300, "y": 90},
                    {"type": "click", "x": 300, "y": 90},
                    {"type": "move", "x": 300, "y": 90},
                    {"type": "click", "x": 300, "y": 90},
                ],
                "expected_evidence": ["The terminal has keyboard focus."],
            }
        )


@pytest.mark.parametrize("text", ["echo ready\n", "echo ready\r", "left\tright"])
def test_controller_action_schema_rejects_control_characters_in_text(
    text: str,
) -> None:
    with pytest.raises(ValidationError, match="control characters"):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Type without committing.",
                "actions": [
                    {
                        "type": "type_text",
                        "text": text,
                        "context": "terminal",
                    }
                ],
                "expected_evidence": ["The exact text is visible at the prompt."],
            }
        )


@pytest.mark.parametrize(
    "follow_up",
    [
        {"type": "key", "keys": ["ENTER"]},
        {"type": "click", "x": 500, "y": 400},
        {"type": "scroll", "direction": "down", "amount": 2},
    ],
)
def test_controller_action_schema_separates_text_from_active_follow_up(
    follow_up: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="active follow-up"):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Prepare text without committing it.",
                "actions": [
                    {
                        "type": "type_text",
                        "text": "find video.mp4",
                        "context": "terminal",
                    },
                    follow_up,
                ],
                "expected_evidence": ["The exact text is visible."],
            }
        )


def test_controller_action_schema_allows_passive_evidence_after_text() -> None:
    decision = ControllerDecision.model_validate(
        {
            "outcome": "act",
            "intent": "Prepare text and wait for settled pixels.",
            "actions": [
                {
                    "type": "type_text",
                    "text": "find video.mp4",
                    "context": "terminal",
                },
                {
                    "type": "wait_for_stable_screen",
                    "stable_ms": 300,
                    "timeout_ms": 1500,
                },
            ],
            "expected_evidence": ["The exact text is visible."],
        }
    )

    assert [action.type for action in decision.actions] == [
        "type_text",
        "wait_for_stable_screen",
    ]


@pytest.mark.asyncio
async def test_start_runs_a_checkpointed_reason_act_verify_slice() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            role: RoleRoute(providers=[provider.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    harness = AgentHarness(
        computer=computer,
        models=pool,
        store=InMemoryRunStore(),
        config=HarnessConfig(max_actions_per_advance=1),
    )

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
        "verifier",
    ]
    reasoner_prompt = " ".join(provider.requests[0].prompt.split())
    assert "Do not invent exact values" in reasoner_prompt
    assert "necessary to satisfy the user's literal request" in reasoner_prompt
    assert "do not invent a numeric zoom threshold" in reasoner_prompt
    assert "authenticated user/operator corrections" in reasoner_prompt
    assert "the latest entry wins" in reasoner_prompt
    controller_prompt = " ".join(provider.requests[1].prompt.split())
    assert "visibly larger terminal glyphs" in controller_prompt
    assert "do not require a numeric zoom indicator" in controller_prompt
    verifier_prompt = " ".join(provider.requests[2].prompt.split())
    assert "Return verified only when every action assessment" in verifier_prompt
    assert "Do not return uncertain merely because the overall task" in verifier_prompt
    assert "visibly larger terminal glyphs are sufficient" in verifier_prompt
    assert "not require a numeric zoom percentage" in verifier_prompt
    assert len(computer.bursts) == 1
    burst = computer.bursts[0]
    assert burst["actions"] == [
        {
            "type": "type_text",
            "text": "hello world",
            "code": False,
            "secret": False,
            "context": "",
        }
    ]
    assert burst["based_on_world_version"] == 7
    assert burst["based_on_control_epoch"] == 2
    assert burst["idempotency_key"].startswith(f"{result.run_id}:action:0:")
    assert result.pending_action is None
    assert result.last_verification is not None
    assert result.last_verification.verdict == "complete"
    attempted = next(
        event for event in result.events if event.kind == "action.attempted"
    )
    completed = next(
        event for event in result.events if event.kind == "action.completed"
    )
    assert attempted.data["tool"] == "pikvm_run_burst"
    assert attempted.data["call_id"].endswith(":attempt:1")
    assert completed.data["call_id"] == attempted.data["call_id"]
    assert completed.data["tool"] == "pikvm_run_burst"
    assert completed.data["status"] == "completed"
    assert completed.data["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_observational_follow_up_uses_one_read_only_model_call() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start("What about now?")

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == ["verifier"]
    assert computer.bursts == []
    assert result.plan is not None
    assert result.plan.constraints == ["Do not send keyboard or pointer input."]
    assert any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_plain_screen_question_uses_one_read_only_model_call() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start("what is on the screen")

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == ["verifier"]
    assert computer.bursts == []
    assert any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_explicit_read_only_screen_description_skips_planning_and_input() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start(
        "Read-only check: describe what is currently visible on the connected "
        "disposable Windows VM. Do not click, type, scroll, press keys, or "
        "perform any computer input."
    )

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == ["verifier"]
    assert computer.bursts == []
    assert result.plan is not None
    assert result.plan.constraints == ["Do not send keyboard or pointer input."]
    assert any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


def test_read_only_prefix_does_not_hide_a_later_action_request() -> None:
    run = RunSnapshot(
        run_id="mixed-read-only-and-action",
        task="Read-only check: describe the screen, then click Save.",
        status=RunStatus.RUNNING,
    )

    assert AgentHarness._is_observation_only_request(run) is False


@pytest.mark.asyncio
async def test_read_only_prefix_does_not_hide_a_later_save_request() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start(
        "Read-only check: describe the current screen. Save the document."
    )

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
        "verifier",
    ]
    assert len(computer.bursts) == 1
    assert not any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_read_only_prefix_does_not_hide_an_afterward_click() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start(
        "Read-only check: describe the screen; after describing it, click Save."
    )

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
        "verifier",
    ]
    assert len(computer.bursts) == 1
    assert not any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_read_only_prefix_does_not_hide_a_contradictory_input_request() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start(
        "Read-only check: describe the screen, but also press Escape."
    )

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
        "verifier",
    ]
    assert len(computer.bursts) == 1
    assert not any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_read_only_prefix_does_not_hide_an_input_before_description() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start(
        "Read-only check: click the window and describe the screen."
    )

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
        "verifier",
    ]
    assert len(computer.bursts) == 1
    assert not any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_read_only_prefix_does_not_hide_a_selection_request() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start(
        "Read-only check: describe the screen and select the first row."
    )

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
        "verifier",
    ]
    assert len(computer.bursts) == 1
    assert not any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_model_phase_is_durable_while_provider_is_still_running() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider(ScriptedProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.role == "reasoner":
                entered.set()
                await release.wait()
            return await super().complete(request)

    provider = BlockingProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)
    created = await harness.create("Type hello world in the open editor.")

    continuation = asyncio.create_task(harness.continue_run(created.run_id))
    await asyncio.wait_for(entered.wait(), timeout=0.5)
    summary = await harness.store.get_summary(created.run_id)

    assert summary.active_activity is not None
    assert summary.active_activity.kind == "model"
    assert summary.active_activity.phase == "request_sent"
    assert summary.active_activity.role == "reasoner"
    assert summary.active_activity.provider == provider.name

    release.set()
    await asyncio.wait_for(continuation, timeout=1)


@pytest.mark.asyncio
async def test_chat_workspace_previews_exact_checkpoint_before_hid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)
    harness.config = HarnessConfig(
        max_actions_per_advance=1,
        interactive_action_preview_ms=300,
    )
    delays: list[float] = []

    async def record_delay(seconds: float) -> None:
        assert computer.bursts == []
        delays.append(seconds)

    monkeypatch.setattr(
        "pikvm_agent.harness.agent.asyncio.sleep",
        record_delay,
    )
    created = await harness.create(
        "Type hello world in the open editor.",
        caller={"interface": "chat_workspace", "label": "chat-workspace"},
    )
    result = await harness.continue_run(created.run_id)

    assert delays == [0.3]
    kinds = [event.kind for event in result.events]
    assert kinds.index("action.checkpointed") < kinds.index(
        "action.preview_window_opened"
    )
    assert kinds.index("action.preview_window_opened") < kinds.index(
        "action.attempted"
    )
    assert computer.bursts


@pytest.mark.asyncio
async def test_run_uses_independent_durable_routes_for_each_model_role() -> None:
    strong = ScriptedProvider()
    strong.name = "strong-model"
    fast = ScriptedProvider()
    fast.name = "fast-model"
    pool = ModelPool(
        providers={strong.name: strong, fast.name: fast},
        routes={
            role: RoleRoute(providers=[fast.name, strong.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    harness = AgentHarness(
        computer=FakeComputer(),
        models=pool,
        store=InMemoryRunStore(),
        config=HarnessConfig(max_actions_per_advance=1),
    )
    route = RunModelRoute(
        reasoner=[strong.name, fast.name],
        controller=[fast.name, strong.name],
        verifier=[strong.name, fast.name],
    )

    created = await harness.create(
        "Type hello world in the open editor.",
        model_route=route,
    )
    result = await harness.continue_run(created.run_id)

    assert result.status is RunStatus.COMPLETED
    assert result.model_route == route
    assert [request.role for request in strong.requests] == [
        "reasoner",
        "verifier",
    ]
    assert [request.role for request in fast.requests] == ["controller"]
    started = [
        event
        for event in result.events
        if event.kind == "model.started"
    ]
    assert [event.data["candidates"] for event in started] == [
        ["strong-model", "fast-model"],
        ["fast-model", "strong-model"],
        ["strong-model", "fast-model"],
    ]


@pytest.mark.asyncio
async def test_operator_steering_is_durable_and_forces_a_fresh_plan() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    created = await harness.create("Type hello world in the open editor.")
    created.plan = PlanDecision(
        summary="Stale plan",
        steps=["Use the current editor"],
        success_criteria=["Old completion criterion"],
    )
    await harness.store.save(created)

    steered = await harness.steer(
        created.run_id,
        "Use the already-open document and preserve its heading.",
    )

    assert steered.status is RunStatus.PAUSED
    assert steered.plan is None
    assert steered.operator_guidance == [
        "Use the already-open document and preserve its heading."
    ]
    assert steered.events[-1].kind == "run.steered"
    assert steered.active_activity is None

    completed = await harness.continue_run(created.run_id)

    assert completed.status is RunStatus.COMPLETED
    reasoner_prompt = provider.requests[0].prompt
    assert "operator_guidance" in reasoner_prompt
    assert "preserve its heading" in reasoner_prompt


@pytest.mark.asyncio
async def test_operator_steering_cannot_discard_an_unsettled_action() -> None:
    store = InMemoryRunStore()
    run = RunSnapshot(
        run_id="unsettled-action",
        task="Edit the document",
        status=RunStatus.RUNNING,
        pending_action=PendingAction(
            index=0,
            intent="Type exact text",
            actions=[{"type": "type_text", "text": "hello"}],
            based_on_world_version=1,
            based_on_control_epoch=1,
            idempotency_key="unsettled-action:action:0:digest",
        ),
    )
    await store.save(run)
    harness = AgentHarness(
        computer=object(),  # type: ignore[arg-type]
        models=object(),  # type: ignore[arg-type]
        store=store,
    )

    with pytest.raises(ValueError, match="pending action must settle"):
        await harness.steer(run.run_id, "Change direction")

    unchanged = await store.get(run.run_id)
    assert unchanged.pending_action is not None
    assert unchanged.operator_guidance == []


@pytest.mark.asyncio
async def test_provider_attempt_budget_pauses_before_any_hid() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            role: RoleRoute(providers=[provider.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    harness = AgentHarness(
        computer=computer,
        models=pool,
        store=InMemoryRunStore(),
        config=HarnessConfig(
            max_actions_per_advance=1,
            max_provider_attempts_per_run=1,
        ),
    )

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.PAUSED
    assert [request.role for request in provider.requests] == ["reasoner"]
    assert computer.bursts == []
    assert result.model_budget.provider_attempts == 1
    assert result.model_budget.provider_attempt_limit == 1
    assert result.model_budget.max_cost_microusd is None
    assert result.error == "model provider attempt budget exhausted"
    assert result.events[-1].kind == "model.budget_exhausted"


@pytest.mark.asyncio
async def test_provider_fallback_cannot_bypass_the_run_attempt_budget() -> None:
    primary = ControllerUnavailableProvider()
    fallback = ScriptedProvider()
    computer = FakeComputer()
    pool = ModelPool(
        providers={primary.name: primary, fallback.name: fallback},
        routes={
            "reasoner": RoleRoute(providers=[fallback.name]),
            "controller": RoleRoute(providers=[primary.name, fallback.name]),
            "verifier": RoleRoute(providers=[fallback.name]),
        },
        failure_cooldowns={primary.name: 0.0},
    )
    harness = AgentHarness(
        computer=computer,
        models=pool,
        store=InMemoryRunStore(),
        config=HarnessConfig(
            max_actions_per_advance=1,
            max_provider_attempts_per_run=2,
        ),
    )

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.PAUSED
    assert [request.role for request in fallback.requests] == ["reasoner"]
    assert [request.role for request in primary.requests] == ["controller"]
    assert result.model_budget.provider_attempts == 2
    assert computer.bursts == []
    assert result.events[-1].kind == "model.budget_exhausted"


@pytest.mark.asyncio
async def test_schema_repair_cannot_bypass_the_run_attempt_budget() -> None:
    provider = InvalidThenRepairedControllerProvider()
    computer = FakeComputer()
    harness = AgentHarness(
        computer=computer,
        models=ModelPool(
            providers={provider.name: provider},
            routes={
                role: RoleRoute(providers=[provider.name])
                for role in ("reasoner", "controller", "verifier")
            },
        ),
        store=InMemoryRunStore(),
        config=HarnessConfig(
            max_actions_per_advance=1,
            max_provider_attempts_per_run=2,
        ),
    )

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.PAUSED
    assert provider.controller_calls == 1
    assert result.model_budget.provider_attempts == 2
    assert computer.bursts == []
    assert result.events[-1].kind == "model.budget_exhausted"


@pytest.mark.asyncio
async def test_metered_cost_budget_blocks_the_next_model_before_hid() -> None:
    provider = MeteredProvider()
    computer = FakeComputer()
    harness = AgentHarness(
        computer=computer,
        models=ModelPool(
            providers={provider.name: provider},
            routes={
                role: RoleRoute(providers=[provider.name])
                for role in ("reasoner", "controller", "verifier")
            },
        ),
        store=InMemoryRunStore(),
        config=HarnessConfig(max_actions_per_advance=1),
        budget_policy=ModelBudgetPolicy(
            max_provider_attempts=100,
            max_cost_microusd=100,
            pricing_version="test-prices-v1",
            provider_costs={
                provider.name: ProviderCostTerms.metered(
                    reservation_microusd=60,
                    usage_usd_per_million={
                        "input_tokens": "2.00",
                        "output_tokens": "8.00",
                    },
                )
            },
        ),
    )

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.PAUSED
    assert [request.role for request in provider.requests] == ["reasoner"]
    assert result.model_budget.provider_attempts == 1
    assert result.model_budget.provider_attempt_limit == 100
    assert result.model_budget.committed_cost_microusd == 60
    assert result.model_budget.max_cost_microusd == 100
    assert result.model_budget.pricing_version == "test-prices-v1"
    assert result.model_budget.outstanding_cost_microusd == 0
    assert result.error == "model cost budget exhausted"
    assert computer.bursts == []


@pytest.mark.asyncio
async def test_metered_provider_without_usage_pauses_before_hid() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = AgentHarness(
        computer=computer,
        models=ModelPool(
            providers={provider.name: provider},
            routes={
                role: RoleRoute(providers=[provider.name])
                for role in ("reasoner", "controller", "verifier")
            },
        ),
        store=InMemoryRunStore(),
        config=HarnessConfig(max_actions_per_advance=1),
        budget_policy=ModelBudgetPolicy(
            max_provider_attempts=100,
            max_cost_microusd=1_000,
            provider_costs={
                provider.name: ProviderCostTerms.metered(
                    reservation_microusd=60,
                    usage_usd_per_million={"input_tokens": "2.00"},
                )
            },
        ),
    )

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.PAUSED
    assert [request.role for request in provider.requests] == ["reasoner"]
    assert result.model_budget.committed_cost_microusd == 60
    assert result.model_budget.outstanding_cost_microusd == 0
    assert result.error == "model usage report missing for metered provider"
    assert computer.bursts == []


@pytest.mark.asyncio
async def test_actual_cost_over_reservation_pauses_before_hid() -> None:
    provider = MeteredProvider()
    computer = FakeComputer()
    harness = AgentHarness(
        computer=computer,
        models=ModelPool(
            providers={provider.name: provider},
            routes={
                role: RoleRoute(providers=[provider.name])
                for role in ("reasoner", "controller", "verifier")
            },
        ),
        store=InMemoryRunStore(),
        config=HarnessConfig(max_actions_per_advance=1),
        budget_policy=ModelBudgetPolicy(
            max_provider_attempts=100,
            max_cost_microusd=50,
            provider_costs={
                provider.name: ProviderCostTerms.metered(
                    reservation_microusd=40,
                    usage_usd_per_million={
                        "input_tokens": "2.00",
                        "output_tokens": "8.00",
                    },
                )
            },
        ),
    )

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.PAUSED
    assert result.model_budget.committed_cost_microusd == 60
    assert result.error == "model cost budget exhausted after provider settlement"
    assert computer.bursts == []


@pytest.mark.asyncio
async def test_pointer_only_noop_is_rejected_before_hid() -> None:
    provider = PointerOnlyControllerProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start("Inspect the terminal and continue the task.")

    assert result.status is RunStatus.PAUSED
    assert result.pending_action is None
    assert computer.bursts == []
    assert result.events[-1].kind == "controller.pointer_noop_rejected"


@pytest.mark.asyncio
async def test_managed_harness_blocks_if_target_identity_changes() -> None:
    provider = ScriptedProvider()
    harness = build_harness(provider, TargetSwitchComputer())

    result = await harness.start("Type hello world in the open editor.")
    continued = await harness.continue_run(result.run_id)

    assert result.status is RunStatus.BLOCKED
    assert result.error == "target identity changed during computer action"
    assert result.events[-1].kind == "target.identity_changed"
    assert result.events[-1].data["previous_fingerprint"] == (
        "target:aaaaaaaaaaaaaaaa"
    )
    assert result.events[-1].data["current_fingerprint"] == (
        "target:bbbbbbbbbbbbbbbb"
    )
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
    ]
    assert continued.status is RunStatus.BLOCKED


@pytest.mark.asyncio
async def test_approval_escapes_the_model_loop_and_only_exact_human_resume_executes() -> None:
    provider = ScriptedProvider()
    computer = ApprovalComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Draft and send a short message.")

    assert paused.status is RunStatus.NEEDS_APPROVAL
    assert paused.pending_approval is not None
    assert paused.pending_approval["approval_id"] == "approval_1"
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
    ]
    assert computer.resolutions == []
    with pytest.raises(ValueError, match="does not match"):
        await harness.resolve_approval(
            paused.run_id, "wrong_id", {"type": "approve"}
        )

    completed = await harness.resolve_approval(
        paused.run_id, "approval_1", {"type": "approve"}
    )

    assert completed.status is RunStatus.COMPLETED
    assert computer.resolutions == [
        {
            "session_id": "s_1",
            "approval_id": "approval_1",
            "decision": {"type": "approve"},
        }
    ]
    assert [request.role for request in provider.requests][-1] == "verifier"
    assert any(
        event.kind == "verification.evidence_refreshed"
        for event in completed.events
    )
    assert completed.observation is not None
    assert completed.observation.image_path == "/tmp/frame-refreshed.jpg"


@pytest.mark.asyncio
async def test_ungrounded_navigation_is_rejected_and_replanned_not_approved() -> None:
    provider = ScriptedProvider()
    computer = UngroundedNavigationComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Focus the visible search field.")

    assert paused.status is RunStatus.PAUSED
    assert paused.pending_action is None
    assert paused.pending_approval is None
    assert paused.observation is not None
    assert paused.session_id == "s_2"
    assert paused.observation.frame_id == 2
    assert computer.opens == 2
    assert computer.resolutions == [
        {
            "session_id": "s_1",
            "approval_id": "unknown_click_1",
            "decision": {
                "type": "reject",
                "reason": (
                    "managed harness rejected an ungrounded navigation "
                    "proposal"
                ),
            },
        }
    ]
    assert paused.events[-1].kind == "action.ungrounded_refreshed"
    assert not any(event.kind == "approval.required" for event in paused.events)
    assert harness._trajectory_signals(paused)[
        "ungrounded_navigation_replans"
    ] == 1


@pytest.mark.asyncio
async def test_repeated_ungrounded_click_is_repaired_before_more_hid() -> None:
    provider = RepeatedUngroundedThenKeyboardProvider()
    computer = UngroundedThenKeyboardComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Minimize the visible obstructing windows.")
    completed = await harness.continue_run(paused.run_id)

    assert paused.status is RunStatus.PAUSED
    assert completed.status is RunStatus.COMPLETED
    assert provider.controller_calls == 3
    assert [burst["actions"] for burst in computer.bursts] == [
        [{"type": "click", "x": 705, "y": 94, "button": "left"}],
        [{"type": "key", "keys": ["META", "M"]}],
    ]
    controller_prompts = [
        request.prompt
        for request in provider.requests
        if request.role == "controller"
    ]
    assert '"last_ungrounded_navigation": {' in controller_prompts[1]
    assert '"x": 705' in controller_prompts[1]
    assert '"controller_feedback": {' in controller_prompts[2]
    assert "was already rejected before HID" in controller_prompts[2]
    assert any(
        event.kind == "controller.ungrounded_repeat_rejected"
        for event in completed.events
    )


@pytest.mark.asyncio
async def test_distinct_ungrounded_targets_use_bounded_replan_budget() -> None:
    provider = DistinctUngroundedThenKeyboardProvider()
    computer = UngroundedThenKeyboardComputer()
    harness = build_harness(provider, computer)

    first = await harness.start("Reveal and focus Microsoft Word.")
    second = await harness.continue_run(first.run_id)
    completed = await harness.continue_run(second.run_id)

    assert first.status is RunStatus.PAUSED
    assert second.status is RunStatus.PAUSED
    assert completed.status is RunStatus.COMPLETED
    assert computer.opens == 3
    assert [burst["actions"] for burst in computer.bursts] == [
        [{"type": "click", "x": 705, "y": 94, "button": "left"}],
        [{"type": "click", "x": 620, "y": 660, "button": "left"}],
        [{"type": "key", "keys": ["META", "M"]}],
    ]
    controller_prompts = [
        request.prompt
        for request in provider.requests
        if request.role == "controller"
    ]
    assert '"ungrounded_navigation_history": [' in controller_prompts[2]
    assert '"x": 705' in controller_prompts[2]
    assert '"x": 620' in controller_prompts[2]
    assert sum(
        event.kind == "action.ungrounded_refreshed"
        for event in completed.events
    ) == 2


@pytest.mark.asyncio
async def test_ungrounded_replan_budget_exhaustion_stays_fail_closed() -> None:
    provider = DistinctUngroundedThenKeyboardProvider()
    computer = UngroundedThenKeyboardComputer()
    harness = build_harness(provider, computer)
    harness.config = HarnessConfig(
        max_actions_per_advance=1,
        max_ungrounded_navigation_replans=1,
    )

    first = await harness.start("Reveal and focus Microsoft Word.")
    blocked = await harness.continue_run(first.run_id)

    assert first.status is RunStatus.PAUSED
    assert blocked.status is RunStatus.BLOCKED
    assert blocked.error == (
        "click targets could not be independently grounded after the "
        "bounded navigation replan budget"
    )
    assert computer.opens == 2
    assert blocked.events[-1].kind == "action.ungrounded_budget_exhausted"
    assert blocked.events[-1].data["recovery_limit"] == 1


@pytest.mark.asyncio
async def test_rejecting_approval_closes_the_underlying_computer_session() -> None:
    provider = ScriptedProvider()
    computer = ApprovalComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Draft and send a short message.")
    rejected = await harness.resolve_approval(
        paused.run_id,
        "approval_1",
        {"type": "reject", "reason": "Do not send"},
    )

    assert rejected.status is RunStatus.REJECTED
    assert rejected.pending_action is None
    assert rejected.pending_approval is None
    assert rejected.observation is not None
    assert rejected.observation.status == "aborted"
    assert computer.aborts == [
        {
            "session_id": "s_1",
            "reason": "approval rejected by operator",
        }
    ]
    assert any(
        event.kind == "computer.aborted_after_rejection"
        for event in rejected.events
    )


@pytest.mark.asyncio
async def test_ambiguous_transport_retry_reuses_checkpointed_action_and_key() -> None:
    provider = ScriptedProvider()
    computer = FlakyComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Type hello world in the open editor.")

    assert paused.status is RunStatus.PAUSED
    assert paused.pending_action is not None
    checkpointed_key = paused.pending_action.idempotency_key
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
    ]

    completed = await harness.continue_run(paused.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert computer.keys == [checkpointed_key, checkpointed_key]
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
        "verifier",
    ]
    attempts = [
        event for event in completed.events if event.kind == "action.attempted"
    ]
    outcomes = [
        event
        for event in completed.events
        if event.kind in {"action.transport_uncertain", "action.completed"}
    ]
    assert [event.data["call_id"] for event in attempts] == [
        f"{checkpointed_key}:attempt:1",
        f"{checkpointed_key}:attempt:2",
    ]
    assert [event.data["call_id"] for event in outcomes] == [
        event.data["call_id"] for event in attempts
    ]
    assert all(event.data["tool"] == "pikvm_run_burst" for event in outcomes)
    assert all(event.data["latency_ms"] >= 0 for event in outcomes)


@pytest.mark.asyncio
async def test_pause_retains_a_checkpointed_action_for_idempotent_resume() -> None:
    provider = ScriptedProvider()
    computer = FlakyComputer()
    harness = build_harness(provider, computer)

    paused_after_ambiguity = await harness.start(
        "Type hello world in the open editor."
    )
    checkpointed = paused_after_ambiguity.pending_action
    assert checkpointed is not None

    paused_by_operator = await harness.pause(
        paused_after_ambiguity.run_id, "operator requested pause"
    )

    assert paused_by_operator.status is RunStatus.PAUSED
    assert paused_by_operator.pending_action == checkpointed
    assert paused_by_operator.events[-1].kind == "run.paused"
    assert paused_by_operator.events[-1].data["source"] == "operator"


@pytest.mark.asyncio
async def test_all_provider_failure_pauses_before_hid_and_can_resume() -> None:
    provider = TemporarilyUnavailableProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Type hello world in the open editor.")

    assert paused.status is RunStatus.PAUSED
    assert paused.events[-1].kind == "model.failed"
    assert computer.bursts == []

    completed = await harness.continue_run(paused.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert len(computer.bursts) == 1


@pytest.mark.asyncio
async def test_model_blocked_run_can_replan_and_resume_without_hid_replay() -> None:
    provider = InitiallyBlockedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    blocked = await harness.start("Type hello world in the open editor.")

    assert blocked.status is RunStatus.BLOCKED
    assert computer.bursts == []

    completed = await harness.continue_run(blocked.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert len(computer.bursts) == 1
    controller_prompts = [
        item.prompt for item in provider.requests if item.role == "controller"
    ]
    normalized_prompt = " ".join(controller_prompts[-1].split())
    assert "Do not wait for human approval" in normalized_prompt


@pytest.mark.asyncio
async def test_definitive_typing_failure_pauses_for_replan_with_new_action_index() -> None:
    provider = ScriptedProvider()
    computer = FocusLostComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Type hello world in the open editor.")

    assert paused.status is RunStatus.PAUSED
    assert paused.plan is None
    assert paused.pending_action is None
    assert paused.next_action_index == 1
    assert paused.events[-1].kind == "action.recoverable_failure"


@pytest.mark.asyncio
async def test_daemon_unverified_typing_cannot_be_overridden_by_model_verifier() -> None:
    provider = ScriptedProvider()
    computer = UnverifiedTypingComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Type hello world in the open editor.")

    assert paused.status is RunStatus.PAUSED
    assert paused.plan is None
    assert paused.pending_action is None
    assert paused.next_action_index == 1
    assert any(
        event.kind == "action.completed_unverified"
        for event in paused.events
    )
    assert not any(request.role == "verifier" for request in provider.requests)


@pytest.mark.asyncio
async def test_action_event_exposes_only_bounded_input_readback_receipts() -> None:
    provider = ScriptedProvider()
    harness = build_harness(provider, InputReceiptComputer())

    completed = await harness.start("Type hello world in the open editor.")

    event = next(
        event for event in completed.events if event.kind == "action.completed"
    )
    assert event.data["input_receipts"] == [
        {
            "index": 0,
            "type": "type_text",
            "status": "verified_exact",
            "verdict": "match",
            "observed_text": "hello world",
            "observed_text_redacted": False,
            "requested_characters": 11,
            "issued_characters": 11,
            "observed_characters": 11,
            "correction_count": 1,
            "delivery_retries": 0,
            "used_fast_path": False,
            "summary": "Typed and verified.",
            "edit_distance": 0,
            "focus_evidence": "read_back_verified",
            "requested_sha256": "a" * 64,
            "issued_prefix_sha256": "a" * 64,
            "readback_sha256": "a" * 64,
            "exact_readback_sha256_match": True,
            "proof_state": "exact_ocr_readback",
        }
    ]
    assert "private_path" not in repr(event.data["input_receipts"])
    assert "unknown" not in repr(event.data["input_receipts"])
    assert harness._recent_input_delivery(completed) == [
        {
            "action_index": 0,
            "input_index": 0,
            "status": "verified_exact",
            "issued_characters": 11,
            "requested_characters": 11,
            "sender_finished": True,
            "readback_exact": True,
            "readback_available": True,
        }
    ]


def test_recent_input_delivery_distinguishes_transport_from_screen_proof() -> None:
    run = RunSnapshot(
        run_id="invisible-whitespace-receipt",
        task="Replace two spaces with one",
        status=RunStatus.PAUSED,
    )
    run.record(
        "action.completed_unverified",
        index=4,
        input_receipts=[
            {
                "index": 0,
                "status": "unverified_ambiguous",
                "issued_characters": 2,
                "requested_characters": 2,
                "requested_sha256": "a" * 64,
                "issued_prefix_sha256": "a" * 64,
                "readback_sha256": "b" * 64,
                "exact_readback_sha256_match": False,
                "observed_text": "",
            }
        ],
    )

    assert AgentHarness._recent_input_delivery(run) == [
        {
            "action_index": 4,
            "input_index": 0,
            "status": "unverified_ambiguous",
            "issued_characters": 2,
            "requested_characters": 2,
            "sender_finished": True,
            "readback_exact": False,
            "readback_available": False,
        }
    ]


def test_unverified_terminal_draft_blocks_suffixes_and_execution_until_cancelled() -> None:
    run = RunSnapshot(
        run_id="unverified-terminal-draft",
        task="Disable the dim-screen setting",
        status=RunStatus.PAUSED,
    )
    run.record(
        "action.checkpointed",
        index=5,
        actions=[
            {
                "type": "type_text",
                "text": (
                    "gsettings set "
                    "org.gnome.settings-daemon.plugins.power idle-dim false"
                ),
                "code": True,
                "context": "terminal",
            }
        ],
    )
    run.record(
        "action.completed_unverified",
        index=5,
        input_receipts=[
            {
                "index": 0,
                "status": "unverified_ambiguous",
                "issued_characters": 68,
                "requested_characters": 68,
                "requested_sha256": "a" * 64,
                "issued_prefix_sha256": "a" * 64,
                "exact_readback_sha256_match": False,
                "observed_text": "",
            }
        ],
    )

    assert AgentHarness._unsafe_unverified_terminal_followup(
        run,
        [{"type": "type_text", "text": "se", "code": True, "context": "terminal"}],
    )
    assert AgentHarness._unsafe_unverified_terminal_followup(
        run,
        [{"type": "key", "keys": ["ENTER"]}],
    )
    assert not AgentHarness._unsafe_unverified_terminal_followup(
        run,
        [{"type": "key", "keys": ["CTRL", "C"]}],
    )
    assert not AgentHarness._unsafe_unverified_terminal_followup(
        run,
        [{"type": "key", "keys": ["META", "ARROWUP"]}],
    )

    run.record(
        "action.checkpointed",
        index=6,
        actions=[{"type": "key", "keys": ["ctrl+c"]}],
    )
    run.record("action.completed", index=6)

    assert not AgentHarness._unsafe_unverified_terminal_followup(
        run,
        [
            {
                "type": "type_text",
                "text": (
                    "gsettings set "
                    "org.gnome.settings-daemon.plugins.power idle-dim false"
                ),
                "code": True,
                "context": "terminal",
            }
        ],
    )


def test_long_terminal_draft_requires_a_verified_legibility_step() -> None:
    run = RunSnapshot(
        run_id="long-terminal-legibility",
        task="Disable the dim-screen setting",
        status=RunStatus.PAUSED,
    )
    proposed = [
        {
            "type": "type_text",
            "text": (
                "gsettings set "
                "org.gnome.settings-daemon.plugins.power idle-dim false"
            ),
            "code": True,
            "context": "terminal",
        }
    ]

    assert AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )
    assert not AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        [
            {
                "type": "type_text",
                "text": "gsettings list-schemas",
                "code": True,
                "context": "terminal",
            }
        ],
    )

    run.record(
        "action.checkpointed",
        index=4,
        intent="Maximize the terminal before entering the exact command.",
        actions=[{"type": "key", "keys": ["META", "UP"]}],
    )
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="The terminal is visibly maximized and the clean prompt is legible.",
    )

    assert AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )

    run.record(
        "action.checkpointed",
        index=5,
        intent="Increase the terminal text size before entering the exact command.",
        actions=[{"type": "key", "keys": ["CTRL", "SHIFT", "EQUAL"]}],
    )
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="The terminal text is visibly zoomed in and larger.",
    )

    assert not AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )

    run.record(
        "action.checkpointed",
        index=6,
        intent=(
            "Open the terminal's hamburger menu to find the zoom-in control."
        ),
        actions=[{"type": "click", "x": 1179, "y": 33}],
    )
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary=(
            "The terminal menu opened and shows the zoom controls."
        ),
    )

    assert not AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )

    run.record(
        "action.checkpointed",
        index=7,
        intent="Type the exact command for visual verification.",
        actions=proposed,
    )
    run.record(
        "action.completed_unverified",
        index=7,
        status="unverified",
        input_receipts=[
            {
                "index": 0,
                "status": "unverified_ambiguous",
                "exact_readback_sha256_match": False,
            }
        ],
    )

    assert AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )

    run.record(
        "action.checkpointed",
        index=8,
        intent="Increase the terminal text size after the unreadable draft.",
        actions=[{"type": "key", "keys": ["CTRL", "SHIFT", "EQUAL"]}],
    )
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="The terminal is zoomed in and the clean prompt remains visible.",
    )

    assert not AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )

    run.record(
        "action.checkpointed",
        index=9,
        intent="Open a new terminal window.",
        actions=[{"type": "key", "keys": ["CTRL", "ALT", "T"]}],
    )
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="A new terminal opened at its default narrow width.",
    )

    assert AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )


@pytest.mark.asyncio
async def test_long_terminal_draft_is_replaced_with_legibility_action_before_hid() -> None:
    class LegibilityProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__()
            self.controller_calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.role == "controller":
                self.requests.append(request)
                self.controller_calls += 1
                actions = (
                    [
                        {
                            "type": "type_text",
                            "text": (
                                "gsettings set "
                                "org.gnome.settings-daemon.plugins.power "
                                "idle-dim false"
                            ),
                            "code": True,
                            "context": "terminal",
                            "verification": "exact",
                        }
                    ]
                    if self.controller_calls == 1
                    else [
                        {
                            "type": "key",
                            "keys": ["CTRL", "SHIFT", "EQUAL"],
                        }
                    ]
                )
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "outcome": "act",
                        "intent": (
                            "Type the exact setting command."
                            if self.controller_calls == 1
                            else (
                                "Increase the terminal text size before "
                                "typing."
                            )
                        ),
                        "actions": actions,
                        "expected_evidence": [
                            "The terminal is visibly maximized and legible."
                        ],
                    },
                )
            if request.role == "verifier":
                self.requests.append(request)
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "verdict": "verified",
                        "summary": (
                            "The terminal text is visibly zoomed in and the "
                            "prompt is legible."
                        ),
                        "evidence": ["The terminal text is visibly larger."],
                    },
                )
            return await super().complete(request)

    provider = LegibilityProvider()
    computer = FakeComputer()
    store = InMemoryRunStore()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            role: RoleRoute(providers=[provider.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    harness = AgentHarness(
        computer=computer,
        models=pool,
        store=store,
        config=HarnessConfig(max_actions_per_advance=1),
    )
    run = RunSnapshot(
        run_id="guard-long-terminal-draft",
        task="Disable the dim-screen setting",
        status=RunStatus.PAUSED,
        session_id="s_1",
        observation=await computer.open("legibility-test"),
        plan=PlanDecision(
            summary="Disable the requested setting.",
            steps=["Enter the exact local setting command."],
            success_criteria=["The dim-screen setting is off."],
            constraints=["Preserve unrelated settings."],
        ),
    )
    run.record(
        "action.checkpointed",
        index=4,
        intent="Maximize the terminal before entering the exact command.",
        actions=[{"type": "key", "keys": ["META", "UP"]}],
    )
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="The terminal is visibly maximized and legible.",
    )
    run.record(
        "action.checkpointed",
        index=5,
        intent="Increase the terminal text size before entering the exact command.",
        actions=[{"type": "key", "keys": ["CTRL", "SHIFT", "EQUAL"]}],
    )
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="The terminal text is visibly zoomed in and larger.",
    )
    run.record(
        "action.checkpointed",
        index=6,
        intent="Type the exact command for visual verification.",
        actions=[
            {
                "type": "type_text",
                "text": (
                    "gsettings set "
                    "org.gnome.settings-daemon.plugins.power idle-dim false"
                ),
                "code": True,
                "context": "terminal",
                "verification": "exact",
            }
        ],
    )
    run.record(
        "action.completed_unverified",
        index=6,
        status="unverified",
        input_receipts=[
            {
                "index": 0,
                "status": "unverified_ambiguous",
                "exact_readback_sha256_match": False,
            }
        ],
    )
    await store.save(run)

    result = await harness.continue_run(run.run_id)

    assert provider.controller_calls == 2
    assert [burst["actions"] for burst in computer.bursts] == [
        [{"type": "key", "keys": ["CTRL", "SHIFT", "EQUAL"]}]
    ], [(event.kind, event.data) for event in result.events[-8:]]
    assert any(
        event.kind == "controller.long_terminal_draft_rejected"
        for event in result.events
    )
    controller_prompts = [
        request.prompt
        for request in provider.requests
        if request.role == "controller"
    ]
    assert '"controller_feedback": {' in controller_prompts[1]
    assert "Do not type any text yet" in controller_prompts[1]
    assert "increase the terminal text size" in controller_prompts[1]


@pytest.mark.asyncio
async def test_unverified_terminal_suffix_is_replaced_with_cancel_before_hid() -> None:
    class RecoveryProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__()
            self.controller_calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.role == "controller":
                self.requests.append(request)
                self.controller_calls += 1
                action = (
                    {
                        "type": "type_text",
                        "text": "se",
                        "code": True,
                        "context": "terminal",
                    }
                    if self.controller_calls == 1
                    else {"type": "key", "keys": ["CTRL", "C"]}
                )
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "outcome": "act",
                        "intent": "Recover the unread terminal draft safely.",
                        "actions": [action],
                        "expected_evidence": ["A clean terminal prompt is visible."],
                    },
                )
            if request.role == "verifier":
                self.requests.append(request)
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "verdict": "failed",
                        "summary": "The task still needs a clean command entry.",
                        "evidence": ["The draft was cancelled safely."],
                    },
                )
            return await super().complete(request)

    provider = RecoveryProvider()
    computer = FakeComputer()
    store = InMemoryRunStore()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            role: RoleRoute(providers=[provider.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    harness = AgentHarness(
        computer=computer,
        models=pool,
        store=store,
        config=HarnessConfig(max_actions_per_advance=1),
    )
    run = RunSnapshot(
        run_id="recover-unverified-terminal-draft",
        task="Disable the dim-screen setting",
        status=RunStatus.PAUSED,
        session_id="s_1",
        observation=await computer.open("recovery-test"),
        plan=PlanDecision(
            summary="Disable the requested setting.",
            steps=["Enter the exact local setting command."],
            success_criteria=["The dim-screen setting is off."],
            constraints=["Preserve unrelated settings."],
        ),
    )
    run.record(
        "action.checkpointed",
        index=5,
        actions=[
            {
                "type": "type_text",
                "text": (
                    "gsettings set "
                    "org.gnome.settings-daemon.plugins.power idle-dim false"
                ),
                "code": True,
                "context": "terminal",
            }
        ],
    )
    run.record(
        "action.completed_unverified",
        index=5,
        input_receipts=[
            {
                "index": 0,
                "issued_characters": 68,
                "requested_characters": 68,
                "requested_sha256": "a" * 64,
                "issued_prefix_sha256": "a" * 64,
                "exact_readback_sha256_match": False,
            }
        ],
    )
    await store.save(run)

    result = await harness.continue_run(run.run_id)

    assert provider.controller_calls == 2
    assert [burst["actions"] for burst in computer.bursts] == [
        [{"type": "key", "keys": ["CTRL", "C"]}]
    ]
    controller_prompts = [
        request.prompt
        for request in provider.requests
        if request.role == "controller"
    ]
    assert '"controller_feedback": {' in controller_prompts[1]
    assert "Do not append text and do not execute the draft" in controller_prompts[1]
    assert any(
        event.kind == "controller.unverified_terminal_followup_rejected"
        for event in result.events
    )


def test_recent_verified_actions_keep_bounded_durable_task_evidence() -> None:
    run = RunSnapshot(
        run_id="durable-verification-memory",
        task="Save and reopen the workbook",
        status=RunStatus.PAUSED,
    )
    run.record(
        "action.checkpointed",
        index=3,
        intent="Select B8 and inspect its stored formula.",
        actions=[{"type": "click", "x": 100, "y": 200}],
    )
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="B8 contains =SUM(B4:B7), not a typed constant.",
    )
    run.record(
        "action.checkpointed",
        index=4,
        intent="Focus the filename field.",
        actions=[{"type": "key", "keys": ["alt+n"]}],
    )
    run.record(
        "model.completed",
        role="verifier",
        verdict="uncertain",
        summary="Focus was not visibly proven.",
    )

    assert AgentHarness._recent_verified_actions(run) == [
        {
            "action_index": 3,
            "intent": "Select B8 and inspect its stored formula.",
            "verdict": "verified",
            "summary": "B8 contains =SUM(B4:B7), not a typed constant.",
        }
    ]


def test_secret_input_receipt_is_redacted_again_at_harness_boundary() -> None:
    receipts = AgentHarness._public_input_receipts(
        {
            "action_receipts": [
                {
                    "index": 0,
                    "type": "type_text",
                    "status": "verified_exact",
                    "observed_text": "maliciously retained secret",
                    "observed_text_redacted": False,
                    "summary": "maliciously retained secret",
                    "typed_characters": 27,
                    "intended_characters": 27,
                    "intended_sha256": "b" * 64,
                    "acknowledged_prefix_sha256": "b" * 64,
                    "observed_sha256": "b" * 64,
                    "exact_sha256_match": True,
                }
            ]
        },
        [{"type": "type_text", "text": "password", "secret": True}],
    )

    assert receipts == [
        {
            "index": 0,
            "type": "type_text",
            "status": "delivered_unverified",
            "verdict": "unverified",
            "focus_evidence": "read_back_not_retained",
            "proof_state": "not_retained",
            "observed_text_redacted": True,
            "requested_characters": 27,
            "issued_characters": 27,
        }
    ]
    assert "secret" not in repr(receipts)


@pytest.mark.asyncio
async def test_continue_recovers_persisted_type_unverified_failure() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)
    legacy = await harness.create("Type hello world in the open editor.")
    legacy.status = RunStatus.FAILED
    legacy.error = "typed text did not change the screen"
    legacy.observation = ComputerObservation(
        session_id=legacy.session_id or "s_1",
        status="failed",
        frame_id=2,
        world_version=7,
        control_epoch=2,
        error=legacy.error,
        raw={"reason": "type_unverified"},
    )
    await harness.store.save(legacy)

    completed = await harness.continue_run(legacy.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert completed.next_action_index == 2
    assert computer.bursts[0]["idempotency_key"].startswith(
        f"{legacy.run_id}:action:1:"
    )


@pytest.mark.asyncio
async def test_verifier_failure_pauses_for_correction_instead_of_ending_run() -> None:
    provider = FailingVerifierProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Type hello world in the open editor.")

    assert paused.status is RunStatus.PAUSED
    assert paused.plan is None
    assert paused.next_action_index == 1
    assert paused.last_verification is not None
    assert paused.last_verification.verdict == "failed"
    assert paused.events[-1].kind == "verification.failed"
    verifier_prompt = next(
        request.prompt for request in provider.requests if request.role == "verifier"
    )
    normalized_prompt = " ".join(verifier_prompt.split())
    assert '"last_controller": {' in normalized_prompt
    assert '"intent": "Type the requested text into the already-focused editor."' in (
        normalized_prompt
    )


@pytest.mark.asyncio
async def test_invalid_structured_controller_gets_one_pre_hid_repair_attempt() -> None:
    provider = InvalidThenRepairedControllerProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    completed = await harness.start("Type hello world in the open editor.")

    assert completed.status is RunStatus.COMPLETED
    assert provider.controller_calls == 2
    assert len(computer.bursts) == 1
    controller_requests = [
        request for request in provider.requests if request.role == "controller"
    ]
    assert "YOUR PREVIOUS JSON WAS REJECTED" not in controller_requests[0].prompt
    assert "YOUR PREVIOUS JSON WAS REJECTED" in controller_requests[1].prompt
    assert '"input"' not in controller_requests[1].prompt


@pytest.mark.asyncio
async def test_exact_repeated_action_is_stopped_before_duplicate_hid() -> None:
    provider = StallingControllerProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    first_checkpoint = await harness.start("Type hello world in the open editor.")
    paused = await harness.continue_run(first_checkpoint.run_id)

    assert first_checkpoint.status is RunStatus.PAUSED
    assert any(
        event.kind == "verification.action_rejected"
        for event in first_checkpoint.events
    )
    assert paused.status is RunStatus.PAUSED
    assert paused.error == "controller repeated the previous action unchanged"
    assert paused.next_action_index == 1
    assert len(computer.bursts) == 1
    assert paused.events[-1].kind == "controller.repeated_actions"
    controller_prompts = [
        request.prompt for request in provider.requests if request.role == "controller"
    ]
    assert '"trajectory_signals": {' in controller_prompts[-1]
    assert '"type_text": 1' in controller_prompts[-1]
    assert "visible no results, do not repeat it" in controller_prompts[-1]


@pytest.mark.asyncio
async def test_stale_refusal_requires_fresh_controller_decision_before_retry() -> None:
    provider = ScriptedProvider()
    computer = StaleThenFreshComputer()
    harness = build_harness(provider, computer)

    stale = await harness.start("Type hello world in the open editor.")
    completed = await harness.continue_run(stale.run_id)

    assert stale.status is RunStatus.PAUSED
    assert stale.observation is not None
    assert stale.observation.world_version == 9
    assert stale.pending_action is None
    assert computer.refreshes == 1
    assert len(computer.bursts) == 2
    assert computer.bursts[1]["based_on_world_version"] == 9
    assert (
        computer.bursts[1]["idempotency_key"]
        == computer.bursts[0]["idempotency_key"]
    )
    assert completed.status is RunStatus.COMPLETED
    assert any(event.kind == "action.stale_world_refreshed" for event in stale.events)
    assert not any(
        event.kind == "action.stale_world_retry_checkpointed" for event in stale.events
    )
    assert sum(request.role == "reasoner" for request in provider.requests) == 1
    assert sum(request.role == "controller" for request in provider.requests) == 2


@pytest.mark.asyncio
async def test_stale_refusal_never_rebases_a_commit_key() -> None:
    provider = KeyOnlyControllerProvider()
    computer = StaleThenFreshComputer()
    harness = build_harness(provider, computer)

    stale = await harness.start("Submit the already prepared value.")

    assert stale.status is RunStatus.PAUSED
    assert stale.pending_action is None
    assert computer.refreshes == 1
    assert len(computer.bursts) == 1
    assert any(event.kind == "action.stale_world_refreshed" for event in stale.events)
    assert not any(
        event.kind == "action.stale_world_retry_checkpointed"
        for event in stale.events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "keys",
    [["CTRL", "ALT", "T"], ["SUPER"], ["META"], ["ESC"]],
)
async def test_stale_refusal_requires_fresh_decision_for_global_shortcut(
    keys: list[str],
) -> None:
    provider = GlobalShortcutControllerProvider(keys)
    computer = StaleThenFreshComputer()
    harness = build_harness(provider, computer)

    stale = await harness.start("Open a terminal.")
    completed = await harness.continue_run(stale.run_id)

    assert stale.status is RunStatus.PAUSED
    assert stale.pending_action is None
    assert computer.refreshes == 1
    assert len(computer.bursts) == 2
    assert computer.bursts[1]["based_on_world_version"] == 9
    assert (
        computer.bursts[1]["idempotency_key"]
        == computer.bursts[0]["idempotency_key"]
    )
    assert completed.status is RunStatus.COMPLETED
    assert any(event.kind == "action.stale_world_refreshed" for event in stale.events)
    assert not any(
        event.kind == "action.stale_world_retry_checkpointed" for event in stale.events
    )
    assert sum(request.role == "controller" for request in provider.requests) == 2


@pytest.mark.asyncio
async def test_stale_refusal_requires_fresh_decision_for_navigation_sequence() -> None:
    provider = GlobalShortcutSequenceControllerProvider()
    computer = StaleThenFreshComputer()
    harness = build_harness(provider, computer)

    stale = await harness.start("Exit fullscreen and open the desktop overview.")
    completed = await harness.continue_run(stale.run_id)

    assert stale.status is RunStatus.PAUSED
    assert stale.pending_action is None
    assert len(computer.bursts) == 2
    assert computer.bursts[1]["actions"] == [
        {"type": "key", "keys": ["ESC"]},
        {"type": "key", "keys": ["META"]},
    ]
    assert (
        computer.bursts[1]["idempotency_key"]
        == computer.bursts[0]["idempotency_key"]
    )
    assert completed.status is RunStatus.COMPLETED
    assert sum(request.role == "controller" for request in provider.requests) == 2


@pytest.mark.asyncio
async def test_stale_refusal_never_rebases_focus_dependent_shortcut() -> None:
    provider = GlobalShortcutControllerProvider(["CTRL", "L"])
    computer = StaleThenFreshComputer()
    harness = build_harness(provider, computer)

    stale = await harness.start("Focus the browser address bar.")

    assert stale.status is RunStatus.PAUSED
    assert stale.pending_action is None
    assert len(computer.bursts) == 1
    assert any(event.kind == "action.stale_world_refreshed" for event in stale.events)
    assert not any(
        event.kind == "action.stale_world_retry_checkpointed"
        for event in stale.events
    )


@pytest.mark.asyncio
async def test_failed_text_search_cannot_repeat_after_intervening_focus_action() -> None:
    provider = RepeatedFailedSearchProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    first = await harness.start("Find the requested setting.")
    focused = await harness.continue_run(first.run_id)
    stopped = await harness.continue_run(focused.run_id)

    assert first.status is RunStatus.PAUSED
    assert focused.status is RunStatus.PAUSED
    assert stopped.status is RunStatus.PAUSED
    assert stopped.error == "controller repeated text input after unsuccessful verification"
    assert len(computer.bursts) == 2
    assert stopped.events[-1].kind == "controller.repeated_unsuccessful_text"


@pytest.mark.asyncio
async def test_verifier_receives_labelled_before_after_composite(
    tmp_path: Path,
) -> None:
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    Image.new("RGB", (10, 10), "#ff0000").save(before)
    Image.new("RGB", (10, 10), "#0000ff").save(after)
    provider = ScriptedProvider()
    harness = build_harness(provider, ImageComputer(before, after))

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.COMPLETED
    request = next(item for item in provider.requests if item.role == "verifier")
    assert request.image_path not in {str(before), str(after)}
    assert request.image_path is not None
    composite = Path(request.image_path)
    assert result.latest_verification_image_path == str(composite)
    assert result.latest_verification_image_revision == 1
    assert len(result.verification_images) == 1
    assert result.verification_images[0].revision == 1
    assert result.verification_images[0].action_index == 1
    assert result.verification_images[0].path == str(composite)
    evidence_event = next(
        event
        for event in result.events
        if event.kind == "verification.evidence_captured"
    )
    assert evidence_event.data == {
        "revision": 1,
        "action_index": 1,
        "before_frame_id": 1,
        "after_frame_id": 2,
    }
    assert composite.is_file()
    assert "before-after" in composite.name
    with Image.open(composite) as image:
        assert image.size == (20, 42)
        assert image.getpixel((5, 37))[0] > 240
        assert image.getpixel((15, 37))[2] > 240
    normalized_prompt = " ".join(request.prompt.split())
    assert "left panel is BEFORE" in normalized_prompt
    assert "right panel is AFTER" in normalized_prompt


@pytest.mark.asyncio
async def test_nearby_toggle_retry_is_stopped_before_second_hid() -> None:
    provider = ToggleRetryProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    first = await harness.start("Enable Do Not Disturb.")
    blocked = await harness.continue_run(first.run_id)

    assert first.status is RunStatus.PAUSED
    assert blocked.status is RunStatus.BLOCKED
    assert blocked.error == (
        "unsafe retry of a state-changing toggle after ambiguous verification"
    )
    assert len(computer.bursts) == 1
    assert blocked.next_action_index == 1
    assert blocked.events[-1].kind == "controller.non_idempotent_retry_stopped"


@pytest.mark.asyncio
async def test_contradictory_complete_verdict_cannot_end_the_task() -> None:
    provider = ContradictoryCompletionProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Type hello world in the open editor.")

    assert paused.status is RunStatus.PAUSED
    assert paused.last_verification is not None
    assert paused.last_verification.verdict == "verified"
    rejected = [
        event
        for event in paused.events
        if event.kind == "verification.complete_rejected"
    ]
    assert len(rejected) == 1
    assert "criterion 0" in rejected[0].data["reason"]
    assert len(computer.bursts) == 1


@pytest.mark.asyncio
async def test_rejected_done_decision_forces_a_fresh_plan() -> None:
    provider = RejectedDoneProvider()
    harness = build_harness(provider, FakeComputer())

    paused = await harness.start("Disable the requested setting.")

    assert paused.status is RunStatus.PAUSED
    assert paused.plan is None
    assert [
        event.kind for event in paused.events
    ][-3:] == [
        "verification.complete_rejected",
        "run.replanning_after_incomplete_done",
        "run.paused",
    ]

    paused_again = await harness.continue_run(paused.run_id)

    assert paused_again.status is RunStatus.PAUSED
    assert sum(
        request.role == "reasoner" for request in provider.requests
    ) == 2


def test_verification_schema_requires_per_criterion_assessments() -> None:
    schema = VerificationDecision.model_json_schema()

    assert "criteria" in schema["properties"]
    assert "action_criteria" in schema["properties"]
    assert schema["properties"]["summary"]["maxLength"] == 1_200


def test_verified_action_requires_every_expected_evidence_item() -> None:
    action = PendingAction(
        index=3,
        intent="Inspect B8's stored formula.",
        actions=[{"type": "click", "x": 75, "y": 243}],
        expected_evidence=[
            "The Name Box reads B8.",
            "The formula bar shows =SUM(B4:B7).",
        ],
        based_on_world_version=4,
        based_on_control_epoch=0,
        idempotency_key="run:action:3",
    )
    verified = VerificationDecision(
        verdict="verified",
        summary="B8 and its formula are visibly confirmed.",
        evidence=["B8 is selected and the formula bar is legible."],
        criteria=[],
        action_criteria=[
            {
                "criterion_index": 0,
                "satisfied": True,
                "evidence": "The Name Box visibly reads B8.",
            },
            {
                "criterion_index": 1,
                "satisfied": True,
                "evidence": "The formula bar visibly reads =SUM(B4:B7).",
            },
        ],
    )

    assert (
        AgentHarness._verified_action_rejection_reason(action, verified)
        is None
    )
    assert "expected indexes 0..1" in (
        AgentHarness._verified_action_rejection_reason(
            action,
            verified.model_copy(update={"action_criteria": []}),
        )
        or ""
    )
    assert "expected evidence 1" in (
        AgentHarness._verified_action_rejection_reason(
            action,
            verified.model_copy(
                update={
                    "action_criteria": [
                        verified.action_criteria[0],
                        verified.action_criteria[1].model_copy(
                            update={"satisfied": False}
                        ),
                    ]
                }
            ),
        )
        or ""
    )


def test_verification_summary_is_bounded_for_user_facing_chat() -> None:
    with pytest.raises(ValidationError):
        VerificationDecision(
            verdict="complete",
            summary="x" * 1_201,
            evidence=[],
            criteria=[],
        )
