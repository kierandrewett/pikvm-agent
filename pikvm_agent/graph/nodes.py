"""Graph node functions.

Each node is pure control flow over ``AgentState`` plus delegation to the owned
services in ``GraphDeps`` (pulled from the run config). Nodes never contain
PiKVM-specific logic directly — they call the backend / parser / operator /
policy / executor. The cardinal invariants live in the services: freshness in
the policy gate, verification in the executor.
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig

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
    return {"operator_decision": dd, "step": step, "status": "done" if done else "running"}


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
        event = {"type": f"human_{rtype}", "message": response.get("message"),
                 "instruction": response.get("instruction")}
        deps.trace.append("human_response", type=rtype)
        return {
            "approval_response": response, "approved": True, "status": "running",
            "recent_events": (state.get("recent_events", []) + [event])[-10:],
        }
    deps.trace.append("approval_denied", type=rtype)
    return {"approval_response": response, "status": "blocked",
            "error": response.get("reason", f"human {rtype}")}


async def execute_transaction(state: dict, config: RunnableConfig) -> dict:
    deps = get_deps(config)
    decision = state["operator_decision"]
    # Approval is NOT force-execute: re-check freshness + hard-block before acting.
    pr = deps.policy.policy_gate(
        decision, state["frame_id"], state["world_version"], approved=state.get("approved", False)
    )
    if pr.status != "allowed":
        deps.trace.append("execute_refused", reason=pr.reason)
        stale = pr.reason in ("stale_frame", "stale_world")
        return {
            "transaction_result": {
                "status": "failed_stale_frame" if stale else "blocked_by_policy",
                "error": pr.reason,
            },
            "status": "running" if stale else "blocked",
            "approved": False,
        }

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
    execute = deps.execute or (lambda t, s: _record_only_execute(deps, t))
    result = await execute(tx, state)
    if not isinstance(result, TransactionResult):
        result = TransactionResult.model_validate(result)
    deps.trace.append("executed", status=result.status,
                      actions=[a["type"] for a in decision["actions"]])
    return {
        "transaction_result": result.model_dump(),
        "approved": False,
        "recent_actions": (state.get("recent_actions", []) + [{"intent": decision["intent"]}])[-10:],
    }


async def verify_result(state: dict, config: RunnableConfig) -> dict:
    # Routing happens in route_after_verify; this node attaches the verification
    # carried by the transaction result (the executor is the verifier).
    tr = state.get("transaction_result") or {}
    return {"verification_result": tr.get("verification") or {}}


async def recover(state: dict, config: RunnableConfig) -> dict:
    deps = get_deps(config)
    deps.trace.append("recover", from_status=state.get("status"))
    return {"status": "running"}


async def finalise(state: dict, config: RunnableConfig) -> dict:
    deps = get_deps(config)
    status = state.get("status")
    final = status if status in ("failed", "blocked") else "done"
    deps.trace.append("finalise", status=final, steps=state.get("step", 0))
    return {"status": final}
