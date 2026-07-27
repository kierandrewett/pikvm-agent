"""Offline tests for the watched, self-correcting typer.

Everything runs against :class:`FakeBackend` (records every HID call) and a tiny
scripted OCR provider — no network, no real screen, no real OCR. Covers chunking,
field localisation, the fast-print path + its caps-lock disable, a single layout
self-correction that never presses Enter, truncated read-backs, and the explicit
region path.
"""

from __future__ import annotations

import asyncio
import io
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
)
from pikvm_agent.pikvm.fake import FakeBackend

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
        return OCRResult(lines=[OCRLine(text=text)] if text else [])


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
        return OCRResult(lines=[OCRLine(text=self.intended)])


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

    assert result.status == "verified_exact"
    assert result.ok is True
    assert result.field_text == intended


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


async def test_dropped_final_chunk_is_retried_once_after_no_pixel_change() -> None:
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
                else []
            )

    backend = DroppedTailBackend()
    typer = WatchedTyper(backend, VisibleTextOCR(backend))

    result = await typer.type_text(
        intended,
        region=Region(x=10, y=10, width=600, height=60),
        code=True,
    )

    assert backend.tail_attempts == 2
    assert backend.visible == intended
    assert result.field_text == intended
    assert result.status == "verified_exact"
    assert result.corrected is True
    _assert_no_enter(backend)


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


async def test_precise_field_read_can_use_an_exact_independent_ocr_candidate() -> None:
    intended = "https://docs.internal/runs/0040?view=screen&attempt=6"
    typer = WatchedTyper(FakeBackend(), AlternativeCandidateOCR(intended))

    observed = await typer._read_field(
        Region(x=10, y=10, width=500, height=50),
        intended=intended,
        precise=True,
    )

    assert observed == intended


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
