"""Offline tests for the watched, self-correcting typer.

Everything runs against :class:`FakeBackend` (records every HID call) and a tiny
scripted OCR provider — no network, no real screen, no real OCR. Covers chunking,
field localisation, the fast-print path + its caps-lock disable, a single layout
self-correction that never presses Enter, truncated read-backs, and the explicit
region path.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import random
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

import pikvm_agent.executor.typing as typing_module
from pikvm_agent.core.models import OCRCandidate, OCRLine, OCRResult, Region
from pikvm_agent.executor.typing import (
    CHUNK_TARGET,
    FAST_PRINT_CHUNK_TARGET,
    FAST_PRINT_MIN,
    FAST_TERMINAL_PRINT_MIN,
    GRID_COLS,
    GRID_ROWS,
    WatchedTyper,
    WatchedTypingResult,
    _substantial_change_outside_region,
    chunk_text,
    locate_capture_change,
    locate_changed_bbox,
    locate_dense_changed_bbox,
    locate_dense_changed_candidates,
    ocr_line_screen_region,
    precise_readback_candidate_region,
    readback_region,
    regions_overlap,
)
from pikvm_agent.pikvm.fake import FakeBackend
from pikvm_agent.pikvm.screenshot import to_captured_frame
from pikvm_agent.vision.frame_diff import grid

_ENTER_KEYS = {"Enter", "NumpadEnter", "Return"}


@pytest.fixture(autouse=True)
def _run_frame_grid_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep deterministic image-grid work off the restricted test worker pool."""

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline)


class ScriptedOCR:
    """An OCRProvider that returns canned text, ignoring the image entirely.

    ``reads`` is consumed one per ``ocr`` call; once exhausted the last value
    repeats (so a steady-state read-back keeps verifying the same way).
    """

    def __init__(self, *reads: str) -> None:
        self.reads: list[str] = list(reads) or [""]
        self.calls = 0

    async def ocr(self, image_path: Path, region: Region | None = None) -> OCRResult:
        i = min(self.calls, len(self.reads) - 1)
        self.calls += 1
        text = self.reads[i]
        return OCRResult(
            lines=[OCRLine(text=text)] if text else [],
            spacing_evidence="verified",
        )


