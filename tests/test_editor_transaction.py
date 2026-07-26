from __future__ import annotations

import pytest

from pikvm_agent.harness.editor_transaction import (
    DiagnosticSnapshot,
    EditorReplacement,
    EditorTransactionState,
    prepare_editor_transaction,
)


def _prepare(
    baseline: str,
    *,
    before: str,
    after: str,
    diagnostics: tuple[str, ...] = (),
    require_diagnostics: bool = False,
):
    return prepare_editor_transaction(
        baseline.encode(),
        EditorReplacement(
            before=before,
            after=after,
            require_diagnostics=require_diagnostics,
        ),
        diagnostics=DiagnosticSnapshot(errors=diagnostics),
    )


def test_wrapped_visual_line_is_bounded_by_the_complete_logical_line() -> None:
    baseline = (
        "policy = allow_when_authenticated_and_authorized_and_audited\n"
        "next = preserve_me\n"
    )
    transaction = _prepare(
        baseline,
        before="authorized",
        after="explicitly_authorized",
    )

    assert transaction.logical_unit_before == (
        "policy = allow_when_authenticated_and_authorized_and_audited\n"
    ).encode()
    assert transaction.expected_bytes == baseline.replace(
        "authorized", "explicitly_authorized", 1
    ).encode()
    assert transaction.receipt.selection_start == baseline.index("authorized")
    assert transaction.receipt.selection_end == (
        baseline.index("authorized") + len("authorized")
    )


def test_ambiguous_replacement_is_refused_before_any_editor_input() -> None:
    with pytest.raises(ValueError, match="exactly once"):
        _prepare(
            "role = reader\nrole = reader\n",
            before="reader",
            after="writer",
        )


def test_merged_adjacent_line_requires_manual_restore() -> None:
    baseline = "allow = read\ncomment = keep this separate\n"
    transaction = _prepare(
        baseline,
        before="read",
        after="read,write",
    )
    corrupted = "allow = read,write comment = keep this separate\n".encode()

    result = transaction.evaluate(
        corrupted,
        diagnostics=DiagnosticSnapshot(),
    )

    assert result.state is EditorTransactionState.MANUAL_RESTORE_REQUIRED
    assert result.outside_logical_unit is True
    assert result.rollback_bytes is None


def test_one_bounded_repair_is_allowed_then_whole_unit_rollback_is_required() -> None:
    baseline = "setting = disabled\nnext = untouched\n"
    transaction = _prepare(
        baseline,
        before="disabled",
        after="enabled",
    )
    mangled = "setting = enabledd\nnext = untouched\n".encode()

    first = transaction.evaluate(
        mangled,
        diagnostics=DiagnosticSnapshot(),
        repair_attempts=0,
    )
    second = transaction.evaluate(
        mangled,
        diagnostics=DiagnosticSnapshot(),
        repair_attempts=1,
    )

    assert first.state is EditorTransactionState.REPAIR_ALLOWED
    assert first.rollback_bytes is None
    assert second.state is EditorTransactionState.ROLLBACK_READY
    assert second.rollback_bytes == baseline.encode()
    assert second.rollback_bytes != transaction.expected_bytes


def test_new_diagnostic_rejects_an_exact_text_change_and_restores_whole_unit() -> None:
    baseline = "value = compute()  # preserve type safety\nnext = ok()\n"
    transaction = _prepare(
        baseline,
        before="compute()",
        after="compute_safe()",
        diagnostics=("existing-warning",),
        require_diagnostics=True,
    )

    result = transaction.evaluate(
        transaction.expected_bytes,
        diagnostics=DiagnosticSnapshot(
            errors=("existing-warning", "undefined-compute-safe")
        ),
    )

    assert result.state is EditorTransactionState.ROLLBACK_READY
    assert result.new_diagnostics == ("undefined-compute-safe",)
    assert result.rollback_bytes == baseline.encode()


def test_missing_required_diagnostics_fails_closed() -> None:
    transaction = _prepare(
        "answer = old\n",
        before="old",
        after="new",
        require_diagnostics=True,
    )

    result = transaction.evaluate(
        transaction.expected_bytes,
        diagnostics=None,
    )

    assert result.state is EditorTransactionState.MANUAL_RESTORE_REQUIRED
    assert result.reason == "required diagnostics were not captured"


def test_exact_expected_bytes_and_diagnostics_commit() -> None:
    transaction = _prepare(
        "answer = old\n",
        before="old",
        after="new",
        diagnostics=("existing-warning",),
        require_diagnostics=True,
    )

    result = transaction.evaluate(
        transaction.expected_bytes,
        diagnostics=DiagnosticSnapshot(errors=("existing-warning",)),
    )

    assert result.state is EditorTransactionState.COMMITTED
    assert result.observed_sha256 == transaction.receipt.expected_sha256
    assert result.rollback_bytes is None


def test_unchanged_file_is_not_misreported_as_a_successful_edit() -> None:
    baseline = b"answer = old\n"
    transaction = prepare_editor_transaction(
        baseline,
        EditorReplacement(before="old", after="new"),
    )

    result = transaction.evaluate(
        baseline,
        diagnostics=DiagnosticSnapshot(),
    )

    assert result.state is EditorTransactionState.UNCHANGED
    assert result.reason == "observer bytes still match the baseline"
