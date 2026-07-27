"""Computer-free, failure-inclusive model-provider conformance benchmark."""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import statistics
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from unicodedata import normalize

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pikvm_agent.harness.agent_models import ModelRequest, ModelResponse
from pikvm_agent.harness.provider_support import (
    CredentialOwner,
    ImplementationContract,
    SupportTier,
)


class ConformanceProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class ProviderConformanceDecision(BaseModel):
    """The complete answer for one blind synthetic screen."""

    model_config = ConfigDict(extra="forbid")

    screen_title: str = Field(min_length=1, max_length=80)
    verification_code: str = Field(min_length=1, max_length=80)
    primary_button_label: str = Field(min_length=1, max_length=80)


class ProviderConformanceCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    run_id: str
    image_path: Path
    image_size: tuple[int, int]
    prompt: str
    metadata: dict[str, object]
    expected: ProviderConformanceDecision


class ProviderConformanceCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_index: int
    provider: str
    model: str | None = None
    schema_valid: bool
    exact: bool
    normalized_exact: bool
    fields_exact: int = Field(default=0, ge=0, le=3)
    fields_normalized_exact: int = Field(default=0, ge=0, le=3)
    latency_ms: int = Field(ge=0)
    expected: ProviderConformanceDecision | None = None
    observed: ProviderConformanceDecision | None = None
    usage: dict[str, int | float] = Field(default_factory=dict)
    error_class: str | None = None


class ProviderConformanceProviderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str
    auth_mode: str
    support_tier: SupportTier | None = None
    implementation_contract: ImplementationContract | None = None
    credential_owner: CredentialOwner | None = None
    configured_model: str | None = None
    interface: str | None = None
    pixel_input: str | None = None
    structured_output: str | None = None
    ready: bool
    readiness_error: str | None = None
    exercised: bool
    cases_requested: int
    calls_attempted: int
    schema_valid: int
    exact: int
    normalized_exact: int
    exact_accuracy: float
    normalized_exact_accuracy: float
    median_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    returned_models: list[str] = Field(default_factory=list)
    failure_counts: dict[str, int] = Field(default_factory=dict)
    usage_totals: dict[str, int | float] = Field(default_factory=dict)
    results: list[ProviderConformanceCaseResult] = Field(default_factory=list)


class ProviderConformanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    suite: str = "provider-conformance"
    created_at: str
    seed: int
    cases_per_provider: int
    concurrency: int
    computer_target_contacted: bool = False
    providers_selected: int
    providers_exercised: int
    providers_unavailable: int
    calls_attempted: int
    calls_schema_valid: int
    calls_exact: int
    calls_normalized_exact: int
    calls_failed: int
    exact_accuracy: float
    normalized_exact_accuracy: float
    evaluation_wall_ms: int
    providers: list[ProviderConformanceProviderResult]


_TITLES = (
    "Signal Review",
    "Release Console",
    "Incident Ledger",
    "Quality Workspace",
    "Desktop Evidence",
    "Provider Observatory",
)
_BUTTONS = (
    "Open report",
    "Review evidence",
    "Inspect results",
    "View timeline",
    "Compare runs",
    "Show details",
)
_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_USAGE_KEYS = {
    "cachedinputtokens": "cached_input_tokens",
    "cachecreationinputtokens": "cache_creation_input_tokens",
    "cachereadinputtokens": "cache_read_input_tokens",
    "candidatestokencount": "output_tokens",
    "candidates": "output_tokens",
    "completiontokens": "output_tokens",
    "inputtokens": "input_tokens",
    "outputtokens": "output_tokens",
    "prompt": "input_tokens",
    "prompttokencount": "input_tokens",
    "reasoningoutputtokens": "reasoning_tokens",
    "thoughtstokencount": "reasoning_tokens",
    "totaltokencount": "total_tokens",
    "totaltokens": "total_tokens",
}


def conformance_expectations(
    *,
    seed: int,
    cases: int,
) -> list[dict[str, str]]:
    """Return deterministic synthetic answers without rendering or model I/O."""

    if not 1 <= cases <= 100:
        raise ValueError("cases must be between 1 and 100")
    rng = random.Random(seed)
    expectations: list[dict[str, str]] = []
    used_codes: set[str] = set()
    for _ in range(cases):
        while True:
            characters = "".join(rng.choices(_CODE_ALPHABET, k=10))
            code = f"{characters[:5]}-{characters[5:]}"
            if code not in used_codes:
                used_codes.add(code)
                break
        expectations.append(
            {
                "screen_title": rng.choice(_TITLES),
                "verification_code": code,
                "primary_button_label": rng.choice(_BUTTONS),
            }
        )
    return expectations


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