class LowConfidenceOCR:
    async def ocr(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        return OCRResult(
            lines=[
                OCRLine(
                    text="const retrv = definitely wrong",
                    confidence=0.31,
                )
            ]
        )


class AlternativeCandidateOCR:
    def __init__(self, intended: str) -> None:
        self.intended = intended

    async def ocr(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        return OCRResult(
            lines=[
                OCRLine(
                    text="https://docs. internal/runs/0040?view=screenkattempt=6",
                    confidence=0.91,
                )
            ],
            alternatives=[
                OCRCandidate(
                    text=self.intended,
                    mean_confidence=0.42,
                )
            ],
        )


def test_ocr_line_screen_region_translates_from_the_actual_crop() -> None:
    line = OCRLine(
        text="1. Observe",
        confidence=0.99,
        bbox=[61, 7, 121, 19],
    )
    crop = Region(x=0, y=96, width=218, height=32)

    translated = ocr_line_screen_region(
        line,
        crop,
        (1280, 800),
        pad=2,
    )

    assert translated == Region(x=59, y=101, width=64, height=16)


class PreciseProfileOCR:
    def __init__(self, intended: str) -> None:
        self.intended = intended
        self.regular_calls = 0
        self.precise_calls = 0

    async def ocr(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        self.regular_calls += 1
        return OCRResult(lines=[OCRLine(text="wrong regular read")])

    async def ocr_precise(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        self.precise_calls += 1
        return OCRResult(
            lines=[OCRLine(text=self.intended)],
            spacing_evidence="verified",
        )


class SpacingCandidateOCR:
    def __init__(self, normalized: str, spacing: str) -> None:
        self.normalized = normalized
        self.spacing = spacing

    async def ocr_precise(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        return OCRResult(
            lines=[OCRLine(text=self.normalized, confidence=0.98)],
            alternatives=[
                OCRCandidate(
                    text=self.spacing,
                    evidence_kind="spacing",
                )
            ],
        )

    async def ocr(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        return OCRResult(lines=[OCRLine(text=self.normalized)])


class CropMissFullScreenOCR:
    """Model the Word failure where the inferred crop misses wrapped prose."""

    def __init__(self, intended: str) -> None:
        self.intended = intended
        self.crop_calls = 0
        self.screen_calls = 0

    async def ocr(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        if region is not None:
            self.crop_calls += 1
            return OCRResult()
        self.screen_calls += 1
        noisy_read = self.intended.replace(
            "Macbeth turns",
            "Macbethturns",
        ).replace(
            "prophecy",
            "prophecv",
        )
        words = noisy_read.split()
        one_third = len(words) // 3
        return OCRResult(
            lines=[
                OCRLine(text="Microsoft Word"),
                OCRLine(
                    text=" ".join(words[:one_third]),
                    confidence=0.96,
                ),
                OCRLine(
                    text=" ".join(words[one_third : one_third * 2]),
                    confidence=0.95,
                ),
                OCRLine(
                    text=" ".join(words[one_third * 2 :]),
                    confidence=0.96,
                ),
                OCRLine(text="Page 1 of 1"),
            ]
        )


def _assert_no_enter(backend: FakeBackend) -> None:
    for method, kw in backend.calls:
        if method == "press_key":
            assert kw.get("code") not in _ENTER_KEYS, f"typer pressed Enter: {kw}"
        if method == "keypress":
            for k in kw.get("keys", []):
                assert k not in _ENTER_KEYS, f"typer emitted Enter chord: {kw}"


# --------------------------------------------------------------------------- #
# chunk_text
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "",
        "short",
        "exactly sixteen!",  # 16 chars, returned as one
        "the quick brown fox jumps over the lazy dog repeatedly",
        "a b c d e f g h i j k l m n o p q r s t",
    ],
)
def test_chunk_text_join_invariant(text: str) -> None:
    chunks = chunk_text(text)
    assert "".join(chunks) == text
    if len(text) <= CHUNK_TARGET:
        assert chunks == ([text] if text else [])


def test_chunk_text_hard_splits_long_word() -> None:
    word = "x" * 40  # no whitespace, far longer than the cap
    chunks = chunk_text(word)
    assert "".join(chunks) == word
    assert all(len(c) <= CHUNK_TARGET for c in chunks)
    assert len(chunks) == 3  # 16 + 16 + 8


def test_chunk_text_keeps_separator_at_the_start_of_the_next_prose_chunk() -> None:
    text = (
        "The consequence spreads outward and remains visible after interruption."
    )
    chunks = chunk_text(text)

    assert "".join(chunks) == text
    assert all(not chunk.endswith(" ") for chunk in chunks[:-1])
    assert all(chunk.startswith(" ") for chunk in chunks[1:])


# --------------------------------------------------------------------------- #
# locate_changed_bbox
# --------------------------------------------------------------------------- #


def _flat_grid() -> np.ndarray:
    return np.zeros(GRID_COLS * GRID_ROWS, dtype=np.uint8)


def test_locate_small_block_returns_region() -> None:
    before = _flat_grid()
    after = before.copy().reshape(GRID_ROWS, GRID_COLS)
    # a tidy 3x4 changed block, well above CELL_DELTA, contiguous (survives prune).
    after[10:13, 20:24] = 200
    region = locate_changed_bbox(before, after.reshape(-1), {"width": 1280, "height": 720})
    assert region is not None
    assert region.width > 0 and region.height > 0
    # within the screen
    assert 0 <= region.x and region.x + region.width <= 1280


def test_autodetected_readback_region_adds_context_without_changing_explicit() -> None:
    located = Region(x=200, y=200, width=220, height=20)

    assert readback_region(
        located,
        (1280, 720),
        explicit=False,
    ) == Region(x=104, y=200, width=412, height=20)
    assert readback_region(
        located,
        (1280, 720),
        explicit=True,
    ) == located
    assert readback_region(
        located,
        (1280, 720),
        explicit=False,
        vertical_context=True,
    ) == Region(x=104, y=120, width=412, height=124)
    assert readback_region(
        Region(x=40, y=666, width=53, height=45),
        (1280, 800),
        explicit=False,
    ) == Region(x=0, y=666, width=245, height=45)
    assert readback_region(
        Region(x=1240, y=200, width=30, height=20),
        (1280, 800),
        explicit=False,
    ) == Region(x=1058, y=200, width=222, height=20)


def test_short_field_context_reaches_from_run_buttons_to_command_field() -> None:
    # A coarse VNC delta can anchor on Run's repainted OK/Cancel row even
    # though the only HID input was in the field above it. Preserve enough
    # read-only context to let precise OCR localize and reread the command.
    button_row = Region(x=78, y=736, width=170, height=18)

    expanded = readback_region(
        button_row,
        (1280, 800),
        explicit=False,
        vertical_context=True,
    )

    assert expanded.y <= 680
    assert expanded.y + expanded.height >= 754


def test_locate_full_repaint_returns_none() -> None:
    before = _flat_grid()
    after = np.full(GRID_COLS * GRID_ROWS, 255, dtype=np.uint8)  # everything changed
    region = locate_changed_bbox(before, after, {"width": 1280, "height": 720})
    assert region is None  # taller than MAX_BOX_HEIGHT_FRAC ⇒ repaint, not a field


def test_locate_too_few_changed_returns_none() -> None:
    before = _flat_grid()
    after = before.copy().reshape(GRID_ROWS, GRID_COLS)
    after[5, 5] = 200  # one isolated cell — pruned away (no changed neighbour)
    region = locate_changed_bbox(before, after.reshape(-1), {"width": 1280, "height": 720})
    assert region is None


def test_dense_locator_recovers_text_line_change_hidden_by_coarse_grid() -> None:
    before = Image.new("RGB", (1280, 800), (32, 32, 32))
    after = before.copy()
    before_draw = ImageDraw.Draw(before)
    after_draw = ImageDraw.Draw(after)
    for x in range(88, 152, 2):
        before_draw.line((x, 304, x, 319), fill=(224, 224, 224))
        after_draw.line((x + 1, 304, x + 1, 319), fill=(224, 224, 224))

    def jpeg(image: Image.Image) -> bytes:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90)
        return output.getvalue()

    before_bytes = jpeg(before)
    after_bytes = jpeg(after)
    assert locate_changed_bbox(
        grid(before_bytes),
        grid(after_bytes),
        (1280, 800),
    ) is None

    region = locate_dense_changed_bbox(
        before_bytes,
        after_bytes,
        (1280, 800),
    )

    assert region is not None
    assert region.x <= 88
    assert region.y <= 304
    assert region.x + region.width >= 151
    assert region.y + region.height >= 319


def test_dense_locator_rejects_caret_sized_change() -> None:
    before = Image.new("RGB", (1280, 800), (32, 32, 32))
    after = before.copy()
    ImageDraw.Draw(after).rectangle((150, 304, 151, 319), fill=(224, 224, 224))

    def jpeg(image: Image.Image) -> bytes:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90)
        return output.getvalue()

    assert (
        locate_dense_changed_bbox(
            jpeg(before),
            jpeg(after),
            (1280, 800),
        )
        is None
    )


def _ambiguous_dense_line_frames() -> tuple[bytes, bytes]:
    before = Image.new("RGB", (1280, 800), (32, 32, 32))
    after = before.copy()
    before_draw = ImageDraw.Draw(before)
    after_draw = ImageDraw.Draw(after)
    for x in range(40, 104, 2):
        before_draw.line((x, 96, x, 111), fill=(224, 224, 224))
        after_draw.line((x + 1, 96, x + 1, 111), fill=(224, 224, 224))
    for x in range(81, 141, 2):
        before_draw.line((x, 481, x, 495), fill=(224, 224, 224))
        after_draw.line((x + 1, 481, x + 1, 495), fill=(224, 224, 224))

    def jpeg(image: Image.Image) -> bytes:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90)
        return output.getvalue()

    return jpeg(before), jpeg(after)


def test_dense_locator_can_nominate_strongest_ambiguous_line_for_exact_ocr() -> None:
    before_bytes, after_bytes = _ambiguous_dense_line_frames()
    assert locate_dense_changed_bbox(
        before_bytes,
        after_bytes,
        (1280, 800),
    ) is None

    nominated = locate_dense_changed_bbox(
        before_bytes,
        after_bytes,
        (1280, 800),
        allow_ambiguous=True,
    )

    assert nominated is not None
    assert nominated.y <= 96
    assert nominated.y + nominated.height >= 111
    assert nominated.y + nominated.height < 481
    candidates = locate_dense_changed_candidates(
        before_bytes,
        after_bytes,
        (1280, 800),
    )
    assert len(candidates) == 2
    assert any(candidate.y < 200 for candidate in candidates)
    assert any(candidate.y > 400 for candidate in candidates)


def _ambiguous_dense_typing_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> FakeBackend:
    backend = FakeBackend(width=1280, height=800)
    before_bytes, after_bytes = _ambiguous_dense_line_frames()
    backend.set_frame_bytes(before_bytes)
    original_type = backend.type_text

    async def typing(
        text: str,
        *,
        code: bool = False,
        secret: bool = False,
    ) -> None:
        await original_type(text, code=code, secret=secret)
        backend.set_frame_bytes(after_bytes)

    backend.type_text = typing  # type: ignore[method-assign]
    monkeypatch.setattr(
        typing_module,
        "locate_changed_bbox",
        lambda *_args, **_kwargs: Region(
            x=88,
            y=488,
            width=86,
            height=32,
        ),
    )
    return backend


class _AdjacentLineOCR:
    def __init__(self) -> None:
        self.target_candidate_reads = 0
        self.regions: list[Region | None] = []

    async def ocr(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        del image_path
        self.regions.append(region)
        if region is None or region.y >= 200:
            return OCRResult()
        if region.y < 92:
            self.target_candidate_reads += 1
            if self.target_candidate_reads > 1:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="2. Act\n3. Verify",
                            confidence=0.99,
                            bbox=[12, 0, 82, 22],
                        ),
                    ],
                    spacing_evidence="verified",
                )
        return OCRResult(
            lines=[
                OCRLine(
                    text="3. Verify",
                    confidence=0.99,
                    bbox=[12, 10, 82, 22],
                )
            ],
            spacing_evidence=(
                "verified"
                if self.target_candidate_reads == 1 and region.y < 92
                else "uncertain"
            ),
        )

    async def ocr_precise(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        return await self.ocr(image_path, region)


@pytest.mark.parametrize(
    ("spacing_evidence", "spacing_alternative", "expected_status"),
    [
        ("verified", False, "verified_exact"),
        ("uncertain", True, "verified_exact"),
        ("uncertain", False, "unverified_ambiguous"),
    ],
)
async def test_short_exact_typing_checks_all_causal_lines_when_coarse_crop_is_wrong(
    monkeypatch: pytest.MonkeyPatch,
    spacing_evidence: str,
    spacing_alternative: bool,
    expected_status: str,
) -> None:
    backend = _ambiguous_dense_typing_backend(monkeypatch)

    class CausalCropOCR:
        def __init__(self) -> None:
            self.regions: list[Region | None] = []

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            self.regions.append(region)
            if region is None or region.y >= 200:
                return OCRResult()
            return OCRResult(
                lines=[OCRLine(text="2. Act", confidence=0.99)],
                alternatives=(
                    [
                        OCRCandidate(
                            text="2. Act",
                            evidence_kind="spacing",
                        )
                    ]
                    if spacing_alternative
                    else []
                ),
                spacing_evidence=spacing_evidence,
            )

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr(image_path, region)

    ocr = CausalCropOCR()
    result = await WatchedTyper(backend, ocr).type_text(
        "2. Act",
        exact=True,
        context="editor",
    )

    assert result.status == expected_status
    assert result.emitted_exactly_once is True
    causal_regions = [
        region
        for region in ocr.regions
        if region is not None and region.y < 200
    ]
    assert causal_regions
    assert all(region.width < 160 for region in causal_regions)


async def test_causal_exact_row_finishes_without_a_noisier_second_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _ambiguous_dense_typing_backend(monkeypatch)

    class OneExactCausalReadOCR:
        def __init__(self) -> None:
            self.target_reads = 0

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is None or region.y >= 200:
                return OCRResult()
            self.target_reads += 1
            return OCRResult(
                lines=[
                    OCRLine(
                        text="2. Act",
                        confidence=0.99,
                        bbox=[40, 6, 78, 18],
                    )
                ],
                spacing_evidence="verified",
            )

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr(image_path, region)

    ocr = OneExactCausalReadOCR()

    result = await WatchedTyper(backend, ocr).type_text(
        "2. Act",
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact"
    assert result.field_text == "2. Act"
    assert result.emitted_exactly_once is True
    assert ocr.target_reads == 1


async def test_short_exact_typing_keeps_the_unique_causal_exact_ocr_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _ambiguous_dense_typing_backend(monkeypatch)
    ocr = _AdjacentLineOCR()
    result = await WatchedTyper(backend, ocr).type_text(
        "3. Verify",
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact"
    assert result.field_text == "3. Verify"
    assert result.emitted_exactly_once is True
    assert ocr.target_candidate_reads == 1
    assert any(
        region is not None and region.y < 92
        for region in ocr.regions
    )


def test_causal_exact_row_canonicalizes_later_ocr_line_wrap() -> None:
    intended = "1. Observe"
    region = Region(x=20, y=80, width=140, height=30)
    typer = WatchedTyper(FakeBackend(), ScriptedOCR(""))
    typer._causal_exact_spacing_intended = intended
    typer._causal_exact_spacing_region = region

    result = typer._with_causal_spacing_proof(
        OCRResult(
            lines=[
                OCRLine(
                    text="1.\nObserve",
                    confidence=0.98,
                    bbox=[22, 82, 150, 104],
                )
            ],
            spacing_evidence="verified",
        ),
        intended=intended,
        precise=True,
        requested_region=region,
    )

    assert result.text == intended
    assert result.spacing_evidence == "verified"


@pytest.mark.parametrize(
    ("observed", "requested_region"),
    [
        ("1.\nObserve!", Region(x=20, y=80, width=140, height=30)),
        ("1.\nObserve", Region(x=400, y=400, width=140, height=30)),
    ],
)
def test_causal_exact_row_does_not_canonicalize_unproven_readback(
    observed: str,
    requested_region: Region,
) -> None:
    intended = "1. Observe"
    typer = WatchedTyper(FakeBackend(), ScriptedOCR(""))
    typer._causal_exact_spacing_intended = intended
    typer._causal_exact_spacing_region = Region(
        x=20,
        y=80,
        width=140,
        height=30,
    )

    result = typer._with_causal_spacing_proof(
        OCRResult(
            lines=[OCRLine(text=observed, confidence=0.98)],
            spacing_evidence="verified",
        ),
        intended=intended,
        precise=True,
        requested_region=requested_region,
    )

    assert result.text == observed


async def test_watched_typer_uses_dense_text_line_change_when_grid_is_unchanged() -> None:
    backend = FakeBackend(width=1280, height=800)
    before = Image.new("RGB", (1280, 800), (32, 32, 32))
    after = before.copy()
    before_draw = ImageDraw.Draw(before)
    after_draw = ImageDraw.Draw(after)
    for x in range(88, 152, 2):
        before_draw.line((x, 304, x, 319), fill=(224, 224, 224))
        after_draw.line((x + 1, 304, x + 1, 319), fill=(224, 224, 224))

    def jpeg(image: Image.Image) -> bytes:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90)
        return output.getvalue()

    before_bytes = jpeg(before)
    after_bytes = jpeg(after)
    backend.set_frame_bytes(before_bytes)
    typed = ""
    original_type = backend.type_text

    async def typing(
        text: str,
        *,
        code: bool = False,
        secret: bool = False,
    ) -> None:
        nonlocal typed
        await original_type(text, code=code, secret=secret)
        typed += text
        backend.set_frame_bytes(after_bytes)

    backend.type_text = typing  # type: ignore[method-assign]

    class CurrentTextOCR:
        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return OCRResult(
                lines=[OCRLine(text=typed, confidence=0.99)] if typed else [],
                spacing_evidence="verified",
            )

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr(image_path, region)

    intended = r"C:\PiKVM-Harness\workspace"
    typer = WatchedTyper(backend, CurrentTextOCR())

    result = await typer.type_text(intended, code=True)

    assert result.status == "verified_exact"
    assert result.typed_characters == len(intended)
    assert result.emitted_exactly_once is True


async def test_capture_change_grounds_terminal_line_when_inferred_crop_is_wrong() -> None:
    intended = "gsettings set org.gnome.settings-daemon.plugins.power idle-dim false"
    backend = FakeBackend()
    before = await backend.screenshot()
    before_grid = grid(before.data)
    backend.set_screen(intended)
    after = await backend.screenshot()

    changed = locate_capture_change(
        before_grid,
        before,
        after,
        (1280, 720),
    )
    command_line = Region(x=12, y=72, width=1100, height=36)
    poisoned_crop = Region(x=560, y=290, width=80, height=24)

    assert changed is not None
    assert regions_overlap(changed, command_line)
    assert not regions_overlap(poisoned_crop, command_line)


def test_focus_change_guard_declines_unknown_screen_dimensions() -> None:
    before = _flat_grid()
    after = before.copy().reshape(GRID_ROWS, GRID_COLS)
    after[10:13, 20:24] = 200

    assert not _substantial_change_outside_region(
        before,
        after.reshape(-1),
        Region(x=10, y=10, width=400, height=40),
        (0, 0),
    )


def test_ocr_localization_ignores_chunk_boundary_space_before_caret() -> None:
    typer = WatchedTyper(FakeBackend(), ScriptedOCR())
    result = OCRResult(
        lines=[
            OCRLine(
                text="The consequence}",
                confidence=0.96,
                bbox=[448, 449, 516, 462],
            )
        ]
    )

    region = typer._locate_ocr_candidate(  # noqa: SLF001 - locator regression
        result,
        "The consequence ",
        (1280, 720),
    )

    assert region is not None
    assert region.x == 440
    assert region.y == 441


def test_ocr_localization_folds_smart_quotes_for_prose_only() -> None:
    typer = WatchedTyper(FakeBackend(), ScriptedOCR())
    result = OCRResult(
        lines=[
            OCRLine(
                text="Banquo’s murder|",
                confidence=0.96,
                bbox=[448, 449, 540, 462],
            )
        ]
    )

    prose_region = typer._locate_ocr_candidate(  # noqa: SLF001
        result,
        " Banquo's murder ",
        (1280, 720),
    )
    precise_region = typer._locate_ocr_candidate(  # noqa: SLF001
        result,
        " Banquo's murder ",
        (1280, 720),
        precise=True,
    )

    assert prose_region is not None
    assert precise_region is None


@pytest.mark.parametrize(
    ("leading", "suffix", "should_match"),
    [
        pytest.param("", "", True, id="exact-wrapped-command"),
        pytest.param("4 ", "", False, id="wrapped-leading-artifact"),
        pytest.param("", "x", False, id="wrapped-extra-suffix"),
    ],
)
def test_full_screen_exact_candidate_reconstructs_adjacent_terminal_rows(
    leading: str,
    suffix: str,
    should_match: bool,
) -> None:
    intended = "gsettings set org.gnome.settings-daemon.plugins.power idle-dim false"
    result = OCRResult(
        lines=[
            OCRLine(
                text=(
                    "J user@vm:~$ "
                    "gsettings set org.gnome.settings-daemon.p"
                ),
                confidence=0.72,
                bbox=[9, 120, 1259, 154],
            ),
            OCRLine(
                text=f"{leading}lugins.power idle-dim false{suffix}",
                confidence=0.71,
                bbox=[10, 154, 533, 194],
            ),
        ]
    )

    candidate = WatchedTyper(
        FakeBackend(),
        ScriptedOCR(),
    )._full_screen_exact_line_candidate(
        result,
        intended,
        (1280, 720),
        allow_semantic_spacing=True,
    )

    assert (candidate is not None) is should_match
    if candidate is not None:
        assert candidate[0] == intended
        assert candidate[1].y <= 120
        assert candidate[1].y + candidate[1].height >= 194


def test_full_screen_prefix_crop_includes_low_confidence_wrapped_row() -> None:
    intended = "gsettings set org.gnome.settings-daemon.plugins.power idle-dim false"
    result = OCRResult(
        lines=[
            OCRLine(
                text=(
                    "user@vm:~$ "
                    "gsettings set org.gnome.settings-daemon.plugins.power"
                ),
                confidence=0.82,
                bbox=[48, 111, 1249, 147],
            ),
            OCRLine(
                text="idle-dim false",
                confidence=0.31,
                bbox=[48, 138, 267, 194],
            ),
        ]
    )

    region = WatchedTyper._full_screen_exact_prefix_region(
        result,
        intended,
        (1280, 720),
    )

    assert region is not None
    assert region.y <= 111
    assert region.y + region.height >= 194


# --------------------------------------------------------------------------- #
# fast print path
# --------------------------------------------------------------------------- #


async def test_fast_path_long_prose_matches() -> None:
    backend = FakeBackend()
    prose = (
        "This is a long, plain sentence with no special symbols so it should "
        "take the fast server-side print path without trouble."
    )
    assert len(prose) > FAST_PRINT_MIN
    # The grid must change between the before/after capture so the field locates.
    # FakeBackend renders the same frame each screenshot; flip the screen between
    # captures by reacting to print_text.
    orig_print = backend.print_text

    async def printing(text: str) -> None:
        await orig_print(text)
        backend.set_screen("typed prose region")

    backend.print_text = printing  # type: ignore[method-assign]

    ocr = ScriptedOCR(prose)  # read-back matches what we printed
    typer = WatchedTyper(backend, ocr)

    result = await typer.type_text(prose)
    assert isinstance(result, WatchedTypingResult)
    assert result.used_fast_path is True
    assert result.verdict == "match"
    assert result.ok is True
    assert result.corrected is False
    assert any(m == "print_text" for m, _ in backend.calls)
    assert not any(m == "type_text" for m, _ in backend.calls)
    _assert_no_enter(backend)


async def test_editor_prose_semicolon_can_use_guarded_fast_path() -> None:
    backend = FakeBackend()
    prose = (
        "Shakespeare treats choice as a human burden; his characters inherit "
        "pressure and prophecy, but they remain responsible for what follows."
    )
    orig_print = backend.print_text

    async def printing(text: str) -> None:
        await orig_print(text)
        backend.set_screen("typed editor prose")

    backend.print_text = printing  # type: ignore[method-assign]
    typer = WatchedTyper(backend, ScriptedOCR(prose))

    result = await typer.type_text(prose, prose=True)

    assert result.used_fast_path is True
    assert result.verdict == "match"
    assert any(method == "print_text" for method, _ in backend.calls)
    assert not any(method == "type_text" for method, _ in backend.calls)
    _assert_no_enter(backend)


async def test_short_exact_editor_text_uses_guarded_fast_print() -> None:
    backend = FakeBackend()
    backend.guarded_exact_print = True  # type: ignore[attr-defined]
    sentence = "Reliable automation starts with observable evidence."
    orig_print = backend.print_text

    async def printing(text: str) -> None:
        await orig_print(text)
        backend.set_screen(sentence)

    backend.print_text = printing  # type: ignore[method-assign]
    result = await WatchedTyper(
        backend,
        ScriptedOCR(sentence),
    ).type_text(
        sentence,
        exact=True,
        context="editor",
    )

    assert result.used_fast_path is True
    assert result.status == "verified_exact"
    assert result.emitted_exactly_once is True
    assert any(method == "print_text" for method, _ in backend.calls)
    assert not any(method == "type_text" for method, _ in backend.calls)
    _assert_no_enter(backend)


async def test_simple_exact_terminal_command_uses_guarded_fast_print() -> None:
    backend = FakeBackend()
    backend.guarded_exact_print = True  # type: ignore[attr-defined]
    command = (
        "gsettings set org.gnome.settings-daemon.plugins.power idle-dim false"
    )
    assert len(command) > FAST_TERMINAL_PRINT_MIN
    orig_print = backend.print_text

    async def printing(text: str) -> None:
        await orig_print(text)
        backend.set_screen(command)

    backend.print_text = printing  # type: ignore[method-assign]
    result = await WatchedTyper(
        backend,
        ScriptedOCR(command),
    ).type_text(
        command,
        code=True,
        exact=True,
        context="terminal",
    )

    assert result.used_fast_path is True
    assert result.status == "verified_exact"
    assert result.emitted_exactly_once is True
    assert any(method == "print_text" for method, _ in backend.calls)
    assert not any(method == "type_text" for method, _ in backend.calls)
    _assert_no_enter(backend)


@pytest.mark.parametrize("value", ["calc", "ms-settings:about"])
async def test_short_exact_field_stays_on_per_key_transport(
    value: str,
) -> None:
    backend = FakeBackend()
    backend.guarded_exact_print = True  # type: ignore[attr-defined]

    def field_frame(text: str) -> bytes:
        image = Image.new("RGB", (1280, 720), (24, 28, 36))
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            [80, 100, 520, 150],
            fill=(235, 235, 235) if text else (250, 250, 250),
        )
        draw.text((100, 115), text, fill=(20, 20, 20))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=95)
        return output.getvalue()

    backend.set_frame_bytes(field_frame(""))
    orig_type = backend.type_text

    async def typing(
        text: str,
        *,
        code: bool = False,
        secret: bool = False,
    ) -> None:
        await orig_type(text, code=code, secret=secret)
        backend.set_frame_bytes(field_frame(value))

    backend.type_text = typing  # type: ignore[method-assign]
    result = await WatchedTyper(
        backend,
        ScriptedOCR(value),
    ).type_text(
        value,
        exact=True,
        context="field",
    )

    assert result.used_fast_path is False
    assert result.status == "verified_exact"
    assert result.emitted_exactly_once is True
    assert any(method == "type_text" for method, _ in backend.calls)
    assert not any(method == "print_text" for method, _ in backend.calls)
    _assert_no_enter(backend)


