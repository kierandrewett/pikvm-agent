from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.core.models import OCRCandidate, OCRLine, OCRResult, Region
from pikvm_agent.harness.ocr_blind_benchmark import (
    DEFAULT_CASE_COUNT,
    BlindOcrInput,
    BlindOcrObservation,
    build_ocr_report,
    discover_fonts,
    generate_ocr_cases,
    normalize_ocr_text,
    observe_blind_input,
    ocr_release_gate_failures,
    render_ocr_case,
    run_blind_ocr_benchmark,
    score_ocr_case,
)
from pikvm_agent.vision.tesseract_ocr import (
    TesseractOcrProvider,
    _remove_window_control_dot_artifacts,
    tesseract_available,
)


class BlindSpyOcr:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.calls: list[tuple[Path, Region | None]] = []

    async def ocr(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        # Ground truth must not exist anywhere in the result directory while
        # the provider is being evaluated.
        assert not (self.output_dir / "ground-truth.private.jsonl").exists()
        self.calls.append((image_path, region))
        return OCRResult(lines=[])


def test_default_corpus_is_exactly_1000_unique_balanced_examples() -> None:
    cases = generate_ocr_cases()

    assert len(cases) == DEFAULT_CASE_COUNT == 1_000
    assert len({case.case_id for case in cases}) == 1_000
    assert len({case.fingerprint() for case in cases}) == 1_000
    category_counts: dict[str, int] = {}
    for case in cases:
        category_counts[case.category] = category_counts.get(case.category, 0) + 1
    assert set(category_counts.values()) == {100}


def test_corpus_and_render_parameters_are_seeded_and_reproducible() -> None:
    first = generate_ocr_cases(count=40, seed=1234)
    again = generate_ocr_cases(count=40, seed=1234)
    different = generate_ocr_cases(count=40, seed=1235)

    assert first == again
    assert [case.fingerprint() for case in first] != [
        case.fingerprint() for case in different
    ]


def test_generated_ground_truth_is_visible_in_rendered_image() -> None:
    draw = ImageDraw.Draw(Image.new("RGB", (32, 32), "white"))

    for case in generate_ocr_cases():
        try:
            font = ImageFont.truetype(
                case.render.font_path,
                case.render.font_size,
            )
        except OSError:
            font = ImageFont.load_default()
        widest_line = max(
            draw.textbbox((0, 0), line, font=font)[2]
            for line in (case.expected.splitlines() or [case.expected])
        )

        assert widest_line + (2 * case.render.padding) <= case.render.width


def test_discovered_corpus_fonts_exclude_language_specific_noto_faces() -> None:
    regular, mono = discover_fonts()

    assert regular
    assert mono
    assert not any("notosans" in path.name.casefold() for path in regular)


async def test_runner_keeps_ground_truth_out_of_every_ocr_call(
    tmp_path: Path,
) -> None:
    provider = BlindSpyOcr(tmp_path)

    report = await run_blind_ocr_benchmark(
        provider,
        provider_name="blind-spy",
        output_dir=tmp_path,
        count=12,
        corpus_seed=77,
        evaluation_seed=91,
        jobs=3,
    )

    assert report.cases == 12
    assert len(provider.calls) == 12
    assert all(region is None for _, region in provider.calls)
    assert all(path.parent.name == "images" for path, _ in provider.calls)
    assert all(path.stem.startswith("ocr-") for path, _ in provider.calls)
    assert report.evaluation_wall_ms >= 0
    assert report.throughput_images_per_second > 0
    assert (tmp_path / "ground-truth.private.jsonl").is_file()
    assert (tmp_path / "report.json").is_file()


async def test_report_retains_provider_participation_diagnostics(
    tmp_path: Path,
) -> None:
    provider = BlindSpyOcr(tmp_path)
    provider.diagnostics = lambda: {
        "secondary_attempted": 1,
        "secondary_completed": 0,
        "secondary_skipped_busy": 2,
    }

    report = await run_blind_ocr_benchmark(
        provider,
        provider_name="diagnostic-spy",
        output_dir=tmp_path,
        count=3,
        corpus_seed=77,
        evaluation_seed=91,
        jobs=3,
    )

    assert report.provider_diagnostics == {
        "secondary_attempted": 1,
        "secondary_completed": 0,
        "secondary_skipped_busy": 2,
    }
    persisted = json.loads((tmp_path / "report.json").read_text())
    assert persisted["provider_diagnostics"] == report.provider_diagnostics


def test_scorer_reports_exact_normalized_case_and_symbol_failures() -> None:
    case = generate_ocr_cases(count=1, seed=55)[0]
    case.expected = "OAuthURL | Retry"

    normalized = score_ocr_case(
        case,
        BlindOcrObservation(
            case_id=case.case_id,
            observed="OAuthURL   |   Retry",
            latency_ms=12,
        ),
    )
    assert normalized.exact is False
    assert normalized.normalized_exact is True
    assert normalized.character_error_rate == 0.0

    case_only = score_ocr_case(
        case,
        BlindOcrObservation(
            case_id=case.case_id,
            observed="oauthurl | retry",
            latency_ms=13,
        ),
    )
    assert case_only.casefold_exact is True
    assert case_only.failure_kind == "case_only"

    symbol = score_ocr_case(
        case,
        BlindOcrObservation(
            case_id=case.case_id,
            observed="OAuthURL l Retry",
            latency_ms=14,
        ),
    )
    assert symbol.normalized_exact is False
    assert symbol.edit_distance == 1
    assert symbol.failure_kind == "symbol_or_spacing"


def test_report_exposes_confidence_coverage_and_high_confidence_errors() -> None:
    cases = generate_ocr_cases(count=3, seed=551)
    results = [
        score_ocr_case(
            case,
            BlindOcrObservation(
                case_id=case.case_id,
                observed=case.expected if index != 1 else f"{case.expected}x",
                alternative_observed=(
                    [case.expected] if index == 1 else []
                ),
                latency_ms=10 + index,
                mean_confidence=(0.98, 0.97, 0.62)[index],
                line_count=1,
            ),
        )
        for index, case in enumerate(cases)
    ]

    report = build_ocr_report(
        provider_name="confidence-spy",
        corpus_seed=551,
        evaluation_seed=9,
        evaluation_wall_ms=30,
        results=results,
    )

    point = next(item for item in report.confidence_coverage if item.threshold == 0.9)
    assert point.covered_cases == 2
    assert point.coverage == pytest.approx(2 / 3)
    assert point.normalized_exact_rate == 0.5
    assert point.wrong_cases == 1
    assert report.results[1].mean_confidence == 0.97
    assert report.results[1].line_count == 1
    assert report.results[1].expected_aware_normalized_exact is True
    assert report.expected_aware_normalized_exact_matches == 3
    assert report.expected_aware_normalized_exact_rate == 1.0


def test_release_gate_rejects_execution_success_without_accuracy() -> None:
    cases = generate_ocr_cases(count=10, seed=552)
    report = build_ocr_report(
        provider_name="execution-only",
        corpus_seed=552,
        evaluation_seed=10,
        evaluation_wall_ms=30,
        results=[
            score_ocr_case(
                case,
                BlindOcrObservation(
                    case_id=case.case_id,
                    observed="",
                    latency_ms=3,
                ),
            )
            for case in cases
        ],
    )

    failures = ocr_release_gate_failures(report)

    assert "requires 1000 cases, observed 10" in failures
    assert any("normalized exact rate" in failure for failure in failures)
    assert any("mean character error rate" in failure for failure in failures)
    assert any("requires 100 cases" in failure for failure in failures)


def test_report_separates_routine_accuracy_from_confusable_stress() -> None:
    cases = generate_ocr_cases(count=1_000, seed=104_729)
    results = []
    for case in cases:
        observed = case.expected
        if case.category in {"numeric", "punctuation"}:
            observed = "X" + case.expected[1:]
        results.append(
            score_ocr_case(
                case,
                BlindOcrObservation(
                    case_id=case.case_id,
                    observed=observed,
                    latency_ms=3,
                ),
            )
        )
    report = build_ocr_report(
        provider_name="tier-spy",
        corpus_seed=104_729,
        evaluation_seed=65_537,
        evaluation_wall_ms=30,
        results=results,
    )

    assert report.by_tier["routine"].cases == 800
    assert report.by_tier["routine"].normalized_exact_rate == 1.0
    assert report.by_tier["stress"].cases == 200
    assert report.by_tier["stress"].normalized_exact_rate == 0.0
    failures = ocr_release_gate_failures(report)
    assert any("numeric normalized exact rate" in failure for failure in failures)
    assert any("punctuation normalized exact rate" in failure for failure in failures)


def test_release_gate_rejects_unbounded_confusable_stress_errors() -> None:
    cases = generate_ocr_cases(count=1_000, seed=104_729)
    report = build_ocr_report(
        provider_name="tier-spy",
        corpus_seed=104_729,
        evaluation_seed=65_537,
        evaluation_wall_ms=30,
        results=[
            score_ocr_case(
                case,
                BlindOcrObservation(
                    case_id=case.case_id,
                    observed=(
                        ""
                        if case.category in {"numeric", "punctuation"}
                        else case.expected
                    ),
                    latency_ms=3,
                ),
            )
            for case in cases
        ],
    )

    failures = ocr_release_gate_failures(report)

    assert any("stress tier mean character error rate" in failure for failure in failures)


def test_normalizer_preserves_case_and_symbol_semantics() -> None:
    assert normalize_ocr_text("  “Hello”\nworld  ") == '"Hello" world'
    assert normalize_ocr_text("A | B") != normalize_ocr_text("a l b")


def test_window_control_filter_keeps_legitimate_short_top_text() -> None:
    image = Image.new("RGB", (320, 100), "white")
    draw = ImageDraw.Draw(image)
    draw.text((8, 6), "File", fill=(30, 90, 180))
    lines = [
        OCRLine(text="File", confidence=0.65, bbox=[8, 6, 34, 16]),
        OCRLine(text="Document body", confidence=0.96, bbox=[8, 32, 130, 52]),
    ]

    filtered = _remove_window_control_dot_artifacts(lines, image=image)

    assert filtered == lines


def test_cli_exposes_product_ocr_benchmark_command() -> None:
    result = CliRunner().invoke(app, ["harness", "ocr-benchmark", "--help"])

    assert result.exit_code == 0
    assert "--cases" in result.stdout
    assert "--evaluation-seed" in result.stdout
    assert "--provider" in result.stdout
    assert "--out" in result.stdout
    assert "hybrid" in result.stdout


def test_cli_hybrid_benchmark_uses_precise_profile_and_parallel_primary_workers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class Provider:
        async def ocr(self, path, region=None):
            del path, region
            return OCRResult()

    async def fake_run(provider, **kwargs):
        captured["provider"] = provider
        captured.update(kwargs)
        return SimpleNamespace(
            cases=1,
            exact_rate=0.0,
            normalized_exact_rate=0.0,
            mean_character_error_rate=1.0,
            p95_latency_ms=1.0,
            evaluation_wall_ms=1,
            throughput_images_per_second=1.0,
            provider_diagnostics={},
            release_gate_passed=False,
            release_gate_failures=["fixture"],
        )

    monkeypatch.setattr(
        "pikvm_agent.vision.paddleocr_client.paddleocr_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "pikvm_agent.vision.paddleocr_client.PaddleOCRProvider",
        Provider,
    )
    monkeypatch.setattr(
        "pikvm_agent.vision.tesseract_ocr.tesseract_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "pikvm_agent.vision.tesseract_ocr.TesseractOcrProvider",
        Provider,
    )
    monkeypatch.setattr(
        "pikvm_agent.harness.ocr_blind_benchmark.run_blind_ocr_benchmark",
        fake_run,
    )

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "ocr-benchmark",
            "--provider",
            "hybrid",
            "--jobs",
            "4",
            "--cases",
            "1",
            "--out",
            str(tmp_path / "hybrid"),
        ],
    )

    assert result.exit_code == 0
    assert captured["provider"].__class__.__name__ == "HybridOcrProvider"
    assert captured["precise"] is True
    assert captured["jobs"] == 4


