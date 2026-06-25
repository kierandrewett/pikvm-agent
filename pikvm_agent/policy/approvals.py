"""Approval requests and post-approval re-checking.

Approval is *not* a force-execute button. A human says "yes, this is what I
want", but by the time they answer the world may have moved on — so an approved
decision is re-run through the full policy gate (freshness + policy) before it
may execute. A reject/abort is always a block.
"""

from __future__ import annotations

import uuid
from typing import Any

from pikvm_agent.core.models import (
    ApprovalRequest,
    ApprovalResponse,
    OperatorDecision,
    PolicyResult,
)
from pikvm_agent.policy.safety import SafetyPolicyEngine


def _as_decision(decision: OperatorDecision | dict[str, Any]) -> OperatorDecision:
    if isinstance(decision, OperatorDecision):
        return decision
    return OperatorDecision.model_validate(decision)


def make_approval_request(
    session_id: str,
    decision: OperatorDecision | dict[str, Any],
    frame_id: int,
    world_version: int,
    screenshot_path: str | None = None,
) -> ApprovalRequest:
    """Build an approval request for a decision the policy held for a human.

    The request carries the freshness stamps it was raised against so the
    re-check after approval can detect a stale answer.
    """
    dec = _as_decision(decision)
    return ApprovalRequest(
        approval_id=str(uuid.uuid4()),
        session_id=session_id,
        frame_id=frame_id,
        world_version=world_version,
        risk=dec.risk.category,
        reason=dec.risk.reason or f"category '{dec.risk.category}' requires approval",
        proposed_action=dec.model_dump(),
        screenshot_path=screenshot_path,
    )


def recheck_after_approval(
    response: ApprovalResponse,
    decision: OperatorDecision | dict[str, Any],
    current_frame_id: int,
    current_world_version: int,
    engine: SafetyPolicyEngine,
) -> PolicyResult:
    """Re-run the policy gate after a human responds to an approval.

    A reject/abort blocks. An approve (or edit/respond/take_over) re-runs the
    full gate against the *current* frame/world with ``approved=True`` — so the
    human-approval requirement is satisfied and a fresh decision proceeds, while
    a stale screen still fails freshness and a hard-blocked category is still
    blocked. Approval is never a force-execute button.
    """
    if response.type in ("reject", "abort"):
        return PolicyResult(
            status="blocked",
            blocked=True,
            reason=response.reason or f"human {response.type}",
        )

    return engine.policy_gate(
        decision, current_frame_id, current_world_version, approved=True
    )
