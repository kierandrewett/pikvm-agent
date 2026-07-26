#!/usr/bin/env python3
"""Measure whether independent OCR agreement is safe verification authority."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pikvm_agent.harness.ocr_blind_benchmark import (
    normalize_ocr_text,
    ocr_category_tier,
)
from pikvm_agent.harness.ocr_confidence import wilson_interval


def _coverage(items: list[bool]) -> dict[str, int | float]:
    matches = sum(items)
    return {
        "cases": len(items),
        "matches": matches,
        "rate": matches / max(1, len(items)),
    }


def _known_intent_candidate_union(
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    by_category: dict[str, list[bool]] = defaultdict(list)
    by_tier: dict[str, list[bool]] = defaultdict(list)
    all_matches: list[bool] = []
    for first, second in rows:
        if (
            first.get("expected") != second.get("expected")
            or first.get("category") != second.get("category")
        ):
            raise ValueError("OCR reports disagree on case ground truth")
        matched = bool(
            first.get(
                "expected_aware_normalized_exact",
                first["normalized_exact"],
            )
            or second.get(
                "expected_aware_normalized_exact",
                second["normalized_exact"],
            )
        )
        category = str(first["category"])
        all_matches.append(matched)
        by_category[category].append(matched)
        by_tier[ocr_category_tier(category)].append(matched)
    totals = _coverage(all_matches)
    return {
        "cases": totals["cases"],
        "matches": totals["matches"],
        "rate": totals["rate"],
        "by_category": {
            category: _coverage(items)
            for category, items in sorted(by_category.items())
        },
        "by_tier": {
            tier: _coverage(items)
            for tier, items in sorted(by_tier.items())
        },
        "known_intent_only": True,
        "nearest_guess_allowed": False,
        "commit_authority": False,
    }


def analyze_agreement(
    first: dict[str, Any],
    second: dict[str, Any],
) -> dict[str, Any]:
    second_by_id = {item["case_id"]: item for item in second["results"]}
    rows = [
        (item, second_by_id[item["case_id"]])
        for item in first["results"]
    ]
    if len(rows) != len(second["results"]):
        raise ValueError("OCR reports must cover the same case ids")
    agreements = [
        (left, right)
        for left, right in rows
        if normalize_ocr_text(left["observed"])
        == normalize_ocr_text(right["observed"])
    ]
    correct = sum(left["normalized_exact"] for left, _ in agreements)
    low, high = wilson_interval(successes=correct, total=len(agreements))
    categories: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = (
        defaultdict(list)
    )
    for left, right in agreements:
        categories[left["category"]].append((left, right))
    by_category = {}
    for category, items in sorted(categories.items()):
        matches = sum(left["normalized_exact"] for left, _ in items)
        category_low, category_high = wilson_interval(
            successes=matches,
            total=len(items),
        )
        by_category[category] = {
            "covered": len(items),
            "correct": matches,
            "wrong": len(items) - matches,
            "accuracy": matches / len(items),
            "wilson_95": [category_low, category_high],
        }
    both = sum(
        left["normalized_exact"] and right["normalized_exact"]
        for left, right in rows
    )
    first_only = sum(
        left["normalized_exact"] and not right["normalized_exact"]
        for left, right in rows
    )
    second_only = sum(
        not left["normalized_exact"] and right["normalized_exact"]
        for left, right in rows
    )
    return {
        "schema_version": 1,
        "cases": len(rows),
        "first_provider": first["provider"],
        "second_provider": second["provider"],
        "both_correct": both,
        "first_only_correct": first_only,
        "second_only_correct": second_only,
        "neither_correct": len(rows) - both - first_only - second_only,
        "oracle_union_correct": both + first_only + second_only,
        "agreement": {
            "covered": len(agreements),
            "coverage": len(agreements) / len(rows),
            "correct": correct,
            "wrong": len(agreements) - correct,
            "accuracy": correct / max(1, len(agreements)),
            "wilson_95": [low, high],
        },
        "agreement_by_category": by_category,
        "known_intent_candidate_union": _known_intent_candidate_union(rows),
        # Independent engines can share preprocessing, fonts, and systematic
        # confusable-character failures. Agreement may request a recheck but
        # never authorizes an irreversible action on its own.
        "safe_commit_authority": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = analyze_agreement(
        json.loads(args.first.read_text()),
        json.loads(args.second.read_text()),
    )
    report["recorded_at"] = datetime.now(UTC).isoformat()
    report["source_reports"] = [
        {
            "provider": provider,
            "name": source.name,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
        for provider, source in (
            (report["first_provider"], args.first),
            (report["second_provider"], args.second),
        )
    ]
    report["evaluation_scope"] = {
        "candidate_union_reconstructed_from_paired_completed_runs": True,
        "hybrid_provider_executed": False,
        "computer_target_contacted": False,
        "nearest_guess_allowed": False,
    }
    rendered = json.dumps(report, indent=2) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered)
    print(rendered, end="")
    return 0 if report["safe_commit_authority"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
