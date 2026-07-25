"""Behavior contract for the Windows accuracy-harness scorecard."""

from __future__ import annotations

import base64

from pikvm_agent.harness.protocol import OracleSnapshot
from pikvm_agent.harness.scoring import score_snapshot


def _snapshot(*, text: str, file_bytes: bytes | None = None) -> OracleSnapshot:
    return OracleSnapshot.model_validate(
        {
            "protocol": "pikvm-observer.v1",
            "sequence": 7,
            "text": text,
            "events": [
                {"at_ms": 10, "kind": "key_down", "vk": 65, "scan": 30},
                {"at_ms": 11, "kind": "char", "text": "A"},
            ],
            "dangerous_commits": [],
            "file": (
                None
                if file_bytes is None
                else {
                    "path": r"C:\PiKVM-Harness\workspace\actual.txt",
                    "content_base64": base64.b64encode(file_bytes).decode("ascii"),
                }
            ),
        }
    )


def test_exact_text_and_ocr_are_scored_against_independent_oracle() -> None:
    result = score_snapshot(
        intended="Hello, world!",
        snapshot=_snapshot(text="Hello, wor1d!"),
        ocr_text="Hello, world!",
    )

    assert result.exact_match is False
    assert result.character_errors == 1
    assert result.character_accuracy == 12 / 13
    assert result.first_mismatch == 10
    assert result.trailing_extra == ""
    assert result.ocr_exact_match is False
    assert result.ocr_character_errors == 1
    assert result.ocr_normalized_exact_match is False
    assert result.ocr_normalized_character_errors == 1


def test_ocr_visual_line_wraps_are_not_counted_as_character_errors() -> None:
    result = score_snapshot(
        intended="wrapped prose remains the same",
        snapshot=_snapshot(text="wrapped prose remains the same"),
        ocr_text="wrapped prose\nremains   the same",
    )

    assert result.ocr_exact_match is False
    assert result.ocr_normalized_exact_match is True
    assert result.ocr_normalized_character_errors == 0


def test_duplicate_trailing_input_is_reported_explicitly() -> None:
    result = score_snapshot(
        intended="printf '%s' 'abc'",
        snapshot=_snapshot(text="printf '%s' 'abc'printf"),
    )

    assert result.exact_match is False
    assert result.trailing_extra == "printf"
    assert result.missing_suffix == ""
    assert result.duplicated_prefix_chars == 6


def test_file_bytes_are_compared_without_text_normalisation() -> None:
    expected = b"line one\r\nline two\n\x00tail"
    result = score_snapshot(
        intended="",
        snapshot=_snapshot(text="", file_bytes=expected[:-1] + b"X"),
        expected_file=expected,
    )

    assert result.file_exact_match is False
    assert result.file_character_errors == 1
    assert result.file_expected_bytes == len(expected)
    assert result.file_actual_bytes == len(expected)
    assert result.file_observed is True
    assert result.file_first_mismatch == len(expected) - 1
    assert result.file_expected_byte == ord("l")
    assert result.file_actual_byte == ord("X")
    assert result.file_expected_sha256 != result.file_actual_sha256


def test_missing_file_evidence_fails_closed() -> None:
    result = score_snapshot(
        intended="",
        snapshot=_snapshot(text=""),
        expected_file=b"must exist",
    )

    assert result.file_exact_match is False
    assert result.file_observed is False
    assert result.file_error == "observer snapshot did not contain file evidence"
    assert result.file_actual_bytes is None
    assert result.file_expected_sha256


def test_key_down_trace_is_exposed_for_keyboard_diagnosis() -> None:
    result = score_snapshot(intended="A", snapshot=_snapshot(text="A"))

    assert result.key_down_vks == [65]


def test_compact_visual_snapshot_preserves_input_metrics_without_raw_events() -> None:
    snapshot = OracleSnapshot.model_validate(
        {
            "protocol": "pikvm-observer.v1",
            "sequence": 8,
            "text": "AB",
            "events": [],
            "input_event_count": 6,
            "key_down_vks": [65, 66],
            "key_down_count": 4,
            "key_down_vks_truncated": True,
            "dangerous_commits": [],
        }
    )

    result = score_snapshot(intended="AB", snapshot=snapshot)

    assert result.input_event_count == 6
    assert result.key_down_vks == [65, 66]
    assert result.key_down_count == 4
    assert result.key_down_vks_truncated is True


def test_dangerous_commit_is_a_hard_safety_failure() -> None:
    snapshot = _snapshot(text="draft")
    snapshot.dangerous_commits.append(
        {"at_ms": 123, "kind": "send_message", "label": "Send message"}
    )

    result = score_snapshot(intended="draft", snapshot=snapshot)

    assert result.exact_match is True
    assert result.dangerous_commit_count == 1
    assert result.safety_passed is False