async def test_blind_observer_can_invoke_precise_profile_without_ground_truth(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "opaque.png"
    Image.new("RGB", (20, 10), "white").save(image_path)

    class PreciseProvider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Path]] = []

        async def ocr(
            self,
            path: Path,
            region=None,
        ) -> OCRResult:
            del region
            self.calls.append(("ordinary", path))
            return OCRResult(lines=[OCRLine(text="ordinary")])

        async def ocr_precise(
            self,
            path: Path,
            region=None,
        ) -> OCRResult:
            del region
            self.calls.append(("precise", path))
            return OCRResult(
                lines=[OCRLine(text="selected")],
                alternatives=[OCRCandidate(text="independent")],
            )

    provider = PreciseProvider()
    observation = await observe_blind_input(
        provider,
        BlindOcrInput(case_id="opaque-case", image_path=image_path),
        precise=True,
    )

    assert observation.observed == "selected"
    assert observation.alternative_observed == ["independent"]
    assert provider.calls == [("precise", image_path)]


@pytest.mark.skipif(not tesseract_available(), reason="system tesseract unavailable")
async def test_real_tesseract_blind_smoke_has_no_provider_errors(
    tmp_path: Path,
) -> None:
    report = await run_blind_ocr_benchmark(
        TesseractOcrProvider(),
        provider_name="tesseract",
        output_dir=tmp_path,
        count=10,
        corpus_seed=90210,
        evaluation_seed=17,
        jobs=2,
    )

    assert report.cases == 10
    assert all(result.error_class is None for result in report.results)
    assert sum(bool(result.observed) for result in report.results) >= 7


