from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.core.models import OCRCandidate, OCRResult
from pikvm_agent.harness.ocr_blind_benchmark import BlindOcrInput
from pikvm_agent.harness.ocr_spacing_benchmark import (
    DEFAULT_SPACING_CASE_COUNT,
    SpacingObservation,
    build_spacing_report,
    generate_spacing_cases,
    merge_spacing_shard_reports,
    observe_spacing_input,
    score_spacing_case,
)


def test_spacing_corpus_is_unique_balanced_and_reproducible() -> None:
    cases = generate_spacing_cases()
    repeated = generate_spacing_cases()

    assert len(cases) == DEFAULT_SPACING_CASE_COUNT == 1_000
    assert cases == repeated
    assert len({case.fingerprint() for case in cases}) == 1_000
    assert sum(case.corruption == "none" for case in cases) == 500
    assert sum(case.corruption == "doubled_space" for case in cases) == 500
    assert set({
        (
            case.geometry,
            case.corruption,
        ): sum(
            other.geometry == case.geometry
            and other.corruption == case.corruption
            for other in cases
            )
            for case in cases
    }.values()) == {125}


def test_spacing_scorer_exposes_false_verification_and_safe_veto() -> None:
    case = next(
        item
        for item in generate_spacing_cases(count=8, seed=77)
        if item.corruption == "doubled_space"
    )
    collapsed = score_spacing_case(
        case,
        SpacingObservation(
            case_id=case.case_id,
            observed=case.intended,
            spacing_evidence="verified",
            latency_ms=2,
        ),
    )
    vetoed = score_spacing_case(
        case,
        SpacingObservation(
            case_id=case.case_id,
            observed=case.intended,
            spacing_candidates=[case.displayed],
            latency_ms=2,
        ),
    )

    assert collapsed.verdict == "false_verified_corruption"
    assert collapsed.screen_exact_candidate is False
    assert vetoed.verdict == "detected_corruption"
    assert vetoed.spacing_candidate_screen_exact is True


def test_uncalibrated_spacing_never_authorizes_exact_completion() -> None:
    case = next(
        item
        for item in generate_spacing_cases(count=8, seed=76)
        if item.corruption == "doubled_space"
    )

    result = score_spacing_case(
        case,
        SpacingObservation(
            case_id=case.case_id,
            observed=case.intended,
            spacing_evidence="uncertain",
            latency_ms=2,
        ),
    )

    assert result.verdict == "conservatively_blocked"


def test_spacing_scorer_counts_false_alarm_on_correct_field() -> None:
    case = next(
        item
        for item in generate_spacing_cases(count=8, seed=78)
        if item.corruption == "none"
    )
    words = case.intended.split(" ")
    wrong = f"{words[0]}  {' '.join(words[1:])}"

    result = score_spacing_case(
        case,
        SpacingObservation(
            case_id=case.case_id,
            observed=case.intended,
            spacing_candidates=[wrong],
            latency_ms=2,
        ),
    )

    assert result.verdict == "false_spacing_alarm"


async def test_spacing_observer_receives_no_expected_text(
    tmp_path: Path,
) -> None:
    class Provider:
        async def ocr_precise(self, image_path: Path) -> OCRResult:
            assert image_path == tmp_path / "opaque.png"
            return OCRResult(
                alternatives=[
                    OCRCandidate(
                        text="visible  spacing",
                        evidence_kind="spacing",
                    ),
                    OCRCandidate(text="generic alternative"),
                ]
            )

    observation = await observe_spacing_input(
        Provider(),
        BlindOcrInput(
            case_id="spacing-0000",
            image_path=tmp_path / "opaque.png",
        ),
    )

    assert observation.spacing_candidates == ["visible  spacing"]


def test_spacing_report_fails_release_gate_on_silent_space_collapse() -> None:
    cases = generate_spacing_cases(count=8, seed=79)
    results = [
        score_spacing_case(
            case,
            SpacingObservation(
                case_id=case.case_id,
                observed=case.intended,
                spacing_evidence="verified",
                latency_ms=2,
            ),
        )
        for case in cases
    ]

    report = build_spacing_report(
        provider_name="collapse-spy",
        corpus_seed=79,
        evaluation_seed=80,
        evaluation_wall_ms=16,
        results=results,
    )

    assert report.false_verified_corruptions == 4
    assert report.release_gate_passed is False
    assert any(
        "doubled-space fields were falsely verified" in failure
        for failure in report.release_gate_failures
    )


def test_cli_exposes_spacing_specific_blind_benchmark() -> None:
    result = CliRunner().invoke(
        app,
        ["harness", "ocr-spacing-benchmark", "--help"],
    )

    assert result.exit_code == 0
    assert "--cases" in result.stdout
    assert "--provider" in result.stdout
    assert "--out" in result.stdout
    assert "--shard-index" in result.stdout
    assert "--shard-count" in result.stdout


def test_shard_merge_refuses_missing_or_duplicate_cases(tmp_path: Path) -> None:
    cases = generate_spacing_cases(count=8, seed=81)
    shard_paths: list[Path] = []
    for shard_index in range(2):
        selected = cases[shard_index::2]
        results = [
            score_spacing_case(
                case,
                SpacingObservation(
                    case_id=case.case_id,
                    observed=case.intended,
                    spacing_evidence="verified",
                    latency_ms=2,
                ),
            )
            for case in selected
        ]
        report = build_spacing_report(
            provider_name="shard-spy",
            corpus_seed=81,
            evaluation_seed=82,
            evaluation_wall_ms=8,
            results=results,
            corpus_cases=8,
            shard_index=shard_index,
            shard_count=2,
        )
        path = tmp_path / f"shard-{shard_index}.json"
        path.write_text(report.model_dump_json())
        shard_paths.append(path)

    merged = merge_spacing_shard_reports(
        shard_paths,
        output_dir=tmp_path / "merged",
    )

    assert merged.cases == 8
    assert merged.source_shards == 2
    assert (tmp_path / "merged" / "report.json").is_file()

    try:
        merge_spacing_shard_reports(
            [shard_paths[0], shard_paths[0]],
            output_dir=tmp_path / "duplicate",
        )
    except ValueError as exc:
        assert "duplicate shard index" in str(exc)
    else:
        raise AssertionError("duplicate shard must be refused")
