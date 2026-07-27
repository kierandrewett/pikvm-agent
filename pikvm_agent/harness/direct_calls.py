"""Visible, policy-controllable records for direct PiKVM MCP calls.

Claude Code, Codex, and similar clients commonly drive the ordinary ``pikvm``
MCP tools themselves instead of delegating to the harness model loop.  This
module lets those calls use the same durable run rail, live frame, audit stream,
and operator controls without pretending that the harness chose the action.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from pikvm_agent.harness.agent_models import (
    ComputerObservation,
    RunSnapshot,
    RunStatus,
    VerificationImageArtifact,
)
from pikvm_agent.harness.agent_store import RunStore
from pikvm_agent.harness.input_receipts import public_input_receipts
from pikvm_agent.harness.redaction import redact_secrets


class DirectComputer(Protocol):
    async def refresh(self, *, session_id: str) -> ComputerObservation: ...

    async def resolve_approval(
        self, *, session_id: str, approval_id: str, decision: dict[str, Any]
    ) -> ComputerObservation: ...

    async def abort(
        self, *, session_id: str, reason: str
    ) -> ComputerObservation: ...


class DirectCaller(BaseModel):
    name: str = Field(default="unknown-mcp-client", max_length=160)
    version: str = Field(default="", max_length=80)
    provider: str = Field(default="", max_length=160)
    model: str = Field(default="", max_length=200)
    label: str = Field(default="", max_length=200)


class DirectCallBegin(BaseModel):
    call_id: str = Field(min_length=1, max_length=160)
    tool: str = Field(min_length=1, max_length=160)
    arguments: dict[str, Any] = Field(default_factory=dict)
    caller: DirectCaller = Field(default_factory=DirectCaller)


class DirectCallFinish(BaseModel):
    call_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(default="", max_length=200)
    status: Literal["completed", "failed"]
    latency_ms: int | None = Field(default=None, ge=0)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = Field(default="", max_length=4_000)


class DirectCallDecision(BaseModel):
    allowed: bool
    run_id: str
    reason: str = ""


class DirectCallCoordinator:
    """One small interface over direct-call audit and operator authority."""

    _OBSERVATION_TOOLS = {
        "pikvm_screenshot",
        "pikvm_parse_screen",
        "pikvm_ocr_region",
        "pikvm_find_text",
        "pikvm_export_memory_update",
        "pikvm_abort",
        "pikvm_panic_stop",
    }
    _MODEL_APPROVAL_TOOLS = {
        "pikvm_resolve_approval",
        "pikvm_autonomous_approve",
    }
    _NO_EFFECT_VERIFICATION_TOOLS = {
        "pikvm_open",
        "pikvm_screenshot",
        "pikvm_parse_screen",
        "pikvm_ocr_region",
        "pikvm_find_text",
        "pikvm_export_memory_update",
    }
    _PASSIVE_ACTIONS = {
        "wait",
        "wait_for_change",
        "wait_for_stable_screen",
    }
    _FAILURE_STATUSES = {"error", "failed", "interrupted", "stopped"}
    _STALE_STATUSES = {"control_changed", "stale_world"}
    _POLICY_REFUSED_STATUSES = {"blocked", "rejected"}

    def __init__(self, *, store: RunStore, computer: DirectComputer) -> None:
        self.store = store
        self.computer = computer
        self._call_runs: dict[str, str] = {}
        self._session_runs: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def begin(self, call: DirectCallBegin) -> DirectCallDecision:
        async with self._lock:
            session_id = str(call.arguments.get("session_id") or "")
            run = await self._run_for_begin(call, session_id)
            reason = self._refusal_reason(run, call.tool)
            if reason:
                run.record(
                    "action.refused_by_operator",
                    call_id=call.call_id,
                    tool=call.tool,
                    reason=reason,
                    effect_state="not_applicable",
                    source="direct_mcp",
                    caller=call.caller.model_dump(mode="json"),
                )
                await self.store.save(run)
                return DirectCallDecision(
                    allowed=False, run_id=run.run_id, reason=reason
                )
            evidence_refusal, action_index = (
                await self._capture_pre_action_evidence(
                    run,
                    call,
                    session_id=session_id,
                )
            )
            if evidence_refusal:
                await self.store.save(run)
                return DirectCallDecision(
                    allowed=False,
                    run_id=run.run_id,
                    reason=evidence_refusal,
                )
            run.record(
                "action.attempted",
                call_id=call.call_id,
                tool=call.tool,
                arguments=redact_secrets(call.arguments),
                **(
                    {"index": action_index}
                    if action_index is not None
                    else {}
                ),
                source="direct_mcp",
                caller=call.caller.model_dump(mode="json"),
            )
            await self.store.save(run)
            self._call_runs[call.call_id] = run.run_id
            if session_id:
                self._session_runs[session_id] = run.run_id
            return DirectCallDecision(allowed=True, run_id=run.run_id)

    async def _capture_pre_action_evidence(
        self,
        run: RunSnapshot,
        call: DirectCallBegin,
        *,
        session_id: str,
    ) -> tuple[str, int | None]:
        """Retain the exact screen a direct pointer action was based on."""

        if not session_id or self._click_coordinates(call) is None:
            return "", None
        try:
            observation = await self.computer.refresh(
                session_id=session_id,
            )
        except Exception:
            run.record(
                "action.pre_action_evidence_unavailable",
                call_id=call.call_id,
                tool=call.tool,
                reason="screen capture unavailable",
                source="harness",
            )
            return "", None

        previous_machine = (
            run.observation.machine if run.observation is not None else {}
        )
        previous_fingerprint = str(
            previous_machine.get("fingerprint") or ""
        )
        current_fingerprint = str(
            observation.machine.get("fingerprint") or ""
        )
        run.observation = observation
        if (
            previous_fingerprint
            and current_fingerprint
            and previous_fingerprint != current_fingerprint
        ):
            run.status = RunStatus.BLOCKED
            run.error = "target identity changed before direct MCP input"
            run.pending_approval = None
            run.record(
                "target.identity_changed",
                call_id=call.call_id,
                previous_fingerprint=previous_fingerprint,
                current_fingerprint=current_fingerprint,
                previous_alias=previous_machine.get("alias"),
                current_alias=observation.machine.get("alias"),
                source="harness",
            )
            return run.error, None

        image_path = Path(observation.image_path) if observation.image_path else None
        if image_path is None or not image_path.is_file():
            run.record(
                "action.pre_action_evidence_unavailable",
                call_id=call.call_id,
                tool=call.tool,
                reason="screen artifact unavailable",
                source="harness",
            )
            return "", None

        action_index = run.next_action_index
        run.next_action_index += 1
        run.latest_verification_image_path = str(image_path)
        run.latest_verification_image_revision += 1
        evidence = VerificationImageArtifact(
            revision=run.latest_verification_image_revision,
            action_index=action_index,
            kind="pre_action",
            before_frame_id=observation.frame_id,
            path=str(image_path),
        )
        run.verification_images = [
            *run.verification_images[-63:],
            evidence,
        ]
        run.record(
            "action.pre_action_evidence_captured",
            call_id=call.call_id,
            tool=call.tool,
            revision=evidence.revision,
            action_index=evidence.action_index,
            evidence_kind=evidence.kind,
            before_frame_id=evidence.before_frame_id,
            source="harness",
        )
        return "", action_index

    @staticmethod
    def _click_coordinates(
        call: DirectCallBegin,
    ) -> tuple[int, int] | None:
        if call.tool == "pikvm_click":
            x = call.arguments.get("x")
            y = call.arguments.get("y")
            if isinstance(x, int) and isinstance(y, int):
                return x, y
            return None
        if call.tool != "pikvm_run_burst":
            return None
        actions = call.arguments.get("actions")
        if not isinstance(actions, list):
            return None
        for action in actions:
            if not isinstance(action, dict):
                continue
            if action.get("type") not in {"click", "double_click"}:
                continue
            x = action.get("x")
            y = action.get("y")
            if isinstance(x, int) and isinstance(y, int):
                return x, y
        return None

    async def resolve_approval(
        self,
        run_id: str,
        approval_id: str,
        decision: dict[str, Any],
    ) -> RunSnapshot:
        async with self._lock:
            run = await self._direct_run(run_id)
            pending = run.pending_approval or {}
            if run.status is not RunStatus.NEEDS_APPROVAL:
                raise ValueError("run is not waiting for approval")
            if approval_id != pending.get("approval_id"):
                raise ValueError(
                    "approval_id does not match the pending approval"
                )
            if not run.session_id:
                raise ValueError("direct run has no computer session")
            decision_type = str(decision.get("type") or "")
            if decision_type not in {"approve", "reject", "take_over"}:
                raise ValueError(
                    "decision.type must be approve, reject, or take_over"
                )
            run.record(
                "approval.resolving",
                approval_id=approval_id,
                decision=decision_type,
                source="operator",
            )
            await self.store.save(run)
            observation = await self.computer.resolve_approval(
                session_id=run.session_id,
                approval_id=approval_id,
                decision=decision,
            )
            run.observation = observation
            run.pending_approval = None
            run.error = observation.error
            if decision_type == "approve":
                run.status = self._run_status(
                    observation.status,
                    tool=str(pending.get("tool") or ""),
                )
                self._record_effect_outcome(
                    run,
                    call_id=str(pending.get("call_id") or ""),
                    tool=str(pending.get("tool") or ""),
                    result=observation.raw,
                    result_status=observation.status,
                    observation=observation,
                )
                run.record(
                    "approval.approved",
                    approval_id=approval_id,
                    source="operator",
                )
            else:
                run.status = (
                    RunStatus.REJECTED
                    if decision_type == "reject"
                    else RunStatus.ABORTED
                )
                run.record(
                    "action.refused_by_operator",
                    call_id=pending.get("call_id"),
                    tool=pending.get("tool"),
                    reason=decision.get("reason") or decision_type,
                    effect_state="not_applicable",
                    source="operator",
                )
                run.record(
                    "approval.not_approved",
                    approval_id=approval_id,
                    decision=decision_type,
                    source="operator",
                )
            await self.store.save(run)
            return run

    async def abort(self, run_id: str, reason: str) -> RunSnapshot:
        async with self._lock:
            run = await self._direct_run(run_id)
            if run.status is RunStatus.ABORTED:
                return run
            if not run.session_id:
                raise ValueError("direct run has no computer session")
            run.record(
                "run.abort_requested",
                reason=reason,
                source="operator",
            )
            await self.store.save(run)
            observation = await self.computer.abort(
                session_id=run.session_id,
                reason=reason,
            )
            run.observation = observation
            run.pending_approval = None
            run.status = RunStatus.ABORTED
            run.error = observation.error
            run.record(
                "run.aborted",
                reason=reason,
                source="operator",
                scope="direct_mcp_session",
            )
            await self.store.save(run)
            return run

    async def pause(self, run_id: str, reason: str) -> RunSnapshot:
        async with self._lock:
            run = await self._direct_run(run_id)
            if run.status not in {
                RunStatus.ABORTED,
                RunStatus.REJECTED,
                RunStatus.COMPLETED,
                RunStatus.FAILED,
            }:
                run.status = RunStatus.PAUSED
                run.record(
                    "run.paused",
                    reason=reason,
                    source="operator",
                    scope="direct_mcp_actions",
                )
                await self.store.save(run)
            return run

    async def resume(self, run_id: str) -> RunSnapshot:
        async with self._lock:
            run = await self._direct_run(run_id)
            if run.status is RunStatus.PAUSED:
                run.status = (
                    RunStatus.NEEDS_APPROVAL
                    if run.pending_approval
                    else RunStatus.RUNNING
                )
                run.record(
                    "run.resumed",
                    source="operator",
                    scope="direct_mcp_actions",
                )
                await self.store.save(run)
            return run

    async def finish(self, call: DirectCallFinish) -> RunSnapshot:
        async with self._lock:
            run = await self._run_for_finish(call)
            result = call.result
            tool = self._tool_for_call(run, call.call_id)
            session_id = str(result.get("session_id") or run.session_id or "")
            if session_id:
                run.session_id = session_id
                self._session_runs[session_id] = run.run_id
            if call.status == "failed":
                run.status = RunStatus.PAUSED
                run.error = self._safe_error(call.error)
                run.record(
                    "action.failed",
                    call_id=call.call_id,
                    tool=tool,
                    error=run.error,
                    latency_ms=call.latency_ms,
                    effect_state="failed",
                    source="direct_mcp",
                    caller=self._attempt_for_call(
                        run, call.call_id
                    ).get("caller", {}),
                )
                await self.store.save(run)
                return run

            previous_machine = (
                run.observation.machine if run.observation is not None else {}
            )
            observation = self._observation(run, result)
            run.observation = observation
            result_status = str(result.get("status") or "running")
            run.status = self._run_status(result_status, tool=tool)
            run.error = str(result.get("error") or "") or None
            run.pending_approval = (
                result.get("approval_request")
                if isinstance(result.get("approval_request"), dict)
                else None
            )
            if result_status != "needs_approval":
                self._record_effect_outcome(
                    run,
                    call_id=call.call_id,
                    tool=tool,
                    result=result,
                    result_status=result_status,
                    observation=observation,
                    latency_ms=call.latency_ms,
                )
            previous_fingerprint = str(
                previous_machine.get("fingerprint") or ""
            )
            current_fingerprint = str(
                observation.machine.get("fingerprint") or ""
            )
            if (
                previous_fingerprint
                and current_fingerprint
                and previous_fingerprint != current_fingerprint
            ):
                run.status = RunStatus.BLOCKED
                run.error = (
                    "target identity changed during direct MCP session"
                )
                run.pending_approval = None
                run.record(
                    "target.identity_changed",
                    previous_fingerprint=previous_fingerprint,
                    current_fingerprint=current_fingerprint,
                    previous_alias=previous_machine.get("alias"),
                    current_alias=observation.machine.get("alias"),
                    source="harness",
                )
            if run.pending_approval:
                run.pending_approval.setdefault("tool", tool)
                run.pending_approval.setdefault("call_id", call.call_id)
                run.record(
                    "approval.required",
                    call_id=call.call_id,
                    tool=tool,
                    approval_id=run.pending_approval.get("approval_id"),
                    risk=run.pending_approval.get("risk"),
                    reason=run.pending_approval.get("reason"),
                    source="daemon",
                )
            await self.store.save(run)
            return run

    async def _run_for_finish(
        self, call: DirectCallFinish
    ) -> RunSnapshot:
        run_id = call.run_id or self._call_runs.get(call.call_id, "")
        if run_id:
            run = await self._direct_run(run_id)
            if self._tool_for_call(run, call.call_id):
                self._call_runs[call.call_id] = run.run_id
                return run
            raise KeyError(
                f"direct call {call.call_id} does not belong to run {run_id}"
            )
        for summary in await self.store.list_summaries(limit=500):
            candidate = await self.store.get_control(summary.run_id)
            if (
                candidate.origin == "direct_mcp"
                and self._tool_for_call(candidate, call.call_id)
            ):
                self._call_runs[call.call_id] = candidate.run_id
                return candidate
        raise KeyError(f"unknown direct call: {call.call_id}")

    async def _run_for_begin(
        self, call: DirectCallBegin, session_id: str
    ) -> RunSnapshot:
        run_id = self._session_runs.get(session_id) if session_id else None
        if run_id:
            run = await self.store.get_control(run_id)
            run.caller = call.caller.model_dump(mode="json")
            return run
        if session_id:
            for summary in await self.store.list_summaries(limit=500):
                candidate = await self.store.get_state(summary.run_id)
                if (
                    candidate.origin == "direct_mcp"
                    and candidate.session_id == session_id
                ):
                    self._session_runs[session_id] = candidate.run_id
                    candidate.caller = call.caller.model_dump(mode="json")
                    return candidate
        task = str(call.arguments.get("label") or "Direct MCP computer session")
        run = RunSnapshot(
            run_id=f"direct-{uuid.uuid4()}",
            task=task,
            status=RunStatus.RUNNING,
            origin="direct_mcp",
            caller=call.caller.model_dump(mode="json"),
            session_id=session_id or None,
        )
        run.record(
            "run.created",
            origin="direct_mcp",
            caller=run.caller,
        )
        return run

    async def _direct_run(self, run_id: str) -> RunSnapshot:
        run = await self.store.get_control(run_id)
        if run.origin != "direct_mcp":
            raise ValueError("run is not a direct MCP session")
        return run

    def _refusal_reason(self, run: RunSnapshot, tool: str) -> str:
        if (
            run.status
            in {
                RunStatus.ABORTED,
                RunStatus.BLOCKED,
                RunStatus.REJECTED,
                RunStatus.COMPLETED,
                RunStatus.FAILED,
            }
            and tool not in self._OBSERVATION_TOOLS
        ):
            return (
                str(run.error)
                if run.error
                else "direct MCP session was stopped by the operator"
            )
        if tool in self._MODEL_APPROVAL_TOOLS:
            return "approval decisions must come from the operator console"
        if (
            run.status is RunStatus.PAUSED
            and tool not in self._OBSERVATION_TOOLS
        ):
            return "operator paused direct MCP actions"
        if (
            run.status is RunStatus.NEEDS_APPROVAL
            and tool not in self._OBSERVATION_TOOLS
        ):
            return "direct MCP action is waiting for operator approval"
        return ""

    @staticmethod
    def _observation(
        run: RunSnapshot, result: dict[str, Any]
    ) -> ComputerObservation:
        result_machine = (
            result.get("machine")
            if isinstance(result.get("machine"), dict)
            else {}
        )
        previous_machine = (
            run.observation.machine if run.observation is not None else {}
        )
        return ComputerObservation(
            session_id=str(result.get("session_id") or run.session_id or ""),
            status=str(result.get("status") or "unknown"),
            machine=result_machine or previous_machine,
            frame_id=result.get("frame_id"),
            world_version=result.get("world_version"),
            control_epoch=result.get("control_epoch"),
            width=result.get("width"),
            height=result.get("height"),
            image_path=(
                str(result["image_path"])
                if result.get("image_path")
                else None
            ),
            image_sha256=(
                str(result["image_sha256"])
                if result.get("image_sha256")
                else None
            ),
            screen_hash=(
                str(result["screen_hash"])
                if result.get("screen_hash")
                else None
            ),
            approval_request=result.get("approval_request"),
            error=result.get("error"),
            raw=result,
        )

    @staticmethod
    def _run_status(status: str, *, tool: str = "") -> RunStatus:
        if status == "completed":
            if tool in {
                "pikvm_autonomous_start",
                "pikvm_autonomous_continue",
            }:
                return RunStatus.COMPLETED
            return RunStatus.RUNNING
        return {
            "needs_approval": RunStatus.NEEDS_APPROVAL,
            "aborted": RunStatus.ABORTED,
            "failed": RunStatus.PAUSED,
            "blocked": RunStatus.BLOCKED,
            "rejected": RunStatus.REJECTED,
        }.get(status, RunStatus.RUNNING)

    @staticmethod
    def _tool_for_call(run: RunSnapshot, call_id: str) -> str:
        for event in reversed(run.events):
            if (
                event.kind == "action.attempted"
                and event.data.get("call_id") == call_id
            ):
                return str(event.data.get("tool") or "")
        return ""

    @staticmethod
    def _attempt_for_call(
        run: RunSnapshot, call_id: str
    ) -> dict[str, Any]:
        for event in reversed(run.events):
            if (
                event.kind == "action.attempted"
                and event.data.get("call_id") == call_id
            ):
                return event.data
        return {}

    @classmethod
    def _actions_for_call(
        cls,
        tool: str,
        arguments: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if tool == "pikvm_run_burst":
            actions = arguments.get("actions")
            if isinstance(actions, list):
                return [
                    dict(action)
                    for action in actions
                    if isinstance(action, dict)
                ]
            return []
        if tool == "pikvm_type_text":
            return [
                {
                    "type": "type_text",
                    "secret": arguments.get("secret") is True,
                }
            ]
        if tool == "pikvm_click":
            return [{"type": "click"}]
        if tool == "pikvm_key":
            return [{"type": "key"}]
        if tool == "pikvm_scroll":
            return [{"type": "scroll"}]
        return []

    @classmethod
    def _exact_input_effect_verified(
        cls,
        actions: list[dict[str, Any]],
        receipts: list[dict[str, Any]],
    ) -> bool:
        active = [
            (index, str(action.get("type") or ""))
            for index, action in enumerate(actions)
            if str(action.get("type") or "") not in cls._PASSIVE_ACTIONS
        ]
        if not active or any(kind != "type_text" for _, kind in active):
            return False
        by_index = {
            receipt.get("index"): receipt
            for receipt in receipts
            if isinstance(receipt.get("index"), int)
        }
        for index, _ in active:
            receipt = by_index.get(index, {})
            delivery_hash = receipt.get(
                "delivery_sha256",
                receipt.get("requested_sha256"),
            )
            readback_hash = receipt.get("readback_sha256")
            if not (
                receipt.get("status") == "verified_exact"
                and receipt.get("verdict") == "match"
                and receipt.get("focus_evidence") == "read_back_verified"
                and receipt.get("proof_state")
                in {"exact_ocr_readback", "exact_readback"}
                and receipt.get("exact_readback_sha256_match") is True
                and isinstance(delivery_hash, str)
                and delivery_hash
                and delivery_hash == readback_hash
                and receipt.get("issued_characters")
                == receipt.get(
                    "delivery_characters",
                    receipt.get("requested_characters"),
                )
            ):
                return False
        return True

    @classmethod
    def _effect_outcome(
        cls,
        *,
        tool: str,
        result_status: str,
        actions: list[dict[str, Any]],
        receipts: list[dict[str, Any]],
    ) -> tuple[str, str]:
        if result_status in cls._FAILURE_STATUSES:
            return "action.failed", "failed"
        if result_status in cls._STALE_STATUSES:
            return "action.refused_stale", "not_applicable"
        if result_status in cls._POLICY_REFUSED_STATUSES:
            return "action.refused_by_policy", "not_applicable"
        if tool in cls._NO_EFFECT_VERIFICATION_TOOLS:
            return "action.completed", "not_applicable"
        if result_status == "unverified":
            return "action.completed_unverified", "unverified"
        if cls._exact_input_effect_verified(actions, receipts):
            return "action.completed", "verified"
        return "action.completed_unverified", "unverified"

    def _record_effect_outcome(
        self,
        run: RunSnapshot,
        *,
        call_id: str,
        tool: str,
        result: dict[str, Any],
        result_status: str,
        observation: ComputerObservation,
        latency_ms: int | None = None,
    ) -> None:
        attempt = self._attempt_for_call(run, call_id)
        arguments = (
            attempt.get("arguments")
            if isinstance(attempt.get("arguments"), dict)
            else {}
        )
        actions = self._actions_for_call(tool, arguments)
        receipts = public_input_receipts(result, actions)
        event_kind, effect_state = self._effect_outcome(
            tool=tool,
            result_status=result_status,
            actions=actions,
            receipts=receipts,
        )
        event_data: dict[str, Any] = {
            "call_id": call_id,
            "tool": tool,
            "latency_ms": latency_ms,
            "status": result_status,
            "frame_id": result.get("frame_id"),
            "world_version": result.get("world_version"),
            "control_epoch": result.get("control_epoch"),
            "image_sha256": result.get("image_sha256"),
            "screen_hash": result.get("screen_hash"),
            "machine": observation.machine,
            "effect_state": effect_state,
            "source": "direct_mcp",
            "caller": (
                attempt.get("caller")
                if isinstance(attempt.get("caller"), dict)
                else {}
            ),
        }
        if receipts:
            event_data["input_receipts"] = receipts
        if event_kind == "action.failed":
            event_data["error"] = self._safe_error(
                str(result.get("error") or result_status)
            )
        run.record(event_kind, **event_data)

    @staticmethod
    def _safe_error(error: str) -> str:
        """Persist a useful failure class, never tool-controlled prose."""
        candidate = error.strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,159}", candidate):
            return candidate
        return "direct MCP tool failed"
