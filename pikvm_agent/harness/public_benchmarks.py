"""Pinned public computer-use benchmark adapters.

The module keeps upstream dataset quirks behind one small interface. Providers
receive the original public instruction and image, while scoring remains local
and deterministic.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
import statistics
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pikvm_agent.harness.agent_models import ModelRequest, ModelResponse


class PublicBenchmarkProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class GroundingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: Literal["positive", "negative"]
    # Use a fixed-length homogeneous list instead of a tuple. OpenAI/Codex
    # strict schemas accept ``items`` + min/max length but reject JSON Schema's
    # tuple-oriented ``prefixItems`` keyword.
    point: list[float] | None = Field(default=None, min_length=2, max_length=2)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def point_matches_result(self) -> "GroundingDecision":
        if self.result == "positive" and self.point is None:
            raise ValueError("positive result requires a point")
        if self.result == "negative" and self.point is not None:
            raise ValueError("negative result cannot include a point")
        if self.point is not None and not all(0 <= value <= 1 for value in self.point):
            raise ValueError("point coordinates must be normalized to [0, 1]")
        return self


class GroundingVerificationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["hit", "miss", "uncertain"]
    corrected_point: list[float] | None = Field(
        default=None,
        min_length=2,
        max_length=2,
    )
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def correction_matches_verdict(self) -> "GroundingVerificationDecision":
        if self.verdict == "miss" and self.corrected_point is None:
            raise ValueError("miss verdict requires a corrected point")
        if self.verdict != "miss" and self.corrected_point is not None:
            raise ValueError(f"{self.verdict} verdict cannot include a correction")
        if self.corrected_point is not None and not all(
            0 <= value <= 1 for value in self.corrected_point
        ):
            raise ValueError("corrected coordinates must be normalized to [0, 1]")
        return self


class ScreenSpotProCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    instruction: str
    image_path: Path
    image_size: tuple[int, int]
    target_bbox: tuple[int, int, int, int]
    application: str
    platform: str
    ui_type: str
    group: str


class PublicBenchmarkCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    application: str
    platform: str
    ui_type: str
    target_bbox: tuple[int, int, int, int]
    initial_point: tuple[int, int] | None
    initial_correct: bool
    predicted_point: tuple[int, int] | None
    click_error_pixels: float | None
    correct: bool
    correction_applied: bool = False
    verifier_suggested_point: tuple[int, int] | None = None
    verification_verdict: str | None = None
    provider: str | None = None
    model: str | None = None
    verifier_provider: str | None = None
    verifier_model: str | None = None
    latency_ms: int
    verification_latency_ms: int = 0
    usage: dict[str, object] = Field(default_factory=dict)
    verifier_usage: dict[str, object] = Field(default_factory=dict)
    error_class: str | None = None
    failure_kind: str | None = None


class PublicBenchmarkSlice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: int
    correct: int
    accuracy: float


class PublicBenchmarkReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 5
    suite: str
    suite_revision: str
    dataset_revision: str
    seed: int
    cases_discovered: int
    cases_evaluated: int
    initial_correct: int
    initial_accuracy: float
    verifier_mode: Literal["none", "veto", "correct"]
    actionable_cases: int
    abstained_cases: int
    actionable_accuracy: float | None
    correct: int
    accuracy: float
    model_calls: int
    model_active_ms: int
    usage_totals: dict[str, int | float]
    model_errors: int
    evaluation_wall_ms: int
    throughput_cases_per_second: float
    median_latency_ms: float
    p95_latency_ms: float
    by_platform: dict[str, PublicBenchmarkSlice]
    by_ui_type: dict[str, PublicBenchmarkSlice]
    by_application: dict[str, PublicBenchmarkSlice]
    results: list[PublicBenchmarkCaseResult]


def _percentile(values: Sequence[int], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _breakdown(
    results: Sequence[PublicBenchmarkCaseResult],
    field: Literal["platform", "ui_type", "application"],
) -> dict[str, PublicBenchmarkSlice]:
    values = sorted({str(getattr(result, field)) for result in results})
    output: dict[str, PublicBenchmarkSlice] = {}
    for value in values:
        selected = [result for result in results if getattr(result, field) == value]
        correct = sum(result.correct for result in selected)
        output[value] = PublicBenchmarkSlice(
            cases=len(selected),
            correct=correct,
            accuracy=correct / len(selected),
        )
    return output


def _usage_totals(
    results: Sequence[PublicBenchmarkCaseResult],
) -> dict[str, int | float]:
    totals: dict[str, int | float] = {}
    for result in results:
        for usage in (result.usage, result.verifier_usage):
            for name, value in usage.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                totals[name] = totals.get(name, 0) + value
    return totals


def _safe_model_error(exc: Exception) -> str:
    match = re.search(r"\bclass=([a-z0-9-]+)", str(exc))
    if match:
        return match.group(1)
    if type(exc).__name__ == "ValidationError":
        return "structured-output-invalid"
    return type(exc).__name__


def _load_screenspot_pro(dataset_dir: Path) -> list[ScreenSpotProCase]:
    cases: list[ScreenSpotProCase] = []
    annotation_dir = dataset_dir / "annotations"
    for annotation_path in sorted(annotation_dir.glob("*.json")):
        records = json.loads(annotation_path.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError(f"{annotation_path} must contain a JSON array")
        for record in records:
            image_path = dataset_dir / "images" / str(record["img_filename"])
            case = ScreenSpotProCase(
                case_id=str(record["id"]),
                instruction=str(record["instruction"]),
                image_path=image_path,
                image_size=tuple(record["img_size"]),
                target_bbox=tuple(record["bbox"]),
                application=str(record["application"]),
                platform=str(record["platform"]),
                ui_type=str(record["ui_type"]),
                group=str(record["group"]),
            )
            cases.append(case)
    if not cases:
        raise ValueError(f"no ScreenSpot-Pro cases found under {dataset_dir}")
    return cases


def _validate_screenspot_image(case: ScreenSpotProCase) -> None:
    if not case.image_path.is_file():
        raise FileNotFoundError(f"missing benchmark image: {case.image_path}")
    with Image.open(case.image_path) as image:
        if image.size != case.image_size:
            raise ValueError(
                f"{case.case_id} annotation size {case.image_size} "
                f"does not match image size {image.size}"
            )


def _grounding_prompt(case: ScreenSpotProCase) -> str:
    return (
        "You are evaluating GUI grounding on the public ScreenSpot-Pro "
        "benchmark. Inspect the attached full-resolution screenshot. Locate "
        "the single interface target described below. Return a positive result "
        "and the center point as normalized x,y coordinates in [0,1]. Return "
        "negative only if the requested target is genuinely absent. Do not "
        "click or perform any action.\n\n"
        f"Instruction: {case.instruction}"
    )


def _point_to_pixels(
    point: Sequence[float] | None,
    image_size: tuple[int, int],
) -> tuple[int, int] | None:
    if point is None:
        return None
    width, height = image_size
    return (
        min(width - 1, round(point[0] * width)),
        min(height - 1, round(point[1] * height)),
    )


def _point_hits(
    point: tuple[int, int] | None,
    bbox: tuple[int, int, int, int],
) -> bool:
    if point is None:
        return False
    x1, y1, x2, y2 = bbox
    return x1 <= point[0] <= x2 and y1 <= point[1] <= y2


def _candidate_overlay(
    case: ScreenSpotProCase,
    point: tuple[int, int],
    output_dir: Path,
) -> Path:
    candidate_dir = output_dir / "candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", case.case_id)
    path = candidate_dir / f"{safe_id}-candidate.png"
    with Image.open(case.image_path) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    radius = max(12, round(min(image.size) * 0.009))
    width = max(3, round(radius / 5))
    x, y = point
    draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius),
        outline=(255, 30, 30),
        width=width,
    )
    draw.line((x - radius, y, x + radius, y), fill=(255, 30, 30), width=width)
    draw.line((x, y - radius, x, y + radius), fill=(255, 30, 30), width=width)
    image.save(path, format="PNG")
    return path


def _verification_prompt(
    case: ScreenSpotProCase,
    point: tuple[int, int],
) -> str:
    width, height = case.image_size
    return (
        "Independently verify a proposed GUI grounding point on the public "
        "ScreenSpot-Pro benchmark. The attached screenshot has a red crosshair "
        "at the candidate point. Decide whether that crosshair is inside the "
        "specific interactive target requested below. If it is not, return a "
        "corrected center point as normalized x,y coordinates in [0,1]. The "
        "correction is diagnostic evidence and cannot authorize a new click "
        "unless the benchmark is explicitly in experimental correction mode. "
        "Return uncertain rather than guessing. Do not use external tools or "
        "perform an action.\n\n"
        f"Instruction: {case.instruction}\n"
        f"Candidate pixel: ({point[0]}, {point[1]}) on {width}x{height}"
    )


async def _run_grounding_case(
    provider: PublicBenchmarkProvider,
    case: ScreenSpotProCase,
    *,
    verifier_provider: PublicBenchmarkProvider | None,
    verifier_mode: Literal["veto", "correct"],
    output_dir: Path,
) -> PublicBenchmarkCaseResult:
    started = time.monotonic()
    response: ModelResponse | None = None
    try:
        response = await provider.complete(
            ModelRequest(
                role="controller",
                prompt=_grounding_prompt(case),
                output_schema=GroundingDecision.model_json_schema(),
                image_path=str(case.image_path),
                run_id=f"screenspot-pro:{case.case_id}",
                metadata={"suite": "screenspot-pro", "case_id": case.case_id},
            )
        )
        decision = GroundingDecision.model_validate(response.data)
        initial_point = _point_to_pixels(decision.point, case.image_size)
        initial_correct = _point_hits(initial_point, case.target_bbox)
        point = initial_point
        verification_verdict: str | None = None
        verification_latency_ms = 0
        verifier_response: ModelResponse | None = None
        correction_applied = False
        verifier_suggested_point: tuple[int, int] | None = None
        if verifier_provider is not None and initial_point is not None:
            overlay = _candidate_overlay(case, initial_point, output_dir)
            verifier_started = time.monotonic()
            try:
                verifier_response = await verifier_provider.complete(
                    ModelRequest(
                        role="verifier",
                        prompt=_verification_prompt(case, initial_point),
                        output_schema=GroundingVerificationDecision.model_json_schema(),
                        image_path=str(overlay),
                        run_id=f"screenspot-pro:{case.case_id}:verify",
                        metadata={
                            "suite": "screenspot-pro",
                            "case_id": case.case_id,
                            "stage": "candidate-verification",
                        },
                    )
                )
                verification_latency_ms = (
                    verifier_response.latency_ms
                    if verifier_response.latency_ms is not None
                    else round((time.monotonic() - verifier_started) * 1_000)
                )
                verification = GroundingVerificationDecision.model_validate(
                    verifier_response.data
                )
                verification_verdict = verification.verdict
                if verification.verdict == "miss":
                    verifier_suggested_point = _point_to_pixels(
                        verification.corrected_point,
                        case.image_size,
                    )
                    if verifier_mode == "correct":
                        point = verifier_suggested_point
                        correction_applied = True
                    else:
                        point = None
                elif verification.verdict == "uncertain":
                    point = None
            except Exception as exc:
                return PublicBenchmarkCaseResult(
                    case_id=case.case_id,
                    application=case.application,
                    platform=case.platform,
                    ui_type=case.ui_type,
                    target_bbox=case.target_bbox,
                    initial_point=initial_point,
                    initial_correct=initial_correct,
                    predicted_point=None,
                    click_error_pixels=None,
                    correct=False,
                    verifier_suggested_point=verifier_suggested_point,
                    verification_verdict="error",
                    provider=response.provider,
                    model=response.model,
                    verifier_provider=(
                        verifier_response.provider
                        if verifier_response
                        else getattr(verifier_provider, "name", None)
                    ),
                    verifier_model=(
                        verifier_response.model if verifier_response else None
                    ),
                    latency_ms=(
                        response.latency_ms
                        if response.latency_ms is not None
                        else round((time.monotonic() - started) * 1_000)
                    ),
                    verification_latency_ms=round(
                        (time.monotonic() - verifier_started) * 1_000
                    ),
                    usage=response.usage,
                    verifier_usage=(
                        verifier_response.usage if verifier_response else {}
                    ),
                    error_class=type(exc).__name__,
                    failure_kind=_safe_model_error(exc),
                )
        x1, y1, x2, y2 = case.target_bbox
        target_x = (x1 + x2) / 2
        target_y = (y1 + y2) / 2
        click_error = (
            math.dist(point, (target_x, target_y)) if point is not None else None
        )
        correct = _point_hits(point, case.target_bbox)
        return PublicBenchmarkCaseResult(
            case_id=case.case_id,
            application=case.application,
            platform=case.platform,
            ui_type=case.ui_type,
            target_bbox=case.target_bbox,
            initial_point=initial_point,
            initial_correct=initial_correct,
            predicted_point=point,
            click_error_pixels=click_error,
            correct=correct,
            correction_applied=correction_applied,
            verifier_suggested_point=verifier_suggested_point,
            verification_verdict=verification_verdict,
            provider=response.provider,
            model=response.model,
            verifier_provider=(
                verifier_response.provider if verifier_response else None
            ),
            verifier_model=verifier_response.model if verifier_response else None,
            latency_ms=response.latency_ms
            if response.latency_ms is not None
            else round((time.monotonic() - started) * 1_000),
            verification_latency_ms=verification_latency_ms,
            usage=response.usage,
            verifier_usage=(
                verifier_response.usage if verifier_response else {}
            ),
            failure_kind=(
                None
                if correct
                else "target_absent"
                if decision.result == "negative"
                else "target_miss"
            ),
        )
    except Exception as exc:
        return PublicBenchmarkCaseResult(
            case_id=case.case_id,
            application=case.application,
            platform=case.platform,
            ui_type=case.ui_type,
            target_bbox=case.target_bbox,
            initial_point=None,
            initial_correct=False,
            predicted_point=None,
            click_error_pixels=None,
            correct=False,
            provider=response.provider if response else getattr(provider, "name", None),
            model=response.model if response else None,
            latency_ms=round((time.monotonic() - started) * 1_000),
            usage=response.usage if response else {},
            error_class=type(exc).__name__,
            failure_kind=_safe_model_error(exc),
        )


async def run_screenspot_pro(
    provider: PublicBenchmarkProvider,
    *,
    verifier_provider: PublicBenchmarkProvider | None = None,
    verifier_mode: Literal["veto", "correct"] = "veto",
    dataset_dir: Path,
    output_dir: Path,
    suite_revision: str,
    dataset_revision: str,
    limit: int | None = None,
    seed: int = 104_729,
    jobs: int = 1,
) -> PublicBenchmarkReport:
    """Run a deterministic ScreenSpot-Pro subset and persist the full score."""

    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if jobs < 1:
        raise ValueError("jobs must be positive")
    if verifier_mode not in {"veto", "correct"}:
        raise ValueError("verifier_mode must be veto or correct")
    discovered = _load_screenspot_pro(dataset_dir)
    selected = list(discovered)
    random.Random(seed).shuffle(selected)
    if limit is not None:
        selected = selected[:limit]
    for case in selected:
        _validate_screenspot_image(case)

    semaphore = asyncio.Semaphore(jobs)

    async def run_one(case: ScreenSpotProCase) -> PublicBenchmarkCaseResult:
        async with semaphore:
            return await _run_grounding_case(
                provider,
                case,
                verifier_provider=verifier_provider,
                verifier_mode=verifier_mode,
                output_dir=output_dir,
            )

    started = time.monotonic()
    results = await asyncio.gather(*(run_one(case) for case in selected))
    wall_ms = round((time.monotonic() - started) * 1_000)
    latencies = [result.latency_ms for result in results]
    correct = sum(result.correct for result in results)
    initial_correct = sum(result.initial_correct for result in results)
    actionable_cases = sum(
        result.predicted_point is not None for result in results
    )
    model_calls = len(results) + sum(
        result.verification_verdict is not None for result in results
    )
    report = PublicBenchmarkReport(
        suite="screenspot-pro",
        suite_revision=suite_revision,
        dataset_revision=dataset_revision,
        seed=seed,
        cases_discovered=len(discovered),
        cases_evaluated=len(results),
        initial_correct=initial_correct,
        initial_accuracy=initial_correct / len(results),
        verifier_mode=(
            verifier_mode if verifier_provider is not None else "none"
        ),
        actionable_cases=actionable_cases,
        abstained_cases=len(results) - actionable_cases,
        actionable_accuracy=(
            correct / actionable_cases if actionable_cases else None
        ),
        correct=correct,
        accuracy=correct / len(results),
        model_calls=model_calls,
        model_active_ms=sum(
            result.latency_ms + result.verification_latency_ms
            for result in results
        ),
        usage_totals=_usage_totals(results),
        model_errors=sum(result.error_class is not None for result in results),
        evaluation_wall_ms=wall_ms,
        throughput_cases_per_second=len(results) / max(0.001, wall_ms / 1_000),
        median_latency_ms=statistics.median(latencies),
        p95_latency_ms=_percentile(latencies, 0.95),
        by_platform=_breakdown(results, "platform"),
        by_ui_type=_breakdown(results, "ui_type"),
        by_application=_breakdown(results, "application"),
        results=results,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        report.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return report
