"""Phase 3 — LangGraph orchestration acceptance.

    * observe -> parse -> decide -> policy -> execute -> ... -> finalise runs
    * the graph pauses on an approval interrupt and resumes
    * state survives a (simulated) restart via the SQLite checkpointer
"""

from __future__ import annotations

import os

import pytest
from langgraph.types import Command

from pikvm_agent.config import AppConfig, PolicyConfig
from pikvm_agent.core.models import OperatorDecision, OperatorRequest, RiskAssessment
from pikvm_agent.graph.checkpoints import build_checkpointer, close_checkpointer
from pikvm_agent.graph.deps import GraphDeps
from pikvm_agent.graph.graph import build_graph
from pikvm_agent.pikvm.fake import FakeBackend
from pikvm_agent.policy.safety import SafetyPolicyEngine
from pikvm_agent.store.frames import FrameStore
from pikvm_agent.store.trace import TraceLog
from pikvm_agent.vision.providers import build_screen_parser


class ScriptOp:
    """A test operator that emits planned decisions citing the *live* frame."""

    def __init__(self, plans: list[dict]) -> None:
        self.plans = plans
        self.i = 0

    async def decide(self, request: OperatorRequest) -> OperatorDecision:
        plan = self.plans[min(self.i, len(self.plans) - 1)]
        self.i += 1
        return OperatorDecision(
            based_on_frame_id=request.frame["id"],
            based_on_world_version=request.frame["world_version"],
            intent=plan["intent"],
            risk=RiskAssessment(**plan["risk"]),
            actions=plan["actions"],
        )


def _deps(tmp_path, operator, session_id="s1") -> GraphDeps:
    backend = FakeBackend()
    return GraphDeps(
        backend=backend,
        frames=FrameStore(session_id, tmp_path, backend),
        trace=TraceLog(session_id, tmp_path),
        screen_parser=build_screen_parser(AppConfig(), backend),
        operator=operator,
        policy=SafetyPolicyEngine(PolicyConfig()),
        max_steps=6,
    )


_LOW = {"level": "low", "category": "navigation", "requires_human": False}
_DONE = {"intent": "DONE complete", "risk": {"level": "low", "category": "read_only_inspection",
                                             "requires_human": False}, "actions": []}


async def test_happy_path_runs_to_finalise(tmp_path) -> None:
    op = ScriptOp([
        {"intent": "press a key", "risk": _LOW, "actions": [{"type": "keypress", "keys": ["KeyA"]}]},
        _DONE,
    ])
    checkpointer = await build_checkpointer(None)
    graph = build_graph(checkpointer)
    config = {"configurable": {"deps": _deps(tmp_path, op), "thread_id": "happy"}}
    result = await graph.ainvoke({"session_id": "s1", "task": "t", "step": 0}, config)
    assert result["status"] == "done"
    assert "__interrupt__" not in result


async def test_pause_on_approval_then_resume(tmp_path) -> None:
    op = ScriptOp([
        {"intent": "send the message", "risk": {"level": "medium", "category": "communication_send",
         "requires_human": True}, "actions": [{"type": "keypress", "keys": ["ControlLeft", "Enter"]}]},
        _DONE,
    ])
    checkpointer = await build_checkpointer(None)
    graph = build_graph(checkpointer)
    config = {"configurable": {"deps": _deps(tmp_path, op), "thread_id": "appr"}}
    paused = await graph.ainvoke({"session_id": "s1", "task": "t", "step": 0}, config)
    assert "__interrupt__" in paused  # the graph stopped at the approval interrupt
    resumed = await graph.ainvoke(Command(resume={"type": "approve"}), config)
    assert resumed["status"] == "done"


async def test_reject_blocks(tmp_path) -> None:
    op = ScriptOp([
        {"intent": "send the message", "risk": {"level": "medium", "category": "communication_send",
         "requires_human": True}, "actions": [{"type": "keypress", "keys": ["ControlLeft", "Enter"]}]},
    ])
    checkpointer = await build_checkpointer(None)
    graph = build_graph(checkpointer)
    config = {"configurable": {"deps": _deps(tmp_path, op), "thread_id": "rej"}}
    await graph.ainvoke({"session_id": "s1", "task": "t", "step": 0}, config)
    out = await graph.ainvoke(Command(resume={"type": "reject", "reason": "no"}), config)
    assert out["status"] == "blocked"


async def test_state_survives_restart(tmp_path) -> None:
    sqlite_path = os.path.join(tmp_path, "ck.sqlite3")
    plans = [
        {"intent": "send the message", "risk": {"level": "medium", "category": "communication_send",
         "requires_human": True}, "actions": [{"type": "keypress", "keys": ["ControlLeft", "Enter"]}]},
        _DONE,
    ]
    # First "process": run to the approval interrupt, then drop the checkpointer.
    ck1 = await build_checkpointer(sqlite_path)
    graph1 = build_graph(ck1)
    config1 = {"configurable": {"deps": _deps(tmp_path, ScriptOp(plans)), "thread_id": "boot"}}
    paused = await graph1.ainvoke({"session_id": "s1", "task": "t", "step": 0}, config1)
    assert "__interrupt__" in paused
    await close_checkpointer(ck1)

    # Second "process": fresh graph + checkpointer + deps on the SAME sqlite file.
    # A stateless operator that, post-approval, sees the task complete -> done.
    ck2 = await build_checkpointer(sqlite_path)
    graph2 = build_graph(ck2)
    deps2 = _deps(tmp_path, ScriptOp([_DONE]), session_id="s2")
    config2 = {"configurable": {"deps": deps2, "thread_id": "boot"}}
    resumed = await graph2.ainvoke(Command(resume={"type": "approve"}), config2)
    assert resumed["status"] == "done"
    # Proof it RESUMED from the persisted interrupt (not a fresh start): the
    # post-restart process executed the approved action.
    kinds = [e["kind"] for e in deps2.trace.read()]
    assert "approved" in kinds and "executed" in kinds
    await close_checkpointer(ck2)
