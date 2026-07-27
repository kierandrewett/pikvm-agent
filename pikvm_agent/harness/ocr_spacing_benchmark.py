"""Blind OCR benchmark for visible single-space versus doubled-space integrity.

The provider receives only opaque rendered images. Ground truth is joined to
its observations after every OCR call has completed. Half of the default 1,000
cases show the requested single-space text; half contain one injected doubled
space that a safe verifier must not silently collapse back to the request.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import re
import statistics
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, Protocol, cast

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field

from pikvm_agent.core.models import OCRResult, Region
from pikvm_agent.harness.ocr_blind_benchmark import (
    DEFAULT_CORPUS_SEED,
    DEFAULT_EVALUATION_SEED,
    BlindOcrInput,
    OcrCaseSpec,
    OcrRenderSpec,
    discover_fonts,
    render_ocr_case,
)

DEFAULT_SPACING_CASE_COUNT = 1_000
SPACING_RELEASE_MIN_CORRUPTION_DETECTION = 0.99
SPACING_RELEASE_MIN_CONTROL_VERIFICATION = 0.99

_GEOMETRIES = (
    "short_proportional",
    "short_monospace",
    "long_proportional",
    "long_monospace",
)
_WORDS = (
    "agent",
    "checks",
    "current",
    "desktop",
    "document",
    "evidence",
    "field",
    "input",
    "message",
    "operator",
    "remote",
    "screen",
    "status",
    "text",
    "typing",
    "visible",
)
_WHITESPACE = re.compile(r"\s+")


class SpacingCaseSpec(BaseModel):
    """Private ground truth for one rendered field."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    geometry: Literal[
        "short_proportional",
        "short_monospace",
        "long_proportional",
        "long_monospace",
    ]
    corruption: Literal["none", "doubled_space"]
    intended: str
    displayed: str
    doubled_gap_index: int | None = None
    render: OcrRenderSpec

    def fingerprint(self) -> str:
        payload = self.model_dump_json(exclude={"case_id"})
        return hashlib.sha256(payload.encode()).hexdigest()


