"""Tests for the operator package.

Covers decision validation (malformed JSON + schema), the JSON-schema helper,
model-lane selection with fallback, prompt assembly, and the deterministic
:class:`FakeOperator` (cites the shown frame, low-risk, round-trips pydantic).
No network: these are pure-logic checks for Phase 3/5 plumbing.
"""

from __future__ import annotations

import asyncio

import pytest

from pikvm_agent.config import OperatorConfig
from pikvm_agent.core.errors import OperatorError
from pikvm_agent.core.models import OperatorDecision, OperatorRequest
from pikvm_agent.operator.fake import FakeOperator
from pikvm_agent.operator.models import select_lane
from pikvm_agent.operator.prompts import OPERATOR_SYSTEM_RULES, build_messages
from pikvm_agent.operator.schemas import decision_json_schema, validate_decision


def _well_formed_decision() -> dict:
    return {
        "based_on_frame_id": 18429,
        "based_on_world_version": 702,
        "intent": "Open VS Code Quick Open.",
        "state_assessment": {"active_app": "vscode", "confidence": 0.88},
        "risk": {
            "level": "low",
            "category": "navigation",
            "requires_human": False,
            "reason": "",
        },
        "preconditions": {"no_blocking_popup": True},
        "actions": [{"type": "keypress", "keys": ["CTRL", "P"]}],
        "postconditions": {"verify_mode": "vscode.quick_open"},
        "fallback": "reobserve",
    }


def test_validate_decision_rejects_bad_payload() -> None:
    with pytest.raises(OperatorError):
        validate_decision('{"bad": true}')


def test_validate_decision_rejects_invalid_json() -> None:
    with pytest.raises(OperatorError):
        validate_decision("{not json")


def test_validate_decision_accepts_well_formed() -> None:
    decision = validate_decision(_well_formed_decision())
    assert isinstance(decision, OperatorDecision)
    assert decision.based_on_frame_id == 18429
    assert decision.actions[0].type == "keypress"


def test_decision_json_schema_has_properties() -> None:
    schema = decision_json_schema()
    assert isinstance(schema, dict)
    assert "properties" in schema


def test_select_lane_named_and_fallback() -> None:
    cfg = OperatorConfig()
    assert select_lane(cfg, "hard") == cfg.lanes["hard"].model
    # Unknown hint falls back to the default lane.
    assert select_lane(cfg, "does-not-exist") == cfg.lanes["default"].model


def test_select_lane_raises_without_lanes() -> None:
    cfg = OperatorConfig(lanes={})
    with pytest.raises(OperatorError):
        select_lane(cfg)


def test_build_messages_includes_rules_and_task() -> None:
    req = OperatorRequest(
        task="open the readme",
        frame={"id": 1, "world_version": 1, "image": "", "age_ms": 0},
    )
    messages = build_messages(req)
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == OPERATOR_SYSTEM_RULES
    assert messages[1]["role"] == "user"
    assert "open the readme" in messages[1]["content"]


def test_fake_operator_cites_frame_and_is_low_risk() -> None:
    req = OperatorRequest(
        task="t",
        frame={"id": 18429, "world_version": 702, "image": "", "age_ms": 50},
    )
    decision = asyncio.run(FakeOperator().decide(req))
    assert isinstance(decision, OperatorDecision)
    assert decision.based_on_frame_id == req.frame["id"]
    assert decision.based_on_world_version == req.frame["world_version"]
    assert decision.risk.requires_human is False
    assert decision.risk.level == "low"
    # Round-trips through pydantic.
    assert OperatorDecision.model_validate(decision.model_dump()) == decision


def test_fake_operator_replays_scripted_then_falls_back() -> None:
    req = OperatorRequest(
        task="t",
        frame={"id": 5, "world_version": 9, "image": "", "age_ms": 0},
    )
    scripted = validate_decision(_well_formed_decision())
    op = FakeOperator(scripted=[scripted])
    first = asyncio.run(op.decide(req))
    assert first == scripted
    # Script exhausted -> deterministic no-op citing the shown frame.
    second = asyncio.run(op.decide(req))
    assert second.based_on_frame_id == 5
    assert second.based_on_world_version == 9
    assert second.intent == "fake: no-op observe step"


def test_fake_operator_honours_keys_hint() -> None:
    req = OperatorRequest(
        task="t",
        frame={"id": 2, "world_version": 3, "image": "", "age_ms": 0, "keys": ["ENTER"]},
    )
    decision = asyncio.run(FakeOperator().decide(req))
    assert decision.actions[0].type == "keypress"
    assert decision.actions[0].keys == ["ENTER"]