async def test_short_exact_field_corrects_one_strong_substitution_once() -> None:
    backend = FakeBackend()
    field_value = ""
    emissions = 0

    def field_frame(text: str) -> bytes:
        image = Image.new("RGB", (1280, 720), (24, 28, 36))
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            [80, 100, 520, 150],
            fill=(210, 210, 210) if text else (250, 250, 250),
        )
        draw.text((100, 115), text, fill=(20, 20, 20))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=95)
        return output.getvalue()

    class FieldOCR:
        async def ocr(self, image_path, region=None):
            return await self.ocr_precise(image_path, region=region)

        async def ocr_precise(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(
                        text=field_value,
                        confidence=0.99,
                        bbox=[100, 115, 150, 135],
                    )
                ]
                if field_value
                else [],
                spacing_evidence="verified",
            )

    original_type = backend.type_text
    original_press = backend.press_key
    backend.set_frame_bytes(field_frame(""))

    async def typing(
        text: str,
        *,
        code: bool = False,
        secret: bool = False,
    ) -> None:
        nonlocal emissions, field_value
        await original_type(text, code=code, secret=secret)
        emissions += 1
        field_value = "cald" if emissions == 1 else text
        backend.set_frame_bytes(field_frame(field_value))

    async def press_key(code: str) -> None:
        nonlocal field_value
        await original_press(code)
        if code == "Backspace":
            field_value = field_value[:-1]
            backend.set_frame_bytes(field_frame(field_value))

    backend.type_text = typing  # type: ignore[method-assign]
    backend.press_key = press_key  # type: ignore[method-assign]

    result = await WatchedTyper(backend, FieldOCR()).type_text(
        "calc",
        exact=True,
        context="field",
    )

    assert result.status == "verified_exact", result
    assert result.correction_count == 1
    assert emissions == 2
    _assert_no_enter(backend)


async def test_short_exact_field_reads_once_with_caret_blurred() -> None:
    backend = FakeBackend()
    emissions = 0
    ocr_calls = 0

    def field_frame(text: str) -> bytes:
        image = Image.new("RGB", (1280, 720), (24, 28, 36))
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            [80, 100, 520, 150],
            fill=(210, 210, 210) if text else (250, 250, 250),
        )
        draw.text((100, 115), text, fill=(20, 20, 20))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=95)
        return output.getvalue()

    class CaretPhaseOCR:
        async def ocr(self, image_path, region=None):
            return await self.ocr_precise(image_path, region=region)

        async def ocr_precise(self, image_path, region=None):
            nonlocal ocr_calls
            del image_path, region
            ocr_calls += 1
            last_keypress = next(
                (
                    kwargs["keys"]
                    for method, kwargs in reversed(backend.calls)
                    if method == "keypress"
                ),
                [],
            )
            observed = "calc" if last_keypress == ["Tab"] else "cald"
            return OCRResult(
                lines=[
                    OCRLine(
                        text=observed,
                        confidence=0.99,
                        bbox=[100, 115, 150, 135],
                    )
                ],
                spacing_evidence="verified",
            )

    original_type = backend.type_text
    backend.set_frame_bytes(field_frame(""))

    async def typing(
        text: str,
        *,
        code: bool = False,
        secret: bool = False,
    ) -> None:
        nonlocal emissions
        await original_type(text, code=code, secret=secret)
        emissions += 1
        backend.set_frame_bytes(field_frame(text))

    backend.type_text = typing  # type: ignore[method-assign]

    result = await WatchedTyper(backend, CaretPhaseOCR()).type_text(
        "calc",
        exact=True,
        context="field",
    )

    assert result.status == "verified_exact", result
    assert result.correction_count == 0
    assert emissions == 1
    assert ocr_calls == 1
    assert ("keypress", {"keys": ["Tab"]}) in backend.calls
    assert ("keypress", {"keys": ["ShiftLeft", "Tab"]}) in backend.calls
    _assert_no_enter(backend)


@pytest.mark.parametrize(
    ("stabilized_read", "expected_status"),
    [
        (r"C:\PiKVM-Harness\workspace\codex-50", "verified_exact"),
        (r"C:#PiKVM-Harness#workspace#codex-50", "failed_keyboard_layout"),
    ],
)
async def test_long_exact_field_moves_caret_home_without_retyping(
    stabilized_read: str,
    expected_status: str,
) -> None:
    backend = FakeBackend(layout="uk")
    intended = r"C:\PiKVM-Harness\workspace\codex-50"
    distorted = r"C:#PiKVM-Harness#workspace#codex-50"
    emitted = ""

    class LayoutLikeCaretOCR:
        async def ocr(self, image_path, region=None):
            return await self.ocr_precise(image_path, region=region)

        async def ocr_precise(self, image_path, region=None):
            del image_path, region
            caret_home = next(
                (
                    True
                    for method, kwargs in reversed(backend.calls)
                    if method == "press_key" and kwargs["code"] == "Home"
                ),
                False,
            )
            observed = emitted
            if emitted == intended:
                observed = (
                    stabilized_read
                    if caret_home
                    else distorted
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=observed,
                        confidence=0.98,
                        bbox=[100, 115, 500, 135],
                    )
                ]
                if observed
                else [],
                spacing_evidence="verified",
            )

    original_type = backend.type_text
    original_keypress = backend.keypress

    async def typing(
        text: str,
        *,
        code: bool = False,
        secret: bool = False,
    ) -> None:
        nonlocal emitted
        await original_type(text, code=code, secret=secret)
        emitted += text

    backend.type_text = typing  # type: ignore[method-assign]

    async def keypress(keys: list[str]) -> None:
        nonlocal emitted
        await original_keypress(keys)
        if keys == ["Tab"]:
            # Windows Save As cancels an unsubmitted address-bar draft when
            # focus moves away; a "blur reread" would destroy the very input
            # this verification transaction is meant to preserve.
            emitted = "Documents"

    backend.keypress = keypress  # type: ignore[method-assign]

    result = await WatchedTyper(backend, LayoutLikeCaretOCR()).type_text(
        intended,
        region=Region(x=80, y=100, width=500, height=50),
        exact=True,
        context="field",
    )

    assert result.status == expected_status, result
    assert result.correction_count == 0
    assert result.emitted_exactly_once is True
    assert emitted == intended
    assert backend.layout == "uk"
    assert ("press_key", {"code": "Home"}) in backend.calls
    assert ("keypress", {"keys": ["ControlLeft", "KeyA"]}) not in backend.calls
    assert ("keypress", {"keys": ["Tab"]}) not in backend.calls
    assert ("keypress", {"keys": ["ShiftLeft", "Tab"]}) not in backend.calls
    assert not any(
        method == "press_key" and kwargs["code"] == "Backspace"
        for method, kwargs in backend.calls
    )
    _assert_no_enter(backend)


async def test_long_exact_field_uses_blind_ocr_only_after_local_mismatch(
) -> None:
    backend = FakeBackend(layout="uk")
    intended = r"C:\PiKVM-Harness\workspace\codex-50"
    emitted = ""

    class LocalThenBlindOCR:
        def __init__(self) -> None:
            self.local_calls = 0
            self.blind_calls = 0

        async def ocr(self, image_path, region=None):
            return await self.ocr_precise(image_path, region=region)

        async def ocr_precise(self, image_path, region=None):
            del image_path, region
            self.local_calls += 1
            return OCRResult(
                lines=[
                    OCRLine(
                        text=r"C:\PiKVM-Hamess\workspace\.codex-50",
                        confidence=0.94,
                        bbox=[100, 115, 500, 135],
                    )
                ],
            )

        async def ocr_precise_fallback(self, image_path, region=None):
            del image_path, region
            self.blind_calls += 1
            return OCRResult(
                lines=[
                    OCRLine(
                        text=intended,
                        confidence=0.98,
                        bbox=[100, 115, 500, 135],
                    )
                ],
            )

    ocr = LocalThenBlindOCR()
    original_type = backend.type_text

    async def typing(
        text: str,
        *,
        code: bool = False,
        secret: bool = False,
    ) -> None:
        nonlocal emitted
        await original_type(text, code=code, secret=secret)
        emitted += text

    backend.type_text = typing  # type: ignore[method-assign]

    result = await WatchedTyper(backend, ocr).type_text(
        intended,
        region=Region(x=80, y=100, width=500, height=50),
        exact=True,
        context="field",
    )

    assert result.status == "verified_exact", result
    assert result.field_text == intended
    assert result.correction_count == 0
    assert result.emitted_exactly_once is True
    assert emitted == intended
    assert ocr.local_calls >= 1
    assert ocr.blind_calls == 1
    _assert_no_enter(backend)


async def test_blind_exact_fallback_recaptures_native_field_crop() -> None:
    intended = "Wait—did the agent type “smart quotes” exactly."
    region = Region(x=40, y=74, width=520, height=45)

    class NativeCropBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__(width=1280, height=800, layout="uk")
            self.requested_regions: list[Region | None] = []

        async def screenshot(self, region=None):
            self.requested_regions.append(region)
            if region is None:
                return await super().screenshot()
            output = io.BytesIO()
            Image.new("RGB", (832, 72), "navy").save(output, "JPEG")
            return to_captured_frame(output.getvalue(), 832, 72)

    class LocalThenBlindOCR:
        def __init__(self) -> None:
            self.blind_image_size = None
            self.blind_region = None

        async def ocr(self, image_path, region=None):
            return await self.ocr_precise(image_path, region=region)

        async def ocr_precise(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(
                        text="Wait-did the agent type “smart quotes” exactly.",
                        confidence=0.99,
                    )
                ]
            )

        async def ocr_precise_fallback(self, image_path, region=None):
            self.blind_image_size = Image.open(image_path).size
            self.blind_region = region
            return OCRResult(
                lines=[OCRLine(text=intended, confidence=0.99)],
                spacing_evidence="verified",
            )

    backend = NativeCropBackend()
    ocr = LocalThenBlindOCR()

    observed = await WatchedTyper(backend, ocr)._read_field(
        region,
        intended=intended,
        precise=True,
        allow_blind_fallback=True,
    )

    assert observed == intended
    assert backend.requested_regions == [None, region]
    assert ocr.blind_image_size == (832, 72)
    assert ocr.blind_region == Region(x=0, y=0, width=832, height=72)


@pytest.mark.parametrize(
    ("blurred_read", "expected_status"),
    [
        ("taskmgr", "verified_exact"),
        ("taskmngr", "unverified_ambiguous"),
    ],
)
async def test_short_exact_field_rechecks_medium_confidence_one_edit_read(
    blurred_read: str,
    expected_status: str,
) -> None:
    backend = FakeBackend()
    emissions = 0

    def field_frame(text: str) -> bytes:
        image = Image.new("RGB", (1280, 720), (24, 28, 36))
        draw = ImageDraw.Draw(image)
        draw.rectangle([80, 100, 520, 150], fill=(210, 210, 210))
        draw.text((100, 115), text, fill=(20, 20, 20))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=95)
        return output.getvalue()

    class IntermittentOCR:
        async def ocr(self, image_path, region=None):
            return await self.ocr_precise(image_path, region=region)

        async def ocr_precise(self, image_path, region=None):
            del image_path, region
            last_keypress = next(
                (
                    kwargs["keys"]
                    for method, kwargs in reversed(backend.calls)
                    if method == "keypress"
                ),
                [],
            )
            observed = (
                blurred_read
                if last_keypress == ["Tab"]
                else "taskmngr"
            )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=observed,
                        confidence=0.93,
                        bbox=[100, 115, 180, 135],
                    )
                ]
            )

    original_type = backend.type_text
    backend.set_frame_bytes(field_frame(""))

    async def typing(
        text: str,
        *,
        code: bool = False,
        secret: bool = False,
    ) -> None:
        nonlocal emissions
        await original_type(text, code=code, secret=secret)
        emissions += 1
        backend.set_frame_bytes(field_frame(text))

    backend.type_text = typing  # type: ignore[method-assign]

    result = await WatchedTyper(backend, IntermittentOCR()).type_text(
        "taskmgr",
        exact=True,
        context="field",
    )

    assert result.status == expected_status, result
    assert result.correction_count == 0
    assert emissions == 1
    assert ("keypress", {"keys": ["Tab"]}) in backend.calls
    assert ("keypress", {"keys": ["ShiftLeft", "Tab"]}) in backend.calls
    _assert_no_enter(backend)


