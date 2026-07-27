"""Deterministic, randomized, blind OCR benchmark for computer-use screens.

The corpus generator owns the private ground truth.  OCR providers receive only
an opaque image path; expected text is joined back to observations after every
OCR call completes.  The default release-gate corpus contains exactly 1,000
cases spread evenly across desktop prose, UI, code, terminal, path, URL,
identifier, numeric, punctuation/confusable, and mixed-case content.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import re
import statistics
import textwrap
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pydantic import BaseModel, ConfigDict, Field

from pikvm_agent.core.models import OCRResult, Region
from pikvm_agent.vision.tesseract_ocr import TesseractOcrProvider

DEFAULT_CASE_COUNT = 1_000
DEFAULT_CORPUS_SEED = 104_729
DEFAULT_EVALUATION_SEED = 65_537
OCR_RELEASE_MIN_NORMALIZED_EXACT = 0.99
OCR_RELEASE_MAX_CHARACTER_ERROR_RATE = 0.001
OCR_RELEASE_MIN_CATEGORY_EXACT = 0.95
OCR_STRESS_MAX_CHARACTER_ERROR_RATE = 0.10

OCR_CATEGORIES = (
    "prose",
    "ui_label",
    "code",
    "terminal",
    "path",
    "url",
    "identifier",
    "numeric",
    "punctuation",
    "mixed_case",
)
OCR_STRESS_CATEGORIES = frozenset({"numeric", "punctuation"})

_FONT_ROOTS = (Path("/usr/share/fonts"), Path("/usr/local/share/fonts"))
_FONT_HINTS = (
    "dejavu",
    "liberation",
    "sourcecodepro",
    "adwaita",
    "cantarell",
    "ubuntu",
)
_MONO_HINTS = ("mono", "code", "console", "courier")

_WORDS = (
    "operator",
    "window",
    "keyboard",
    "verification",
    "checkpoint",
    "screen",
    "document",
    "remote",
    "session",
    "evidence",
    "visible",
    "response",
    "accurate",
    "bounded",
    "review",
    "status",
    "desktop",
    "message",
    "editor",
    "confirm",
)
_VERBS = (
    "checks",
    "records",
    "renders",
    "compares",
    "preserves",
    "reports",
    "retries",
    "observes",
)
_UI_ACTIONS = (
    "Open settings",
    "Review changes",
    "Retry connection",
    "Cancel upload",
    "Apply filter",
    "Show details",
    "Copy identifier",
    "Check for updates",
)
_DOMAINS = (
    "example.test",
    "docs.internal",
    "status.service.test",
    "api.sandbox.test",
)


class OcrRenderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int
    font_size: int
    font_path: str
    theme: str
    foreground: tuple[int, int, int]
    background: tuple[int, int, int]
    padding: int
    line_spacing: int
    scale: float
    blur: float
    noise: float
    rotation: float
    image_format: str
    jpeg_quality: int
    chrome: bool
    tight_crop: bool
    render_seed: int


class OcrCaseSpec(BaseModel):
    """Private corpus record. ``expected`` never crosses the provider boundary."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    category: str
    expected: str
    render: OcrRenderSpec

    def fingerprint(self) -> str:
        payload = self.model_dump_json(exclude={"case_id"})
        return hashlib.sha256(payload.encode()).hexdigest()


