"""Coverage ledger for every critical/high historical PiKVM incident."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INCIDENTS_PATH = ROOT / "bench" / "historical_pikvm_incidents.json"
COVERAGE_PATH = ROOT / "bench" / "historical_pikvm_coverage.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_critical_and_high_incident_has_one_honest_coverage_entry() -> None:
    incidents = _load(INCIDENTS_PATH)
    coverage = _load(COVERAGE_PATH)
    expected = {
        incident["id"]
        for incident in incidents["incidents"]
        if incident["severity"] in {"critical", "high"}
    }
    mapped = [
        incident_id
        for family in coverage["control_families"]
        for incident_id in family["incident_ids"]
    ]

    assert coverage["scope"]["incident_count"] == len(expected) == 47
    assert set(mapped) == expected
    assert len(mapped) == len(set(mapped))


def test_coverage_status_counts_and_limitations_are_not_hand_waved() -> None:
    coverage = _load(COVERAGE_PATH)
    counts: Counter[str] = Counter()
    for family in coverage["control_families"]:
        status = family["status"]
        assert status in {"covered_local", "partial", "open"}
        assert family["controls"]
        assert family["remaining"].strip()
        counts[status] += len(family["incident_ids"])
        if status == "open":
            assert not family["regression_tests"]
        else:
            assert family["regression_tests"]

    assert {
        status: counts[status]
        for status in ("covered_local", "partial", "open")
    } == coverage["status_counts"]
    assert counts["partial"] + counts["open"] > counts["covered_local"]


def test_every_claimed_regression_node_exists() -> None:
    coverage = _load(COVERAGE_PATH)
    for family in coverage["control_families"]:
        for node_id in family["regression_tests"]:
            relative_path, function_name = node_id.split("::", maxsplit=1)
            source_path = ROOT / relative_path
            assert source_path.is_file(), node_id
            source = source_path.read_text(encoding="utf-8")
            assert f"def {function_name}(" in source, node_id
