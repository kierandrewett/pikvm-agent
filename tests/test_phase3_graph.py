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


async def test_parse_screen_skips_when_world_unchanged(tmp_path) -> None:
    # Perf: parse_screen must reuse the cached element map (skip OmniParser + OCR) when
    # world_version is unchanged, and re-stamp frame_id so the actionability gate passes.
    import os as _os

    from pikvm_agent.core.models import ElementMap, VisualElement, BBox
    from pikvm_agent.graph.nodes import parse_screen

    _os.environ["PIKVM_AGENT_FAKE"] = "1"
    calls = {"n": 0}

    class CountingParser:
        async def parse(self, path, frame_id, world_version):
            calls["n"] += 1
            el = VisualElement(id="e0", frame_id=frame_id, world_version=world_version,
                               bbox=BBox(x=1, y=1, w=10, h=10), kind="button", text="OK")
            return ElementMap(frame_id=frame_id, world_version=world_version,
                              elements=[el], ocr_text="hello")

    backend = FakeBackend()
    deps = GraphDeps(
        backend=backend, frames=FrameStore("s", tmp_path, backend), trace=TraceLog("s", tmp_path),
        screen_parser=CountingParser(), operator=ScriptOp([]),
        policy=SafetyPolicyEngine(PolicyConfig()),
    )
    config = {"configurable": {"deps": deps, "thread_id": "p"}}

    # First parse at world_version 7, frame 1 -> real parse.
    s1 = {"frame_path": str(tmp_path / "f.jpg"), "frame_id": 1, "world_version": 7}
    out1 = await parse_screen(s1, config)
    assert calls["n"] == 1 and out1["element_map"]["world_version"] == 7

    # Same world_version, NEW frame id 2 -> skipped; reuse map but re-stamp frame_id.
    s2 = {**out1, "frame_path": str(tmp_path / "f.jpg"), "frame_id": 2, "world_version": 7}
    out2 = await parse_screen(s2, config)
    assert calls["n"] == 1  # NOT re-parsed
    assert out2["element_map"]["frame_id"] == 2
    assert out2["element_map"]["elements"][0]["frame_id"] == 2  # elements re-stamped too
    assert out2["element_map"]["elements"][0]["world_version"] == 7  # world invariant kept

    # World moved (8) -> parse runs again.
    s3 = {**out2, "frame_path": str(tmp_path / "f.jpg"), "frame_id": 3, "world_version": 8}
    await parse_screen(s3, config)
    assert calls["n"] == 2