async def test_short_exact_field_reuses_a_refined_crop_for_its_recheck() -> None:
    backend = FakeBackend()
    emissions = 0
    observed_regions: list[Region] = []
    refined = Region(x=47, y=673, width=198, height=34)

    def field_frame(text: str) -> bytes:
        image = Image.new("RGB", (1280, 800), (24, 28, 36))
        draw = ImageDraw.Draw(image)
        draw.rectangle([40, 666, 250, 712], fill=(210, 210, 210))
        draw.text((51, 680), text, fill=(20, 20, 20))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=95)
        return output.getvalue()

    original_type = backend.type_text
    backend.set_frame_bytes(field_frame(""))
    typer = WatchedTyper(backend, ScriptedOCR())

    async def typing(
        text: str,
        *,
        code: bool = False,
        secret: bool = False,
    ) -> None:
        nonlocal emissions
        await original_type(text, code=code, secret=secret)
        emissions += 1
        backend.set_frame_bytes(field_frame(text))

    async def scripted_read(
        region: Region,
        **_kwargs,
    ) -> str:
        observed_regions.append(region)
        if len(observed_regions) == 1:
            typer._last_field_ocr_result = OCRResult(
                lines=[
                    OCRLine(
                        text="taskmngr",
                        confidence=0.93,
                        bbox=[4, 2, 80, 20],
                    )
                ]
            )
            typer._refined_readback_region = refined
            typer._refined_readback_intended = "taskmgr"
            return "taskmngr"
        typer._last_field_ocr_result = OCRResult(
            lines=[
                OCRLine(
                    text="taskmgr",
                    confidence=0.98,
                    bbox=[4, 2, 80, 20],
                )
            ]
        )
        return "taskmgr"

    backend.type_text = typing  # type: ignore[method-assign]
    typer._read_field = scripted_read  # type: ignore[method-assign]

    result = await typer.type_text(
        "taskmgr",
        exact=True,
        context="field",
    )

    assert result.status == "verified_exact", result
    assert emissions == 1
    assert len(observed_regions) == 2
    assert observed_regions[1] == refined
    _assert_no_enter(backend)


async def test_refined_crop_is_not_reused_after_intended_text_grows() -> None:
    backend = FakeBackend()
    intended = "Reliable automation starts with observable evidence."
    full_region = Region(x=40, y=70, width=760, height=55)
    first_chunk_region = Region(x=42, y=82, width=200, height=27)
    observed: list[tuple[Region, str]] = []
    typer = WatchedTyper(backend, ScriptedOCR())

    async def scripted_read(
        region: Region,
        *,
        intended: str | None = None,
        **_kwargs,
    ) -> str:
        observed.append((region, intended or ""))
        typer._last_field_ocr_result = OCRResult(
            lines=[
                OCRLine(
                    text=intended or "",
                    confidence=0.99,
                    bbox=[2, 2, 180, 22],
                )
            ]
        )
        if len(observed) == 1:
            typer._refined_readback_region = first_chunk_region
            typer._refined_readback_intended = intended or ""
        return intended or ""

    typer._read_field = scripted_read  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        region=full_region,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert len(observed) >= 2
    assert observed[0][0] == full_region
    assert observed[1][1] != observed[0][1]
    assert observed[1][0] == full_region
    assert observed[1][0] != first_chunk_region
    _assert_no_enter(backend)


async def test_short_exact_field_with_whitespace_moves_caret_for_safe_readback(
) -> None:
    backend = FakeBackend()
    field_value = ""

    def field_frame(text: str) -> bytes:
        image = Image.new("RGB", (1280, 720), (24, 28, 36))
        draw = ImageDraw.Draw(image)
        draw.rectangle([80, 100, 520, 150], fill=(210, 210, 210))
        draw.text((100, 115), text, fill=(20, 20, 20))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=95)
        return output.getvalue()

    class AddressBarOCR:
        async def ocr(self, image_path, region=None):
            return await self.ocr_precise(image_path, region=region)

        async def ocr_precise(self, image_path, region=None):
            del image_path, region
            caret_home = next(
                (
                    True
                    for method, kwargs in reversed(backend.calls)
                    if method == "press_key" and kwargs["code"] == "Home"
                ),
                False,
            )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=field_value if caret_home else "This Pd",
                        confidence=0.99,
                        bbox=[100, 115, 180, 135],
                    )
                ],
                spacing_evidence="verified" if caret_home else "uncertain",
            )

    original_type = backend.type_text
    original_keypress = backend.keypress
    backend.set_frame_bytes(field_frame("Home"))

    async def typing(
        text: str,
        *,
        code: bool = False,
        secret: bool = False,
    ) -> None:
        nonlocal field_value
        await original_type(text, code=code, secret=secret)
        field_value = text
        backend.set_frame_bytes(field_frame(text))

    async def keypress(keys: list[str]) -> None:
        nonlocal field_value
        await original_keypress(keys)
        if keys == ["Tab"]:
            field_value = "Home"
            backend.set_frame_bytes(field_frame(field_value))

    backend.type_text = typing  # type: ignore[method-assign]
    backend.keypress = keypress  # type: ignore[method-assign]

    result = await WatchedTyper(backend, AddressBarOCR()).type_text(
        "This PC",
        exact=True,
        context="field",
    )

    assert result.status == "verified_exact", result
    assert result.field_text == "This PC"
    assert ("keypress", {"keys": ["Tab"]}) not in backend.calls
    assert ("press_key", {"code": "Home"}) in backend.calls
    assert ("keypress", {"keys": ["ControlLeft", "KeyA"]}) not in backend.calls
    _assert_no_enter(backend)


async def test_short_exact_field_accepts_one_complete_exact_context_row() -> None:
    backend = FakeBackend()
    ocr_calls = 0

    def field_frame(text: str) -> bytes:
        image = Image.new("RGB", (1280, 720), (24, 28, 36))
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            [80, 100, 520, 150],
            fill=(210, 210, 210) if text else (250, 250, 250),
        )
        draw.text((100, 115), text, fill=(20, 20, 20))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=95)
        return output.getvalue()

    class ContextRowOCR:
        async def ocr(self, image_path, region=None):
            return await self.ocr_precise(image_path, region=region)

        async def ocr_precise(self, image_path, region=None):
            nonlocal ocr_calls
            del image_path, region
            ocr_calls += 1
            if ocr_calls > 1:
                return OCRResult(
                    lines=[OCRLine(text="calc", confidence=0.999)],
                    spacing_evidence="verified",
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text="Open:",
                        confidence=0.98,
                        bbox=[10, 10, 55, 28],
                    ),
                    OCRLine(
                        text="calc",
                        confidence=0.999,
                        bbox=[70, 10, 110, 28],
                    ),
                    OCRLine(
                        text="Cancel",
                        confidence=0.99,
                        bbox=[120, 10, 180, 28],
                    ),
                ],
                spacing_evidence="verified",
            )

    backend.set_frame_bytes(field_frame(""))
    original_type = backend.type_text

    async def typing(
        text: str,
        *,
        code: bool = False,
        secret: bool = False,
    ) -> None:
        await original_type(text, code=code, secret=secret)
        backend.set_frame_bytes(field_frame(text))

    backend.type_text = typing  # type: ignore[method-assign]
    result = await WatchedTyper(backend, ContextRowOCR()).type_text(
        "calc",
        exact=True,
        context="field",
    )

    assert result.status == "verified_exact", result
    assert result.correction_count == 0
    assert ocr_calls == 1
    _assert_no_enter(backend)


async def test_short_exact_field_accepts_geometrically_verified_context_row(
) -> None:
    backend = FakeBackend()

    def field_frame(text: str) -> bytes:
        image = Image.new("RGB", (1280, 720), (24, 28, 36))
        draw = ImageDraw.Draw(image)
        draw.rectangle([80, 100, 520, 150], fill=(210, 210, 210))
        draw.text((100, 115), text, fill=(20, 20, 20))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=95)
        return output.getvalue()

    class ContextRowOCR:
        async def ocr(self, image_path, region=None):
            return await self.ocr_precise(image_path, region=region)

        async def ocr_precise(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(text=">", confidence=0.42),
                    OCRLine(text="This PC", confidence=0.995),
                ],
                alternatives=[
                    OCRCandidate(
                        text="This PC",
                        mean_confidence=0.995,
                        evidence_kind="spacing",
                    )
                ],
                spacing_evidence="uncertain",
            )

    backend.set_frame_bytes(field_frame("Home"))
    original_type = backend.type_text

    async def typing(
        text: str,
        *,
        code: bool = False,
        secret: bool = False,
    ) -> None:
        await original_type(text, code=code, secret=secret)
        backend.set_frame_bytes(field_frame(text))

    backend.type_text = typing  # type: ignore[method-assign]
    result = await WatchedTyper(backend, ContextRowOCR()).type_text(
        "This PC",
        exact=True,
        context="field",
    )

    assert result.status == "verified_exact", result
    assert result.field_text == "This PC"
    _assert_no_enter(backend)


async def test_short_exact_field_blurs_a_one_character_ocr_truncation() -> None:
    backend = FakeBackend()
    emissions = 0
    ocr_calls = 0

    def field_frame(text: str) -> bytes:
        image = Image.new("RGB", (1280, 720), (24, 28, 36))
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            [80, 100, 520, 150],
            fill=(210, 210, 210) if text else (250, 250, 250),
        )
        draw.text((100, 115), text, fill=(20, 20, 20))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=95)
        return output.getvalue()

    class CaretCropOCR:
        async def ocr(self, image_path, region=None):
            return await self.ocr_precise(image_path, region=region)

        async def ocr_precise(self, image_path, region=None):
            nonlocal ocr_calls
            del image_path, region
            ocr_calls += 1
            observed = "cal" if ocr_calls <= 2 else "calc"
            return OCRResult(
                lines=[
                    OCRLine(
                        text=observed,
                        confidence=0.99,
                        bbox=[100, 115, 150, 135],
                    )
                ],
                spacing_evidence="verified",
            )

    original_type = backend.type_text
    backend.set_frame_bytes(field_frame(""))

    async def typing(
        text: str,
        *,
        code: bool = False,
        secret: bool = False,
    ) -> None:
        nonlocal emissions
        await original_type(text, code=code, secret=secret)
        emissions += 1
        backend.set_frame_bytes(field_frame(text))

    backend.type_text = typing  # type: ignore[method-assign]

    result = await WatchedTyper(backend, CaretCropOCR()).type_text(
        "calc",
        exact=True,
        context="field",
    )

    assert result.status == "verified_exact", result
    assert result.correction_count == 0
    assert emissions == 1
    assert ("keypress", {"keys": ["Tab"]}) in backend.calls
    assert ("keypress", {"keys": ["ShiftLeft", "Tab"]}) in backend.calls
    _assert_no_enter(backend)


async def test_terminal_metacharacters_stay_on_per_key_transport() -> None:
    backend = FakeBackend()
    command = "printf dangerous > local-file.txt"
    typed = ""
    orig_type = backend.type_text

    async def typing(
        text: str,
        *,
        code: bool = False,
        secret: bool = False,
    ) -> None:
        nonlocal typed
        await orig_type(text, code=code, secret=secret)
        typed += text
        backend.set_screen(typed)

    backend.type_text = typing  # type: ignore[method-assign]
    result = await WatchedTyper(
        backend,
        ScriptedOCR(command),
    ).type_text(
        command,
        code=True,
        exact=True,
        context="terminal",
    )

    assert result.used_fast_path is False
    assert any(method == "type_text" for method, _ in backend.calls)
    assert not any(method == "print_text" for method, _ in backend.calls)
    _assert_no_enter(backend)


async def test_fast_editor_prose_falls_back_to_full_screen_readback() -> None:
    backend = FakeBackend()
    prose = (
        "Macbeth turns the same question toward ambition. The witches name a "
        "possibility; they do not perform it. The prophecy tells Macbeth what "
        "may be true, and leaves the murder entirely to him."
    )
    orig_print = backend.print_text

    async def printing(text: str) -> None:
        await orig_print(text)
        backend.set_screen("wrapped editor prose changed several lines")

    backend.print_text = printing  # type: ignore[method-assign]
    ocr = CropMissFullScreenOCR(prose)
    typer = WatchedTyper(backend, ocr)

    result = await typer.type_text(prose, prose=True)

    assert result.used_fast_path is True
    assert result.verdict == "match"
    assert result.field_text != prose
    assert "Macbethturns" in result.field_text
    assert ocr.crop_calls >= 1
    assert ocr.screen_calls == 1
    _assert_no_enter(backend)


async def test_fast_editor_continuation_matches_inside_existing_ocr_line() -> None:
    """Word can place new prose directly after text that predates this call."""

    backend = FakeBackend()
    continuation = (
        " convenience the court has agreed to, and it is chosen again each day "
        "it is not corrected. Hamlet's hesitation is therefore not a flaw of "
        "temperament but a moral position: he refuses to act until the ground "
        "of the act is known."
    )
    orig_print = backend.print_text

    async def printing(text: str) -> None:
        await orig_print(text)
        backend.set_screen("wrapped editor continuation changed several lines")

    backend.print_text = printing  # type: ignore[method-assign]

    class ContinuationOCR:
        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            if region is not None:
                return OCRResult()
            return OCRResult(
                lines=[
                    OCRLine(text="Microsoft Word"),
                    OCRLine(
                        text=(
                            "The earlier sentence was taken against his own "
                            "argument. That lie is a convenience the court has "
                            "agrecd to, "
                            "and it is chosen again each day it is"
                        ),
                        confidence=0.96,
                    ),
                    OCRLine(
                        text=(
                            "not corrected. Hamlet's hesitation is therefore "
                            "not a flaw of temperarnent but a moral"
                        ),
                        confidence=0.95,
                    ),
                    OCRLine(
                        text=(
                            "position: he refuses to act until the ground of "
                            "the act is known."
                        ),
                        confidence=0.96,
                    ),
                    OCRLine(text="Page 1 of 1"),
                ]
            )

    typer = WatchedTyper(backend, ContinuationOCR())

    result = await typer.type_text(continuation, prose=True)

    assert result.used_fast_path is True
    assert result.verdict == "match"
    assert result.ok is True
    assert result.field_text.startswith("convenience the court")
    assert result.field_text.endswith("the act is known.")
    _assert_no_enter(backend)


