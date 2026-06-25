"""Operator decision schema + validation.

The operator must return strict JSON only. The canonical contract lives in
:mod:`pikvm_agent.core.models`; this module re-exports it (so call sites import
the *operator* schema, not several copies) and adds the validation + JSON-schema
helpers the OpenRouter client needs in a later phase.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from pikvm_agent.core.errors import OperatorError
from pikvm_agent.core.models import (
    Action,
    OperatorDecision,
    OperatorRequest,
    RiskAssessment,
)

__all__ = [
    "Action",
    "OperatorDecision",
    "OperatorRequest",
    "RiskAssessment",
    "validate_decision",
    "decision_json_schema",
]


def validate_decision(raw: dict[str, Any] | str) -> OperatorDecision:
    """Validate a raw operator response into an :class:`OperatorDecision`.

    Accepts either a JSON string (from the model) or an already-parsed dict.
    Raises :class:`OperatorError` on malformed JSON or schema-invalid payloads,
    embedding the underlying error so the malformed response never executes.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OperatorError(f"operator returned invalid JSON: {exc}") from exc
    try:
        return OperatorDecision.model_validate(raw)
    except ValidationError as exc:
        raise OperatorError(f"operator decision failed validation: {exc}") from exc


def decision_json_schema() -> dict[str, Any]:
    """JSON Schema for :class:`OperatorDecision` (for structured-output requests)."""
    return OperatorDecision.model_json_schema()