def render_conformance_case(
    *,
    expected: Mapping[str, str],
    index: int,
    output_dir: Path,
) -> ProviderConformanceCase:
    """Render one screen whose answer is absent from its model request."""

    answer = ProviderConformanceDecision.model_validate(dict(expected))
    output_dir.mkdir(parents=True, exist_ok=True)
    image_path = output_dir / f"provider-conformance-{index:03d}.png"
    image = Image.new("RGB", (960, 540), (12, 17, 25))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (72, 60, 888, 480),
        radius=20,
        fill=(24, 31, 42),
        outline=(53, 66, 83),
        width=2,
    )
    draw.text(
        (116, 101),
        answer.screen_title,
        font=_font(36, bold=True),
        fill=(235, 241, 248),
    )
    draw.text(
        (116, 181),
        "Verification code",
        font=_font(20),
        fill=(145, 159, 178),
    )
    draw.rounded_rectangle(
        (112, 222, 650, 315),
        radius=12,
        fill=(14, 20, 29),
        outline=(70, 86, 108),
        width=2,
    )
    draw.text(
        (142, 246),
        answer.verification_code,
        font=_font(34, bold=True),
        fill=(248, 250, 252),
    )
    draw.rounded_rectangle(
        (112, 366, 430, 432),
        radius=11,
        fill=(44, 111, 229),
    )
    button_font = _font(22, bold=True)
    box = draw.textbbox((0, 0), answer.primary_button_label, font=button_font)
    text_width = box[2] - box[0]
    draw.text(
        (112 + (318 - text_width) / 2, 385),
        answer.primary_button_label,
        font=button_font,
        fill=(255, 255, 255),
    )
    image.save(image_path, format="PNG", optimize=True)
    return ProviderConformanceCase(
        index=index,
        run_id=f"provider-conformance:{index}",
        image_path=image_path,
        image_size=image.size,
        prompt=(
            "Inspect the attached synthetic desktop screen without using any "
            "tools. Transcribe exactly: (1) the large screen title, (2) the "
            "value inside the Verification code field, and (3) the label of "
            "the blue primary button. Preserve capitalization, spacing, and "
            "punctuation. Do not infer text from the filename."
        ),
        metadata={"suite": "provider-conformance", "case_index": index},
        expected=answer,
    )


def _normalized(value: str) -> str:
    return " ".join(normalize("NFKC", value).split()).casefold()


def _safe_failure_class(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "structured-output-invalid"
    text = str(exc).casefold()
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "rate limit" in text or "rate-limited" in text:
        return "rate-limited"
    if "authentication" in text or "unauthorized" in text:
        return "authentication-failed"
    if "quota" in text or "billing" in text:
        return "quota-or-billing"
    if "unavailable" in text or "overloaded" in text:
        return "provider-unavailable"
    if "schema" in text or "structured" in text:
        return "structured-output-error"
    return "provider-error"


def _normalized_usage(usage: Mapping[str, Any]) -> dict[str, int | float]:
    totals: dict[str, int | float] = {}

    def visit(value: Any, name: str = "") -> None:
        if isinstance(value, Mapping):
            for child_name, child in value.items():
                visit(child, str(child_name))
            return
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
        ):
            return
        key = re.sub(r"[^a-z0-9]", "", name.casefold())
        canonical = _USAGE_KEYS.get(key)
        if canonical is not None:
            totals[canonical] = totals.get(canonical, 0) + value

    visit(usage)
    return totals


def _usage_totals(
    results: Sequence[ProviderConformanceCaseResult],
) -> dict[str, int | float]:
    totals: dict[str, int | float] = {}
    for result in results:
        for name, value in result.usage.items():
            totals[name] = totals.get(name, 0) + value
    return totals


def _p95(values: Sequence[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(0.95 * len(ordered) + 0.999) - 1))
    return float(ordered[index])


