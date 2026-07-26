from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.scorecard import (
    SCORECARD_END,
    SCORECARD_START,
    render_scorecard,
    replace_scorecard,
    update_scorecard,
)


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    report = tmp_path / "result.json"
    report.write_text(
        json.dumps(
            {
                "cases": 1000,
                "correct": 789,
                "accuracy": 0.789,
                "latency": {"median_ms": 874.2, "p95_ms": 2540},
                "results": [1, 2, 3],
                "outcome": "release-gate-failed",
            }
        )
    )
    manifest = tmp_path / "scorecard.yaml"
    manifest.write_text(
        """
schema_version: 1
as_of: "2026-07-26"
rows:
  - suite: "Blind OCR"
    route: "CPU"
    source: "result.json"
    fields:
      cases: {path: "cases", format: "integer"}
      correct: {path: "correct", format: "integer"}
      accuracy: {path: "accuracy", format: "percent", digits: 1}
      median: {path: "latency.median_ms", format: "milliseconds", digits: 0}
      p95: {path: "latency.p95_ms", format: "seconds_from_ms", digits: 2}
      result_count: {path: "results", format: "count"}
      outcome: {path: "outcome", format: "text"}
    cases: "{cases}"
    result: "{correct}/{cases} ({accuracy}); {result_count} retained"
    latency: "{median} / {p95}"
    wall: "—"
    status: "Failing release gate: {outcome}"
"""
    )
    readme = tmp_path / "README.md"
    readme.write_text(f"# Evidence\n\n{SCORECARD_START}\nstale\n{SCORECARD_END}\n")
    return manifest, report, readme


def test_render_scorecard_reads_and_hashes_machine_evidence(tmp_path: Path) -> None:
    manifest, report, _readme = _fixture(tmp_path)

    rendered = render_scorecard(manifest)

    digest = hashlib.sha256(report.read_bytes()).hexdigest()[:12]
    assert "| Blind OCR | CPU | 1,000 | 789/1,000 (78.9%); 3 retained |" in rendered
    assert "874ms / 2.54s" in rendered
    assert "Failing release gate: release-gate-failed" in rendered
    assert f"[JSON](result.json) · `sha256:{digest}`" in rendered


def test_scorecard_check_detects_report_drift_without_writing(tmp_path: Path) -> None:
    manifest, report, readme = _fixture(tmp_path)
    assert update_scorecard(
        manifest_path=manifest,
        document_path=readme,
        check=False,
    )
    current = readme.read_text()
    assert update_scorecard(
        manifest_path=manifest,
        document_path=readme,
        check=True,
    )

    report.write_text(report.read_text().replace('"correct": 789', '"correct": 788'))

    assert not update_scorecard(
        manifest_path=manifest,
        document_path=readme,
        check=True,
    )
    assert readme.read_text() == current


def test_scorecard_refuses_missing_fields_and_escaping_sources(
    tmp_path: Path,
) -> None:
    manifest, _report, _readme = _fixture(tmp_path)
    missing = manifest.read_text().replace('path: "cases"', 'path: "missing"')
    manifest.write_text(missing)
    with pytest.raises(ValueError, match="no scalar path"):
        render_scorecard(manifest)

    manifest.write_text(missing.replace('source: "result.json"', 'source: "../secret"'))
    with pytest.raises(ValueError, match="beneath"):
        render_scorecard(manifest)


def test_replace_scorecard_requires_one_exact_marker_pair() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        replace_scorecard("# no markers", "rendered")


def test_scorecard_cli_updates_then_checks_the_document(tmp_path: Path) -> None:
    manifest, _report, readme = _fixture(tmp_path)
    runner = CliRunner()
    arguments = [
        "harness",
        "scorecard",
        "--manifest",
        str(manifest),
        "--document",
        str(readme),
    ]

    update = runner.invoke(app, arguments)
    check = runner.invoke(app, [*arguments, "--check"])

    assert update.exit_code == 0
    assert "Scorecard updated" in update.stdout
    assert check.exit_code == 0
    assert "evidence is current" in check.stdout


def test_checked_in_public_scorecard_matches_its_json_evidence() -> None:
    root = Path(__file__).resolve().parents[1]

    assert update_scorecard(
        manifest_path=root / "bench" / "scorecard.yaml",
        document_path=root / "bench" / "README.md",
        check=True,
    )
