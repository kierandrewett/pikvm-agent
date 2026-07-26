from __future__ import annotations

from pikvm_agent.harness.payload_shape_benchmark import (
    evaluate_payload_shape_cases,
    generate_payload_shape_cases,
)


def test_payload_shape_corpus_is_seeded_unique_and_balanced() -> None:
    first = generate_payload_shape_cases()
    second = generate_payload_shape_cases()

    assert first == second
    assert len(first) == 1000
    assert len({case.case_id for case in first}) == 1000
    assert sum(case.expected_allowed for case in first) == 200
    assert sum(not case.expected_allowed for case in first) == 800
    assert {case.family for case in first if not case.expected_allowed} == {
        "base64_transfer",
        "segmented_base64_transfer",
        "encoded_powershell",
        "heredoc",
        "complex_nested_shell",
    }


def test_payload_shape_gate_catches_unsafe_without_safe_false_positives() -> None:
    report = evaluate_payload_shape_cases(generate_payload_shape_cases())

    assert report["cases"] == 1000
    assert report["unsafe_cases"] == 800
    assert report["safe_controls"] == 200
    assert report["unsafe_refused"] == 800
    assert report["safe_allowed"] == 200
    assert report["false_negative_count"] == 0
    assert report["false_positive_count"] == 0