async def run_provider_conformance(
    *,
    providers: Mapping[str, ConformanceProvider],
    provider_metadata: Mapping[str, Mapping[str, object]],
    provider_names: Sequence[str],
    cases: int,
    seed: int,
    concurrency: int,
    workspace: Path,
    now: Callable[[], datetime] | None = None,
) -> ProviderConformanceReport:
    """Exercise selected providers on identical blind screens.

    This function receives no daemon, PiKVM, VNC, or computer client. A failed
    provider call remains in the denominator, and only coarse error classes are
    retained.
    """

    if not 1 <= cases <= 100:
        raise ValueError("cases must be between 1 and 100")
    if not 1 <= concurrency <= 16:
        raise ValueError("concurrency must be between 1 and 16")
    selected = list(dict.fromkeys(provider_names))
    known = set(providers) | set(provider_metadata)
    unknown = sorted(set(selected) - known)
    if unknown:
        raise ValueError("unknown providers: " + ", ".join(unknown))
    expectations = conformance_expectations(seed=seed, cases=cases)
    rendered = [
        render_conformance_case(
            expected=expected,
            index=index,
            output_dir=workspace,
        )
        for index, expected in enumerate(expectations)
    ]
    semaphore = asyncio.Semaphore(concurrency)
    provider_semaphores = {name: asyncio.Semaphore(1) for name in selected}

    async def run_one(
        provider_name: str,
        provider: ConformanceProvider,
        case: ProviderConformanceCase,
    ) -> ProviderConformanceCaseResult:
        async with provider_semaphores[provider_name]:
            async with semaphore:
                started = time.monotonic()
                try:
                    response = await provider.complete(
                        ModelRequest(
                            role="verifier",
                            prompt=case.prompt,
                            output_schema=(
                                ProviderConformanceDecision.model_json_schema()
                            ),
                            image_path=str(case.image_path),
                            run_id=case.run_id,
                            metadata=case.metadata,
                        )
                    )
                    latency_ms = (
                        response.latency_ms
                        if response.latency_ms is not None
                        else round((time.monotonic() - started) * 1_000)
                    )
                    observed = ProviderConformanceDecision.model_validate(
                        response.data
                    )
                    expected_values = case.expected.model_dump()
                    observed_values = observed.model_dump()
                    fields_exact = sum(
                        observed_values[name] == value
                        for name, value in expected_values.items()
                    )
                    fields_normalized = sum(
                        _normalized(observed_values[name])
                        == _normalized(value)
                        for name, value in expected_values.items()
                    )
                    return ProviderConformanceCaseResult(
                        case_index=case.index,
                        provider=provider_name,
                        model=response.model,
                        schema_valid=True,
                        exact=fields_exact == 3,
                        normalized_exact=fields_normalized == 3,
                        fields_exact=fields_exact,
                        fields_normalized_exact=fields_normalized,
                        latency_ms=latency_ms,
                        expected=case.expected,
                        observed=observed,
                        usage=_normalized_usage(response.usage),
                    )
                except Exception as exc:
                    return ProviderConformanceCaseResult(
                        case_index=case.index,
                        provider=provider_name,
                        schema_valid=False,
                        exact=False,
                        normalized_exact=False,
                        latency_ms=round(
                            (time.monotonic() - started) * 1_000
                        ),
                        error_class=_safe_failure_class(exc),
                    )

    started = time.monotonic()
    tasks: list[asyncio.Task[ProviderConformanceCaseResult]] = []
    ready_names: list[str] = []
    for name in selected:
        metadata = provider_metadata.get(name, {})
        provider = providers.get(name)
        if not bool(metadata.get("ready", True)) or provider is None:
            continue
        ready_names.append(name)
        for case in rendered:
            tasks.append(asyncio.create_task(run_one(name, provider, case)))
    all_results = await asyncio.gather(*tasks)
    wall_ms = round((time.monotonic() - started) * 1_000)

    provider_results: list[ProviderConformanceProviderResult] = []
    for name in selected:
        metadata = provider_metadata.get(name, {})
        provider = providers.get(name)
        ready = bool(metadata.get("ready", True)) and provider is not None
        results = [
            result for result in all_results if result.provider == name
        ]
        latencies = [result.latency_ms for result in results]
        exact = sum(result.exact for result in results)
        normalized_exact = sum(
            result.normalized_exact for result in results
        )
        attempted = len(results)
        failures = Counter(
            result.error_class
            for result in results
            if result.error_class is not None
        )
        configured_model = (
            str(metadata["model"]) if metadata.get("model") else None
        )
        provider_results.append(
            ProviderConformanceProviderResult(
                name=name,
                kind=str(metadata.get("kind") or "unknown"),
                auth_mode=str(metadata.get("auth_mode") or "unknown"),
                support_tier=metadata.get("support_tier"),
                implementation_contract=metadata.get(
                    "implementation_contract"
                ),
                credential_owner=metadata.get("credential_owner"),
                configured_model=configured_model,
                interface=(
                    str(metadata["interface"])
                    if metadata.get("interface")
                    else None
                ),
                pixel_input=(
                    str(metadata["pixel_input"])
                    if metadata.get("pixel_input")
                    else None
                ),
                structured_output=(
                    str(metadata["structured_output"])
                    if metadata.get("structured_output")
                    else None
                ),
                ready=ready,
                readiness_error=(
                    str(metadata.get("error") or "provider-not-configured")
                    if not ready
                    else None
                ),
                exercised=bool(results),
                cases_requested=cases,
                calls_attempted=attempted,
                schema_valid=sum(result.schema_valid for result in results),
                exact=exact,
                normalized_exact=normalized_exact,
                exact_accuracy=exact / attempted if attempted else 0.0,
                normalized_exact_accuracy=(
                    normalized_exact / attempted if attempted else 0.0
                ),
                median_latency_ms=(
                    float(statistics.median(latencies))
                    if latencies
                    else None
                ),
                p95_latency_ms=_p95(latencies),
                returned_models=sorted(
                    {
                        result.model
                        for result in results
                        if result.model is not None
                    }
                ),
                failure_counts=dict(sorted(failures.items())),
                usage_totals=_usage_totals(results),
                results=results,
            )
        )

    calls_attempted = len(all_results)
    calls_exact = sum(result.exact for result in all_results)
    calls_normalized = sum(
        result.normalized_exact for result in all_results
    )
    timestamp = (now or (lambda: datetime.now(UTC)))()
    return ProviderConformanceReport(
        created_at=timestamp.isoformat(),
        seed=seed,
        cases_per_provider=cases,
        concurrency=concurrency,
        providers_selected=len(selected),
        providers_exercised=len(ready_names),
        providers_unavailable=len(selected) - len(ready_names),
        calls_attempted=calls_attempted,
        calls_schema_valid=sum(
            result.schema_valid for result in all_results
        ),
        calls_exact=calls_exact,
        calls_normalized_exact=calls_normalized,
        calls_failed=sum(
            not result.schema_valid for result in all_results
        ),
        exact_accuracy=(
            calls_exact / calls_attempted if calls_attempted else 0.0
        ),
        normalized_exact_accuracy=(
            calls_normalized / calls_attempted if calls_attempted else 0.0
        ),
        evaluation_wall_ms=wall_ms,
        providers=provider_results,
    )