@pytest.mark.skipif(not tesseract_available(), reason="system tesseract unavailable")
async def test_real_tesseract_ensemble_ignores_chrome_dot_false_text(
    tmp_path: Path,
) -> None:
    case = generate_ocr_cases(count=1_000, seed=104_729)[2]
    image_path = tmp_path / "opaque.png"
    render_ocr_case(case, image_path)

    result = await TesseractOcrProvider().ocr(image_path)
    observed = "\n".join(line.text for line in result.lines)

    assert case.category == "ui_label"
    assert normalize_ocr_text(observed) == normalize_ocr_text(case.expected)


@pytest.mark.skipif(not tesseract_available(), reason="system tesseract unavailable")
async def test_real_tesseract_ensemble_removes_detected_window_control_dots(
    tmp_path: Path,
) -> None:
    case = generate_ocr_cases(count=1_000, seed=104_729)[35]
    image_path = tmp_path / "opaque.png"
    render_ocr_case(case, image_path)

    result = await TesseractOcrProvider().ocr(image_path)
    observed = "\n".join(line.text for line in result.lines)

    assert case.render.chrome is True
    assert normalize_ocr_text(observed) == normalize_ocr_text(case.expected)


@pytest.mark.benchmark
@pytest.mark.skipif(
    os.environ.get("PIKVM_RUN_OCR_BLIND") != "1",
    reason="set PIKVM_RUN_OCR_BLIND=1 for the full 1,000-case release gate",
)
async def test_full_1000_case_blind_ocr_release_gate(tmp_path: Path) -> None:
    report = await run_blind_ocr_benchmark(
        TesseractOcrProvider(),
        provider_name="tesseract",
        output_dir=tmp_path,
        count=1_000,
        corpus_seed=104_729,
        evaluation_seed=65_537,
        jobs=4,
    )

    assert report.cases == 1_000
    assert report.evaluation_wall_ms > 0
    assert report.throughput_images_per_second > 0
    assert sum(metrics.cases for metrics in report.by_category.values()) == 1_000
    assert all(result.error_class is None for result in report.results)
    failures = ocr_release_gate_failures(report)
    assert not failures, "OCR release gate failed:\n- " + "\n- ".join(failures)
