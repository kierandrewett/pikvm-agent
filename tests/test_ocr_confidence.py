from __future__ import annotations

import pytest

from bench.ocr_ensemble_analysis import analyze_agreement
from pikvm_agent.harness.ocr_confidence import (
    confidence_calibration,
    wilson_interval,
)


def test_wilson_interval_does_not_treat_small_perfect_sample_as_certain() -> None:
    lower, upper = wilson_interval(28, 28)

    assert lower == pytest.approx(0.879351, abs=0.00001)
    assert upper == pytest.approx(1.0)


def test_confidence_calibration_finds_zero_error_slice_but_rejects_99_claim() -> None:
    report = confidence_calibration(
        [
            {
                "case_id": "correct-high",
                "category": "code",
                "mean_confidence": 0.97,
                "normalized_exact": True,
            },
            {
                "case_id": "correct-mid",
                "category": "prose",
                "mean_confidence": 0.91,
                "normalized_exact": True,
            },
            {
                "case_id": "wrong-mid",
                "category": "path",
                "mean_confidence": 0.94,
                "normalized_exact": False,
            },
            {
                "case_id": "wrong-low",
                "category": "terminal",
                "mean_confidence": 0.40,
                "normalized_exact": False,
            },
        ],
        thresholds=[0.9, 0.95],
    )

    assert report["thresholds"][0]["wrong_cases"] == 1
    assert report["thresholds"][1]["wrong_cases"] == 0
    assert report["highest_coverage_zero_observed_error"]["threshold"] == 0.97
    assert report["highest_coverage_with_99_percent_wilson_lower"] is None
    assert report["high_confidence_failures"][0] == {
        "case_id": "wrong-mid",
        "category": "path",
        "confidence": 0.94,
    }


def test_independent_ocr_agreement_is_not_assumed_to_be_correct() -> None:
    def item(
        case_id: str,
        observed: str,
        *,
        expected: str,
        category: str = "ui_label",
    ) -> dict:
        return {
            "case_id": case_id,
            "observed": observed,
            "expected": expected,
            "category": category,
            "normalized_exact": observed == expected,
        }

    first = {
        "provider": "first",
        "results": [
            item("a", "Send", expected="Send"),
            item("b", "Delete", expected="Delete"),
            item("c", "Senci", expected="Send"),
            item("d", "Open", expected="Open"),
        ],
    }
    second = {
        "provider": "second",
        "results": [
            item("a", "Send", expected="Send"),
            item("b", "DeIete", expected="Delete"),
            item("c", "Senci", expected="Send"),
            item("d", "Open", expected="Open"),
        ],
    }

    report = analyze_agreement(first, second)

    assert report["agreement"]["covered"] == 3
    assert report["agreement"]["correct"] == 2
    assert report["agreement"]["wrong"] == 1
    assert report["oracle_union_correct"] == 3
    assert report["safe_commit_authority"] is False


def test_even_large_perfect_ocr_agreement_is_recheck_evidence_not_authority() -> None:
    results = [
        {
            "case_id": f"case-{index:04d}",
            "observed": f"label-{index:04d}",
            "expected": f"label-{index:04d}",
            "category": "ui_label",
            "normalized_exact": True,
        }
        for index in range(1_000)
    ]

    report = analyze_agreement(
        {"provider": "first", "results": results},
        {"provider": "second", "results": results},
    )

    assert report["agreement"]["wilson_95"][0] > 0.99
    assert report["safe_commit_authority"] is False


def test_independent_known_intent_candidates_report_routine_and_stress_union() -> None:
    def item(
        case_id: str,
        *,
        category: str,
        normalized_exact: bool,
        candidate_exact: bool | None = None,
    ) -> dict:
        value = {
            "case_id": case_id,
            "observed": case_id,
            "expected": case_id,
            "category": category,
            "normalized_exact": normalized_exact,
        }
        if candidate_exact is not None:
            value["expected_aware_normalized_exact"] = candidate_exact
        return value

    first = {
        "provider": "fast-primary",
        "results": [
            item(
                "routine-primary",
                category="code",
                normalized_exact=True,
            ),
            item(
                "routine-alternative",
                category="path",
                normalized_exact=False,
                candidate_exact=True,
            ),
            item(
                "routine-secondary",
                category="prose",
                normalized_exact=False,
            ),
            item(
                "stress-abstain",
                category="numeric",
                normalized_exact=False,
            ),
        ],
    }
    second = {
        "provider": "independent-secondary",
        "results": [
            item(
                "routine-primary",
                category="code",
                normalized_exact=False,
            ),
            item(
                "routine-alternative",
                category="path",
                normalized_exact=False,
            ),
            item(
                "routine-secondary",
                category="prose",
                normalized_exact=True,
            ),
            item(
                "stress-abstain",
                category="numeric",
                normalized_exact=False,
            ),
        ],
    }

    report = analyze_agreement(first, second)
    union = report["known_intent_candidate_union"]

    assert union["matches"] == 3
    assert union["rate"] == 0.75
    assert union["by_tier"]["routine"] == {
        "cases": 3,
        "matches": 3,
        "rate": 1.0,
    }
    assert union["by_tier"]["stress"] == {
        "cases": 1,
        "matches": 0,
        "rate": 0.0,
    }
    assert union["nearest_guess_allowed"] is False
    assert union["commit_authority"] is False
