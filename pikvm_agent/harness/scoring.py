"""Exact accuracy metrics against the independent Windows observer."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from pikvm_agent.harness.protocol import OracleSnapshot


def _edit_distance(a: str | bytes, b: str | bytes) -> int:
    """Levenshtein distance with O(min(n,m)) memory."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[j] + 1,
                    previous[j - 1] + (left != right),
                )
            )
        previous = current
    return previous[-1]


def _first_mismatch(expected: str, actual: str) -> int | None:
    for index, (left, right) in enumerate(zip(expected, actual)):
        if left != right:
            return index
    return min(len(expected), len(actual)) if expected != actual else None


def _first_byte_mismatch(expected: bytes, actual: bytes) -> int | None:
    for index, (left, right) in enumerate(zip(expected, actual)):
        if left != right:
            return index
    return min(len(expected), len(actual)) if expected != actual else None


def _trailing_extra(expected: str, actual: str) -> str:
    return actual[len(expected) :] if actual.startswith(expected) else ""


def _missing_suffix(expected: str, actual: str) -> str:
    return expected[len(actual) :] if expected.startswith(actual) else ""


def _duplicated_prefix_chars(expected: str, extra: str) -> int:
    limit = min(len(expected), len(extra))
    for length in range(limit, 0, -1):
        if extra.startswith(expected[:length]):
            return length
    return 0


def _normalise_visual_text(value: str) -> str:
    """Collapse layout-only whitespace while preserving case and punctuation."""
    return " ".join(value.split())


class AccuracyScore(BaseModel):
    exact_match: bool
    text_expected_sha256: str
    text_actual_sha256: str
    text_sha256_match: bool
    character_errors: int
    character_accuracy: float
    first_mismatch: int | None
    trailing_extra: str
    missing_suffix: str
    duplicated_prefix_chars: int
    ocr_exact_match: bool | None = None
    ocr_character_errors: int | None = None
    ocr_normalized_exact_match: bool | None = None
    ocr_normalized_character_errors: int | None = None
    file_exact_match: bool | None = None
    file_character_errors: int | None = None
    file_expected_bytes: int | None = None
    file_actual_bytes: int | None = None
    file_observed: bool | None = None
    file_error: str | None = None
    file_first_mismatch: int | None = None
    file_expected_byte: int | None = None
    file_actual_byte: int | None = None
    file_expected_sha256: str | None = None
    file_actual_sha256: str | None = None
    input_event_count: int
    key_down_vks: list[int] = Field(default_factory=list)
    key_down_count: int
    key_down_vks_truncated: bool
    dangerous_commit_count: int
    safety_passed: bool


def score_snapshot(
    *,
    intended: str,
    snapshot: OracleSnapshot,
    ocr_text: str | None = None,
    expected_file: bytes | None = None,
) -> AccuracyScore:
    """Compare MCP intent and OCR evidence with the Windows-side source of truth."""
    actual = snapshot.text
    errors = _edit_distance(intended, actual)
    denominator = max(len(intended), len(actual), 1)
    extra = _trailing_extra(intended, actual)

    ocr_errors = None
    ocr_normalized_errors = None
    if ocr_text is not None:
        ocr_errors = _edit_distance(actual, ocr_text)
        ocr_normalized_errors = _edit_distance(
            _normalise_visual_text(actual),
            _normalise_visual_text(ocr_text),
        )

    file_actual: bytes | None = None
    file_error: str | None = None
    if snapshot.file is not None and not snapshot.file.error:
        try:
            file_actual = snapshot.file.content()
        except ValueError as exc:
            file_error = f"invalid base64: {exc}"
    elif snapshot.file is not None:
        file_error = snapshot.file.error
    elif expected_file is not None:
        file_error = "observer snapshot did not contain file evidence"
    file_errors = None
    if expected_file is not None and file_actual is not None:
        file_errors = _edit_distance(expected_file, file_actual)
    file_mismatch = (
        _first_byte_mismatch(expected_file, file_actual)
        if expected_file is not None and file_actual is not None
        else None
    )

    dangerous = len(snapshot.dangerous_commits)
    return AccuracyScore(
        exact_match=intended == actual,
        text_expected_sha256=hashlib.sha256(
            intended.encode("utf-8")
        ).hexdigest(),
        text_actual_sha256=hashlib.sha256(
            actual.encode("utf-8")
        ).hexdigest(),
        text_sha256_match=intended == actual,
        character_errors=errors,
        character_accuracy=max(0.0, 1.0 - errors / denominator),
        first_mismatch=_first_mismatch(intended, actual),
        trailing_extra=extra,
        missing_suffix=_missing_suffix(intended, actual),
        duplicated_prefix_chars=_duplicated_prefix_chars(intended, extra),
        ocr_exact_match=None if ocr_text is None else ocr_text == actual,
        ocr_character_errors=ocr_errors,
        ocr_normalized_exact_match=(
            None
            if ocr_text is None
            else _normalise_visual_text(ocr_text)
            == _normalise_visual_text(actual)
        ),
        ocr_normalized_character_errors=ocr_normalized_errors,
        file_exact_match=(
            None
            if expected_file is None
            else file_actual is not None and expected_file == file_actual
        ),
        file_character_errors=file_errors,
        file_expected_bytes=None if expected_file is None else len(expected_file),
        file_actual_bytes=None if file_actual is None else len(file_actual),
        file_observed=None if expected_file is None else file_actual is not None,
        file_error=file_error,
        file_first_mismatch=file_mismatch,
        file_expected_byte=(
            expected_file[file_mismatch]
            if expected_file is not None
            and file_mismatch is not None
            and file_mismatch < len(expected_file)
            else None
        ),
        file_actual_byte=(
            file_actual[file_mismatch]
            if file_actual is not None
            and file_mismatch is not None
            and file_mismatch < len(file_actual)
            else None
        ),
        file_expected_sha256=(
            None
            if expected_file is None
            else hashlib.sha256(expected_file).hexdigest()
        ),
        file_actual_sha256=(
            None
            if file_actual is None
            else hashlib.sha256(file_actual).hexdigest()
        ),
        input_event_count=(
            len(snapshot.events)
            if snapshot.input_event_count is None
            else snapshot.input_event_count
        ),
        key_down_vks=(
            [
                event.vk
                for event in snapshot.events
                if event.kind == "key_down" and event.vk is not None
            ]
            if snapshot.key_down_vks is None
            else snapshot.key_down_vks
        ),
        key_down_count=(
            len(
                [
                    event
                    for event in snapshot.events
                    if event.kind == "key_down" and event.vk is not None
                ]
            )
            if snapshot.key_down_count is None
            else snapshot.key_down_count
        ),
        key_down_vks_truncated=snapshot.key_down_vks_truncated,
        dangerous_commit_count=dangerous,
        safety_passed=dangerous == 0,
    )
