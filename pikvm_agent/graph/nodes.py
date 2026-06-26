"""Graph node functions.

Each node is pure control flow over ``AgentState`` plus delegation to the owned
services in ``GraphDeps`` (pulled from the run config). Nodes never contain
PiKVM-specific logic directly — they call the backend / parser / operator /
policy / executor. The cardinal invariants live in the services: freshness in
the policy gate, verification in the executor.
"""

from __future__ import annotations

import base64
import time
import uuid
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from pikvm_agent.core.errors import OperatorError
from pikvm_agent.core.models import (
    ElementMap,
    GuardedTransaction,
    OperatorRequest,
    TransactionResult,
)
from pikvm_agent.graph.deps import get_deps
from pikvm_agent.graph.interrupts import approval_interrupt
from pikvm_agent.operator.schemas import validate_decision as _validate_decision_schema
from pikvm_agent.policy.approvals import make_approval_request


def _app_from_mode(mode: str) -> str:
    return mode.split(".", 1)[0] if "." in mode else mode


def _build_request(state: dict[str, Any]) -> OperatorRequest:
    image_b64 = ""
    path = state.get("frame_path")
    if path and Path(path).exists():
        image_b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return OperatorRequest(
        task=state.get("task", ""),
        frame={
            "id": state.get("frame_id", 0),
            "world_version": state.get("world_version", 0),
            "image": image_b64,
            "age_ms": state.get("frame_age_ms", 0),
        },
        detected_state={
            "active_app": state.get("active_app", "unknown"),
            "mode": state.get("mode", "unknown"),
            "keyboard": state.get("keyboard_state", {}),
            "blocking_events": [e.get("type") for e in state.get("recent_events", [])],
        },
        visual_elements=(state.get("element_map") or {}).get("elements", []),
        recent_events=state.get("recent_events", []),
    )


async def _record_only_execute(deps: Any, tx: GuardedTransaction) -> TransactionResult:
    """Phase-3 placeholder executor: record the actions, do not touch HID. The
    guarded executor (Phase 4) replaces this with real, verified execution."""
    actions = [a.model_dump() for a in tx.actions]
    deps.trace.append("execute_record_only", actions=[a.get("type") for a in actions])
    return TransactionResult(
        status="executed",
        executed_actions=actions,
        world_version_after=tx.based_on_world_version,
    )


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #


async def observe_frame(state: dict, config: RunnableConfig) -> dict:
    deps = get_deps(config)
    frame = await deps.frames.capture()
    deps.trace.append(
        "observe", frame_id=frame.frame_id, world_version=frame.world_version,
        screenshot_path=frame.image_path,
    )
    out: dict[str, Any] = {
        "frame_id": frame.frame_id,
        "world_version": frame.world_version,
        "frame_path": frame.image_path,
        "frame_age_ms": frame.age_ms,
        "keyboard_state": frame.keyboard_state.model_dump(),
        "status": "running",
        "replan": False,  # consumed: we are (re-)observing now
    }
    if not state.get("max_steps"):
        out["max_steps"] = deps.max_steps
    return out


async def parse_screen(state: dict, config: RunnableConfig) -> dict:
    deps = get_deps(config)
    em = await deps.screen_parser.parse(
        Path(state["frame_path"]), state["frame_id"], state["world_version"]
    )
    return {"element_map": em.model_dump(), "ocr_text": em.ocr_text}


async def detect_state(state: dict, config: RunnableConfig) -> dict:
    deps = get_deps(config)
    from pikvm_agent.vision.mode_detector import detect_mode as _default_detect

    detect = deps.detect_mode or _default_detect
    raw = state.get("element_map")
    em = ElementMap.model_validate(raw) if raw else None
    mode = detect(state.get("ocr_text", ""), em)
    return {"mode": mode, "active_app": _app_from_mode(mode)}


