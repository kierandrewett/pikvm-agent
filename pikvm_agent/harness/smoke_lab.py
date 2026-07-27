"""Target-free full-stack smoke lab for managed-client acceptance."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from PIL import Image, ImageDraw

from pikvm_agent.harness.agent import AgentHarness
from pikvm_agent.harness.agent_models import (
    ComputerObservation,
    HarnessConfig,
    ModelRequest,
    ModelResponse,
)
from pikvm_agent.harness.agent_store import RunStore, SqliteRunStore
from pikvm_agent.harness.api import create_harness_app
from pikvm_agent.harness.model_pool import ModelPool, RoleRoute


class ManagedSmokeProvider:
    """Deterministic structured provider that exercises all three model lanes."""

    name = "managed-smoke"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "reasoner":
            data = {
                "summary": "Activate the managed smoke canvas and verify it.",
                "steps": ["Activate the canvas", "Verify the completed state"],
                "success_criteria": [
                    "The canvas visibly reports Managed task complete."
                ],
                "constraints": [
                    "Use only the bounded target-free smoke action."
                ],
            }
        elif request.role == "controller":
            data = {
                "outcome": "act",
                "intent": "Activate the target-free managed smoke canvas.",
                "actions": [
                    {
                        "type": "click",
                        "x": 640,
                        "y": 360,
                        "button": "left",
                    }
                ],
                "expected_evidence": [
                    "The canvas visibly reports Managed task complete."
                ],
            }
        else:
            data = {
                "verdict": "complete",
                "summary": "The managed smoke task is visibly complete.",
                "evidence": [
                    "The after frame reports Managed task complete."
                ],
                "criteria": [
                    {
                        "criterion_index": 0,
                        "satisfied": True,
                        "evidence": (
                            "The canvas visibly reports Managed task complete."
                        ),
                    }
                ],
            }
        return ModelResponse(
            provider=self.name,
            model="deterministic-smoke-v1",
            data=data,
        )


class ManagedSmokeComputer:
    """No-machine computer adapter with durable before/after frame evidence."""

    def __init__(self, *, before: Path, after: Path) -> None:
        self.before = before
        self.after = after
        self._completed = False

    def _observation(
        self,
        *,
        status: str,
        frame_id: int,
        image_path: Path,
    ) -> ComputerObservation:
        return ComputerObservation(
            session_id="managed-smoke-session",
            status=status,
            frame_id=frame_id,
            world_version=frame_id,
            control_epoch=1,
            image_path=str(image_path),
            machine={
                "alias": "Managed smoke canvas",
                "fingerprint": "target:0000000000000000",
                "desktop_layer": "No-machine managed smoke lab",
            },
        )

    async def open(self, _label: str) -> ComputerObservation:
        self._completed = False
        return self._observation(
            status="paused",
            frame_id=1,
            image_path=self.before,
        )

    async def refresh(self, *, session_id: str) -> ComputerObservation:
        if session_id != "managed-smoke-session":
            raise ValueError("unknown managed smoke session")
        return self._observation(
            status="completed" if self._completed else "paused",
            frame_id=2 if self._completed else 1,
            image_path=self.after if self._completed else self.before,
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
        if session_id != "managed-smoke-session":
            raise ValueError("unknown managed smoke session")
        if based_on_world_version != 1 or based_on_control_epoch != 1:
            raise ValueError("stale managed smoke action")
        if (
            len(actions) != 1
            or actions[0].get("type") != "click"
            or not idempotency_key
        ):
            raise ValueError("unexpected managed smoke action")
        self._completed = True
        return self._observation(
            status="completed",
            frame_id=2,
            image_path=self.after,
        )

    async def resolve_approval(
        self,
        *,
        session_id: str,
        approval_id: str,
        decision: dict[str, Any],
    ) -> ComputerObservation:
        del session_id, approval_id, decision
        raise ValueError("managed smoke actions never require approval")

    async def abort(
        self,
        *,
        session_id: str,
        reason: str,
    ) -> ComputerObservation:
        del reason
        if session_id != "managed-smoke-session":
            raise ValueError("unknown managed smoke session")
        return self._observation(
            status="aborted",
            frame_id=2 if self._completed else 1,
            image_path=self.after if self._completed else self.before,
        )


def _write_smoke_frame(path: Path, *, completed: bool) -> None:
    image = Image.new("RGB", (1280, 720), (14, 20, 31))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (240, 170, 1040, 550),
        radius=28,
        fill=(25, 35, 52),
        outline=(76, 104, 142),
        width=3,
    )
    draw.text(
        (320, 260),
        "Managed task complete" if completed else "Managed smoke canvas",
        fill=(126, 231, 180) if completed else (235, 240, 248),
        font_size=38,
    )
    draw.text(
        (320, 340),
        (
            "Verified through the dedicated harness loop"
            if completed
            else "Awaiting one bounded managed action"
        ),
        fill=(165, 180, 201),
        font_size=22,
    )
    image.save(path, format="PNG")


def build_managed_smoke_app(
    *,
    root: Path,
    access_token: str,
    agent_token: str,
    allowed_origin: str,
    store: RunStore | None = None,
    models: ModelPool | None = None,
) -> FastAPI:
    """Build the real managed operator app over a synthetic computer.

    The offline default uses the deterministic smoke provider. Callers may
    inject the production model pool for an explicitly authorized live-model
    acceptance run without introducing a VNC, PiKVM, daemon, or HID target.
    """

    root = root.expanduser().resolve()
    frame_dir = root / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    before = frame_dir / "managed-smoke-before.png"
    after = frame_dir / "managed-smoke-after.png"
    _write_smoke_frame(before, completed=False)
    _write_smoke_frame(after, completed=True)

    if models is None:
        provider = ManagedSmokeProvider()
        models = ModelPool(
            providers={provider.name: provider},
            routes={
                role: RoleRoute(providers=[provider.name])
                for role in ("reasoner", "controller", "verifier")
            },
            provider_metadata={
                provider.name: {
                    "kind": "deterministic_smoke",
                    "configured_model": "deterministic-smoke-v1",
                    "billing_mode": "synthetic",
                    "interface": "Managed smoke provider",
                    "pixel_input": "Labelled local PNG checkpoints",
                    "structured_output": "Native validated schema",
                    "credential": "none",
                    "auth_mode": "none",
                }
            },
        )
    run_store = store or SqliteRunStore(root / "state.sqlite3")
    harness = AgentHarness(
        computer=ManagedSmokeComputer(before=before, after=after),
        models=models,
        store=run_store,
        config=HarnessConfig(
            max_actions_per_advance=4,
            max_actions_per_burst=1,
            max_total_actions=4,
            max_provider_attempts_per_run=8,
        ),
    )
    app = create_harness_app(
        harness=harness,
        store=run_store,
        models=models,
        access_token=access_token,
        agent_token=agent_token,
        allowed_origins={allowed_origin},
        max_autonomous_resumes=4,
    )
    app.state.synthetic_smoke_lab = True
    app.state.harness = harness
    app.state.harness_store = run_store
    app.state.model_pool = models
    return app
