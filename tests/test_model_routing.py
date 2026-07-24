from __future__ import annotations

import json

from pikvm_agent.config import OperatorRoutingConfig
from pikvm_agent.core.models import (
    OperatorDecision,
    OperatorRequest,
    RiskAssessment,
)
from pikvm_agent.graph.deps import GraphDeps
from pikvm_agent.graph.nodes import operator_decide
from pikvm_agent.operator.prompts import build_messages
from pikvm_agent.operator.routing import OperatorModelRouter


def test_router_uses_hard_reasoner_then_fast_controller() -> None:
    router = OperatorModelRouter(
        OperatorRoutingConfig(
            reasoner_lane="deep",
            controller_lane="fast",
            refresh_every_steps=4,
        )
    )

    assert router.select({"step": 0}).lane == "deep"
    routine = router.select({"step": 1, "reasoning_plan": {"steps": ["open"]}})
    assert (routine.role, routine.lane, routine.reason) == (
        "controller",
        "fast",
        "routine_step",
    )
    assert router.select(
        {"step": 2, "reasoning_plan": {}, "model_replan": True}
    ).reason == "explicit_replan"
    assert router.select(
        {
            "step": 3,
            "reasoning_plan": {},
            "verification_result": {"safe_to_continue": False},
        }
    ).reason == "verification_failure"
    assert router.select(
        {"step": 4, "reasoning_plan": {"steps": ["continue"]}}
    ).reason == (
        "periodic_refresh"
    )


def test_operator_prompt_makes_model_role_and_plan_explicit() -> None:
    request = OperatorRequest(
        task="edit the file",
        frame={"id": 1, "world_version": 2},
        model_role="controller",
        model_lane="fast",
        reasoning_plan={"steps": ["focus editor", "type bounded chunk"]},
    )

    payload = json.loads(build_messages(request)[1]["content"])

    assert payload["orchestration"]["role"] == "controller"
    assert payload["orchestration"]["lane"] == "fast"
    assert payload["orchestration"]["reasoning_plan"]["steps"][0] == "focus editor"
    assert "Do not expand" in payload["orchestration"]["instructions"]


async def test_operator_node_passes_lane_and_checkpoints_reasoner_plan() -> None:
    class Trace:
        def __init__(self) -> None:
            self.events = []

        def append(self, kind, **fields) -> None:
            self.events.append((kind, fields))

    class Operator:
        def __init__(self) -> None:
            self.seen = []

        async def decide(self, request, *, lane="default"):
            self.seen.append((request.model_role, lane))
            return OperatorDecision(
                based_on_frame_id=request.frame["id"],
                based_on_world_version=request.frame["world_version"],
                intent="Focus the editor",
                state_assessment={"plan": {"steps": ["focus", "type", "verify"]}},
                risk=RiskAssessment(
                    level="low",
                    category="navigation",
                    requires_human=False,
                ),
                actions=[{"type": "wait", "ms": 200}],
            )

    operator = Operator()
    trace = Trace()
    deps = GraphDeps(
        backend=None,
        frames=None,
        trace=trace,
        screen_parser=None,
        operator=operator,
        policy=None,
        model_router=OperatorModelRouter(OperatorRoutingConfig()),
    )
    result = await operator_decide(
        {
            "task": "edit",
            "step": 0,
            "frame_id": 7,
            "world_version": 11,
        },
        {"configurable": {"deps": deps}},
    )

    assert operator.seen == [("reasoner", "hard")]
    assert result["reasoning_plan"]["steps"] == ["focus", "type", "verify"]
    assert result["model_role"] == "reasoner"
    assert result["model_lane"] == "hard"
    assert trace.events[-1][1]["route_reason"] == "initial_plan"


async def test_legacy_operator_without_lane_remains_supported() -> None:
    class Trace:
        def append(self, _kind, **_fields) -> None:
            pass

    class LegacyOperator:
        async def decide(self, request):
            return OperatorDecision(
                based_on_frame_id=request.frame["id"],
                based_on_world_version=request.frame["world_version"],
                intent="DONE",
                risk=RiskAssessment(
                    level="low",
                    category="read_only_inspection",
                    requires_human=False,
                ),
                actions=[],
            )

    deps = GraphDeps(
        backend=None,
        frames=None,
        trace=Trace(),
        screen_parser=None,
        operator=LegacyOperator(),
        policy=None,
        model_router=OperatorModelRouter(OperatorRoutingConfig()),
    )
    result = await operator_decide(
        {"task": "inspect", "frame_id": 1, "world_version": 1},
        {"configurable": {"deps": deps}},
    )

    assert result["status"] == "done"