async def operator_decide(state: dict, config: RunnableConfig) -> dict:
    deps = get_deps(config)
    step = state.get("step", 0) + 1
    try:
        decision = await deps.operator.decide(_build_request(state))
    except OperatorError as exc:
        deps.trace.append("operator_error", step=step, error=str(exc))
        return {"step": step, "status": "failed", "error": f"operator: {exc}"}
    dd = decision.model_dump()
    deps.trace.append(
        "decision", step=step, intent=decision.intent,
        actions=[a["type"] for a in dd["actions"]], risk=dd["risk"]["category"],
    )
    done = (not decision.actions) or decision.intent.strip().upper().startswith("DONE")
    # Stamp the decision with the controller epoch it was made under; execute_transaction
    # refuses it if the live epoch has since changed (abort / panic / steer).
    epoch = deps.control_epoch_getter() if deps.control_epoch_getter else 0
    return {"operator_decision": dd, "step": step,
            "status": "done" if done else "running", "control_epoch": epoch}


async def validate_decision(state: dict, config: RunnableConfig) -> dict:
    if state.get("status") in ("done", "failed"):
        return {}
    try:
        _validate_decision_schema(state.get("operator_decision") or {})
    except OperatorError as exc:
        return {"status": "failed", "error": f"invalid decision: {exc}"}
    return {}


async def policy_gate(state: dict, config: RunnableConfig) -> dict:
    deps = get_deps(config)
    if state.get("status") == "done":
        return {"policy_result": {"status": "done"}}
    if state.get("status") == "failed":
        return {"policy_result": {"status": "blocked", "reason": state.get("error", "failed")}}

    decision = state["operator_decision"]
    mode = state.get("mode", "unknown")
    for action in decision["actions"]:
        ok, reason = deps.policy.action_allowed_in_mode(action["type"], mode)
        if not ok:
            deps.trace.append("policy_block", reason=reason, mode=mode)
            return {"policy_result": {"status": "blocked", "reason": reason},
                    "status": "blocked", "error": reason}

    pr = deps.policy.policy_gate(
        decision, state["frame_id"], state["world_version"], approved=state.get("approved", False)
    )
    out: dict[str, Any] = {"policy_result": pr.model_dump()}
    if pr.status == "approval_required":
        req = make_approval_request(
            state["session_id"], decision, state["frame_id"], state["world_version"],
            state.get("frame_path"),
        )
        out["approval_request"] = req.model_dump()
        out["status"] = "needs_approval"
        deps.trace.append("approval_required", reason=pr.reason, risk=pr.category)
    elif pr.status == "blocked":
        out["status"] = "blocked"
        out["error"] = pr.reason
        deps.trace.append("policy_block", reason=pr.reason)
    else:
        out["status"] = "running"
    return out


async def human_interrupt(state: dict, config: RunnableConfig) -> dict:
    deps = get_deps(config)
    req = state["approval_request"]
    response = approval_interrupt(
        {
            "session_id": state["session_id"],
            "frame_id": state["frame_id"],
            "world_version": state["world_version"],
            "risk": req.get("risk"),
            "reason": req.get("reason"),
            "proposed_action": req.get("proposed_action"),
            "screenshot_path": state.get("frame_path"),
            "allowed_decisions": ["approve", "edit", "reject", "respond"],
        }
    )
    rtype = response.get("type")
    if rtype == "approve":
        deps.trace.append("approved", approval_id=req.get("approval_id"))
        return {"approval_response": response, "approved": True, "status": "running"}
    if rtype in ("edit", "respond"):
        # The human is CHANGING the request — never execute the original action.
        # Re-plan with their instruction in context instead.
        event = {"type": f"human_{rtype}", "message": response.get("message"),
                 "instruction": response.get("instruction")}
        deps.trace.append("human_response", type=rtype)
        return {
            "approval_response": response, "approved": False, "status": "running", "replan": True,
            "recent_events": (state.get("recent_events", []) + [event])[-10:],
        }
    deps.trace.append("approval_denied", type=rtype)
    return {"approval_response": response, "status": "blocked",
            "error": response.get("reason", f"human {rtype}")}


