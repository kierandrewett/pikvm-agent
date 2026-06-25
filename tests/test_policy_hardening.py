"""Regression tests from the GPT-5.5 (Codex) Sprint A review — policy engine."""

from __future__ import annotations

from pikvm_agent.config import PolicyConfig
from pikvm_agent.core.models import ApprovalResponse, OperatorDecision, RiskAssessment
from pikvm_agent.policy.approvals import recheck_after_approval
from pikvm_agent.policy.safety import SafetyPolicyEngine


def _dec(category: str, *, frame: int = 1, world: int = 1, requires_human: bool = False,
         level: str = "low", actions=None) -> OperatorDecision:
    return OperatorDecision(
        based_on_frame_id=frame,
        based_on_world_version=world,
        intent="t",
        risk=RiskAssessment(level=level, category=category, requires_human=requires_human),
        actions=actions or [{"type": "wait", "ms": 100}],
    )


def test_fresh_approved_decision_proceeds() -> None:
    # P1.1: a require-human category needs approval, but once approved + fresh, runs.
    eng = SafetyPolicyEngine(PolicyConfig())
    d = _dec("communication_send")
    assert eng.policy_gate(d, 1, 1).status == "approval_required"
    assert eng.policy_gate(d, 1, 1, approved=True).status == "allowed"


def test_approval_does_not_bypass_freshness() -> None:
    eng = SafetyPolicyEngine(PolicyConfig())
    d = _dec("communication_send", frame=1, world=1)
    stale = eng.policy_gate(d, 2, 1, approved=True)
    assert stale.status == "blocked" and stale.reason == "stale_frame"


def test_hard_block_wins_over_approval() -> None:
    eng = SafetyPolicyEngine(
        PolicyConfig(require_human_for=["disk_or_partition"], always_block=["disk_or_partition"])
    )
    d = _dec("disk_or_partition", requires_human=True)
    assert eng.policy_gate(d, 1, 1).status == "blocked"
    assert eng.policy_gate(d, 1, 1, approved=True).status == "blocked"  # not approvable


def test_recheck_after_approval_flow() -> None:
    eng = SafetyPolicyEngine(PolicyConfig())
    d = _dec("communication_send")
    assert recheck_after_approval(ApprovalResponse(type="approve"), d, 1, 1, eng).status == "allowed"
    assert recheck_after_approval(ApprovalResponse(type="reject"), d, 1, 1, eng).status == "blocked"
    # approve on a screen that moved on -> still blocked by freshness
    assert recheck_after_approval(ApprovalResponse(type="approve"), d, 9, 9, eng).status == "blocked"
