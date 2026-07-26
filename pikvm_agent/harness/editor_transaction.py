"""Exact, helper-backed safety oracle for bounded editor replacements.

This module never edits the guest. It prepares a private known-good baseline,
an operator-readable receipt, and a fail-closed evaluation of bytes captured
by the disposable-lab observer after HID input.
"""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass, field
from enum import Enum


class EditorTransactionState(str, Enum):
    COMMITTED = "committed"
    UNCHANGED = "unchanged"
    REPAIR_ALLOWED = "repair_allowed"
    ROLLBACK_READY = "rollback_ready"
    MANUAL_RESTORE_REQUIRED = "manual_restore_required"


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    """Stable diagnostic identities captured by an application adapter."""

    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EditorReplacement:
    """One unique inline replacement inside one complete logical line."""

    before: str
    after: str
    encoding: str = "utf-8"
    require_diagnostics: bool = False


@dataclass(frozen=True, slots=True)
class EditorTransactionReceipt:
    """Non-secret control metadata suitable for an operator transaction."""

    baseline_sha256: str
    expected_sha256: str
    baseline_bytes: int
    expected_bytes: int
    selection_start: int
    selection_end: int
    logical_unit_start: int
    logical_unit_end: int
    diff_preview: str


@dataclass(frozen=True, slots=True)
class EditorTransactionEvaluation:
    state: EditorTransactionState
    reason: str
    observed_sha256: str
    outside_logical_unit: bool
    new_diagnostics: tuple[str, ...] = ()
    rollback_bytes: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class EditorTransaction:
    """Private baseline plus deterministic post-edit evaluation."""

    baseline_bytes: bytes = field(repr=False)
    expected_bytes: bytes = field(repr=False)
    logical_unit_before: bytes = field(repr=False)
    logical_unit_after: bytes = field(repr=False)
    replacement: EditorReplacement
    diagnostics_before: DiagnosticSnapshot
    receipt: EditorTransactionReceipt

    def _localized_to_logical_unit(self, observed: bytes) -> bool:
        prefix = self.baseline_bytes[: self.receipt.logical_unit_start]
        suffix = self.baseline_bytes[self.receipt.logical_unit_end :]
        minimum = len(prefix) + len(suffix)
        if (
            len(observed) < minimum
            or not observed.startswith(prefix)
            or not observed.endswith(suffix)
        ):
            return False
        unit_end = len(observed) - len(suffix) if suffix else len(observed)
        observed_unit = observed[len(prefix) : unit_end]
        if self.logical_unit_before.endswith(b"\r\n"):
            return observed_unit.endswith(b"\r\n")
        if self.logical_unit_before.endswith(b"\n"):
            return observed_unit.endswith(b"\n")
        return b"\r" not in observed_unit and b"\n" not in observed_unit

    def evaluate(
        self,
        observed: bytes,
        *,
        diagnostics: DiagnosticSnapshot | None,
        repair_attempts: int = 0,
    ) -> EditorTransactionEvaluation:
        """Classify exact observer bytes without performing a repair or rollback."""

        if repair_attempts < 0:
            raise ValueError("repair_attempts cannot be negative")
        observed_sha = _sha256(observed)
        localized = self._localized_to_logical_unit(observed)
        outside = not localized

        if observed == self.baseline_bytes:
            return EditorTransactionEvaluation(
                state=EditorTransactionState.UNCHANGED,
                reason="observer bytes still match the baseline",
                observed_sha256=observed_sha,
                outside_logical_unit=False,
            )

        if self.replacement.require_diagnostics and diagnostics is None:
            return EditorTransactionEvaluation(
                state=EditorTransactionState.MANUAL_RESTORE_REQUIRED,
                reason="required diagnostics were not captured",
                observed_sha256=observed_sha,
                outside_logical_unit=outside,
            )

        new_diagnostics = _new_diagnostics(
            self.diagnostics_before,
            diagnostics or DiagnosticSnapshot(),
        )
        if outside:
            return EditorTransactionEvaluation(
                state=EditorTransactionState.MANUAL_RESTORE_REQUIRED,
                reason="observer bytes changed outside the prepared logical unit",
                observed_sha256=observed_sha,
                outside_logical_unit=True,
                new_diagnostics=new_diagnostics,
            )

        if observed == self.expected_bytes and not new_diagnostics:
            return EditorTransactionEvaluation(
                state=EditorTransactionState.COMMITTED,
                reason="observer bytes and diagnostic invariants match",
                observed_sha256=observed_sha,
                outside_logical_unit=False,
            )

        rollback = (
            self.baseline_bytes[: self.receipt.logical_unit_start]
            + self.logical_unit_before
            + self.baseline_bytes[self.receipt.logical_unit_end :]
        )
        if new_diagnostics:
            return EditorTransactionEvaluation(
                state=EditorTransactionState.ROLLBACK_READY,
                reason="the edit introduced new diagnostics",
                observed_sha256=observed_sha,
                outside_logical_unit=False,
                new_diagnostics=new_diagnostics,
                rollback_bytes=rollback,
            )
        if repair_attempts == 0:
            return EditorTransactionEvaluation(
                state=EditorTransactionState.REPAIR_ALLOWED,
                reason="one bounded in-unit repair is available",
                observed_sha256=observed_sha,
                outside_logical_unit=False,
            )
        return EditorTransactionEvaluation(
            state=EditorTransactionState.ROLLBACK_READY,
            reason="the single bounded repair budget was exhausted",
            observed_sha256=observed_sha,
            outside_logical_unit=False,
            rollback_bytes=rollback,
        )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _new_diagnostics(
    before: DiagnosticSnapshot,
    after: DiagnosticSnapshot,
) -> tuple[str, ...]:
    existing = set(before.errors)
    return tuple(item for item in after.errors if item not in existing)


