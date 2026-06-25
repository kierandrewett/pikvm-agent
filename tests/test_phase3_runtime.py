"""Phase 3 — the runtime drives the graph end to end (start -> continue ->
approval -> resume -> done), the path the daemon/MCP expose."""

from __future__ import annotations

from pikvm_agent.core.models import OperatorDecision, OperatorRequest, RiskAssessment
from pikvm_agent.runtime import Runtime


class ScriptOp:
    def __init__(self, plans: list[dict]) -> None:
        self.plans = plans
        self.i = 0

    async def decide(self, request: OperatorRequest) -> OperatorDecision:
        plan = self.plans[min(self.i, len(self.plans) - 1)]
        self.i += 1
        return OperatorDecision(
            based_on_frame_id=request.frame["id"],
            based_on_world_version=request.frame["world_version"],
            intent=plan["intent"], risk=RiskAssessment(**plan["risk"]), actions=plan["actions"],
        )


_LOW = {"level": "low", "category": "navigation", "requires_human": False}
_HUMAN = {"level": "medium", "category": "communication_send", "requires_human": True}
_DONE = {"intent": "DONE complete",
         "risk": {"level": "low", "category": "read_only_inspection", "requires_human": False},
         "actions": []}


async def test_runtime_drives_graph_to_done(runtime: Runtime) -> None:
    runtime._operator = ScriptOp([
        {"intent": "press a key", "risk": _LOW, "actions": [{"type": "keypress", "keys": ["KeyA"]}]},
        _DONE,
    ])
    started = await runtime.start_session("do a thing")
    sid = started["session_id"]
    out = await runtime.continue_session(sid)
    assert out["status"] == "done"
    assert out["frame_id"] and out["world_version"]


async def test_runtime_approval_round_trip(runtime: Runtime) -> None:
    runtime._operator = ScriptOp([
        {"intent": "send the message", "risk": _HUMAN,
         "actions": [{"type": "keypress", "keys": ["ControlLeft", "Enter"]}]},
        _DONE,
    ])
    started = await runtime.start_session("send something")
    sid = started["session_id"]

    paused = await runtime.continue_session(sid)
    assert paused["status"] == "needs_approval"
    appr = paused["approval_request"]
    assert appr["approval_id"] and appr["risk"] == "communication_send"

    # the approval was persisted as pending
    pend = await runtime.store.pending_approvals(sid)
    assert len(pend) == 1

    resumed = await runtime.submit_approval(sid, appr["approval_id"], {"type": "approve"})
    assert resumed["status"] == "done"
    # and resolved
    stored = await runtime.store.get_approval(appr["approval_id"])
    assert stored["status"] == "approved"


async def test_runtime_reject_blocks(runtime: Runtime) -> None:
    runtime._operator = ScriptOp([
        {"intent": "send the message", "risk": _HUMAN,
         "actions": [{"type": "keypress", "keys": ["ControlLeft", "Enter"]}]},
    ])
    started = await runtime.start_session("send something")
    sid = started["session_id"]
    paused = await runtime.continue_session(sid)
    appr = paused["approval_request"]
    out = await runtime.submit_approval(sid, appr["approval_id"], {"type": "reject", "reason": "no"})
    assert out["status"] == "blocked"
