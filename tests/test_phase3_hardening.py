"""Regression tests from the GPT-5.5 (Codex) Sprint B review — graph/runtime."""

from __future__ import annotations

import os

from langgraph.types import Command

from pikvm_agent.config import AppConfig, PolicyConfig
from pikvm_agent.core.models import OperatorDecision, OperatorRequest, RiskAssessment, TransactionResult
from pikvm_agent.graph.checkpoints import build_checkpointer
from pikvm_agent.graph.deps import GraphDeps
from pikvm_agent.graph.graph import build_graph
from pikvm_agent.pikvm.fake import FakeBackend
from pikvm_agent.policy.safety import SafetyPolicyEngine
from pikvm_agent.runtime import Runtime
from pikvm_agent.store.frames import FrameStore
from pikvm_agent.store.trace import TraceLog
from pikvm_agent.vision.providers import build_screen_parser


class ScriptOp:
    def __init__(self, plans: list[dict]) -> None:
        self.plans = plans
        self.i = 0

    async def decide(self, request: OperatorRequest) -> OperatorDecision:
        plan = self.plans[min(self.i, len(self.plans) - 1)]
        self.i += 1
        return OperatorDecision(
            based_on_frame_id=request.frame["id"], based_on_world_version=request.frame["world_version"],
            intent=plan["intent"], risk=RiskAssessment(**plan["risk"]), actions=plan["actions"],
        )


_LOW = {"level": "low", "category": "navigation", "requires_human": False}
_HUMAN = {"level": "medium", "category": "communication_send", "requires_human": True}
_SEND = {"intent": "send the message", "risk": _HUMAN,
         "actions": [{"type": "keypress", "keys": ["ControlLeft", "Enter"]}]}
_DONE = {"intent": "DONE complete",
         "risk": {"level": "low", "category": "read_only_inspection", "requires_human": False},
         "actions": []}


async def test_stale_world_after_approval_is_refused(runtime: Runtime) -> None:
    # P1.1: if the screen changes during the human's deliberation, the approved
    # action must be refused as stale — not executed against the new world.
    runtime._operator = ScriptOp([_SEND, _DONE])
    sid = (await runtime.start_session("send"))["session_id"]
    sr = runtime._get(sid)
    paused = await runtime.continue_session(sid)
    assert paused["status"] == "needs_approval"

    runtime.backend.set_screen("a dialog opened over the target", bg=(200, 20, 20))
    out = await runtime.submit_approval(sid, paused["approval_request"]["approval_id"], {"type": "approve"})

    kinds = [e["kind"] for e in sr.trace.read()]
    assert any(e["kind"] == "execute_refused" and e.get("reason") == "stale_world"
               for e in sr.trace.read())
    assert "executed" not in kinds  # the stale send never ran
    assert out["status"] in ("done", "running", "failed")  # it recovered, not executed


async def test_edit_replans_does_not_execute_original(runtime: Runtime) -> None:
    # P1.2: edit/respond re-plans; the original (superseded) action never runs.
    runtime._operator = ScriptOp([
        _SEND,
        {"intent": "press X", "risk": _LOW, "actions": [{"type": "keypress", "keys": ["KeyX"]}]},
        _DONE,
    ])
    sid = (await runtime.start_session("send"))["session_id"]
    sr = runtime._get(sid)
    executed: list[str] = []

    async def recorder(tx, _state) -> TransactionResult:
        executed.append(tx.intent)
        return TransactionResult(status="executed", executed_actions=[a.model_dump() for a in tx.actions])

    sr.deps.execute = recorder
    paused = await runtime.continue_session(sid)
    out = await runtime.submit_approval(sid, paused["approval_request"]["approval_id"],
                                        {"type": "edit", "instruction": "use X instead"})
    assert out["status"] == "done"
    assert "send the message" not in executed  # original never executed
    assert "press X" in executed               # the re-planned action did


async def test_invalid_approval_id_is_rejected(runtime: Runtime) -> None:
    # P1.3: a stale/mistyped approval id must not resume/approve the graph.
    runtime._operator = ScriptOp([_SEND])
    sid = (await runtime.start_session("send"))["session_id"]
    paused = await runtime.continue_session(sid)
    real_id = paused["approval_request"]["approval_id"]
    out = await runtime.submit_approval(sid, "bogus-id", {"type": "approve"})
    assert out["status"] == "error"
    # the real approval is untouched / still pending
    pend = await runtime.store.pending_approvals(sid)
    assert any(a["id"] == real_id for a in pend)