def _diff_preview(before: str, after: str) -> str:
    preview = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="baseline",
            tofile="expected",
            n=2,
        )
    )
    return preview[:8_000]


def prepare_editor_transaction(
    baseline: bytes,
    replacement: EditorReplacement,
    *,
    diagnostics: DiagnosticSnapshot | None = None,
) -> EditorTransaction:
    """Prepare one exact logical-line replacement from observer-captured bytes."""

    if not baseline:
        raise ValueError("editor baseline cannot be empty")
    if not replacement.before:
        raise ValueError("replacement before text cannot be empty")
    if replacement.before == replacement.after:
        raise ValueError("replacement must change the logical unit")
    if any(marker in replacement.before for marker in ("\r", "\n")):
        raise ValueError("replacement before text must stay within one logical line")
    if any(marker in replacement.after for marker in ("\r", "\n")):
        raise ValueError("replacement after text must stay within one logical line")
    try:
        baseline_text = baseline.decode(replacement.encoding)
    except (LookupError, UnicodeDecodeError) as exc:
        raise ValueError("editor baseline could not be decoded exactly") from exc
    if baseline_text.count(replacement.before) != 1:
        raise ValueError("replacement before text must occur exactly once")

    selection_start_chars = baseline_text.index(replacement.before)
    selection_end_chars = selection_start_chars + len(replacement.before)
    line_start_chars = baseline_text.rfind("\n", 0, selection_start_chars) + 1
    line_break = baseline_text.find("\n", selection_end_chars)
    line_end_chars = len(baseline_text) if line_break < 0 else line_break + 1

    expected_text = (
        baseline_text[:selection_start_chars]
        + replacement.after
        + baseline_text[selection_end_chars:]
    )
    expected_bytes = expected_text.encode(replacement.encoding)

    selection_start = len(
        baseline_text[:selection_start_chars].encode(replacement.encoding)
    )
    selection_end = len(
        baseline_text[:selection_end_chars].encode(replacement.encoding)
    )
    logical_unit_start = len(
        baseline_text[:line_start_chars].encode(replacement.encoding)
    )
    logical_unit_end = len(
        baseline_text[:line_end_chars].encode(replacement.encoding)
    )

    expected_line_end_chars = (
        line_end_chars
        + len(replacement.after)
        - len(replacement.before)
    )
    logical_unit_after = expected_text[
        line_start_chars:expected_line_end_chars
    ].encode(replacement.encoding)
    receipt = EditorTransactionReceipt(
        baseline_sha256=_sha256(baseline),
        expected_sha256=_sha256(expected_bytes),
        baseline_bytes=len(baseline),
        expected_bytes=len(expected_bytes),
        selection_start=selection_start,
        selection_end=selection_end,
        logical_unit_start=logical_unit_start,
        logical_unit_end=logical_unit_end,
        diff_preview=_diff_preview(baseline_text, expected_text),
    )
    return EditorTransaction(
        baseline_bytes=baseline,
        expected_bytes=expected_bytes,
        logical_unit_before=baseline[
            logical_unit_start:logical_unit_end
        ],
        logical_unit_after=logical_unit_after,
        replacement=replacement,
        diagnostics_before=diagnostics or DiagnosticSnapshot(),
        receipt=receipt,
    )