async def test_exact_fast_editor_append_localizes_suffix_from_full_document() -> None:
    """A proven editor prefix must not make an exact appended suffix ambiguous."""

    backend = FakeBackend()
    backend.guarded_exact_print = True  # type: ignore[attr-defined]
    prefix = (
        "Ambition first gives Macbeth's imagination a private image of "
        "kingship, and that "
        "image begins to displace his loyalty and judgment."
    )
    continuation = (
        " Lady Macbeth strengthens that ambition by challenging his courage "
        "and persuading him that murder is the only path to the crown. "
        "Although Macbeth knows Duncan is a generous king and loyal guest, "
        "his ambition overwhelms his conscience. He orders Banquo's murder"
    )
    full_document = prefix + continuation
    wrapped_document = full_document.replace(
        "loyalty and judgment. Lady Macbeth",
        "loyalty and judgment.\nLady Macbeth",
    ).replace(
        "generous king and loyal guest, his ambition",
        "generous king and loyal guest,\nhis ambition",
    ).replace("'", "’")

    typer = WatchedTyper(
        backend,
        ScriptedOCR(""),
    )

    async def full_field_read(
        region: Region,
        *,
        intended: str | None = None,
        precise: bool = False,
        allow_semantic_spacing: bool = False,
        allow_blind_fallback: bool = False,
        minimum_confidence: float = 0.78,
    ) -> str:
        del (
            region,
            intended,
            precise,
            allow_semantic_spacing,
            allow_blind_fallback,
            minimum_confidence,
        )
        return wrapped_document

    async def unexpected_screen_read(*, precise: bool = False) -> OCRResult:
        del precise
        raise AssertionError(
            "grounded field evidence must be localized before screen OCR"
        )

    typer._read_field = full_field_read  # type: ignore[method-assign]
    typer._read_screen = unexpected_screen_read  # type: ignore[method-assign]

    result = await typer.type_text(
        continuation,
        region=Region(x=10, y=10, width=900, height=300),
        exact=True,
        context="editor",
    )

    assert result.used_fast_path is True
    assert result.status == "verified_exact"
    assert result.field_text == continuation
    assert result.emitted_characters == len(continuation)
    assert result.emitted_exactly_once is True
    assert (
        "keypress",
        {"keys": ["ControlLeft", "Home"]},
    ) not in backend.calls
    _assert_no_enter(backend)


async def test_exact_short_editor_suffix_proves_its_leading_word_boundary() -> None:
    """Full-document pixels can prove one OCR-elided continuation space."""

    backend = FakeBackend()
    intended = " his tragic downfall."
    typer = WatchedTyper(backend, ScriptedOCR(""))
    screen_reads = 0

    async def boundary_blind_field_read(
        region: Region,
        *,
        intended: str | None = None,
        **_kwargs,
    ) -> str:
        del region
        return (intended or "").lstrip()

    async def complete_document_screen(
        *,
        precise: bool = False,
    ) -> OCRResult:
        nonlocal screen_reads
        del precise
        screen_reads += 1
        return OCRResult(
            lines=[
                OCRLine(
                    text=(
                        "By the end, ambition directs every choice toward "
                        "ruin and brings his tragic downfall."
                    ),
                    confidence=0.99,
                )
            ]
        )

    typer._read_field = boundary_blind_field_read  # type: ignore[method-assign]
    typer._read_screen = complete_document_screen  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=900, height=300),
        prose=True,
        exact=True,
        context="editor",
    )

    assert result.used_fast_path is False
    assert result.status == "verified_exact"
    assert result.field_text == intended
    assert result.emitted_exactly_once is True
    assert screen_reads == 1
    _assert_no_enter(backend)


async def test_exact_fast_editor_canonicalizes_visual_word_wrap() -> None:
    """A soft visual wrap must not turn exact prose into a raw-hash mismatch."""

    backend = FakeBackend()
    backend.guarded_exact_print = True  # type: ignore[attr-defined]
    intended = (
        "Ambition drives Macbeth by transforming a celebrated soldier's "
        "desire for honor into a consuming need for power. After hearing the "
        "witches predict that he will be king, he begins to imagine murder as "
        "a shortcut to the crown. Although he hesitates"
    )
    wrapped = intended.replace(
        "he will be king, he begins",
        "he will be king, he\nbegins",
    )
    typer = WatchedTyper(backend, ScriptedOCR(""))

    async def wrapped_field_read(
        region: Region,
        *,
        intended: str | None = None,
        precise: bool = False,
        allow_semantic_spacing: bool = False,
        allow_blind_fallback: bool = False,
        minimum_confidence: float = 0.78,
    ) -> str:
        del (
            region,
            intended,
            precise,
            allow_semantic_spacing,
            allow_blind_fallback,
            minimum_confidence,
        )
        return wrapped

    async def unexpected_screen_read(*, precise: bool = False) -> OCRResult:
        del precise
        raise AssertionError(
            "grounded exact editor readback must avoid full-screen OCR"
        )

    typer._read_field = wrapped_field_read  # type: ignore[method-assign]
    typer._read_screen = unexpected_screen_read  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=900, height=300),
        exact=True,
        context="editor",
    )

    assert result.used_fast_path is True
    assert result.status == "verified_exact"
    assert result.field_text == intended
    assert result.emitted_exactly_once is True
    _assert_no_enter(backend)


async def test_long_exact_editor_moves_caret_and_restores_document_end() -> None:
    """A suffix caret is read-only stabilized without changing append position."""

    backend = FakeBackend()
    backend.guarded_exact_print = True  # type: ignore[attr-defined]
    intended = (
        "Macbeth treats prophecy as permission to force the future, while "
        "each violent choice makes retreat more difficult and costly."
    )
    assert len(intended) > FAST_PRINT_MIN
    typer = WatchedTyper(backend, ScriptedOCR(""))

    async def caret_sensitive_field_read(
        region: Region,
        *,
        intended: str | None = None,
        **_kwargs,
    ) -> str:
        del region
        keypresses = [
            kwargs["keys"]
            for method, kwargs in backend.calls
            if method == "keypress"
        ]
        if ["ControlLeft", "Home"] in keypresses:
            return intended or ""
        return f"{intended or ''}|"

    async def unexpected_screen_read(*, precise: bool = False) -> OCRResult:
        del precise
        raise AssertionError(
            "caret-stabilized editor readback must avoid full-screen OCR"
        )

    typer._read_field = caret_sensitive_field_read  # type: ignore[method-assign]
    typer._read_screen = unexpected_screen_read  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=900, height=300),
        exact=True,
        context="editor",
    )

    keypresses = [
        kwargs["keys"]
        for method, kwargs in backend.calls
        if method == "keypress"
    ]
    assert result.status == "verified_exact"
    assert result.emitted_exactly_once is True
    assert keypresses[-2:] == [
        ["ControlLeft", "Home"],
        ["ControlLeft", "End"],
    ]
    _assert_no_enter(backend)


async def test_caps_lock_disables_fast_path() -> None:
    backend = FakeBackend()
    backend.caps_lock = True
    prose = (
        "This is a long, plain sentence with no special symbols so it should "
        "be eligible for the fast path were caps lock not engaged."
    )
    assert len(prose) > FAST_PRINT_MIN

    orig_type = backend.type_text

    async def typing(text: str, *, code: bool = False, secret: bool = False) -> None:
        await orig_type(text, code=code, secret=secret)
        backend.set_screen("typed " + text[:6])

    backend.type_text = typing  # type: ignore[method-assign]

    ocr = ScriptedOCR(prose)
    typer = WatchedTyper(backend, ocr)

    result = await typer.type_text(prose)
    assert result.used_fast_path is False
    assert any(m == "type_text" for m, _ in backend.calls)
    assert not any(m == "print_text" for m, _ in backend.calls)
    _assert_no_enter(backend)


# --------------------------------------------------------------------------- #
# layout self-correction
# --------------------------------------------------------------------------- #


async def test_layout_slip_triggers_single_correction_no_enter() -> None:
    backend = FakeBackend()  # starts on "us"
    intended = "ls | sort"  # precise (pipe + command head) — symbols load-bearing

    # Drive the screen on each chunk so the field auto-locates.
    orig_type = backend.type_text

    async def typing(text: str, *, code: bool = False, secret: bool = False) -> None:
        await orig_type(text, code=code, secret=secret)
        backend.set_screen("cmd " + text)

    backend.type_text = typing  # type: ignore[method-assign]

    # First read shows the layout slip (| → ~), second (post-correction) reads clean.
    ocr = ScriptedOCR("ls ~ sort", "ls | sort")
    typer = WatchedTyper(backend, ocr)

    result = await typer.type_text(intended, region=Region(x=10, y=10, width=400, height=40))

    assert result.corrected is True
    assert backend.layout == "uk"  # flipped from us
    assert result.verdict == "match"
    # The clear-for-retype removes only the fresh input, preserving prior text.
    pressed = [kw.get("code") for m, kw in backend.calls if m == "press_key"]
    assert pressed.count("Backspace") == len(intended)
    assert "Home" not in pressed
    _assert_no_enter(backend)


async def test_case_only_slip_toggles_caps_lock_and_retypes_once() -> None:
    backend = FakeBackend()
    intended = "HARNESSE2E42"
    ocr = ScriptedOCR("harnesse2e42", intended)
    typer = WatchedTyper(backend, ocr)

    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=400, height=40),
        code=True,
    )

    assert result.corrected is True
    assert result.verdict == "match"
    assert backend.layout == "us"
    pressed = [kw.get("code") for method, kw in backend.calls if method == "press_key"]
    assert pressed.count("CapsLock") == 1
    _assert_no_enter(backend)


async def test_corrected_but_ambiguous_readback_never_claims_verified() -> None:
    backend = FakeBackend()
    intended = "HARNESSE2E42"
    ocr = ScriptedOCR("harnesse2e42", "HARNESSF2E42")
    typer = WatchedTyper(backend, ocr)

    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=400, height=40),
        code=True,
    )

    assert result.corrected is True
    assert result.status == "unverified_ambiguous"
    assert "verified" not in result.summary.lower()


async def test_autolocate_retries_once_for_delayed_video_update() -> None:
    backend = FakeBackend()
    intended = "HARNESSE2E42"
    ocr = ScriptedOCR(intended)
    typer = WatchedTyper(backend, ocr)
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[10:13, 20:24] = 200
    grids = [flat, flat, changed.reshape(-1)]

    async def delayed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = delayed_grid  # type: ignore[method-assign]

    result = await typer.type_text(intended, code=True)

    assert result.status != "failed_focus_lost"
    assert result.verdict == "match"
    assert result.ok is True


@pytest.mark.parametrize(
    ("intended", "code", "stale_frame_count"),
    [
        ("2. Act", False, 1),
        ("alpha  beta", True, 5),
    ],
)
async def test_short_exact_editor_retries_delayed_video_before_unverified(
    monkeypatch: pytest.MonkeyPatch,
    intended: str,
    code: bool,
    stale_frame_count: int,
) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    backend = FakeBackend()
    ocr = ScriptedOCR(intended)
    typer = WatchedTyper(backend, ocr)
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[10:13, 20:24] = 200
    grids = [
        flat,
        *[flat] * stale_frame_count,
        changed.reshape(-1),
    ]

    async def delayed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = delayed_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        code=code,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact"
    assert result.typed_characters == len(intended)
    assert [
        call["text"]
        for method, call in backend.calls
        if method == "type_text"
    ] == [intended]


async def test_short_exact_editor_uses_precise_ocr_when_grid_misses_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    backend = FakeBackend()
    intended = "2. Act"

    class PreciseGroundedOCR:
        def __init__(self) -> None:
            self.precise_calls = 0

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            return OCRResult()

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            self.precise_calls += 1
            return OCRResult(
                lines=[
                    OCRLine(
                        text=intended,
                        confidence=0.99,
                        bbox=[20, 100, 100, 122],
                    )
                ],
                spacing_evidence="verified",
            )

    ocr = PreciseGroundedOCR()
    typer = WatchedTyper(backend, ocr)
    flat = _flat_grid()

    async def unchanged_grid() -> np.ndarray:
        return flat

    typer._grid = unchanged_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact"
    assert ocr.precise_calls >= 1
    assert [
        call["text"]
        for method, call in backend.calls
        if method == "type_text"
    ] == [intended]


async def test_autolocate_waits_for_second_delayed_remote_video_update() -> None:
    """A remote Word repaint can arrive later than the first 200 ms sample."""

    backend = FakeBackend()
    intended = "That lie is a"
    ocr = ScriptedOCR("", intended)
    typer = WatchedTyper(backend, ocr)
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[10:13, 20:24] = 200
    grids = [
        flat,  # before input
        flat,  # immediate post-HID capture
        flat,  # first delayed VNC frame
        changed.reshape(-1),  # second delayed VNC frame
    ]

    async def delayed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = delayed_grid  # type: ignore[method-assign]

    result = await typer.type_text(intended, code=True)

    assert result.status == "verified_exact"
    assert result.typed_characters == len(intended)
    assert result.ok is True
    typed = [
        call["text"]
        for method, call in backend.calls
        if method == "type_text"
    ]
    assert typed == [intended]


async def test_autolocate_waits_for_third_delayed_remote_video_update() -> None:
    """A busy VNC guest may publish the first glyphs after both quick retries."""

    backend = FakeBackend()
    intended = "A useful"
    ocr = ScriptedOCR(intended)
    typer = WatchedTyper(backend, ocr)
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[10:13, 20:24] = 200
    grids = [
        flat,  # before input
        flat,  # immediate post-HID capture
        flat,  # 200 ms retry
        flat,  # 450 ms retry
        changed.reshape(-1),  # final bounded slow-video retry
    ]

    async def delayed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = delayed_grid  # type: ignore[method-assign]

    result = await typer.type_text(intended, code=True)

    assert result.status == "verified_exact"
    assert result.typed_characters == len(intended)
    typed = [
        call["text"]
        for method, call in backend.calls
        if method == "type_text"
    ]
    assert typed == [intended]


async def test_autolocate_waits_for_bounded_very_slow_vnc_update() -> None:
    """The disposable Windows VNC path can trail the first HID chunk by seconds."""

    backend = FakeBackend()
    intended = "Every action"
    ocr = ScriptedOCR(intended)
    typer = WatchedTyper(backend, ocr)
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[10:13, 20:24] = 200
    grids = [
        flat,  # before input
        flat,  # immediate post-HID capture
        flat,  # 200 ms retry
        flat,  # 450 ms retry
        flat,  # 1 second retry
        changed.reshape(-1),  # bounded very-slow-video retry
    ]

    async def delayed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = delayed_grid  # type: ignore[method-assign]

    result = await typer.type_text(intended, code=True)

    assert result.status == "verified_exact"
    assert result.typed_characters == len(intended)
    typed = [
        call["text"]
        for method, call in backend.calls
        if method == "type_text"
    ]
    assert typed == [intended]


async def test_autolocate_uses_grounded_ocr_when_video_grid_misses_text() -> None:
    backend = FakeBackend()
    intended = "HARNESSE2E42"

    class GroundedOCR:
        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return OCRResult(
                lines=[
                    OCRLine(
                        text=f"existing text {intended}",
                        confidence=0.99,
                        bbox=[20, 100, 420, 132],
                    )
                ]
            )

    typer = WatchedTyper(backend, GroundedOCR())
    flat = _flat_grid()

    async def unchanged_grid() -> np.ndarray:
        return flat

    typer._grid = unchanged_grid  # type: ignore[method-assign]

    result = await typer.type_text(intended, code=True)

    assert result.status == "unverified_wrong_region"
    assert result.ok is True  # legacy: ambiguous, not a confirmed mismatch
    assert result.field_text == f"existing text {intended}"


