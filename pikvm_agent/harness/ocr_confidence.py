"""Case-level OCR confidence calibration for release evidence."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any


def wilson_interval(
    successes: int,
    total: int,
    *,
    z: float = 1.959963984540054,
) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 0.0)
    rate = successes / total
    denominator = 1 + (z * z / total)
    centre = rate + (z * z / (2 * total))
    radius = z * math.sqrt(
        (rate * (1 - rate) / total) + (z * z / (4 * total * total))
    )
    return (
        max(0.0, (centre - radius) / denominator),
        min(1.0, (centre + radius) / denominator),
    )


def confidence_calibration(
    results: Iterable[dict[str, Any]],
    *,
    thresholds: Iterable[float] = (
        0.0,
        0.5,
        0.6,
        0.7,
        0.8,
        0.85,
        0.9,
        0.925,
        0.95,
        0.975,
        0.99,
    ),
) -> dict[str, Any]:
    cases = [
        {
            "case_id": str(item.get("case_id") or ""),
            "category": str(item.get("category") or ""),
            "confidence": max(
                0.0, min(1.0, float(item.get("mean_confidence") or 0.0))
            ),
            "correct": bool(item.get("normalized_exact")),
        }
        for item in results
    ]
    if not cases:
        raise ValueError("confidence calibration requires at least one result")

    rows = [
        _threshold_row(cases, float(threshold))
        for threshold in thresholds
    ]
    wrong = [case for case in cases if not case["correct"]]
    next_safe_threshold = (
        min(
            (
                case["confidence"]
                for case in cases
                if case["correct"]
                and case["confidence"]
                > max(item["confidence"] for item in wrong)
            ),
            default=None,
        )
        if wrong
        else 0.0
    )
    zero_error_row = (
        _threshold_row(cases, next_safe_threshold)
        if next_safe_threshold is not None
        else None
    )
    all_unique_rows = [
        _threshold_row(cases, threshold)
        for threshold in sorted(
            {case["confidence"] for case in cases}, reverse=True
        )
    ]
    statistically_safe = [
        row
        for row in all_unique_rows
        if row["wilson_accuracy_lower_95"] >= 0.99
    ]
    ece, bins = _expected_calibration_error(cases)
    return {
        "case_count": len(cases),
        "correct_cases": sum(case["correct"] for case in cases),
        "thresholds": rows,
        "highest_coverage_zero_observed_error": zero_error_row,
        "highest_coverage_with_99_percent_wilson_lower": (
            max(statistically_safe, key=lambda row: row["covered_cases"])
            if statistically_safe
            else None
        ),
        "discrimination_auc": _auc(cases),
        "brier_score": sum(
            (case["confidence"] - float(case["correct"])) ** 2
            for case in cases
        )
        / len(cases),
        "expected_calibration_error_10_bin": ece,
        "calibration_bins": bins,
        "high_confidence_failures": [
            {
                "case_id": case["case_id"],
                "category": case["category"],
                "confidence": case["confidence"],
            }
            for case in sorted(
                wrong,
                key=lambda case: case["confidence"],
                reverse=True,
            )[:25]
        ],
    }


def _threshold_row(
    cases: list[dict[str, Any]], threshold: float
) -> dict[str, Any]:
    selected = [
        case for case in cases if case["confidence"] >= threshold
    ]
    correct = sum(case["correct"] for case in selected)
    lower, upper = wilson_interval(correct, len(selected))
    return {
        "threshold": threshold,
        "covered_cases": len(selected),
        "coverage": len(selected) / len(cases),
        "correct_cases": correct,
        "wrong_cases": len(selected) - correct,
        "observed_accuracy": (
            correct / len(selected) if selected else None
        ),
        "wilson_accuracy_lower_95": lower,
        "wilson_accuracy_upper_95": upper,
    }


def _expected_calibration_error(
    cases: list[dict[str, Any]],
) -> tuple[float, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    weighted_error = 0.0
    for index in range(10):
        lower = index / 10
        upper = (index + 1) / 10
        selected = [
            case
            for case in cases
            if lower <= case["confidence"] <= upper
            and (index == 9 or case["confidence"] < upper)
        ]
        if not selected:
            continue
        mean_confidence = sum(
            case["confidence"] for case in selected
        ) / len(selected)
        accuracy = sum(case["correct"] for case in selected) / len(
            selected
        )
        weighted_error += (
            len(selected) / len(cases)
        ) * abs(accuracy - mean_confidence)
        rows.append(
            {
                "lower": lower,
                "upper": upper,
                "cases": len(selected),
                "mean_confidence": mean_confidence,
                "accuracy": accuracy,
            }
        )
    return weighted_error, rows


def _auc(cases: list[dict[str, Any]]) -> float | None:
    correct = [case for case in cases if case["correct"]]
    wrong = [case for case in cases if not case["correct"]]
    if not correct or not wrong:
        return None
    favourable = 0.0
    for positive in correct:
        for negative in wrong:
            if positive["confidence"] > negative["confidence"]:
                favourable += 1
            elif positive["confidence"] == negative["confidence"]:
                favourable += 0.5
    return favourable / (len(correct) * len(wrong))