class BlindOcrInput(BaseModel):
    """The complete value supplied to the evaluator before it calls OCR."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    image_path: Path


class BlindOcrObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    observed: str
    alternative_observed: list[str] = Field(default_factory=list)
    latency_ms: int
    error_class: str | None = None
    mean_confidence: float | None = None
    line_count: int = 0


class OcrCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    category: str
    expected: str
    observed: str
    alternative_observed: list[str] = Field(default_factory=list)
    exact: bool
    normalized_exact: bool
    expected_aware_normalized_exact: bool = False
    casefold_exact: bool
    edit_distance: int
    character_error_rate: float
    latency_ms: int
    error_class: str | None = None
    failure_kind: str | None = None
    mean_confidence: float | None = None
    line_count: int = 0
    render: OcrRenderSpec


class OcrCategoryMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: int
    exact_rate: float
    normalized_exact_rate: float
    mean_character_error_rate: float
    median_latency_ms: float
    p95_latency_ms: float


class OcrConfidenceCoverage(BaseModel):
    """Observed accuracy after accepting only OCR at or above a threshold."""

    model_config = ConfigDict(extra="forbid")

    threshold: float
    covered_cases: int
    coverage: float
    normalized_exact_rate: float
    mean_character_error_rate: float
    wrong_cases: int


class OcrBlindReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 4
    provider: str
    corpus_seed: int
    evaluation_seed: int
    cases: int
    exact_matches: int
    normalized_exact_matches: int
    expected_aware_normalized_exact_matches: int = 0
    exact_rate: float
    normalized_exact_rate: float
    expected_aware_normalized_exact_rate: float = 0.0
    mean_character_error_rate: float
    median_latency_ms: float
    p95_latency_ms: float
    evaluation_wall_ms: int
    throughput_images_per_second: float
    failure_kinds: dict[str, int]
    by_category: dict[str, OcrCategoryMetrics]
    by_tier: dict[str, OcrCategoryMetrics] = Field(default_factory=dict)
    confidence_coverage: list[OcrConfidenceCoverage] = Field(default_factory=list)
    provider_diagnostics: dict[str, Any] = Field(default_factory=dict)
    results: list[OcrCaseResult]


def ocr_category_tier(category: str) -> str:
    """Separate ordinary desktop text from deliberately ambiguous stress text."""

    return "stress" if category in OCR_STRESS_CATEGORIES else "routine"


def ocr_release_gate_failures(report: OcrBlindReport) -> list[str]:
    """Return every production OCR release-gate failure without hiding debt."""

    failures: list[str] = []
    if report.cases != DEFAULT_CASE_COUNT:
        failures.append(
            f"requires {DEFAULT_CASE_COUNT} cases, observed {report.cases}"
        )
    if report.normalized_exact_rate < OCR_RELEASE_MIN_NORMALIZED_EXACT:
        failures.append(
            "normalized exact rate "
            f"{report.normalized_exact_rate:.3%} is below "
            f"{OCR_RELEASE_MIN_NORMALIZED_EXACT:.1%}"
        )
    if (
        report.mean_character_error_rate
        > OCR_RELEASE_MAX_CHARACTER_ERROR_RATE
    ):
        failures.append(
            "mean character error rate "
            f"{report.mean_character_error_rate:.3%} exceeds "
            f"{OCR_RELEASE_MAX_CHARACTER_ERROR_RATE:.1%}"
        )
    provider_errors = report.failure_kinds.get("provider_error", 0)
    if provider_errors:
        failures.append(f"{provider_errors} provider calls failed")
    stress = report.by_tier.get("stress")
    if stress is None:
        failures.append("stress OCR tier is missing")
    elif (
        stress.mean_character_error_rate
        > OCR_STRESS_MAX_CHARACTER_ERROR_RATE
    ):
        failures.append(
            "stress tier mean character error rate "
            f"{stress.mean_character_error_rate:.3%} exceeds "
            f"{OCR_STRESS_MAX_CHARACTER_ERROR_RATE:.1%}"
        )
    for category in OCR_CATEGORIES:
        metrics = report.by_category.get(category)
        if metrics is None:
            failures.append(f"{category} category is missing")
            continue
        if metrics.cases != DEFAULT_CASE_COUNT // len(OCR_CATEGORIES):
            failures.append(
                f"{category} requires 100 cases, observed {metrics.cases}"
            )
        if (
            metrics.normalized_exact_rate
            < OCR_RELEASE_MIN_CATEGORY_EXACT
        ):
            failures.append(
                f"{category} normalized exact rate "
                f"{metrics.normalized_exact_rate:.3%} is below "
                f"{OCR_RELEASE_MIN_CATEGORY_EXACT:.1%}"
            )
    return failures


class BlindOcrProvider(Protocol):
    async def ocr(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult: ...


class PreciseBlindOcrProvider(BlindOcrProvider, Protocol):
    async def ocr_precise(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult: ...


def discover_fonts() -> tuple[list[Path], list[Path]]:
    """Return stable regular and monospace font lists available on this host."""

    fonts: list[Path] = []
    for root in _FONT_ROOTS:
        if not root.is_dir():
            continue
        fonts.extend(
            path
            for path in root.rglob("*")
            if path.suffix.casefold() in {".ttf", ".otf"}
            and any(hint in path.name.casefold() for hint in _FONT_HINTS)
            and "italic" not in path.name.casefold()
        )
    regular = sorted(set(fonts), key=lambda path: str(path).casefold())
    mono = [
        path
        for path in regular
        if any(hint in path.name.casefold() for hint in _MONO_HINTS)
    ]
    return regular, mono or regular


def _style(
    rng: random.Random,
    *,
    category: str,
    regular_fonts: Sequence[Path],
    mono_fonts: Sequence[Path],
) -> OcrRenderSpec:
    code_like = category in {
        "code",
        "terminal",
        "path",
        "url",
        "identifier",
        "numeric",
        "punctuation",
    }
    candidates = mono_fonts if code_like else regular_fonts
    font_path = str(rng.choice(candidates)) if candidates else ""
    theme = rng.choices(
        ("light", "dark", "low_contrast_light", "low_contrast_dark"),
        weights=(36, 36, 14, 14),
        k=1,
    )[0]
    palettes = {
        "light": ((20, 23, 28), (248, 249, 251)),
        "dark": ((232, 235, 239), (31, 34, 39)),
        "low_contrast_light": ((82, 86, 94), (224, 226, 230)),
        "low_contrast_dark": ((166, 171, 180), (47, 51, 58)),
    }
    foreground, background = palettes[theme]
    image_format = rng.choices(("PNG", "JPEG"), weights=(62, 38), k=1)[0]
    font_limits = (14, 25) if code_like else (15, 34)
    return OcrRenderSpec(
        width=rng.randint(360, 1_080),
        font_size=rng.randint(*font_limits),
        font_path=font_path,
        theme=theme,
        foreground=foreground,
        background=background,
        padding=rng.randint(7, 30),
        line_spacing=rng.randint(3, 14),
        scale=rng.choice((0.72, 0.84, 1.0, 1.0, 1.0, 1.18, 1.35)),
        blur=rng.choice((0.0, 0.0, 0.0, 0.18, 0.32, 0.48)),
        noise=rng.choice((0.0, 0.0, 0.7, 1.2, 1.8)),
        rotation=rng.choice((0.0, 0.0, 0.0, -0.22, 0.18)),
        image_format=image_format,
        jpeg_quality=rng.randint(58, 94),
        chrome=category == "ui_label" or rng.random() < 0.12,
        tight_crop=rng.random() < 0.14,
        render_seed=rng.randrange(1, 2**31),
    )


def _case_token(index: int, rng: random.Random) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return f"{index:04d}-" + "".join(rng.choice(alphabet) for _ in range(5))


def _wrap_prose(text: str, style: OcrRenderSpec) -> str:
    columns = max(24, round((style.width - 2 * style.padding) / (style.font_size * 0.56)))
    return "\n".join(textwrap.wrap(text, width=columns, break_long_words=False))


def _expected_text(
    category: str,
    index: int,
    rng: random.Random,
    style: OcrRenderSpec,
) -> str:
    token = _case_token(index, rng)
    if category == "prose":
        subject = " ".join(rng.sample(_WORDS, 3))
        tail = " ".join(rng.sample(_WORDS, 4))
        sentence = (
            f"The {subject} {rng.choice(_VERBS)} {tail} before review "
            f"reference {token}."
        )
        return _wrap_prose(sentence, style)
    if category == "ui_label":
        return f"{rng.choice(_UI_ACTIONS)}  [{token}]"
    if category == "code":
        variable = f"retryCount{index:04d}"
        return rng.choice(
            (
                f"const {variable} = Math.min(attempt + 1, 5); // {token}",
                f"if ({variable} >= limit) return {{ status: \"paused\", id: \"{token}\" }};",
                f"result[{index}] = verify(input, expected) && code !== 0; // {token}",
            )
        )
    if category == "terminal":
        return rng.choice(
            (
                f"PS C:\\Users\\tester> Get-Content .\\report-{token}.txt",
                f"$ rg -n \"approval|permission\" logs/run-{token}.jsonl",
                f"user@host:~/work$ git status --short  # {token}",
            )
        )
    if category == "path":
        return rng.choice(
            (
                f"C:\\Users\\QA\\Documents\\OCR Tests\\case-{token}.txt",
                f"/var/tmp/pikvm-agent/{token}/artifacts/frame_0007.png",
                f"\\\\fileserver\\validation\\2026\\case-{token}\\result.json",
            )
        )
    if category == "url":
        return (
            f"https://{rng.choice(_DOMAINS)}/runs/{token}"
            f"?view=screen&attempt={index % 17}#evidence"
        )
    if category == "identifier":
        return rng.choice(
            (
                f"IDEMPOTENCY_RETRY_{token.replace('-', '_')}",
                f"run_{token.lower().replace('-', '_')}_frame_{index:06d}",
                f"sha256:{hashlib.sha256(token.encode()).hexdigest()[:32]}",
            )
        )
    if category == "numeric":
        return (
            f"{index:04d}  0O1Il|  £{index * 17.43:,.2f}  "
            f"{(index * 7919) % 1_000_000:06d}  {token}"
        )
    if category == "punctuation":
        return (
            f"{token} :: []{{}}() <> | & && || != == => <= "
            f"'single' \"double\" `code` /\\ _-+*=#@!?"
        )
    if category == "mixed_case":
        return f"OAuthURLParser{index:04d} keeps ApiID_{token} beside hTtP2Frame"
    raise ValueError(f"unknown OCR category: {category}")


def generate_ocr_cases(
    count: int = DEFAULT_CASE_COUNT,
    seed: int = DEFAULT_CORPUS_SEED,
) -> list[OcrCaseSpec]:
    """Generate exactly ``count`` unique deterministic private case records."""

    if count < 1:
        raise ValueError("count must be positive")
    rng = random.Random(seed)
    regular_fonts, mono_fonts = discover_fonts()
    categories = [OCR_CATEGORIES[index % len(OCR_CATEGORIES)] for index in range(count)]
    rng.shuffle(categories)
    cases: list[OcrCaseSpec] = []
    for index, category in enumerate(categories):
        render = _style(
            rng,
            category=category,
            regular_fonts=regular_fonts,
            mono_fonts=mono_fonts,
        )
        expected = _expected_text(category, index, rng, render)
        probe = Image.new("RGB", (32, 32), render.background)
        probe_draw = ImageDraw.Draw(probe)
        font = _font(render)
        required_width = max(
            probe_draw.textbbox((0, 0), line, font=font)[2]
            for line in (expected.splitlines() or [expected])
        ) + (2 * render.padding)
        if required_width > render.width:
            render = render.model_copy(
                update={"width": required_width + 2}
            )
        cases.append(
            OcrCaseSpec(
                case_id=f"ocr-{index:04d}",
                category=category,
                expected=expected,
                render=render,
            )
        )
    fingerprints = {case.fingerprint() for case in cases}
    if len(fingerprints) != count:
        raise RuntimeError("OCR corpus generator produced duplicate examples")
    return cases


def _font(spec: OcrRenderSpec) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if spec.font_path:
        try:
            return ImageFont.truetype(spec.font_path, spec.font_size)
        except OSError:
            pass
    return ImageFont.load_default()


def render_ocr_case(case: OcrCaseSpec, image_path: Path) -> None:
    """Render one case deterministically without putting ground truth in its name."""

    spec = case.render
    font = _font(spec)
    lines = case.expected.splitlines() or [case.expected]
    probe = Image.new("RGB", (spec.width, 64), spec.background)
    probe_draw = ImageDraw.Draw(probe)
    boxes = [probe_draw.textbbox((0, 0), line, font=font) for line in lines]
    text_height = sum(max(1, box[3] - box[1]) for box in boxes)
    text_height += spec.line_spacing * max(0, len(lines) - 1)
    chrome_height = 25 if spec.chrome else 0
    vertical_pad = 2 if spec.tight_crop else spec.padding
    height = max(32, text_height + 2 * vertical_pad + chrome_height)
    image = Image.new("RGB", (spec.width, height), spec.background)
    draw = ImageDraw.Draw(image)
    if spec.chrome:
        bar = tuple(max(0, min(255, value + (-14 if value > 128 else 14))) for value in spec.background)
        draw.rectangle((0, 0, spec.width - 1, chrome_height), fill=bar)
        for offset, color in zip((12, 26, 40), ((226, 91, 82), (229, 181, 72), (86, 179, 95))):
            draw.ellipse((offset, 8, offset + 8, 16), fill=color)
        draw.rectangle((0, 0, spec.width - 1, height - 1), outline=bar)
    y = vertical_pad + chrome_height
    for line, box in zip(lines, boxes):
        draw.text((spec.padding, y - box[1]), line, fill=spec.foreground, font=font)
        y += max(1, box[3] - box[1]) + spec.line_spacing

    if spec.rotation:
        image = image.rotate(
            spec.rotation,
            resample=Image.Resampling.BICUBIC,
            expand=False,
            fillcolor=spec.background,
        )
    if spec.blur:
        image = image.filter(ImageFilter.GaussianBlur(spec.blur))
    if spec.noise:
        generator = np.random.default_rng(spec.render_seed)
        array = np.asarray(image).astype(np.float32)
        array += generator.normal(0.0, spec.noise, array.shape)
        image = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), mode="RGB")
    if spec.scale != 1.0:
        scaled = (
            max(1, round(image.width * spec.scale)),
            max(1, round(image.height * spec.scale)),
        )
        image = image.resize(scaled, Image.Resampling.LANCZOS)

    image_path.parent.mkdir(parents=True, exist_ok=True)
    if spec.image_format == "JPEG":
        image.save(
            image_path,
            format="JPEG",
            quality=spec.jpeg_quality,
            optimize=True,
            subsampling=2,
        )
    else:
        image.save(image_path, format="PNG", optimize=True)


def normalize_ocr_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.translate(
        str.maketrans(
            {
                "‘": "'",
                "’": "'",
                "“": '"',
                "”": '"',
                "\u00a0": " ",
            }
        )
    )
    return re.sub(r"\s+", " ", value).strip()


def edit_distance(left: str, right: str) -> int:
    """Memory-bounded Levenshtein distance."""

    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row, char_left in enumerate(left, start=1):
        current = [row]
        for column, char_right in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (char_left != char_right),
                )
            )
        previous = current
    return previous[-1]


def classify_ocr_failure(expected: str, observed: str) -> str | None:
    ne = normalize_ocr_text(expected)
    no = normalize_ocr_text(observed)
    if ne == no:
        return None
    if not no:
        return "empty"
    if ne.casefold() == no.casefold():
        return "case_only"
    if ne.startswith(no):
        return "missing_suffix"
    if no.startswith(ne):
        return "trailing_extra"
    alnum = lambda value: re.sub(r"[^a-z0-9]", "", value.casefold())
    if alnum(ne) == alnum(no):
        return "symbol_or_spacing"
    if len(ne) == len(no) and any(
        left != right and (not left.isalnum() or not right.isalnum())
        for left, right in zip(ne, no)
    ):
        return "symbol_or_spacing"
    return "substitution_or_layout"


def score_ocr_case(
    case: OcrCaseSpec,
    observation: BlindOcrObservation,
) -> OcrCaseResult:
    expected = case.expected
    observed = observation.observed
    normalized_expected = normalize_ocr_text(expected)
    normalized_observed = normalize_ocr_text(observed)
    expected_aware_normalized_exact = (
        normalized_expected == normalized_observed
        or any(
            normalized_expected == normalize_ocr_text(candidate)
            for candidate in observation.alternative_observed
        )
    )
    distance = edit_distance(normalized_expected, normalized_observed)
    return OcrCaseResult(
        case_id=case.case_id,
        category=case.category,
        expected=expected,
        observed=observed,
        alternative_observed=observation.alternative_observed,
        exact=expected == observed,
        normalized_exact=normalized_expected == normalized_observed,
        expected_aware_normalized_exact=expected_aware_normalized_exact,
        casefold_exact=normalized_expected.casefold() == normalized_observed.casefold(),
        edit_distance=distance,
        character_error_rate=distance / max(1, len(normalized_expected)),
        latency_ms=observation.latency_ms,
        error_class=observation.error_class,
        mean_confidence=observation.mean_confidence,
        line_count=observation.line_count,
        failure_kind=(
            "provider_error"
            if observation.error_class
            else classify_ocr_failure(expected, observed)
        ),
        render=case.render,
    )


async def observe_blind_input(
    provider: BlindOcrProvider,
    blind_input: BlindOcrInput,
    *,
    precise: bool = False,
) -> BlindOcrObservation:
    """Call OCR with no expected-text value in scope or in the argument."""

    started = time.monotonic()
    try:
        if precise:
            precise_provider = cast(PreciseBlindOcrProvider, provider)
            result = await precise_provider.ocr_precise(blind_input.image_path)
        else:
            result = await provider.ocr(blind_input.image_path)
        observed = "\n".join(line.text for line in result.lines)
        alternative_observed = [
            candidate.text for candidate in result.alternatives
        ]
        confidences = [
            float(line.confidence)
            for line in result.lines
            if line.confidence is not None
        ]
        mean_confidence = (
            statistics.fmean(confidences) if confidences else None
        )
        line_count = len(result.lines)
        error_class = None
    except Exception as exc:  # benchmark boundary: record class, continue corpus
        observed = ""
        alternative_observed = []
        mean_confidence = None
        line_count = 0
        error_class = type(exc).__name__
    return BlindOcrObservation(
        case_id=blind_input.case_id,
        observed=observed,
        alternative_observed=alternative_observed,
        latency_ms=round((time.monotonic() - started) * 1_000),
        error_class=error_class,
        mean_confidence=mean_confidence,
        line_count=line_count,
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


def build_ocr_report(
    *,
    provider_name: str,
    corpus_seed: int,
    evaluation_seed: int,
    evaluation_wall_ms: int,
    results: list[OcrCaseResult],
) -> OcrBlindReport:
    by_category_results: dict[str, list[OcrCaseResult]] = defaultdict(list)
    by_tier_results: dict[str, list[OcrCaseResult]] = defaultdict(list)
    for result in results:
        by_category_results[result.category].append(result)
        by_tier_results[ocr_category_tier(result.category)].append(result)

    def metrics(items: list[OcrCaseResult]) -> OcrCategoryMetrics:
        latencies = [item.latency_ms for item in items]
        return OcrCategoryMetrics(
            cases=len(items),
            exact_rate=sum(item.exact for item in items) / len(items),
            normalized_exact_rate=(
                sum(item.normalized_exact for item in items) / len(items)
            ),
            mean_character_error_rate=statistics.fmean(
                item.character_error_rate for item in items
            ),
            median_latency_ms=statistics.median(latencies),
            p95_latency_ms=_percentile(latencies, 0.95),
        )

    latencies = [result.latency_ms for result in results]
    failures = Counter(
        result.failure_kind for result in results if result.failure_kind is not None
    )
    confidence_coverage: list[OcrConfidenceCoverage] = []
    for threshold in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95):
        covered = [
            result
            for result in results
            if result.mean_confidence is not None
            and result.mean_confidence >= threshold
        ]
        if not covered:
            continue
        exact = sum(result.normalized_exact for result in covered)
        confidence_coverage.append(
            OcrConfidenceCoverage(
                threshold=threshold,
                covered_cases=len(covered),
                coverage=len(covered) / max(1, len(results)),
                normalized_exact_rate=exact / len(covered),
                mean_character_error_rate=statistics.fmean(
                    result.character_error_rate for result in covered
                ),
                wrong_cases=len(covered) - exact,
            )
        )
    return OcrBlindReport(
        provider=provider_name,
        corpus_seed=corpus_seed,
        evaluation_seed=evaluation_seed,
        cases=len(results),
        exact_matches=sum(result.exact for result in results),
        normalized_exact_matches=sum(result.normalized_exact for result in results),
        expected_aware_normalized_exact_matches=sum(
            result.expected_aware_normalized_exact for result in results
        ),
        exact_rate=sum(result.exact for result in results) / max(1, len(results)),
        normalized_exact_rate=(
            sum(result.normalized_exact for result in results) / max(1, len(results))
        ),
        expected_aware_normalized_exact_rate=(
            sum(result.expected_aware_normalized_exact for result in results)
            / max(1, len(results))
        ),
        mean_character_error_rate=statistics.fmean(
            (result.character_error_rate for result in results),
        )
        if results
        else 0.0,
        median_latency_ms=statistics.median(latencies) if latencies else 0.0,
        p95_latency_ms=_percentile(latencies, 0.95),
        evaluation_wall_ms=evaluation_wall_ms,
        throughput_images_per_second=(
            len(results) / max(0.001, evaluation_wall_ms / 1_000)
        ),
        failure_kinds=dict(sorted(failures.items())),
        by_category={
            category: metrics(by_category_results[category])
            for category in sorted(by_category_results)
        },
        by_tier={
            tier: metrics(by_tier_results[tier])
            for tier in sorted(by_tier_results)
        },
        confidence_coverage=confidence_coverage,
        results=sorted(results, key=lambda result: result.case_id),
    )


def _write_jsonl(path: Path, values: Sequence[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values),
        encoding="utf-8",
    )


def _write_contact_sheet(report: OcrBlindReport, images: dict[str, Path], path: Path) -> None:
    failures = sorted(
        (result for result in report.results if not result.normalized_exact),
        key=lambda result: (-result.character_error_rate, result.case_id),
    )[:24]
    if not failures:
        return
    cell_width, cell_height = 600, 170
    sheet = Image.new("RGB", (cell_width * 2, cell_height * 12), "white")
    draw = ImageDraw.Draw(sheet)
    label_font = ImageFont.load_default()
    for index, result in enumerate(failures):
        column, row = index % 2, index // 2
        x, y = column * cell_width, row * cell_height
        image = Image.open(images[result.case_id]).convert("RGB")
        image.thumbnail((cell_width - 20, 92), Image.Resampling.LANCZOS)
        sheet.paste(image, (x + 10, y + 10))
        expected = normalize_ocr_text(result.expected)[:78]
        observed = normalize_ocr_text(result.observed)[:78]
        draw.text(
            (x + 10, y + 108),
            f"{result.case_id} {result.category} CER={result.character_error_rate:.3f}",
            fill="black",
            font=label_font,
        )
        draw.text((x + 10, y + 126), f"E: {expected}", fill="black", font=label_font)
        draw.text((x + 10, y + 144), f"O: {observed}", fill="black", font=label_font)
    sheet.save(path, format="JPEG", quality=88, optimize=True)


async def run_blind_ocr_benchmark(
    provider: BlindOcrProvider,
    *,
    provider_name: str,
    output_dir: Path,
    count: int = DEFAULT_CASE_COUNT,
    corpus_seed: int = DEFAULT_CORPUS_SEED,
    evaluation_seed: int = DEFAULT_EVALUATION_SEED,
    jobs: int = 4,
    precise: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> OcrBlindReport:
    """Render, shuffle, blindly evaluate, score, and persist one corpus run."""

    if jobs < 1:
        raise ValueError("jobs must be positive")
    output_dir = output_dir.resolve()
    image_dir = output_dir / "images"
    output_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)
    cases = generate_ocr_cases(count=count, seed=corpus_seed)
    private_cases = {case.case_id: case for case in cases}
    images: dict[str, Path] = {}
    blind_inputs: list[BlindOcrInput] = []
    for case in cases:
        extension = ".jpg" if case.render.image_format == "JPEG" else ".png"
        image_path = image_dir / f"{case.case_id}{extension}"
        render_ocr_case(case, image_path)
        images[case.case_id] = image_path
        blind_inputs.append(
            BlindOcrInput(case_id=case.case_id, image_path=image_path)
        )

    random.Random(evaluation_seed).shuffle(blind_inputs)
    semaphore = asyncio.Semaphore(jobs)
    completed = 0

    async def observe(item: BlindOcrInput) -> BlindOcrObservation:
        nonlocal completed
        async with semaphore:
            observation = await observe_blind_input(
                provider,
                item,
                precise=precise,
            )
        completed += 1
        if progress is not None:
            progress(completed, count)
        return observation

    # The ground-truth manifest is deliberately not written until every OCR
    # call has completed.  Provider calls receive only opaque image filenames.
    evaluation_started = time.monotonic()
    observations = await asyncio.gather(*(observe(item) for item in blind_inputs))
    evaluation_wall_ms = round((time.monotonic() - evaluation_started) * 1_000)
    results = [
        score_ocr_case(private_cases[observation.case_id], observation)
        for observation in observations
    ]
    report = build_ocr_report(
        provider_name=provider_name,
        corpus_seed=corpus_seed,
        evaluation_seed=evaluation_seed,
        evaluation_wall_ms=evaluation_wall_ms,
        results=results,
    )
    diagnostics = getattr(provider, "diagnostics", None)
    if callable(diagnostics):
        report.provider_diagnostics = diagnostics()
    _write_jsonl(
        output_dir / "ground-truth.private.jsonl",
        [case.model_dump(mode="json") for case in cases],
    )
    _write_jsonl(
        output_dir / "failures.jsonl",
        [
            result.model_dump(mode="json")
            for result in report.results
            if not result.normalized_exact
        ],
    )
    (output_dir / "report.json").write_text(
        report.model_dump_json(indent=2),
        encoding="utf-8",
    )
    _write_contact_sheet(report, images, output_dir / "failures-contact-sheet.jpg")
    return report


async def run_closing_blind_ocr_benchmark(
    provider: BlindOcrProvider,
    **kwargs: Any,
) -> OcrBlindReport:
    """Run one owned benchmark and always close native provider workers."""

    try:
        return await run_blind_ocr_benchmark(provider, **kwargs)
    finally:
        closer = getattr(provider, "aclose", None)
        if callable(closer):
            await closer()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=DEFAULT_CASE_COUNT)
    parser.add_argument("--seed", type=int, default=DEFAULT_CORPUS_SEED)
    parser.add_argument("--evaluation-seed", type=int, default=DEFAULT_EVALUATION_SEED)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--provider",
        choices=("tesseract", "paddleocr", "hybrid"),
        default="tesseract",
    )
    args = parser.parse_args(argv)
    precise = False
    if args.provider in {"paddleocr", "hybrid"}:
        from pikvm_agent.vision.paddleocr_client import PaddleOCRProvider

        if args.provider == "hybrid":
            from pikvm_agent.vision.hybrid_ocr import HybridOcrProvider

            provider = HybridOcrProvider(
                TesseractOcrProvider(),
                PaddleOCRProvider(),
            )
            precise = True
        else:
            provider = PaddleOCRProvider()
            args.jobs = 1
    else:
        provider = TesseractOcrProvider()
    last_printed = 0

    def show_progress(done: int, total: int) -> None:
        nonlocal last_printed
        if done == total or done - last_printed >= max(10, total // 20):
            print(f"OCR blind test: {done}/{total}", flush=True)
            last_printed = done

    report = asyncio.run(
        run_closing_blind_ocr_benchmark(
            provider,
            provider_name=args.provider,
            output_dir=args.out,
            count=args.cases,
            corpus_seed=args.seed,
            evaluation_seed=args.evaluation_seed,
            jobs=args.jobs,
            precise=precise,
            progress=show_progress,
        )
    )
    print(
        json.dumps(
            {
                "cases": report.cases,
                "exact_rate": report.exact_rate,
                "normalized_exact_rate": report.normalized_exact_rate,
                "mean_character_error_rate": report.mean_character_error_rate,
                "p95_latency_ms": report.p95_latency_ms,
                "evaluation_wall_ms": report.evaluation_wall_ms,
                "throughput_images_per_second": report.throughput_images_per_second,
                "provider_diagnostics": report.provider_diagnostics,
                "by_tier": {
                    tier: {
                        "cases": metrics.cases,
                        "normalized_exact_rate": metrics.normalized_exact_rate,
                        "mean_character_error_rate": (
                            metrics.mean_character_error_rate
                        ),
                    }
                    for tier, metrics in report.by_tier.items()
                },
                "report": str((args.out / "report.json").resolve()),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