async def test_fast_prose_autolocates_after_word_smartens_apostrophe() -> None:
    """The first changed Word chunk may exist only in OCR with a curly quote."""

    backend = FakeBackend()
    intended = (
        " Banquo's murder follows the same logic without the same excuse: no "
        "prophecy demands it, only Macbeth's wish to keep what the first crime "
        "won. Each killing leaves the next choice narrower."
    )
    smart_read = intended.replace("'", "’")

    class SmartQuoteOCR:
        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return OCRResult(
                lines=[
                    OCRLine(
                        text=smart_read,
                        confidence=0.97,
                        bbox=[440, 630, 820, 690],
                    )
                ]
            )

    typer = WatchedTyper(backend, SmartQuoteOCR())
    flat = _flat_grid()

    async def unchanged_grid() -> np.ndarray:
        return flat

    typer._grid = unchanged_grid  # type: ignore[method-assign]

    result = await typer.type_text(intended, prose=True)

    assert result.used_fast_path is True
    assert result.status == "verified_exact"
    assert result.typed_characters == len(intended)
    assert result.ok is True


async def test_autolocate_refines_dynamic_results_panel_to_typed_field() -> None:
    intended = "Notepad"

    class DynamicResultsBackend(FakeBackend):
        async def type_text(
            self,
            text: str,
            *,
            code: bool = False,
            secret: bool = False,
        ) -> None:
            await super().type_text(text, code=code, secret=secret)
            image = Image.new("RGB", (1280, 720), (24, 28, 36))
            draw = ImageDraw.Draw(image)
            draw.rectangle((20, 80, 500, 370), fill=(56, 60, 68))
            draw.text((40, 100), intended, fill=(240, 240, 240))
            output = io.BytesIO()
            image.save(output, "PNG")
            self.set_frame_bytes(output.getvalue())

    class DynamicResultsOCR:
        def __init__(self) -> None:
            self.regions: list[Region | None] = []

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            self.regions.append(region)
            if region is None:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=intended,
                            confidence=0.96,
                            bbox=[40, 100, 105, 118],
                        )
                    ]
                )
            if region.height <= 50:
                return OCRResult(
                    lines=[OCRLine(text=intended, confidence=0.96)]
                )
            return OCRResult(
                lines=[OCRLine(text="Typed.", confidence=0.94)]
            )

    backend = DynamicResultsBackend()
    ocr = DynamicResultsOCR()
    result = await WatchedTyper(backend, ocr).type_text(
        intended,
        code=True,
    )

    assert result.status == "verified_exact"
    assert result.field_text == intended
    assert None in ocr.regions
    assert any(
        region is not None and region.height <= 50
        for region in ocr.regions
    )
    _assert_no_enter(backend)


async def test_autolocate_uses_dimensions_from_the_first_captured_frame() -> None:
    """A post-reboot resolution change must not move the OCR crop off-screen."""

    intended = "ms-settings:about"
    actual_width, actual_height = 1280, 800

    class PostRebootBackend(FakeBackend):
        guarded_exact_print = True

        def __init__(self) -> None:
            # Model the PiKVM websocket's stale pre-capture dimensions.
            super().__init__(width=1920, height=1080, layout="uk")
            self._frame = self._render(typed=False)

        @staticmethod
        def _render(*, typed: bool) -> bytes:
            image = Image.new(
                "RGB",
                (actual_width, actual_height),
                (24, 28, 36),
            )
            if typed:
                draw = ImageDraw.Draw(image)
                draw.rectangle(
                    (48, 676, 248, 704),
                    fill=(246, 246, 246),
                )
                draw.text(
                    (52, 684),
                    intended,
                    fill=(24, 24, 24),
                )
            output = io.BytesIO()
            image.save(output, "PNG")
            return output.getvalue()

        async def screenshot(
            self,
            region: Region | None = None,
        ):
            del region
            # The first real frame corrects the backend's cached dimensions.
            self.dims = {
                "width": actual_width,
                "height": actual_height,
            }
            return to_captured_frame(
                self._frame,
                actual_width,
                actual_height,
            )

        async def type_text(
            self,
            text: str,
            *,
            code: bool = False,
            secret: bool = False,
        ) -> None:
            await super().type_text(text, code=code, secret=secret)
            self._frame = self._render(typed=True)

    class InBoundsFieldOCR:
        def __init__(self) -> None:
            self.regions: list[Region | None] = []

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region=region)

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            self.regions.append(region)
            if (
                region is not None
                and region.y < actual_height
                and region.y + region.height > 676
            ):
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=intended,
                            confidence=0.99,
                        )
                    ],
                    spacing_evidence="verified",
                )
            return OCRResult()

    backend = PostRebootBackend()
    ocr = InBoundsFieldOCR()
    result = await WatchedTyper(backend, ocr).type_text(
        intended,
        exact=True,
        context="field",
    )

    assert result.status == "verified_exact"
    assert result.field_text == intended
    assert any(
        region is not None
        and 0 <= region.y < actual_height
        and region.y + region.height <= actual_height
        for region in ocr.regions
    )
    _assert_no_enter(backend)


# --------------------------------------------------------------------------- #
# truncated read-back — never a destructive retype
# --------------------------------------------------------------------------- #


async def test_truncated_readback_is_unverified_not_corrected() -> None:
    backend = FakeBackend()
    intended = "the quick brown fox jumps over the lazy dog"  # plain prose

    orig_type = backend.type_text

    async def typing(text: str, *, code: bool = False, secret: bool = False) -> None:
        await orig_type(text, code=code, secret=secret)
        backend.set_screen("field " + text[:6])

    backend.type_text = typing  # type: ignore[method-assign]

    # OCR only ever sees a strict prefix (viewport truncation).
    ocr = ScriptedOCR("the quick brown")
    typer = WatchedTyper(backend, ocr)

    result = await typer.type_text(
        intended, region=Region(x=10, y=10, width=400, height=40)
    )

    assert result.verdict == "unverified"
    assert result.corrected is False
    assert result.ok is True  # unverified is not a hard failure
    # No destructive clear: no Delete/Backspace keys were pressed.
    pressed = [kw.get("code") for m, kw in backend.calls if m == "press_key"]
    assert "Delete" not in pressed and "Backspace" not in pressed
    _assert_no_enter(backend)


async def test_precise_prefix_gets_one_settled_reread_before_failing_closed() -> None:
    backend = FakeBackend()
    intended = "ls ~ ~/D* ~/V*"
    prefix = "ls ~ ~/D*"
    ocr = ScriptedOCR(prefix, prefix, intended)
    typer = WatchedTyper(backend, ocr)

    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=500, height=50),
        code=True,
    )

    assert result.status == "verified_exact"
    assert result.field_text == intended
    assert result.corrected is False
    assert ocr.calls == 3
    _assert_no_enter(backend)


async def test_precise_prefix_gets_second_bounded_settled_reread() -> None:
    backend = FakeBackend()
    intended = "ls ~ ~/D* ~/V*"
    prefix = "ls ~ ~/D*"
    ocr = ScriptedOCR(prefix, prefix, prefix, intended)
    typer = WatchedTyper(backend, ocr)

    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=500, height=50),
        code=True,
    )

    assert result.status == "verified_exact"
    assert result.field_text == intended
    assert result.corrected is False
    assert ocr.calls == 4
    _assert_no_enter(backend)


async def test_precise_prefix_gets_third_bounded_settled_reread() -> None:
    """A lagging VNC framebuffer may publish the final word after 0.9 seconds."""

    backend = FakeBackend()
    intended = "gsettings set org.gnome.settings-daemon.plugins.power idle-dim false"
    prefix = intended.removesuffix("m false")
    ocr = ScriptedOCR(prefix, prefix, prefix, prefix, prefix, intended)
    typer = WatchedTyper(backend, ocr)

    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=800, height=50),
        code=True,
    )

    assert result.status == "verified_exact"
    assert result.field_text == intended
    assert result.emitted_exactly_once is True
    assert ocr.calls == 6
    _assert_no_enter(backend)


async def test_simple_terminal_argv_accepts_only_safe_whitespace_normalization() -> None:
    class UncertainSpacingOCR:
        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(
                        text=(
                            "user@host:~$  gsettings   set "
                            "org.gnome.settings-daemon.plugins.power "
                            "idle-dim false"
                        ),
                        confidence=0.99,
                    )
                ],
                spacing_evidence="uncertain",
            )

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr(image_path, region)

    backend = FakeBackend()
    intended = "gsettings set org.gnome.settings-daemon.plugins.power idle-dim false"

    result = await WatchedTyper(backend, UncertainSpacingOCR()).type_text(
        intended,
        region=Region(x=10, y=10, width=800, height=50),
        code=True,
        context="terminal",
    )

    assert result.status == "verified_safe_normalized"
    assert result.verdict == "match"
    assert result.emitted_exactly_once is True
    _assert_no_enter(backend)


async def test_terminal_prefix_normalization_cannot_verify_a_stale_final_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    intended = "gsettings set org.gnome.settings-daemon.plugins.power idle-dim false"
    first_chunk = chunk_text(intended)[0]

    class StalePrefixOCR:
        def __init__(self) -> None:
            self.calls = 0

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            self.calls += 1
            text = first_chunk if self.calls == 1 else intended.removesuffix("m false")
            return OCRResult(
                lines=[OCRLine(text=text, confidence=0.99)],
                spacing_evidence="uncertain",
            )

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr(image_path, region)

    backend = FakeBackend()

    result = await WatchedTyper(backend, StalePrefixOCR()).type_text(
        intended,
        region=Region(x=10, y=10, width=800, height=50),
        code=True,
        context="terminal",
    )

    assert result.status.startswith("unverified_")
    assert result.verdict == "unverified"
    assert result.emitted_exactly_once is True
    _assert_no_enter(backend)


@pytest.mark.parametrize(
    (
        "line_bbox",
        "line_suffix",
        "poison_readback",
        "full_screen_misses",
        "expected_status",
    ),
    [
        pytest.param(
            [20, 72, 1040, 100],
            "",
            False,
            0,
            "verified_exact",
            id="grounded-complete-line",
        ),
        pytest.param(
            [20, 72, 1040, 100],
            "",
            False,
            1,
            "verified_exact",
            id="delayed-full-screen-frame",
        ),
        pytest.param(
            [20, 72, 1040, 100],
            "",
            True,
            0,
            "verified_exact",
            id="causal-delta-recovers-poisoned-crop",
        ),
        pytest.param(
            [20, 400, 1040, 428],
            "",
            False,
            0,
            "unverified_ambiguous",
            id="matching-text-elsewhere",
        ),
        pytest.param(
            [20, 72, 1040, 100],
            "x",
            False,
            0,
            "unverified_ambiguous",
            id="extra-suffix",
        ),
    ],
)
@pytest.mark.parametrize("context", ["terminal", "editor"])
async def test_exact_readback_recovers_only_from_grounded_complete_line(
    monkeypatch: pytest.MonkeyPatch,
    line_bbox: list[int],
    line_suffix: str,
    poison_readback: bool,
    full_screen_misses: int,
    expected_status: str,
    context: str,
) -> None:
    """A fresh grounded full-screen line can rescue a bad inferred OCR crop."""

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    if poison_readback:
        monkeypatch.setattr(
            "pikvm_agent.executor.typing.readback_region",
            lambda *_args, **_kwargs: Region(
                x=560,
                y=290,
                width=80,
                height=24,
            ),
        )
    intended = "gsettings set org.gnome.settings-daemon.plugins.power idle-dim false"

    class VisibleTerminalBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.visible = ""

        async def type_text(
            self,
            text: str,
            *,
            code: bool = False,
            secret: bool = False,
        ) -> None:
            await super().type_text(text, code=code, secret=secret)
            self.visible += text
            self.set_screen(self.visible)

    class EmptyCropExactScreenOCR:
        def __init__(self, backend: VisibleTerminalBackend) -> None:
            self.backend = backend
            self.full_screen_precise_calls = 0

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is not None:
                return OCRResult()
            return OCRResult()

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is not None:
                return OCRResult()
            self.full_screen_precise_calls += 1
            if self.full_screen_precise_calls <= full_screen_misses:
                return OCRResult()
            return OCRResult(
                lines=[
                    OCRLine(
                        text=(
                            "user@vm:~$ "
                            f"{self.backend.visible}{line_suffix}"
                        ),
                        confidence=0.96,
                        bbox=line_bbox,
                    )
                ],
                spacing_evidence="uncertain",
            )

    backend = VisibleTerminalBackend()
    ocr = EmptyCropExactScreenOCR(backend)

    result = await WatchedTyper(backend, ocr).type_text(
        intended,
        code=True,
        context=context,
    )

    assert result.status == expected_status
    assert result.field_text == (
        intended if expected_status == "verified_exact" else ""
    )
    assert result.emitted_exactly_once is True
    assert len(result.readback_frame_sha256) == 64
    expected_calls = (
        full_screen_misses + 1
        if expected_status == "verified_exact"
        else 3
    )
    assert ocr.full_screen_precise_calls == expected_calls
    _assert_no_enter(backend)


