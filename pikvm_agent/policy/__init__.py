"""Hard-coded safety policy: command risk, the policy gate, and approvals.

The policy is *owned and hard-coded*, never prompt-only. It classifies risk from
the proposal text, gates on plan freshness and the policy profile, and treats an
approval as a re-check rather than a force-execute. The graph's ``policy_gate``
node delegates here; it does not embed policy of its own.
"""

from __future__ import annotations

from pikvm_agent.policy.approvals import make_approval_request, recheck_after_approval
from pikvm_agent.policy.risk import (
    DANGEROUS_COMMAND_RE,
    HIGH_RISK_CHARS,
    CommandRisk,
    classify_command,
    requires_strict_verification,
)
from pikvm_agent.policy.safety import LocalRisk, SafetyPolicyEngine

__all__ = [
    "DANGEROUS_COMMAND_RE",
    "HIGH_RISK_CHARS",
    "CommandRisk",
    "LocalRisk",
    "SafetyPolicyEngine",
    "classify_command",
    "make_approval_request",
    "recheck_after_approval",
    "requires_strict_verification",
]
