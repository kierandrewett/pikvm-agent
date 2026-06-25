"""Core domain + config invariants."""

from __future__ import annotations

from pikvm_agent.config import load_config
from pikvm_agent.core.models import (
    BBox,
    GuardedTransaction,
    OperatorDecision,
    VerificationResult,
)


def test_discriminated_union_actions_round_trip() -> None:
    d = OperatorDecision.model_validate(
        {
            "based_on_frame_id": 1,
            "based_on_world_version": 2,
            "intent": "open quick open",
            "risk": {"level": "low", "category": "navigation", "requires_human": False},
            "actions": [
                {"type": "keypress", "keys": ["CTRL", "P"]},
                {"type": "type_text", "text": "readme.md"},
                {"type": "scroll", "direction": "up", "amount": 5},
                {"type": "wait_for_mode", "mode": "vscode.quick_open", "timeout_ms": 1000},
            ],
        }
    )
    assert [a.type for a in d.actions] == ["keypress", "type_text", "scroll", "wait_for_mode"]
    assert d.actions[2].amount == 5  # E10: scroll keeps a real amount


def test_scroll_amount_defaults_nonzero() -> None:
    from pikvm_agent.core.models import ScrollAction

    assert ScrollAction(type="scroll", direction="down").amount == 3  # never (0,0)


def test_bbox_center_and_area() -> None:
    b = BBox(x=10, y=20, w=100, h=40)
    assert b.center() == (60, 40)
    assert b.area() == 4000


def test_verification_verified_property() -> None:
    assert VerificationResult(status="verified_exact", safe_to_continue=True).verified is True
    assert VerificationResult(status="verified_safe_normalized", safe_to_continue=True).verified is True
    assert VerificationResult(status="unverified_truncated", safe_to_continue=False).verified is False
    assert VerificationResult(status="failed_symbol_mismatch", safe_to_continue=False).verified is False


def test_guarded_transaction_requires_freshness_stamp() -> None:
    tx = GuardedTransaction(
        id="t1",
        session_id="s1",
        based_on_frame_id=42,
        based_on_world_version=7,
        intent="x",
        actions=[{"type": "keypress", "keys": ["Enter"]}],
        risk={"level": "low", "category": "navigation", "requires_human": False},
    )
    assert tx.based_on_frame_id == 42 and tx.based_on_world_version == 7


def test_config_defaults() -> None:
    c = load_config()
    assert c.operator.provider == "fake"
    assert c.ocr.provider == "tesseract"
    assert c.daemon.host == "127.0.0.1" and c.daemon.port == 8765
    assert (c.watchers.fp_move, c.watchers.fp_settle, c.watchers.fp_meaningful) == (0.04, 0.015, 0.05)
    assert "sudo" in c.policy.require_human_for


def test_config_example_file_loads() -> None:
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    c = load_config(example)
    assert set(c.operator.lanes) == {"cheap", "default", "hard"}
    assert c.operator.lanes["hard"].model.startswith("qwen/")
