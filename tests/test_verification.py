"""Text read-back verifier — E1–E6 regression fixtures + algorithm coverage.

These reproduce the incidents that drove the verifier: symbol/case slips that
must fail (E1), shell-prompt false misses (E4), truncated read-backs (E5), and
wrong-region reads (E6), plus the lenient OCR-confusable tolerance that must
never destroy correct prose.
"""

from __future__ import annotations

from pikvm_agent.core.models import VERIFIED_STATUSES
from pikvm_agent.executor.verification import (
    compute_verdict,
    is_exact_text,
    verify_text,
)


# --------------------------------------------------------------------------- #
# E1 — find README symbol/case: README→readme and |→~ must NOT verify.
# --------------------------------------------------------------------------- #


def test_e1_symbol_and_case_slip_fails() -> None:
    res = verify_text(
        "find . -name 'README*' | sort && echo \"=== ROOT ===\"",
        "find . -name 'readme*' ~ sort && echo @=== root ===@",
        "terminal.readline",
    )
    assert res.status.startswith("failed_")
    assert res.safe_to_continue is False
    assert res.verified is False


# --------------------------------------------------------------------------- #
# E4 — shell prompt false miss: strip a leading prompt before comparing.
# --------------------------------------------------------------------------- #


def test_e4_dollar_prompt_stripped() -> None:
    assert verify_text("git status", "$ git status", "terminal.readline").verified is True


def test_e4_userhost_prompt_stripped() -> None:
    res = verify_text(
        "ls -la", "drewettk@HOST MINGW64 ~/p$ ls -la", "terminal.readline"
    )
    assert res.verified is True


# --------------------------------------------------------------------------- #
# E5 — truncated read-back: a prefix-only read is unverified, NOT a retype.
# --------------------------------------------------------------------------- #


def test_e5_truncated_is_unverified_truncated() -> None:
    res = verify_text(
        "clear; wc -l README.md; sed -n '1,45p' image-build/oel9-cis/README.md",
        "$ clear; wc -l README.md; sed -n '1,45p' image-build",
        "terminal.readline",
    )
    assert res.status == "unverified_truncated"
    assert res.safe_to_continue is False


# --------------------------------------------------------------------------- #
# E6 — wrong-region read (OCR over a results list), not a typing error.
# --------------------------------------------------------------------------- #


def test_e6_wrong_region_not_failed() -> None:
    res = verify_text(
        "oel9-cis/README.md",
        "® README.md image-build oel9-cis runner RS NE a",
    )
    assert res.status in {"unverified_wrong_region", "unverified_ambiguous"}
    assert not res.status.startswith("failed_")


# --------------------------------------------------------------------------- #
# Keyboard-layout slip (alnum equal, symbols differ).
# --------------------------------------------------------------------------- #


def test_layout_slip_classified() -> None:
    res = verify_text("ls | sort", "$ ls ~ sort", "terminal.readline")
    assert res.status == "failed_keyboard_layout"
    assert res.safe_to_continue is False


# --------------------------------------------------------------------------- #
# Exact (lenient) match of ordinary prose.
# --------------------------------------------------------------------------- #


def test_exact_prose_match() -> None:
    assert verify_text("Hello there", "Hello there").verified is True


# --------------------------------------------------------------------------- #
# Confusable tolerance (non-precise): oel9 vs oe19 must NOT be a failure.
# --------------------------------------------------------------------------- #


def test_confusable_tolerance_not_failed() -> None:
    res = verify_text("oel9 notes", "oe19 notes")
    assert not res.status.startswith("failed_")
    assert res.status in VERIFIED_STATUSES or res.status.startswith("unverified_")


# --------------------------------------------------------------------------- #
# Algorithm-level coverage of the helpers / verdict ladder.
# --------------------------------------------------------------------------- #


def test_is_exact_text_triggers() -> None:
    assert is_exact_text("ls | sort") is True  # pipe + command head
    assert is_exact_text("git status") is True  # command head
    assert is_exact_text("cd ~/projects") is True  # tilde + command head
    assert is_exact_text("https://example.com") is True  # url scheme
    assert is_exact_text("Hello there") is False  # plain prose
    assert is_exact_text("name@domain.com") is False  # email stays lenient


def test_empty_read_is_unverified() -> None:
    assert compute_verdict("anything", "") == "unverified"
    assert verify_text("anything", "").status == "unverified_ambiguous"


def test_contains_is_warning() -> None:
    res = verify_text("hello", "well hello there friend")
    assert res.status == "verified_with_warnings"
    assert res.verified is True


def test_precise_match_is_exact() -> None:
    res = verify_text("git commit -m fix", "git commit -m fix", code=True)
    assert res.status == "verified_exact"
    assert res.verified is True


def test_case_only_mismatch_in_precise() -> None:
    # Letters match case-folded, only the case differs, symbols identical.
    res = verify_text("MyVar = value;", "myvar = value;", code=True)
    assert res.status in {"failed_case_mismatch", "failed_keyboard_layout"}
    assert res.safe_to_continue is False


def test_pipe_is_not_confusable_folded() -> None:
    # `| -> l` must remain a distinguishing slip, not be hidden as a match.
    res = verify_text("cat file | wc", "cat file l wc")
    assert res.verified is False
