"""Regression tests from the GPT-5.5 (Codex) Sprint A review.

Each guards a safety-critical fix to the text verifier.
"""

from __future__ import annotations

from pikvm_agent.executor.verification import is_exact_text, verify_text


def test_high_risk_chars_force_strict_verification() -> None:
    # P1.4: any high-risk character must put verification into precise mode.
    for s in ["ship it!", "a & b", "x * y", 'say "hi"', "it's mine", "$PATH", "a|b"]:
        assert is_exact_text(s), s
    # plain prose with no high-risk char stays lenient
    assert is_exact_text("open the readme file") is False


def test_quote_distinctions_preserved_but_curly_folds() -> None:
    # P1.5: semantic quote differences must NOT verify...
    assert not verify_text('say "hi"', "say 'hi'").verified  # double vs single
    assert not verify_text("echo `date`", "echo 'date'").verified  # backtick vs quote
    # ...but OCR's straight-vs-curly within a family still verifies.
    assert verify_text('say "hi"', "say “hi”").verified  # straight vs curly double
    assert verify_text("it's fine", "it‘s fine").verified  # straight vs curly single