async def test_max_steps_exhaustion_is_failed(tmp_path) -> None:
    # P2.4: never report a step-capped loop as "done".
    os.environ["PIKVM_AGENT_FAKE"] = "1"
    backend = FakeBackend()
    deps = GraphDeps(
        backend=backend, frames=FrameStore("s", tmp_path, backend), trace=TraceLog("s", tmp_path),
        screen_parser=build_screen_parser(AppConfig(), backend),
        operator=ScriptOp([{"intent": "loop forever", "risk": _LOW,
                            "actions": [{"type": "keypress", "keys": ["KeyA"]}]}]),
        policy=SafetyPolicyEngine(PolicyConfig()), max_steps=3,
    )
    graph = build_graph(await build_checkpointer(None))
    config = {"configurable": {"deps": deps, "thread_id": "cap"}}
    result = await graph.ainvoke({"session_id": "s", "task": "t", "step": 0, "max_steps": 3}, config)
    assert result["status"] == "failed"
    assert "max_steps" in result.get("error", "")


async def test_failed_transaction_is_failed_not_done(tmp_path) -> None:
    # A failed action must finalise as "failed" with its reason — never a silent "done".
    os.environ["PIKVM_AGENT_FAKE"] = "1"
    backend = FakeBackend()

    async def _failing_execute(tx, state):
        return TransactionResult(status="failed", error="click missed: element not found")

    deps = GraphDeps(
        backend=backend, frames=FrameStore("s", tmp_path, backend), trace=TraceLog("s", tmp_path),
        screen_parser=build_screen_parser(AppConfig(), backend),
        operator=ScriptOp([{"intent": "click the Chat icon", "risk": _LOW,
                            "actions": [{"type": "keypress", "keys": ["KeyA"]}]}]),
        policy=SafetyPolicyEngine(PolicyConfig()), max_steps=5, execute=_failing_execute,
    )
    graph = build_graph(await build_checkpointer(None))
    config = {"configurable": {"deps": deps, "thread_id": "fail"}}
    result = await graph.ainvoke({"session_id": "s", "task": "t", "step": 0, "max_steps": 5}, config)
    assert result["status"] == "failed"
    assert "click missed" in result.get("error", "")


async def test_control_epoch_change_refuses_execution(tmp_path) -> None:
    # If the controller epoch changes between decide and execute (an abort / panic /
    # steer happened), the transaction is REFUSED — the action never runs.
    os.environ["PIKVM_AGENT_FAKE"] = "1"
    backend = FakeBackend()
    executed: list = []

    async def _record_execute(tx, state):
        executed.append(tx)
        return TransactionResult(status="verified")

    seq = iter([0, 1])  # decide() captures epoch 0; execute() then sees 1 -> stale
    deps = GraphDeps(
        backend=backend, frames=FrameStore("s", tmp_path, backend), trace=TraceLog("s", tmp_path),
        screen_parser=build_screen_parser(AppConfig(), backend),
        operator=ScriptOp([{"intent": "click something", "risk": _LOW,
                            "actions": [{"type": "keypress", "keys": ["KeyA"]}]}]),
        policy=SafetyPolicyEngine(PolicyConfig()), max_steps=5, execute=_record_execute,
        control_epoch_getter=lambda: next(seq, 1),
    )
    graph = build_graph(await build_checkpointer(None))
    config = {"configurable": {"deps": deps, "thread_id": "ce"}}
    result = await graph.ainvoke({"session_id": "s", "task": "t", "step": 0, "max_steps": 5}, config)
    assert result["status"] == "failed"
    assert "control" in result.get("error", "").lower()
    assert executed == []  # the action was refused, never executed


async def test_panic_stop_halts_all_sessions(runtime: Runtime) -> None:
    sid = (await runtime.start_session("do a thing"))["session_id"]
    epoch0 = runtime._sessions[sid].control_epoch
    res = await runtime.panic_stop()
    assert res["ok"] and sid in res["stopped"]
    assert runtime._sessions[sid].status == "failed"
    assert runtime._sessions[sid].control_epoch == epoch0 + 1  # in-flight plans invalidated
    assert runtime._sessions[sid].stopped is True               # sticky terminal latch set
    assert any(c[0] == "release_all" for c in runtime.backend.calls)  # held HID released