class SpacingObservation(BaseModel):
    """Provider output captured without any expected text in scope."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    observed: str
    spacing_candidates: list[str] = Field(default_factory=list)
    spacing_evidence: Literal[
        "not_evaluated",
        "verified",
        "uncertain",
    ] = "not_evaluated"
    latency_ms: int
    error_class: str | None = None


class SpacingCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    geometry: str
    corruption: Literal["none", "doubled_space"]
    intended: str
    displayed: str
    observed: str
    spacing_candidates: list[str] = Field(default_factory=list)
    spacing_evidence: str = "not_evaluated"
    canonical_screen_exact: bool
    spacing_candidate_screen_exact: bool
    screen_exact_candidate: bool
    whitespace_mismatch_flagged: bool
    verdict: Literal[
        "verified_control",
        "detected_corruption",
        "conservatively_blocked",
        "false_verified_corruption",
        "false_spacing_alarm",
        "provider_error",
    ]
    latency_ms: int
    error_class: str | None = None
    render: OcrRenderSpec


class SpacingGeometryMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: int
    controls: int
    corruptions: int
    control_verified_rate: float
    corruption_detection_rate: float
    false_verified_corruptions: int
    false_spacing_alarms: int
    screen_exact_candidate_rate: float


class SpacingBlindReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    provider: str
    corpus_seed: int
    evaluation_seed: int
    corpus_cases: int = DEFAULT_SPACING_CASE_COUNT
    shard_index: int = 0
    shard_count: int = 1
    source_shards: int = 1
    cases: int
    controls: int
    corruptions: int
    verified_controls: int
    detected_corruptions: int
    conservatively_blocked: int
    false_verified_corruptions: int
    false_spacing_alarms: int
    provider_errors: int
    control_verified_rate: float
    corruption_detection_rate: float
    screen_exact_candidate_rate: float
    median_latency_ms: float
    p95_latency_ms: float
    evaluation_wall_ms: int
    throughput_images_per_second: float
    release_gate_passed: bool
    release_gate_failures: list[str]
    by_geometry: dict[str, SpacingGeometryMetrics]
    results: list[SpacingCaseResult]


class SpacingBlindProvider(Protocol):
    async def ocr(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult: ...


class PreciseSpacingBlindProvider(SpacingBlindProvider, Protocol):
    async def ocr_precise(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult: ...


def _font(spec: OcrRenderSpec) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if spec.font_path:
        try:
            return ImageFont.truetype(spec.font_path, spec.font_size)
        except OSError:
            pass
    return ImageFont.load_default()


def _render_spec(
    rng: random.Random,
    *,
    monospace: bool,
    regular_fonts: Sequence[Path],
    mono_fonts: Sequence[Path],
) -> OcrRenderSpec:
    fonts = mono_fonts if monospace else regular_fonts
    theme = rng.choice(
        ("light", "light", "dark", "dark", "low_contrast_light", "low_contrast_dark")
    )
    palettes = {
        "light": ((20, 23, 28), (248, 249, 251)),
        "dark": ((232, 235, 239), (31, 34, 39)),
        "low_contrast_light": ((82, 86, 94), (224, 226, 230)),
        "low_contrast_dark": ((166, 171, 180), (47, 51, 58)),
    }
    foreground, background = palettes[theme]
    image_format = rng.choices(("PNG", "JPEG"), weights=(3, 2), k=1)[0]
    return OcrRenderSpec(
        width=rng.randint(280, 960),
        font_size=rng.randint(14, 32),
        font_path=str(rng.choice(fonts)) if fonts else "",
        theme=theme,
        foreground=foreground,
        background=background,
        padding=rng.randint(8, 28),
        line_spacing=rng.randint(3, 10),
        scale=rng.choice((0.72, 0.84, 1.0, 1.0, 1.18, 1.35)),
        blur=rng.choice((0.0, 0.0, 0.18, 0.32, 0.48)),
        noise=rng.choice((0.0, 0.0, 0.7, 1.2, 1.8)),
        rotation=rng.choice((0.0, 0.0, -0.22, 0.18)),
        image_format=image_format,
        jpeg_quality=rng.randint(58, 94),
        chrome=rng.random() < 0.16,
        tight_crop=rng.random() < 0.18,
        render_seed=rng.randrange(1, 2**31),
    )


def _fit_width(text: str, spec: OcrRenderSpec) -> OcrRenderSpec:
    probe = Image.new("RGB", (32, 32), spec.background)
    width = ImageDraw.Draw(probe).textbbox(
        (0, 0),
        text,
        font=_font(spec),
    )[2]
    required = width + (2 * spec.padding) + 2
    return spec if required <= spec.width else spec.model_copy(update={"width": required})


def generate_spacing_cases(
    count: int = DEFAULT_SPACING_CASE_COUNT,
    seed: int = DEFAULT_CORPUS_SEED,
) -> list[SpacingCaseSpec]:
    """Generate a balanced deterministic private spacing corpus."""

    if count < 1:
        raise ValueError("count must be positive")
    if count % (len(_GEOMETRIES) * 2):
        raise ValueError("count must be divisible by 8 for a balanced corpus")
    rng = random.Random(seed)
    regular_fonts, mono_fonts = discover_fonts()
    shapes = [
        (geometry, corruption)
        for geometry in _GEOMETRIES
        for corruption in ("none", "doubled_space")
        for _ in range(count // (len(_GEOMETRIES) * 2))
    ]
    rng.shuffle(shapes)
    cases: list[SpacingCaseSpec] = []
    for index, (geometry, corruption) in enumerate(shapes):
        short = geometry.startswith("short_")
        monospace = geometry.endswith("_monospace")
        word_count = rng.randint(2, 3) if short else rng.randint(7, 11)
        words = rng.sample(_WORDS, word_count)
        words[-1] = f"{words[-1]}-{index:04d}"
        intended = " ".join(words)
        doubled_gap_index = (
            rng.randrange(word_count - 1)
            if corruption == "doubled_space"
            else None
        )
        displayed = intended
        if doubled_gap_index is not None:
            parts = intended.split(" ")
            displayed = " ".join(parts[: doubled_gap_index + 1])
            displayed += "  " + " ".join(parts[doubled_gap_index + 1 :])
        render = _fit_width(
            displayed,
            _render_spec(
                rng,
                monospace=monospace,
                regular_fonts=regular_fonts,
                mono_fonts=mono_fonts,
            ),
        )
        cases.append(
            SpacingCaseSpec(
                case_id=f"spacing-{index:04d}",
                geometry=cast(
                    Literal[
                        "short_proportional",
                        "short_monospace",
                        "long_proportional",
                        "long_monospace",
                    ],
                    geometry,
                ),
                corruption=cast(
                    Literal["none", "doubled_space"],
                    corruption,
                ),
                intended=intended,
                displayed=displayed,
                doubled_gap_index=doubled_gap_index,
                render=render,
            )
        )
    if len({case.fingerprint() for case in cases}) != count:
        raise RuntimeError("spacing corpus generator produced duplicate examples")
    return cases


def _same_non_whitespace(left: str, right: str) -> bool:
    return _WHITESPACE.sub("", left) == _WHITESPACE.sub("", right)


def _whitespace_signature(value: str) -> tuple[str, ...]:
    return tuple(match.group(0) for match in _WHITESPACE.finditer(value))


def _flags_spacing_mismatch(intended: str, candidate: str) -> bool:
    return (
        intended != candidate
        and _same_non_whitespace(intended, candidate)
        and _whitespace_signature(intended) != _whitespace_signature(candidate)
    )


async def observe_spacing_input(
    provider: SpacingBlindProvider,
    blind_input: BlindOcrInput,
) -> SpacingObservation:
    """Call precise OCR with only an opaque image path."""

    started = time.monotonic()
    try:
        precise = getattr(provider, "ocr_precise", None)
        result = (
            await precise(blind_input.image_path)
            if callable(precise)
            else await provider.ocr(blind_input.image_path)
        )
        observed = result.text
        spacing_candidates = [
            candidate.text
            for candidate in result.alternatives
            if candidate.evidence_kind == "spacing"
        ]
        spacing_evidence = result.spacing_evidence
        error_class = None
    except Exception as exc:  # benchmark boundary records failures in denominator
        observed = ""
        spacing_candidates = []
        spacing_evidence = "not_evaluated"
        error_class = type(exc).__name__
    return SpacingObservation(
        case_id=blind_input.case_id,
        observed=observed,
        spacing_candidates=spacing_candidates,
        spacing_evidence=spacing_evidence,
        latency_ms=round((time.monotonic() - started) * 1_000),
        error_class=error_class,
    )


def score_spacing_case(
    case: SpacingCaseSpec,
    observation: SpacingObservation,
) -> SpacingCaseResult:
    candidates = [observation.observed, *observation.spacing_candidates]
    mismatch_flagged = any(
        _flags_spacing_mismatch(case.intended, candidate)
        for candidate in observation.spacing_candidates
    )
    canonical_screen_exact = observation.observed == case.displayed
    spacing_candidate_screen_exact = case.displayed in observation.spacing_candidates
    screen_exact_candidate = case.displayed in candidates
    if observation.error_class:
        verdict = "provider_error"
    elif case.corruption == "doubled_space":
        if mismatch_flagged:
            verdict = "detected_corruption"
        elif (
            observation.observed == case.intended
            and observation.spacing_evidence == "verified"
        ):
            verdict = "false_verified_corruption"
        else:
            verdict = "conservatively_blocked"
    elif mismatch_flagged:
        verdict = "false_spacing_alarm"
    elif (
        observation.observed == case.intended
        and observation.spacing_evidence == "verified"
    ):
        verdict = "verified_control"
    else:
        verdict = "conservatively_blocked"
    return SpacingCaseResult(
        case_id=case.case_id,
        geometry=case.geometry,
        corruption=case.corruption,
        intended=case.intended,
        displayed=case.displayed,
        observed=observation.observed,
        spacing_candidates=observation.spacing_candidates,
        spacing_evidence=observation.spacing_evidence,
        canonical_screen_exact=canonical_screen_exact,
        spacing_candidate_screen_exact=spacing_candidate_screen_exact,
        screen_exact_candidate=screen_exact_candidate,
        whitespace_mismatch_flagged=mismatch_flagged,
        verdict=cast(
            Literal[
                "verified_control",
                "detected_corruption",
                "conservatively_blocked",
                "false_verified_corruption",
                "false_spacing_alarm",
                "provider_error",
            ],
            verdict,
        ),
        latency_ms=observation.latency_ms,
        error_class=observation.error_class,
        render=case.render,
    )


def _percentile(values: Sequence[int], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def spacing_release_gate_failures(
    *,
    cases: int,
    controls: int,
    corruptions: int,
    control_verified_rate: float,
    corruption_detection_rate: float,
    false_verified_corruptions: int,
    false_spacing_alarms: int,
    provider_errors: int,
) -> list[str]:
    failures: list[str] = []
    if cases != DEFAULT_SPACING_CASE_COUNT:
        failures.append(
            f"requires {DEFAULT_SPACING_CASE_COUNT} cases, observed {cases}"
        )
    if controls != cases // 2 or corruptions != cases // 2:
        failures.append("requires an even control/corruption split")
    if false_verified_corruptions:
        failures.append(
            f"{false_verified_corruptions} doubled-space fields were falsely verified"
        )
    if false_spacing_alarms:
        failures.append(f"{false_spacing_alarms} correct fields raised spacing alarms")
    if corruption_detection_rate < SPACING_RELEASE_MIN_CORRUPTION_DETECTION:
        failures.append(
            "corruption detection rate "
            f"{corruption_detection_rate:.3%} is below "
            f"{SPACING_RELEASE_MIN_CORRUPTION_DETECTION:.1%}"
        )
    if control_verified_rate < SPACING_RELEASE_MIN_CONTROL_VERIFICATION:
        failures.append(
            "control verification rate "
            f"{control_verified_rate:.3%} is below "
            f"{SPACING_RELEASE_MIN_CONTROL_VERIFICATION:.1%}"
        )
    if provider_errors:
        failures.append(f"{provider_errors} provider calls failed")
    return failures


def build_spacing_report(
    *,
    provider_name: str,
    corpus_seed: int,
    evaluation_seed: int,
    evaluation_wall_ms: int,
    results: list[SpacingCaseResult],
    corpus_cases: int | None = None,
    shard_index: int = 0,
    shard_count: int = 1,
    source_shards: int = 1,
) -> SpacingBlindReport:
    controls = [item for item in results if item.corruption == "none"]
    corruptions = [
        item for item in results if item.corruption == "doubled_space"
    ]
    verified_controls = sum(
        item.verdict == "verified_control" for item in controls
    )
    detected_corruptions = sum(
        item.verdict == "detected_corruption" for item in corruptions
    )
    false_verified = sum(
        item.verdict == "false_verified_corruption" for item in corruptions
    )
    false_alarms = sum(
        item.verdict == "false_spacing_alarm" for item in controls
    )
    provider_errors = sum(item.verdict == "provider_error" for item in results)
    blocked = sum(item.verdict == "conservatively_blocked" for item in results)
    control_rate = verified_controls / max(1, len(controls))
    detection_rate = detected_corruptions / max(1, len(corruptions))
    screen_rate = sum(item.screen_exact_candidate for item in results) / max(
        1,
        len(results),
    )
    failures = spacing_release_gate_failures(
        cases=len(results),
        controls=len(controls),
        corruptions=len(corruptions),
        control_verified_rate=control_rate,
        corruption_detection_rate=detection_rate,
        false_verified_corruptions=false_verified,
        false_spacing_alarms=false_alarms,
        provider_errors=provider_errors,
    )

    by_geometry: dict[str, SpacingGeometryMetrics] = {}
    for geometry in _GEOMETRIES:
        items = [item for item in results if item.geometry == geometry]
        geometry_controls = [item for item in items if item.corruption == "none"]
        geometry_corruptions = [
            item for item in items if item.corruption == "doubled_space"
        ]
        by_geometry[geometry] = SpacingGeometryMetrics(
            cases=len(items),
            controls=len(geometry_controls),
            corruptions=len(geometry_corruptions),
            control_verified_rate=sum(
                item.verdict == "verified_control"
                for item in geometry_controls
            )
            / max(1, len(geometry_controls)),
            corruption_detection_rate=sum(
                item.verdict == "detected_corruption"
                for item in geometry_corruptions
            )
            / max(1, len(geometry_corruptions)),
            false_verified_corruptions=sum(
                item.verdict == "false_verified_corruption"
                for item in geometry_corruptions
            ),
            false_spacing_alarms=sum(
                item.verdict == "false_spacing_alarm"
                for item in geometry_controls
            ),
            screen_exact_candidate_rate=sum(
                item.screen_exact_candidate for item in items
            )
            / max(1, len(items)),
        )

    latencies = [item.latency_ms for item in results]
    return SpacingBlindReport(
        provider=provider_name,
        corpus_seed=corpus_seed,
        evaluation_seed=evaluation_seed,
        corpus_cases=corpus_cases or len(results),
        shard_index=shard_index,
        shard_count=shard_count,
        source_shards=source_shards,
        cases=len(results),
        controls=len(controls),
        corruptions=len(corruptions),
        verified_controls=verified_controls,
        detected_corruptions=detected_corruptions,
        conservatively_blocked=blocked,
        false_verified_corruptions=false_verified,
        false_spacing_alarms=false_alarms,
        provider_errors=provider_errors,
        control_verified_rate=control_rate,
        corruption_detection_rate=detection_rate,
        screen_exact_candidate_rate=screen_rate,
        median_latency_ms=statistics.median(latencies) if latencies else 0.0,
        p95_latency_ms=_percentile(latencies, 0.95),
        evaluation_wall_ms=evaluation_wall_ms,
        throughput_images_per_second=(
            len(results) / max(0.001, evaluation_wall_ms / 1_000)
        ),
        release_gate_passed=not failures,
        release_gate_failures=failures,
        by_geometry=by_geometry,
        results=sorted(results, key=lambda item: item.case_id),
    )


async def run_blind_spacing_benchmark(
    provider: SpacingBlindProvider,
    *,
    provider_name: str,
    output_dir: Path,
    count: int = DEFAULT_SPACING_CASE_COUNT,
    corpus_seed: int = DEFAULT_CORPUS_SEED,
    evaluation_seed: int = DEFAULT_EVALUATION_SEED,
    jobs: int = 4,
    shard_index: int = 0,
    shard_count: int = 1,
    progress: Callable[[int, int], None] | None = None,
) -> SpacingBlindReport:
    """Render, blindly evaluate, score, and persist one spacing corpus."""

    if jobs < 1:
        raise ValueError("jobs must be positive")
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    if shard_index < 0 or shard_index >= shard_count:
        raise ValueError("shard_index must be within shard_count")
    output_dir = output_dir.resolve()
    image_dir = output_dir / "images"
    output_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)
    complete_corpus = generate_spacing_cases(count=count, seed=corpus_seed)
    cases = complete_corpus[shard_index::shard_count]
    private_cases = {case.case_id: case for case in cases}
    blind_inputs: list[BlindOcrInput] = []
    for case in cases:
        extension = ".jpg" if case.render.image_format == "JPEG" else ".png"
        image_path = image_dir / f"{case.case_id}{extension}"
        render_ocr_case(
            OcrCaseSpec(
                case_id=case.case_id,
                category="spacing",
                expected=case.displayed,
                render=case.render,
            ),
            image_path,
        )
        blind_inputs.append(
            BlindOcrInput(case_id=case.case_id, image_path=image_path)
        )

    random.Random(evaluation_seed).shuffle(blind_inputs)
    semaphore = asyncio.Semaphore(jobs)
    completed = 0

    async def observe(item: BlindOcrInput) -> SpacingObservation:
        nonlocal completed
        async with semaphore:
            observation = await observe_spacing_input(provider, item)
        completed += 1
        if progress is not None:
            progress(completed, len(cases))
        return observation

    started = time.monotonic()
    observations = await asyncio.gather(*(observe(item) for item in blind_inputs))
    wall_ms = round((time.monotonic() - started) * 1_000)
    results = [
        score_spacing_case(private_cases[item.case_id], item)
        for item in observations
    ]
    report = build_spacing_report(
        provider_name=provider_name,
        corpus_seed=corpus_seed,
        evaluation_seed=evaluation_seed,
        evaluation_wall_ms=wall_ms,
        results=results,
        corpus_cases=count,
        shard_index=shard_index,
        shard_count=shard_count,
    )
    (output_dir / "ground-truth.private.jsonl").write_text(
        "".join(
            json.dumps(case.model_dump(mode="json"), sort_keys=True) + "\n"
            for case in cases
        ),
        encoding="utf-8",
    )
    (output_dir / "failures.jsonl").write_text(
        "".join(
            json.dumps(item.model_dump(mode="json"), sort_keys=True) + "\n"
            for item in report.results
            if item.verdict
            not in {"verified_control", "detected_corruption"}
        ),
        encoding="utf-8",
    )
    (output_dir / "report.json").write_text(
        report.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return report


def merge_spacing_shard_reports(
    report_paths: Sequence[Path],
    *,
    output_dir: Path,
) -> SpacingBlindReport:
    """Validate and merge a complete set of independently executed shards."""

    if not report_paths:
        raise ValueError("at least one shard report is required")
    reports = [
        SpacingBlindReport.model_validate_json(path.read_text(encoding="utf-8"))
        for path in report_paths
    ]
    first = reports[0]
    identity = (
        first.provider,
        first.corpus_seed,
        first.evaluation_seed,
        first.corpus_cases,
        first.shard_count,
    )
    if first.shard_count <= 1:
        raise ValueError("reports are not a sharded benchmark")
    for report in reports:
        if (
            report.provider,
            report.corpus_seed,
            report.evaluation_seed,
            report.corpus_cases,
            report.shard_count,
        ) != identity:
            raise ValueError("shard report identities do not match")
    indexes = [report.shard_index for report in reports]
    if len(indexes) != len(set(indexes)):
        raise ValueError("duplicate shard index")
    expected_indexes = set(range(first.shard_count))
    if set(indexes) != expected_indexes:
        missing = sorted(expected_indexes - set(indexes))
        raise ValueError(f"missing shard indexes: {missing}")
    results = [item for report in reports for item in report.results]
    case_ids = [item.case_id for item in results]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("duplicate case IDs across shards")
    if len(results) != first.corpus_cases:
        raise ValueError(
            f"merged corpus requires {first.corpus_cases} cases, "
            f"observed {len(results)}"
        )
    merged = build_spacing_report(
        provider_name=first.provider,
        corpus_seed=first.corpus_seed,
        evaluation_seed=first.evaluation_seed,
        evaluation_wall_ms=sum(
            report.evaluation_wall_ms for report in reports
        ),
        results=results,
        corpus_cases=first.corpus_cases,
        source_shards=len(reports),
    )
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "failures.jsonl").write_text(
        "".join(
            json.dumps(item.model_dump(mode="json"), sort_keys=True) + "\n"
            for item in merged.results
            if item.verdict
            not in {"verified_control", "detected_corruption"}
        ),
        encoding="utf-8",
    )
    (output_dir / "report.json").write_text(
        merged.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return merged


async def run_closing_blind_spacing_benchmark(
    provider: SpacingBlindProvider,
    **kwargs: object,
) -> SpacingBlindReport:
    """Run one owned benchmark and always close native provider workers."""

    try:
        return await run_blind_spacing_benchmark(provider, **kwargs)
    finally:
        closer = getattr(provider, "aclose", None)
        if callable(closer):
            await closer()
