"""LangGraph state schema.

A single ``AgentState`` dict flows through the graph and is checkpointed after
every node, so a session can pause on an approval interrupt and resume — or
survive a daemon restart — with its full context intact. Runtime services are
NOT stored here (they aren't serializable); they are injected per-invocation via
GraphDeps in the run config.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class AgentState(TypedDict, total=False):
    session_id: str
    task: str

    # Freshness / world (the plan-invalidation stamp every decision cites).
    frame_id: int
    world_version: int
    frame_path: str
    frame_age_ms: int

    # Vision evidence.
    element_map: dict[str, Any]
    ocr_text: str

    # Detected state.
    active_app: str
    mode: str
    keyboard_state: dict[str, Any]

    # Rolling context.
    recent_events: list[dict[str, Any]]
    recent_actions: list[dict[str, Any]]

    # Per-iteration artefacts.
    operator_decision: dict[str, Any]
    policy_result: dict[str, Any]
    transaction_result: dict[str, Any]
    verification_result: dict[str, Any]

    approval_request: dict[str, Any]
    approval_response: dict[str, Any]
    approved: bool
    replan: bool  # human edited/responded — re-plan instead of executing

    # Loop control.
    step: int
    max_steps: int
    # Controller epoch captured when the current decision was made. If the LIVE epoch
    # (bumped by abort / panic / steering) differs at execute time, the decision is
    # stale and the transaction is refused — the hard control gate.
    control_epoch: int

    status: Literal[
        "running",
        "needs_approval",
        "human_takeover",
        "blocked",
        "failed",
        "done",
    ]

    error: str
