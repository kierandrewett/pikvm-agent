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
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from pikvm_agent.harness.agent_models import (
    ComputerObservation,
    RunSnapshot,
    RunStatus,
)
from pikvm_agent.harness.agent_store import RunStore
from pikvm_agent.harness.redaction import redact_secrets


class DirectComputer(Protocol):
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
                    source="direct_mcp",
                )
                await self.store.save(run)
                return DirectCallDecision(
                    allowed=False, run_id=run.run_id, reason=reason
                )
            run.record(
                "action.attempted",
                call_id=call.call_id,
                tool=call.tool,
                arguments=redact_secrets(call.arguments),
                source="direct_mcp",
                caller=call.caller.model_dump(mode="json"),
            )
            await self.store.save(run)
            self._call_runs[call.call_id] = run.run_id
            if session_id:
                self._session_runs[session_id] = run.run_id
            return DirectCallDecision(allowed=True, run_id=run.run_id)

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
                    source="direct_mcp",
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
            run.record(
                "action.completed",
                call_id=call.call_id,
                tool=tool,
                latency_ms=call.latency_ms,
                status=result_status,
                frame_id=result.get("frame_id"),
                world_version=result.get("world_version"),
                control_epoch=result.get("control_epoch"),
                machine=observation.machine,
                source="direct_mcp",
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
            return await self.store.get_control(run_id)
        if session_id:
            for summary in await self.store.list_summaries(limit=500):
                candidate = await self.store.get_state(summary.run_id)
                if (
                    candidate.origin == "direct_mcp"
                    and candidate.session_id == session_id
                ):
                    self._session_runs[session_id] = candidate.run_id
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
    def _safe_error(error: str) -> str:
        """Persist a useful failure class, never tool-controlled prose."""
        candidate = error.strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,159}", candidate):
            return candidate
        return "direct MCP tool failed"