async def test_exact_editor_readback_grounds_to_secondary_causal_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editor status-bar churn must not hide the exact changed text row."""

    async def no_sleep(_seconds: float) -> None:
        return None

    status_region = Region(x=72, y=474, width=88, height=30)
    text_region = Region(x=36, y=160, width=84, height=54)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        "pikvm_agent.executor.typing.readback_region",
        lambda *_args, **_kwargs: status_region,
    )
    monkeypatch.setattr(
        "pikvm_agent.executor.typing.locate_capture_change",
        lambda *_args, **_kwargs: status_region,
    )
    monkeypatch.setattr(
        "pikvm_agent.executor.typing.locate_dense_changed_candidates",
        lambda *_args, **_kwargs: [status_region, text_region],
    )

    class VisibleEditorBackend(FakeBackend):
        async def type_text(
            self,
            text: str,
            *,
            code: bool = False,
            secret: bool = False,
        ) -> None:
            await super().type_text(text, code=code, secret=secret)
            self.set_screen(text)

    class ExactScreenOCR:
        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            return OCRResult()

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is not None:
                return OCRResult()
            return OCRResult(
                lines=[
                    OCRLine(
                        text="Proof",
                        confidence=0.99,
                        bbox=[42, 190, 78, 207],
                    )
                ]
            )

    backend = VisibleEditorBackend()
    result = await WatchedTyper(backend, ExactScreenOCR()).type_text(
        "Proof",
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact"
    assert result.field_text == "Proof"
    assert result.emitted_exactly_once is True
    _assert_no_enter(backend)


async def test_terminal_wrapped_readback_reocrs_the_causal_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        "pikvm_agent.executor.typing.readback_region",
        lambda *_args, **_kwargs: Region(
            x=560,
            y=290,
            width=80,
            height=24,
        ),
    )
    intended = "gsettings set org.gnome.settings-daemon.plugins.power idle-dim false"

    class WrappedTerminalBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.visible = ""

        async def type_text(
            self,
            text: str,
            *,
            code: bool = False,
            secret: bool = False,
        ) -> None:
            await super().type_text(text, code=code, secret=secret)
            self.visible += text
            self.set_screen(self.visible)

    class WrappedTerminalOCR:
        def __init__(self) -> None:
            self.full_screen_calls = 0
            self.grounded_crop_calls = 0

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            return OCRResult()

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is None:
                self.full_screen_calls += 1
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=(
                                "J user@vm:~$ "
                                "gsettings set org.gnome.settings-daemon.p"
                            ),
                            confidence=0.72,
                            bbox=[9, 72, 1259, 100],
                        ),
                        OCRLine(
                            text="4 lugins.power idle-dim falseI",
                            confidence=0.71,
                            bbox=[10, 100, 533, 128],
                        ),
                    ]
                )
            if region.y >= 200:
                return OCRResult()
            self.grounded_crop_calls += 1
            return OCRResult(
                lines=[
                    OCRLine(
                        text=(
                            "user@vm:~$ "
                            "gsettings set org.gnome.settings-daemon.p"
                        ),
                        confidence=0.89,
                    ),
                    OCRLine(
                        text="lugins.power idle-dim falsef",
                        confidence=0.60,
                    ),
                ],
                alternatives=[
                    OCRCandidate(
                        text=(
                            "user@vm:~$ "
                            "gsettings set org.gnome.settings-daemon.p"
                            "lugins.power\nidle-dim false"
                        )
                    ),
                    OCRCandidate(
                        text=(
                            "user@vm: S "
                            "gsettings set org.gnome.settings-daemon.p"
                            "lugins.power\nidle-dim false"
                        )
                    ),
                ],
            )

    backend = WrappedTerminalBackend()
    ocr = WrappedTerminalOCR()
    result = await WatchedTyper(backend, ocr).type_text(
        intended,
        code=True,
        context="terminal",
    )

    assert result.status == "verified_exact"
    assert result.field_text == intended
    assert result.emitted_exactly_once is True
    assert ocr.full_screen_calls == 1
    assert ocr.grounded_crop_calls == 1
    _assert_no_enter(backend)


async def test_quoted_terminal_command_keeps_exact_spacing_requirement() -> None:
    class UncertainSpacingOCR:
        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            return OCRResult(
                lines=[OCRLine(text="printf '%s' hello", confidence=0.99)],
                spacing_evidence="uncertain",
            )

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr(image_path, region)

    backend = FakeBackend()

    result = await WatchedTyper(backend, UncertainSpacingOCR()).type_text(
        "printf '%s' hello",
        region=Region(x=10, y=10, width=500, height=50),
        code=True,
        context="terminal",
    )

    assert result.status == "unverified_ambiguous"
    assert result.emitted_exactly_once is True
    _assert_no_enter(backend)


async def test_dropped_final_chunk_is_not_replayed_after_no_pixel_change() -> None:
    intended = "ffprobe -hide_banner /home/user/video.mp4"

    class DroppedTailBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.visible = ""
            self.tail_attempts = 0

        async def type_text(
            self,
            text: str,
            *,
            code: bool = False,
            secret: bool = False,
        ) -> None:
            await super().type_text(text, code=code, secret=secret)
            if text == ".mp4":
                self.tail_attempts += 1
                if self.tail_attempts == 1:
                    return
            self.visible += text
            self.set_screen(self.visible)

    class VisibleTextOCR:
        def __init__(self, backend: DroppedTailBackend) -> None:
            self.backend = backend

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return OCRResult(
                lines=[OCRLine(text=self.backend.visible)]
                if self.backend.visible
                else [],
                spacing_evidence="verified",
            )

    backend = DroppedTailBackend()
    typer = WatchedTyper(backend, VisibleTextOCR(backend))

    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=600, height=60),
        code=True,
    )

    assert backend.tail_attempts == 1
    assert backend.visible == intended.removesuffix(".mp4")
    assert result.field_text == intended.removesuffix(".mp4")
    assert result.status == "unverified_truncated"
    assert result.corrected is False
    assert result.delivery_retries == 0
    _assert_no_enter(backend)


async def test_ambiguous_partial_chunk_is_never_replayed_after_its_space_lands() -> None:
    """A stale read must not turn one chunk-boundary space into two."""

    intended = "alpha command beta gamma delta"
    chunks = chunk_text(intended)
    assert chunks == ["alpha command", " beta gamma delta"]

    class PartialBoundaryBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.visible = ""
            self.second_chunk_attempts = 0

        async def type_text(
            self,
            text: str,
            *,
            code: bool = False,
            secret: bool = False,
        ) -> None:
            await super().type_text(text, code=code, secret=secret)
            if text == chunks[1]:
                self.second_chunk_attempts += 1
                if self.second_chunk_attempts == 1:
                    self.visible += text[0]
                    return
            self.visible += text

    class TrailingSpaceBlindOCR:
        def __init__(self, backend: PartialBoundaryBackend) -> None:
            self.backend = backend

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            visible = self.backend.visible.rstrip(" ")
            return OCRResult(
                lines=[OCRLine(text=visible)] if visible else [],
                spacing_evidence="verified",
            )

    backend = PartialBoundaryBackend()
    typer = WatchedTyper(backend, TrailingSpaceBlindOCR(backend))

    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=600, height=60),
        code=True,
    )

    assert backend.second_chunk_attempts == 1
    assert backend.visible == "alpha command "
    assert "  " not in backend.visible
    assert result.delivery_retries == 0
    assert result.status == "unverified_truncated"
    assert result.emitted_characters == len(intended)
    assert result.emitted_sha256 == hashlib.sha256(intended.encode()).hexdigest()
    assert result.emitted_exactly_once is True
    assert len(result.readback_frame_sha256) == 64
    _assert_no_enter(backend)


async def test_at_most_once_emission_across_1000_stale_readbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale OCR may block typing, but must never duplicate its payload."""

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    rng = random.Random(20_260_727)
    vocabulary = (
        "agent",
        "checks",
        "desktop",
        "evidence",
        "field",
        "input",
        "remote",
        "screen",
        "text",
        "typing",
        "visible",
    )

    for case_index in range(1_000):
        words = [rng.choice(vocabulary) for _ in range(rng.randint(5, 14))]
        words[-1] = f"{words[-1]}-{case_index:04d}"
        intended = " ".join(words)
        chunks = chunk_text(intended)
        assert len(chunks) >= 2

        backend = FakeBackend()
        typer = WatchedTyper(backend, ScriptedOCR(""))
        flat = _flat_grid()

        async def unchanged_grid() -> np.ndarray:
            return flat

        async def stale_prefix(
            region: Region,
            *,
            intended: str | None = None,
            precise: bool = False,
            allow_semantic_spacing: bool = False,
            allow_blind_fallback: bool = False,
        ) -> str:
            del region, intended, precise, allow_semantic_spacing, allow_blind_fallback
            return chunks[0]

        typer._grid = unchanged_grid  # type: ignore[method-assign]
        typer._read_field = stale_prefix  # type: ignore[method-assign]

        result = await typer.type_text(
            intended,
            region=Region(x=10, y=10, width=600, height=60),
            code=True,
        )
        emitted = "".join(
            call["text"]
            for method, call in backend.calls
            if method == "type_text"
        )

        assert emitted == intended
        assert "  " not in emitted
        assert result.emitted_characters == len(intended)
        assert result.emitted_sha256 == hashlib.sha256(
            intended.encode()
        ).hexdigest()
        assert result.emitted_exactly_once is True
        assert result.delivery_retries == 0


async def test_low_confidence_ocr_cannot_trigger_destructive_retype() -> None:
    backend = FakeBackend()
    intended = "const retry = attempt < 3 ? 'retry' : 'stop';"
    typer = WatchedTyper(backend, LowConfidenceOCR())

    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=500, height=50),
        code=True,
    )

    assert result.verdict == "unverified"
    assert result.corrected is False
    assert not result.status.startswith("failed_")
    pressed = [kw.get("code") for method, kw in backend.calls if method == "press_key"]
    assert "Delete" not in pressed


async def test_precise_field_read_does_not_promote_an_intent_matching_candidate() -> None:
    intended = "https://docs.internal/runs/0040?view=screen&attempt=6"
    typer = WatchedTyper(FakeBackend(), AlternativeCandidateOCR(intended))

    observed = await typer._read_field(
        Region(x=10, y=10, width=500, height=50),
        intended=intended,
        precise=True,
    )

    assert observed == "https://docs. internal/runs/0040?view=screenkattempt=6"


async def test_precise_field_read_uses_the_provider_precision_profile() -> None:
    intended = "const exact = preserveSymbols('[]{}|&');"
    ocr = PreciseProfileOCR(intended)
    typer = WatchedTyper(FakeBackend(), ocr)

    observed = await typer._read_field(
        Region(x=10, y=10, width=500, height=50),
        intended=intended,
        precise=True,
    )

    assert observed == intended
    assert ocr.precise_calls == 1
    assert ocr.regular_calls == 0


async def test_precise_readback_refines_a_large_dialog_crop_to_its_field() -> None:
    intended = "taskmgr"

    class DelayedDialogOCR:
        def __init__(self) -> None:
            self.regions: list[Region | None] = []

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region=region)

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            self.regions.append(region)
            if len(self.regions) == 1:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="Open: | askmg",
                            confidence=0.44,
                            bbox=[18, 92, 118, 104],
                        ),
                        OCRLine(
                            text=(
                                "This task will be created with "
                                "administrative privileges."
                            ),
                            confidence=0.82,
                            bbox=[50, 110, 242, 119],
                        ),
                    ]
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=intended,
                        confidence=0.26,
                        bbox=[4, 2, 72, 12],
                    )
                ]
            )

    backend = FakeBackend(width=1280, height=800)
    ocr = DelayedDialogOCR()
    typer = WatchedTyper(backend, ocr)

    observed = await typer._read_field(
        Region(x=0, y=592, width=403, height=208),
        intended=intended,
        precise=True,
    )

    assert observed == intended
    assert len(ocr.regions) == 2
    refined = ocr.regions[1]
    assert refined is not None
    assert typer._refined_readback_region == refined
    assert refined.height <= 32
    assert refined.height >= 26
    assert refined.width >= 140
    assert 675 <= refined.y <= 678


def test_precise_readback_localizes_measured_save_as_filename_noise() -> None:
    intended = "text-01.txt"
    result = OCRResult(
        lines=[
            OCRLine(
                text="Filegame:",
                confidence=0.9817,
                bbox=[61, 22, 98, 32],
            ),
            OCRLine(
                text="text-01.bd",
                confidence=0.9577,
                bbox=[102, 21, 138, 31],
            ),
            OCRLine(
                text="Save as bvoec",
                confidence=0.7865,
                bbox=[55, 40, 98, 45],
            ),
            OCRLine(
                text='Tet documents (".bt)',
                confidence=0.9155,
                bbox=[100, 38, 174, 45],
            ),
        ]
    )

    refined = precise_readback_candidate_region(
        result,
        intended,
        Region(x=117, y=414, width=339, height=46),
        (1280, 800),
    )

    assert refined == Region(x=217, y=429, width=200, height=24)


async def test_uncalibrated_precise_ocr_cannot_verify_visible_spaces() -> None:
    intended = "exactly one space"

    class UncalibratedOCR:
        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            return OCRResult(lines=[OCRLine(text=intended, confidence=0.99)])

    observed = await WatchedTyper(
        FakeBackend(),
        UncalibratedOCR(),
    )._read_field(
        Region(x=10, y=10, width=500, height=50),
        intended=intended,
        precise=True,
    )

    assert observed == ""


async def test_precise_field_read_prioritizes_visible_spacing_mismatch() -> None:
    intended = "exactly one space"
    observed_with_extra_space = "exactly one  space"
    typer = WatchedTyper(
        FakeBackend(),
        SpacingCandidateOCR(intended, observed_with_extra_space),
    )

    observed = await typer._read_field(
        Region(x=10, y=10, width=500, height=50),
        intended=intended,
        precise=True,
    )

    assert observed == observed_with_extra_space


def test_precise_readback_retains_duplicate_suffix_before_hashing() -> None:
    assert WatchedTyper._typed_candidate(
        "quarterly earningss",
        "quarterly earnings",
        True,
    ) == "quarterly earningss"


def test_precise_filename_readback_excludes_only_known_save_as_type_chrome() -> None:
    intended = "text-01.txt"

    assert WatchedTyper._typed_candidate(
        f"{intended}\nText documents (*.txt)",
        intended,
        True,
    ) == intended
    assert WatchedTyper._typed_candidate(
        f"{intended}\nAll files (*.*)",
        intended,
        True,
    ) == intended
    assert WatchedTyper._typed_candidate(
        (
            f"File name: {intended}\n"
            "Save as type: Text documents (*.txt)"
        ),
        intended,
        True,
    ) == intended
    assert WatchedTyper._typed_candidate(
        f"File name: {intended}\nSave as type: All files (*.*)",
        intended,
        True,
    ) == intended
    assert WatchedTyper._typed_candidate(
        f"{intended}\nUnexpected adjacent text",
        intended,
        True,
    ) == f"{intended}\nUnexpected adjacent text"
    assert WatchedTyper._typed_candidate(
        (
            f"File name: {intended}t\n"
            "Save as type: Text documents (*.txt)"
        ),
        intended,
        True,
    ) == (
        f"File name: {intended}t\n"
        "Save as type: Text documents (*.txt)"
    )
    assert WatchedTyper._typed_candidate(
        f"Filename: {intended}\nSave as type: Text documents (*.txt)",
        intended,
        True,
    ) == (
        f"Filename: {intended}\n"
        "Save as type: Text documents (*.txt)"
    )