def write_provider_conformance_report(
    destination: Path,
    report: ProviderConformanceReport | Mapping[str, object],
) -> None:
    """Write one reviewable report without following an overwrite path."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        report.model_dump(mode="json")
        if isinstance(report, ProviderConformanceReport)
        else dict(report)
    )
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def read_provider_conformance_health(
    source: Path,
    *,
    provider_names: Sequence[str],
) -> dict[str, dict[str, object]]:
    """Return only UI-safe aggregates from the latest local report."""

    names = list(dict.fromkeys(provider_names))
    if not source.is_file():
        return {
            name: {"conformance_status": "not-run"}
            for name in names
        }
    try:
        if source.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("provider conformance report is too large")
        report = ProviderConformanceReport.model_validate_json(
            source.read_bytes()
        )
    except (OSError, ValidationError, ValueError):
        return {
            name: {"conformance_status": "invalid-report"}
            for name in names
        }

    by_name = {provider.name: provider for provider in report.providers}
    health: dict[str, dict[str, object]] = {}
    for name in names:
        provider = by_name.get(name)
        if provider is None:
            health[name] = {"conformance_status": "not-in-report"}
            continue
        if not provider.ready:
            status = "unavailable"
        elif not provider.exercised or provider.calls_attempted == 0:
            status = "not-exercised"
        elif (
            provider.calls_attempted == provider.cases_requested
            and provider.exact == provider.calls_attempted
            and provider.schema_valid == provider.calls_attempted
        ):
            status = "passed"
        elif provider.schema_valid == 0:
            status = "failed"
        else:
            status = "degraded"
        health[name] = {
            "conformance_status": status,
            "conformance_created_at": report.created_at,
            "conformance_cases_requested": provider.cases_requested,
            "conformance_calls_attempted": provider.calls_attempted,
            "conformance_schema_valid": provider.schema_valid,
            "conformance_exact": provider.exact,
            "conformance_normalized_exact": provider.normalized_exact,
            "conformance_exact_accuracy": provider.exact_accuracy,
            "conformance_normalized_exact_accuracy": (
                provider.normalized_exact_accuracy
            ),
            "conformance_median_latency_ms": provider.median_latency_ms,
            "conformance_p95_latency_ms": provider.p95_latency_ms,
            "conformance_failure_counts": provider.failure_counts,
        }
    return health
