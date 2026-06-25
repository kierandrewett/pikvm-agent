"""Operator: structured-decision schema, prompts, model lanes, and a fake.

The operator *proposes*; it never executes. This package owns the decision
contract (validation + JSON Schema), the mandatory prompt rules, model-lane
selection, and a deterministic :class:`FakeOperator` for graph/policy tests. The
OpenRouter HTTP client is a later phase and is not imported here.
"""

from __future__ import annotations

from pikvm_agent.operator.fake import FakeOperator
from pikvm_agent.operator.models import select_lane
from pikvm_agent.operator.prompts import (
    OPERATOR_SYSTEM_RULES,
    build_messages,
    build_request_payload,
)
from pikvm_agent.operator.schemas import (
    Action,
    OperatorDecision,
    OperatorRequest,
    RiskAssessment,
    decision_json_schema,
    validate_decision,
)

__all__ = [
    "Action",
    "FakeOperator",
    "OPERATOR_SYSTEM_RULES",
    "OperatorDecision",
    "OperatorRequest",
    "RiskAssessment",
    "build_messages",
    "build_request_payload",
    "decision_json_schema",
    "select_lane",
    "validate_decision",
]