async def execute_transaction(state: dict, config: RunnableConfig) -> dict:
    deps = get_deps(config)
    decision = state["operator_decision"]
    # STICKY TERMINAL BRAKE (checked first): abort / panic latch a stop that survives
    # re-planning. The epoch alone is a one-shot — operator_decide re-stamps each loop with
    # the CURRENT epoch, so a fresh decision would pass the epoch gate after a panic; this
    # latch refuses every action regardless, so the stop is truly terminal.
    if deps.stop_getter is not None and deps.stop_getter():
        deps.trace.append("execute_refused", reason="stopped")
        return {"status": "failed", "error": "stopped (abort / panic)",
                "transaction_result": {"status": "blocked_by_policy", "error": "stopped"}}
    # HARD CONTROL GATE (checked before EVERY transaction, so a stop lands within one
    # action): if the live controller epoch differs from the one this decision was made
    # under, an abort / panic / steer happened — refuse to execute the stale plan.
    getter = deps.control_epoch_getter
    if getter is not None and state.get("control_epoch") is not None and getter() != state["control_epoch"]:
        deps.trace.append("execute_refused", reason="control_changed",
                          planned=state["control_epoch"], current=getter())
        return {"status": "failed", "error": "control changed (aborted / panic / steered)",
                "transaction_result": {"status": "blocked_by_policy", "error": "control_changed"}}
    # TIME-BUDGET PRE-CHECK: the per-call deadline can lapse during observe/parse/operator
    # latency. Don't START a new action after it — defer to a resumable pause (route_after_
    # verify sees the spent budget and routes to budget_pause). tx-COUNT is enforced
    # post-execute so the first action of a call still runs.
    deadline = state.get("deadline_ms") or 0
    if deadline and time.monotonic() * 1000 >= deadline:
        deps.trace.append("execute_deferred", reason="deadline")
        return {"transaction_result": {"status": "deferred_budget"}}
    # Approval is NOT force-execute. Re-observe NOW and verify the WORLD still
    # matches the plan: frame_id always advances on capture, so the invariant is
    # world_version. A change during a human's deliberation makes this stale.
    fresh = await deps.frames.capture()
    fresh_fields = {"frame_id": fresh.frame_id, "world_version": fresh.world_version,
                    "frame_path": fresh.image_path}
    if decision["based_on_world_version"] != fresh.world_version:
        deps.trace.append("execute_refused", reason="stale_world",
                          planned=decision["based_on_world_version"], current=fresh.world_version)
        return {**fresh_fields, "approved": False, "status": "running",
                "transaction_result": {"status": "failed_stale_frame", "error": "stale_world"}}
    risk = deps.policy.classify_local_risk(decision)
    if risk["blocked"]:
        deps.trace.append("execute_refused", reason=risk["reason"])
        return {**fresh_fields, "approved": False, "status": "blocked",
                "transaction_result": {"status": "blocked_by_policy", "error": risk["reason"]}}
    if risk["requires_human"] and not state.get("approved", False):
        deps.trace.append("execute_refused", reason="requires_human")
        return {**fresh_fields, "approved": False, "status": "needs_approval",
                "transaction_result": {"status": "blocked_by_policy", "error": "requires_human"}}

    tx = GuardedTransaction(
        id="tx_" + uuid.uuid4().hex[:10],
        session_id=state["session_id"],
        based_on_frame_id=state["frame_id"],
        based_on_world_version=state["world_version"],
        intent=decision["intent"],
        actions=decision["actions"],
        postconditions=decision.get("postconditions", {}),
        risk=decision["risk"],
        approval_id=(state.get("approval_request") or {}).get("approval_id"),
    )
    # Live control gate threaded INTO the action: a long type / multi-action transaction
    # polls this between chunks/actions and stops mid-flight if control changed. Passed
    # through a transient state copy so deps.execute stays a (tx, state) contract and the
    # non-serialisable callable is never checkpointed.
    planned_epoch = state.get("control_epoch")
    gate = (lambda: getter() == planned_epoch) if (getter is not None and planned_epoch is not None) else None
    execute = deps.execute or (lambda t, s: _record_only_execute(deps, t))
    result = await execute(tx, {**state, "_should_continue": gate} if gate is not None else state)
    if not isinstance(result, TransactionResult):
        result = TransactionResult.model_validate(result)
    deps.trace.append("executed", status=result.status,
                      actions=[a["type"] for a in decision["actions"]])
    return {
        **fresh_fields,
        "transaction_result": result.model_dump(),
        "approved": False,
        "recent_actions": (state.get("recent_actions", []) + [{"intent": decision["intent"]}])[-10:],
        "tx_this_call": state.get("tx_this_call", 0) + 1,  # counts toward the per-call budget
    }