async def test_precise_ocr_noise_stops_without_destructive_retype() -> None:
    backend = FakeBackend()
    intended = "const retry = (attempt, limit) => attempt < limit;"
    # Representative high-confidence Tesseract substitutions from a small
    # monospace Windows editor crop. They do not prove the field is wrong.
    ocr = ScriptedOCR("const retry = (atteapt, Limit) => attempt < limit;")
    typer = WatchedTyper(backend, ocr)

    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=500, height=50),
        code=True,
    )

    assert result.verdict == "unverified"
    assert result.corrected is False
    assert result.status == "unverified_ambiguous"
    pressed = [
        kw.get("code")
        for method, kw in backend.calls
        if method == "press_key"
    ]
    assert "Delete" not in pressed


# --------------------------------------------------------------------------- #
# explicit region skips auto-locate
# --------------------------------------------------------------------------- #


async def test_explicit_region_skips_autolocate() -> None:
    backend = FakeBackend()
    intended = "hello there friend how are you doing today my friend"

    # The screen NEVER changes — auto-locate would find no field and (past
    # ABORT_MIN_CHARS) hard-fail "no focus". With an explicit region it must not.
    ocr = ScriptedOCR(intended)
    typer = WatchedTyper(backend, ocr)

    region = Region(x=5, y=5, width=300, height=30)
    result = await typer.type_text(intended, region=region)

    # Not a "no focus" failure — the explicit region was trusted and verified.
    assert result.status != "failed_focus_lost"
    assert result.verdict == "match"
    assert result.ok is True
    _assert_no_enter(backend)


# --------------------------------------------------------------------------- #
# interruptible HID (Layer 4): a long type stops mid-text when control changes
# --------------------------------------------------------------------------- #


async def test_type_text_interrupts_mid_text_and_releases() -> None:
    # A long string is typed in word-boundary chunks; if control is taken away
    # (should_continue flips False) the typer must STOP after the current chunk —
    # not run the whole string — and drop any held keys.
    backend = FakeBackend()
    backend.caps_lock = True  # force the humanized per-chunk path (not fast-print)
    intended = (
        "the quick brown fox jumps over the lazy dog while the agent keeps typing"
    )
    assert len(chunk_text(intended)) > 2  # several chunks, so "mid-text" is meaningful

    ocr = ScriptedOCR(intended)
    typer = WatchedTyper(backend, ocr)

    def gate() -> bool:
        # Allow exactly one chunk to land, then revoke control.
        typed = sum(1 for m, _ in backend.calls if m == "type_text")
        return typed < 1

    # Explicit region so the loop trusts focus (the static fake screen would otherwise
    # auto-locate to "no focus" before the gate is reached); we're testing the gate.
    region = Region(x=10, y=10, width=400, height=40)
    result = await typer.type_text(intended, region=region, should_continue=gate)

    typed_chunks = sum(1 for m, _ in backend.calls if m == "type_text")
    assert typed_chunks == 1  # stopped after the first chunk — not the whole string
    assert any(m == "release_all" for m, _ in backend.calls)  # held keys dropped
    assert result.status == "blocked_by_policy"
    assert result.ok is False
    assert result.typed_characters == len(chunk_text(intended)[0])
    assert result.intended_characters == len(intended)
    _assert_no_enter(backend)


async def test_typing_stops_before_next_chunk_after_out_of_field_screen_change() -> None:
    """A notification/focus steal between chunks must stop before more HID."""

    backend = FakeBackend()
    backend.caps_lock = True
    intended = (
        "the quick brown fox jumps over the lazy dog while focus changes"
    )
    chunks = chunk_text(intended)
    typer = WatchedTyper(backend, ScriptedOCR(""))
    base = _flat_grid().reshape(GRID_ROWS, GRID_COLS)
    field = base.copy()
    field[2:4, 2:7] = 200
    notification = field.copy()
    notification[14:17, 28:35] = 200
    grids = iter(
        [
            base.reshape(-1),
            field.reshape(-1),
            notification.reshape(-1),
        ]
    )

    async def changing_grid() -> np.ndarray:
        return next(grids, notification.reshape(-1))

    typer._grid = changing_grid  # type: ignore[method-assign]
    result = await typer.type_text(
        intended,
        region=Region(x=20, y=40, width=280, height=100),
        code=True,
    )

    typed = [
        call["text"]
        for method, call in backend.calls
        if method == "type_text"
    ]
    assert typed == [chunks[0]]
    assert result.status == "failed_focus_lost"
    assert result.typed_characters == len(chunks[0])
    assert result.intended_characters == len(intended)
    assert any(method == "release_all" for method, _ in backend.calls)
    _assert_no_enter(backend)


async def test_short_exact_field_text_uses_one_guarded_chunk() -> None:
    backend = FakeBackend()
    intended = "ms-settings:about"
    assert CHUNK_TARGET < len(intended) <= 20
    typer = WatchedTyper(backend, ScriptedOCR(intended))
    base = _flat_grid().reshape(GRID_ROWS, GRID_COLS)
    field = base.copy()
    field[14:16, 2:10] = 200
    grids = iter([base.reshape(-1), field.reshape(-1)])

    async def changing_grid() -> np.ndarray:
        return next(grids, field.reshape(-1))

    typer._grid = changing_grid  # type: ignore[method-assign]
    result = await typer.type_text(
        intended,
        code=True,
    )

    typed = [
        call["text"]
        for method, call in backend.calls
        if method == "type_text"
    ]
    assert typed == [intended]
    assert result.status == "verified_exact"


async def test_type_text_runs_to_completion_when_control_held() -> None:
    # The same gate, but control is never revoked — the whole string types normally.
    backend = FakeBackend()
    backend.caps_lock = True
    intended = "the quick brown fox jumps over the lazy dog"
    chunks = chunk_text(intended)

    ocr = ScriptedOCR(intended)
    typer = WatchedTyper(backend, ocr)

    region = Region(x=10, y=10, width=400, height=40)
    result = await typer.type_text(intended, region=region, should_continue=lambda: True)
    typed_chunks = sum(1 for m, _ in backend.calls if m == "type_text")
    assert typed_chunks == len(chunks)  # every chunk typed
    assert not any(m == "release_all" for m, _ in backend.calls)
    assert result.status != "blocked_by_policy"


async def test_fast_print_stops_between_chunks_when_control_is_revoked() -> None:
    """Regression: long prose used to be one uninterruptible print call."""
    backend = FakeBackend()
    intended = "long prose " * 20
    ocr = ScriptedOCR(intended)
    typer = WatchedTyper(backend, ocr)
    flat = _flat_grid()

    async def unchanged_grid() -> np.ndarray:
        return flat

    typer._grid = unchanged_grid  # type: ignore[method-assign]

    def gate() -> bool:
        return sum(method == "print_text" for method, _ in backend.calls) < 1

    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=500, height=50),
        should_continue=gate,
    )

    printed = [call["text"] for method, call in backend.calls if method == "print_text"]
    assert len(printed) == 1
    assert len(printed[0]) <= 16
    assert result.status == "blocked_by_policy"
    assert result.typed_characters == len(printed[0])
    assert result.intended_characters == len(intended)
    assert any(method == "release_all" for method, _ in backend.calls)


async def test_guarded_editor_print_uses_fewer_chunks_after_focus_probe() -> None:
    backend = FakeBackend()
    backend.guarded_exact_print = True  # type: ignore[attr-defined]
    intended = (
        "Ambition turns Macbeth's imagination toward power while the exact "
        "editor path retains visible evidence, cooperative cancellation, and "
        "a final byte-for-byte readback. "
    ) * 3
    typer = WatchedTyper(backend, ScriptedOCR(""))
    flat = _flat_grid()
    field_reads = 0

    async def unchanged_grid() -> np.ndarray:
        return flat

    async def exact_field_read(
        region: Region,
        *,
        intended: str | None = None,
        **_kwargs,
    ) -> str:
        nonlocal field_reads
        del region
        field_reads += 1
        return intended or ""

    typer._grid = unchanged_grid  # type: ignore[method-assign]
    typer._read_field = exact_field_read  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=900, height=300),
        prose=True,
        exact=True,
        context="editor",
    )

    printed = [
        call["text"]
        for method, call in backend.calls
        if method == "print_text"
    ]
    assert "".join(printed) == intended
    assert len(printed[0]) <= CHUNK_TARGET
    assert any(len(chunk) > CHUNK_TARGET for chunk in printed[1:])
    assert all(len(chunk) <= FAST_PRINT_CHUNK_TARGET for chunk in printed)
    assert len(printed) <= 10
    assert field_reads <= 4
    assert result.status == "verified_exact"
    assert result.emitted_exactly_once is True
    _assert_no_enter(backend)


async def test_fast_print_stops_after_out_of_field_screen_change() -> None:
    backend = FakeBackend()
    intended = "long prose for a watched destination " * 6
    assert len(intended) > FAST_PRINT_MIN
    chunks = chunk_text(intended)
    typer = WatchedTyper(backend, ScriptedOCR(""))
    base = _flat_grid().reshape(GRID_ROWS, GRID_COLS)
    field = base.copy()
    field[2:4, 2:7] = 200
    notification = field.copy()
    notification[14:17, 28:35] = 200
    grids = iter(
        [
            base.reshape(-1),
            field.reshape(-1),
            notification.reshape(-1),
        ]
    )

    async def changing_grid() -> np.ndarray:
        return next(grids, notification.reshape(-1))

    typer._grid = changing_grid  # type: ignore[method-assign]
    result = await typer.type_text(
        intended,
        region=Region(x=20, y=40, width=280, height=100),
    )

    printed = [
        call["text"]
        for method, call in backend.calls
        if method == "print_text"
    ]
    assert printed == [chunks[0]]
    assert result.used_fast_path is True
    assert result.status == "failed_focus_lost"
    assert result.typed_characters == len(chunks[0])
    assert result.intended_characters == len(intended)
    assert any(method == "release_all" for method, _ in backend.calls)


async def test_fast_print_relocates_after_verified_editor_page_reflow() -> None:
    backend = FakeBackend()
    intended = (
        "long prose remains grounded while a word processor paginates the "
        "document and moves the active line onto the next visible page. "
    ) * 2
    assert len(intended) > FAST_PRINT_MIN
    chunks = typing_module._guarded_print_chunks(intended)
    typer = WatchedTyper(backend, ScriptedOCR(""))
    base = _flat_grid().reshape(GRID_ROWS, GRID_COLS)
    first_line = base.copy()
    first_line[2:4, 2:7] = 200
    reflow = first_line.copy()
    reflow[14:17, 28:35] = 200
    next_page_line = reflow.copy()
    next_page_line[15:17, 29:34] = 240
    grids = iter(
        [
            base.reshape(-1),
            first_line.reshape(-1),
            reflow.reshape(-1),
            next_page_line.reshape(-1),
        ]
    )

    async def changing_grid() -> np.ndarray:
        return next(grids, next_page_line.reshape(-1))

    async def relocated_screen() -> OCRResult:
        first_word, remaining = chunks[0].strip().split(" ", 1)
        return OCRResult(
            lines=[
                OCRLine(
                    text=first_word,
                    confidence=0.99,
                    bbox=[370, 185, 420, 205],
                ),
                OCRLine(
                    text=remaining,
                    confidence=0.99,
                    bbox=[370, 205, 470, 225],
                ),
            ]
        )

    typer._grid = changing_grid  # type: ignore[method-assign]
    typer._read_screen = relocated_screen  # type: ignore[method-assign]

    def keep_two_chunks() -> bool:
        return (
            sum(method == "print_text" for method, _ in backend.calls) < 2
        )

    result = await typer.type_text(
        intended,
        should_continue=keep_two_chunks,
    )

    printed = [
        call["text"]
        for method, call in backend.calls
        if method == "print_text"
    ]
    assert printed == chunks[:2]
    assert result.status == "blocked_by_policy"
    assert result.typed_characters == len("".join(chunks[:2]))
    assert any(method == "release_all" for method, _ in backend.calls)
    _assert_no_enter(backend)


async def test_fast_print_rejects_unmoved_text_after_remote_notification() -> None:
    backend = FakeBackend()
    intended = (
        "long prose remains visible while an unrelated notification changes "
        "pixels elsewhere on the screen and must not authorize more input. "
    ) * 2
    assert len(intended) > FAST_PRINT_MIN
    chunks = chunk_text(intended)
    typer = WatchedTyper(backend, ScriptedOCR(""))
    base = _flat_grid().reshape(GRID_ROWS, GRID_COLS)
    first_line = base.copy()
    first_line[2:4, 2:7] = 200
    notification = first_line.copy()
    notification[14:17, 28:35] = 200
    grids = iter(
        [
            base.reshape(-1),
            first_line.reshape(-1),
            notification.reshape(-1),
        ]
    )

    async def changing_grid() -> np.ndarray:
        return next(grids, notification.reshape(-1))

    async def unchanged_text_screen() -> OCRResult:
        return OCRResult(
            lines=[
                OCRLine(
                    text=chunks[0].strip(),
                    confidence=0.99,
                    bbox=[40, 45, 220, 70],
                )
            ]
        )

    typer._grid = changing_grid  # type: ignore[method-assign]
    typer._read_screen = unchanged_text_screen  # type: ignore[method-assign]

    result = await typer.type_text(intended)

    printed = [
        call["text"]
        for method, call in backend.calls
        if method == "print_text"
    ]
    assert printed == chunks[:1]
    assert result.status == "failed_focus_lost"
    assert any(method == "release_all" for method, _ in backend.calls)
    _assert_no_enter(backend)


async def test_fast_print_mismatch_never_clears_and_replays_long_prose() -> None:
    backend = FakeBackend()
    intended = "long prose that must not be replayed after OCR mismatch " * 4
    chunks = typing_module._guarded_print_chunks(intended)
    typer = WatchedTyper(backend, ScriptedOCR("different visible content"))
    flat = _flat_grid()

    async def unchanged_grid() -> np.ndarray:
        return flat

    typer._grid = unchanged_grid  # type: ignore[method-assign]
    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=600, height=80),
    )

    printed = [
        call["text"]
        for method, call in backend.calls
        if method == "print_text"
    ]
    assert printed == chunks
    assert result.corrected is False
    assert result.status.startswith("unverified_")
    pressed = [
        call["code"]
        for method, call in backend.calls
        if method == "press_key"
    ]
    assert "Delete" not in pressed
    assert "Backspace" not in pressed
