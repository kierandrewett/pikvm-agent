"""Offline tests for the watched, self-correcting typer.

Everything runs against :class:`FakeBackend` (records every HID call) and a tiny
scripted OCR provider — no network, no real screen, no real OCR. Covers chunking,
field localisation, the fast-print path + its caps-lock disable, a single layout
self-correction that never presses Enter, truncated read-backs, and the explicit
region path.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from pikvm_agent.core.models import OCRLine, OCRResult, Region
from pikvm_agent.executor.typing import (
    CHUNK_TARGET,
    GRID_COLS,
    GRID_ROWS,
    FAST_PRINT_MIN,
    WatchedTyper,
    WatchedTypingResult,
    chunk_text,
    locate_changed_bbox,
)
from pikvm_agent.pikvm.fake import FakeBackend

_ENTER_KEYS = {"Enter", "NumpadEnter", "Return"}


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
    # The clear-for-retype used Home + Delete; no Backspace/Enter beyond the whitelist.
    pressed = [kw.get("code") for m, kw in backend.calls if m == "press_key"]
    assert "Home" in pressed
    assert all(c in {"Home", "Delete", "Backspace", "End"} for c in pressed)
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