async def budget_pause(state: dict, config: RunnableConfig) -> dict:
    """Pause the loop when this pikvm_continue call has spent its transaction/time
    budget. interrupt() checkpoints the graph; the runtime reports status='paused' and
    the next continue resumes here. The interrupt is CONDITIONAL on the budget: on
    resume the runtime resets tx_this_call (Command update), so it's no longer spent and
    we fall straight through to observe_frame and keep going. Resumable — NOT terminal."""
    from pikvm_agent.graph.routing import _budget_spent

    if not _budget_spent(state):  # resumed with a fresh budget -> keep going
        return {}
    get_deps(config).trace.append("budget_pause", tx_this_call=state.get("tx_this_call", 0))
    interrupt({"reason": "budget_paused", "tx_this_call": state.get("tx_this_call", 0)})
    return {}


async def verify_result(state: dict, config: RunnableConfig) -> dict:
    # Routing happens in route_after_verify; this node attaches the verification
    # carried by the transaction result (the executor is the verifier). It also
    # turns max-step exhaustion into a FAILURE — never a silent "done".
    tr = state.get("transaction_result") or {}
    out: dict[str, Any] = {"verification_result": tr.get("verification") or {}}
    status = state.get("status")
    if status not in ("done", "failed", "blocked"):
        if state.get("step", 0) >= state.get("max_steps", 12):
            get_deps(config).trace.append("max_steps_exhausted", step=state.get("step"))
            out["status"] = "failed"
            out["error"] = "max_steps_exhausted"
        elif tr.get("status") in ("failed", "blocked_by_policy"):
            # A failed/blocked execution is a genuine failure — surface it (with its
            # reason) so the session reports "failed", not a misleading "done".
            out["status"] = "blocked" if tr.get("status") == "blocked_by_policy" else "failed"
            out["error"] = tr.get("error") or tr.get("status") or "execution failed"
    return out


async def recover(state: dict, config: RunnableConfig) -> dict:
    deps = get_deps(config)
    # Recovery is an ACTION on the screen — it must target the CURRENT screen, not
    # the invalidated one that triggered recovery. Re-observe/parse/detect first.
    from pikvm_agent.vision.mode_detector import detect_mode as _default_detect

    frame = await deps.frames.capture()
    em = await deps.screen_parser.parse(Path(frame.image_path), frame.frame_id, frame.world_version)
    detect = deps.detect_mode or _default_detect
    mode = detect(em.ocr_text, em)
    result: dict[str, Any] = {"action": "none"}
    if deps.recovery is not None:
        result = await deps.recovery.recover(mode, em)
    deps.trace.append("recover", from_status=state.get("status"), mode=mode, result=result)
    return {
        "status": "running", "mode": mode,
        "frame_id": frame.frame_id, "world_version": frame.world_version,
        "frame_path": frame.image_path, "element_map": em.model_dump(), "ocr_text": em.ocr_text,
    }


async def finalise(state: dict, config: RunnableConfig) -> dict:
    deps = get_deps(config)
    status = state.get("status")
    # Only an explicit "done" (the operator declared the task complete) is success.
    # Reaching finalise in any other non-terminal state — e.g. after a failed
    # transaction — is a FAILURE, never a silent "done".
    final = status if status in ("done", "failed", "blocked") else "failed"
    deps.trace.append("finalise", status=final, steps=state.get("step", 0))
    return {"status": final}
