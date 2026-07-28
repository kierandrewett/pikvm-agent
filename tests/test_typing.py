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

from pikvm_agent.core.models import OCRCandidate, OCRLine, OCRResult, Region
from pikvm_agent.executor.typing import (
    CHUNK_TARGET,
    GRID_COLS,
    GRID_ROWS,
    FAST_PRINT_MIN,
    WatchedTyper,
    WatchedTypingResult,
    _substantial_change_outside_region,
    chunk_text,
    locate_changed_bbox,
    locate_dense_changed_bbox,
    readback_region,
)
from pikvm_agent.pikvm.fake import FakeBackend
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
        ) -> str:
            del region, intended, precise
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
    chunks = chunk_text(intended)
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
    chunks = chunk_text(intended)
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
