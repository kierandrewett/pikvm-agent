"""StateGraph construction.

Wires the owned nodes into the observe → parse → detect → decide → validate →
policy → [interrupt] → execute → verify → continue/recover/finalise loop, with a
checkpointer so the graph can interrupt for approval and resume (or survive a
restart). Mirrors ``docs/PLAN.md`` → *LangGraph graph*.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from pikvm_agent.graph.nodes import (
    budget_pause,
    detect_state,
    execute_transaction,
    finalise,
    human_interrupt,
    observe_frame,
    operator_decide,
    parse_screen,
    policy_gate,
    recover,
    validate_decision,
    verify_result,
)
from pikvm_agent.graph.routing import (
    route_after_interrupt,
    route_after_policy,
    route_after_verify,
)
from pikvm_agent.graph.state import AgentState


def build_graph(checkpointer: Any) -> Any:
    builder = StateGraph(AgentState)

    builder.add_node("observe_frame", observe_frame)
    builder.add_node("parse_screen", parse_screen)
    builder.add_node("detect_state", detect_state)
    builder.add_node("operator_decide", operator_decide)
    builder.add_node("validate_decision", validate_decision)
    builder.add_node("policy_gate", policy_gate)
    builder.add_node("human_interrupt", human_interrupt)
    builder.add_node("execute_transaction", execute_transaction)
    builder.add_node("verify_result", verify_result)
    builder.add_node("recover", recover)
    builder.add_node("budget_pause", budget_pause)
    builder.add_node("finalise", finalise)

    builder.add_edge(START, "observe_frame")
    builder.add_edge("observe_frame", "parse_screen")
    builder.add_edge("parse_screen", "detect_state")
    builder.add_edge("detect_state", "operator_decide")
    builder.add_edge("operator_decide", "validate_decision")
    builder.add_edge("validate_decision", "policy_gate")

    builder.add_conditional_edges(
        "policy_gate",
        route_after_policy,
        {
            "approval": "human_interrupt",
            "stale": "observe_frame",
            "blocked": "finalise",
            "allowed": "execute_transaction",
            "done": "finalise",
        },
    )
    builder.add_conditional_edges(
        "human_interrupt",
        route_after_interrupt,
        {"execute": "execute_transaction", "replan": "observe_frame", "blocked": "finalise"},
    )
    builder.add_edge("execute_transaction", "verify_result")
    builder.add_conditional_edges(
        "verify_result",
        route_after_verify,
        {
            "continue": "observe_frame",
            "recover": "recover",
            "pause": "budget_pause",
            "done": "finalise",
            "failed": "finalise",
        },
    )
    builder.add_edge("recover", "observe_frame")
    builder.add_edge("budget_pause", "observe_frame")  # resume -> keep going
    builder.add_edge("finalise", END)

    return builder.compile(checkpointer=checkpointer)