async def test_panic_stop_halts_paused_session_and_blocks_resume(runtime: Runtime) -> None:
    # Codex P1/P2a: a budget-PAUSED session must be halted by a panic AND must not be
    # resumable afterwards — otherwise the next continue re-plans under the bumped epoch
    # and the emergency stop is not terminal.
    runtime._operator = ScriptOp([{"intent": "tap", "risk": _LOW,
                                   "actions": [{"type": "keypress", "keys": ["KeyA"]}]}])
    sid = (await runtime.start_session("loop"))["session_id"]
    paused = await runtime.continue_session(sid, max_transactions=1)
    assert paused["status"] == "paused"

    res = await runtime.panic_stop()
    assert sid in res["stopped"]                       # the paused session WAS halted
    assert runtime._sessions[sid].status == "failed"
    assert runtime._sessions[sid].stopped is True

    # Resuming a stopped session must NOT run the loop again — it stays failed.
    resumed = await runtime.continue_session(sid, max_transactions=1)
    assert resumed["status"] == "failed"


async def test_abort_blocks_resume(runtime: Runtime) -> None:
    # The same latch applies to a normal abort: once aborted, continue can't revive it.
    runtime._operator = ScriptOp([{"intent": "tap", "risk": _LOW,
                                   "actions": [{"type": "keypress", "keys": ["KeyA"]}]}])
    sid = (await runtime.start_session("loop"))["session_id"]
    await runtime.continue_session(sid, max_transactions=1)
    await runtime.abort_session(sid, "human stop")
    resumed = await runtime.continue_session(sid, max_transactions=1)
    assert resumed["status"] == "failed"


async def test_expired_deadline_pauses_before_executing(tmp_path) -> None:
    # Codex P2b: if the per-call TIME budget is already spent when we reach execution
    # (slow observe/parse/operator), the action must NOT run — it defers to a resumable
    # pause instead of doing HID after the deadline.
    os.environ["PIKVM_AGENT_FAKE"] = "1"
    backend = FakeBackend()
    executed: list = []

    async def _exec(tx, state):
        executed.append(tx.intent)
        return TransactionResult(status="verified")

    deps = GraphDeps(
        backend=backend, frames=FrameStore("s", tmp_path, backend), trace=TraceLog("s", tmp_path),
        screen_parser=build_screen_parser(AppConfig(), backend),
        operator=ScriptOp([{"intent": "click", "risk": _LOW,
                            "actions": [{"type": "keypress", "keys": ["KeyA"]}]}]),
        policy=SafetyPolicyEngine(PolicyConfig()), max_steps=10, execute=_exec,
    )
    graph = build_graph(await build_checkpointer(None))
    config = {"configurable": {"deps": deps, "thread_id": "dl"}}

    # deadline_ms = 1.0 is far in the past (monotonic clock is well past 1ms) -> spent.
    r = await graph.ainvoke(
        {"session_id": "s", "task": "t", "step": 0, "max_steps": 10,
         "max_transactions": 0, "tx_this_call": 0, "deadline_ms": 1.0}, config)
    assert "__interrupt__" in r
    assert r["__interrupt__"][0].value.get("reason") == "budget_paused"
    assert executed == []  # the deadline pre-check deferred the action — nothing ran


async def test_bounded_continue_pauses_then_resumes(tmp_path) -> None:
    # A per-call budget of 1 transaction runs ONE action then pauses (resumable);
    # resuming with a fresh budget runs one more — so the agent can't run unbounded.
    os.environ["PIKVM_AGENT_FAKE"] = "1"
    backend = FakeBackend()
    executed: list = []

    async def _exec(tx, state):
        executed.append(tx.intent)
        return TransactionResult(status="verified")

    deps = GraphDeps(
        backend=backend, frames=FrameStore("s", tmp_path, backend), trace=TraceLog("s", tmp_path),
        screen_parser=build_screen_parser(AppConfig(), backend),
        operator=ScriptOp([{"intent": "click", "risk": _LOW,
                            "actions": [{"type": "keypress", "keys": ["KeyA"]}]}]),
        policy=SafetyPolicyEngine(PolicyConfig()), max_steps=10, execute=_exec,
    )
    graph = build_graph(await build_checkpointer(None))
    config = {"configurable": {"deps": deps, "thread_id": "bud"}}

    r1 = await graph.ainvoke(
        {"session_id": "s", "task": "t", "step": 0, "max_steps": 10,
         "max_transactions": 1, "tx_this_call": 0, "deadline_ms": 0}, config)
    assert "__interrupt__" in r1
    assert r1["__interrupt__"][0].value.get("reason") == "budget_paused"
    assert len(executed) == 1  # exactly one transaction this call

    r2 = await graph.ainvoke(
        Command(resume=None, update={"tx_this_call": 0, "max_transactions": 1, "deadline_ms": 0}), config)
    assert "__interrupt__" in r2
    assert len(executed) == 2  # resumed and ran exactly one more
