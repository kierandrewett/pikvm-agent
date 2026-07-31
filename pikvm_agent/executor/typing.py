"""Watched, self-correcting typer — the brain behind ``type_text``.

Ported faithfully from the battle-tested TypeScript ``src/agent/watched-typing.ts``
in ``~/dev/pikvm-desktop-agentic``. Types in humanized word-boundary chunks while
WATCHING the field: a cheap grayscale grid-diff after each chunk confirms the
keystrokes are landing AND auto-locates the field (bounding box of changed cells),
and at adaptive checkpoints a cropped image of the field is OCR'd and compared to
what we meant to type. A wrong keyboard layout (or other confident structural
mismatch) is self-corrected inline — at most once — without burning an agent turn.

It is a pure orchestrator: every side effect (keystrokes, capture, OCR, layout)
is reached through the injected ``backend``/``ocr``, so it is unit-testable and
imports no I/O of its own. It NEVER emits Enter — the only keys it may press for a
correction are Home / Delete / Backspace / End / CapsLock. A short field
readback may also move focus with Tab/Shift-Tab or select an existing draft with
Ctrl+A solely to remove a blinking caret from OCR. Committing is the caller's
job.

The verdict/classification logic lives in :mod:`pikvm_agent.executor.verification`
and is reused verbatim; this module owns only the *typing loop* + correction.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import math
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np
from PIL import Image
from pydantic import BaseModel

from pikvm_agent.core.models import (
    CapturedFrame,
    OCRLine,
    OCRResult,
    Region,
    VerificationResult,
    VerificationStatus,
)
from pikvm_agent.debuglog import DEBUG
from pikvm_agent.executor.verification import (
    Verdict,
    classify_mismatch,
    compute_verdict,
    has_whitespace_only_difference,
    is_exact_text,
    levenshtein,
    norm,
    strip_prompt,
    verify_text,
)
from pikvm_agent.pikvm.text import flatten_line_breaks
from pikvm_agent.vision.frame_diff import GRID_COLS, GRID_ROWS, grid

# --------------------------------------------------------------------------- #
# Watched-typing tuning constants (reproduce the TS values exactly).
# --------------------------------------------------------------------------- #

CELL_DELTA = 18           # grayscale delta for a grid cell to count as changed
MIN_CHANGED_CELLS = 2     # fewer (after prune) ⇒ nothing landed
LOCATE_MIN_CHARS = 5      # only auto-locate once first chunk ≥ this
PRECISE_LOCATE_MIN_CHARS = 4  # short allowlisted Run names still need proof
ABORT_MIN_CHARS = 8       # only HARD-fail "no focus" when ≥ this typed
MAX_BOX_HEIGHT_FRAC = 0.6  # a change taller than this frac of screen = repaint
CHUNK_TARGET = 16         # word-boundary chunk target length
MAX_TOTAL_CORRECTIONS = 1  # one clean retry; never a compounding loop
MAX_BACKSPACES = 400      # safety cap on a correction's clear
FAST_PRINT_MIN = 120      # above this, plain text takes the (bursty) fast print path;
                          # shorter text stays on the fully-humanized per-key path
FAST_TERMINAL_PRINT_MIN = 32  # exact simple argv can use PiKVM's guarded printer
FAST_EDITOR_PRINT_MIN = 32  # verified editor focus can use the same guarded printer
MIN_MISMATCH_OCR_CONFIDENCE = 0.78
MIN_GROUNDED_EXACT_OCR_CONFIDENCE = 0.55
MIN_ONE_EDIT_RECHECK_CONFIDENCE = 0.90
MAX_AUTODETECTED_FIELD_HEIGHT = 80
MAX_AUTODETECTED_FIELD_HEIGHT_FRAC = 0.15
MAX_PROSE_EDGE_CONTEXT_CHARS = 96
AUTODETECTED_READBACK_MARGIN_X_FRAC = 0.075
SHORT_FIELD_CONTEXT_ABOVE_PX = 80
SHORT_FIELD_CONTEXT_BELOW_PX = 24
DENSE_PIXEL_DELTA = 10
DENSE_MIN_CHANGED_PIXELS = 80
DENSE_MIN_WIDTH = 8
DENSE_MIN_HEIGHT = 4
DENSE_MAX_HEIGHT = 64

# Pauses (seconds) — let a print / clear land and the video settle before reading.
_PRINT_SETTLE_S = 0.45
_CLEAR_SETTLE_S = 0.15
_VIDEO_RETRY_SETTLE_S = 0.20
_SLOW_VIDEO_RETRY_SETTLE_S = 1.00
_VERY_SLOW_VIDEO_RETRY_SETTLE_S = 2.50
_CARET_BLINK_RECHECK_S = 0.65
_PRECISE_READBACK_SETTLES_S = (0.45, 0.90, 1.80)
_PRECISE_FULL_SCREEN_SETTLES_S = (0.0, 0.45, 0.90)

_SIMPLE_TERMINAL_ARGV = re.compile(
    r"[A-Za-z0-9_./:@=+-]+(?: [A-Za-z0-9_./:@=+-]+)*"
)
_SAFE_FILENAME = re.compile(
    r'[^\\/:*?"<>|\r\n]{1,180}\.[A-Za-z0-9]{1,12}'
)

NO_FOCUS_SUMMARY = (
    "Typed but the screen did not change — the field isn't focused. STOP: do not "
    "call type_text again yet. First screenshot/get_regions, click inside the "
    "target field (or otherwise focus it), verify the caret/focus, then call "
    "type_text once."
)
FOCUS_CHANGED_SUMMARY = (
    "Typing stopped because substantial pixels changed outside the established "
    "field between chunks. Treat this as focus theft or an unexpected window: "
    "capture a fresh screen and re-establish the exact destination before typing."
)
INTERRUPTED_SUMMARY = (
    "Typing interrupted: control changed (abort / panic / steer) mid-text; held "
    "keys released. The field holds only what was typed before the stop."
)


# --------------------------------------------------------------------------- #
# Injected ports (structural — FakeBackend / the real backend both satisfy them).
# --------------------------------------------------------------------------- #


@runtime_checkable
class TypingBackend(Protocol):
    """The HID + capture surface the typer drives (a structural subset of
    :class:`~pikvm_agent.core.ports.ComputerBackend` plus state getters)."""

    async def type_text(self, text: str, *, code: bool = False, secret: bool = False) -> None: ...
    async def press_key(self, code: str) -> None: ...
    async def keypress(self, keys: list[str]) -> None: ...
    async def screenshot(self, region: Region | None = None) -> CapturedFrame: ...
    def get_caps_lock(self) -> bool | None: ...
    def get_layout(self) -> str: ...
    def set_layout(self, layout: str) -> None: ...


@runtime_checkable
class TypingOCR(Protocol):
    async def ocr(self, image_path: Path, region: Region | None = None) -> OCRResult: ...


# --------------------------------------------------------------------------- #
# Result contract.
# --------------------------------------------------------------------------- #


class WatchedTypingResult(BaseModel):
    """Outcome of a watched ``type_text`` — verdict + the verifier's status."""

    verdict: Verdict
    ok: bool
    status: VerificationStatus
    field_text: str
    corrected: bool
    used_fast_path: bool
    summary: str
    typed_characters: int = 0
    intended_characters: int = 0
    correction_count: int = 0
    delivery_retries: int = 0
    emitted_characters: int = 0
    emitted_sha256: str = ""
    emitted_exactly_once: bool = False
    readback_frame_sha256: str = ""


# --------------------------------------------------------------------------- #
# Geometry helpers.
# --------------------------------------------------------------------------- #


def _dims_wh(dims: Any) -> tuple[int, int]:
    """Read (width, height) from a dict, a (w, h) tuple, or an object with attrs."""
    if isinstance(dims, dict):
        return int(dims["width"]), int(dims["height"])
    if isinstance(dims, (tuple, list)):
        return int(dims[0]), int(dims[1])
    return int(dims.width), int(dims.height)


def union_region(a: Region, b: Region) -> Region:
    """Smallest box covering both regions (grows the located field as typing extends)."""
    x = min(a.x, b.x)
    y = min(a.y, b.y)
    x2 = max(a.x + a.width, b.x + b.width)
    y2 = max(a.y + a.height, b.y + b.height)
    return Region(x=x, y=y, width=x2 - x, height=y2 - y)


def regions_overlap(a: Region, b: Region) -> bool:
    """Whether two screen regions share at least one pixel."""

    return (
        a.x < b.x + b.width
        and b.x < a.x + a.width
        and a.y < b.y + b.height
        and b.y < a.y + a.height
    )


def ocr_line_region(
    line: OCRLine,
    dims: tuple[int, int],
    *,
    pad: int = 8,
) -> Region | None:
    """Convert one valid OCR line bbox into a clamped screen region."""

    box = line.bbox
    if (
        not isinstance(box, list)
        or len(box) != 4
        or any(isinstance(value, list) for value in box)
    ):
        return None
    x0, y0, x1, y1 = (int(value) for value in box)
    if x1 <= x0 or y1 <= y0:
        return None
    width, height = dims
    x = max(0, x0 - pad)
    y = max(0, y0 - pad)
    return Region(
        x=x,
        y=y,
        width=max(1, min(width, x1 + pad) - x),
        height=max(1, min(height, y1 + pad) - y),
    )


def vertically_adjacent_rows(previous: Region, current: Region) -> bool:
    """Whether two OCR boxes plausibly form consecutive wrapped text rows."""

    vertical_gap = current.y - previous.y - previous.height
    return vertical_gap <= max(
        12,
        previous.height,
        current.height,
    )


def readback_region(
    region: Region,
    dims: tuple[int, int],
    *,
    explicit: bool,
    vertical_context: bool = False,
) -> Region:
    """Add OCR context without weakening the narrower focus-theft guard."""

    if explicit:
        return region
    width, height = dims
    if width <= 0 or height <= 0:
        return region
    margin_x = max(16, round(width * AUTODETECTED_READBACK_MARGIN_X_FRAC))
    raw_x = int(region.x) - margin_x
    raw_x2 = math.ceil(region.x + region.width) + margin_x
    desired_width = min(width, max(1, raw_x2 - raw_x))
    x = max(0, raw_x)
    x2 = min(width, raw_x2)
    # Preserve the intended amount of read-only context when a field touches
    # either screen edge. Clamping only one coordinate made the Windows Run
    # field's crop 55 px narrower and repeatedly forced exact OCR back onto the
    # surrounding dialog.
    if x2 - x < desired_width:
        if x == 0:
            x2 = min(width, desired_width)
        elif x2 == width:
            x = max(0, width - desired_width)
    margin_y_above = SHORT_FIELD_CONTEXT_ABOVE_PX if vertical_context else 0
    margin_y_below = SHORT_FIELD_CONTEXT_BELOW_PX if vertical_context else 0
    y = max(0, int(region.y) - margin_y_above)
    y2 = min(
        height,
        math.ceil(region.y + region.height) + margin_y_below,
    )
    return Region(
        x=x,
        y=y,
        width=max(1, x2 - x),
        height=max(1, y2 - y),
    )


def precise_readback_candidate_region(
    result: OCRResult,
    intended: str,
    container: Region,
    dims: tuple[int, int],
) -> Region | None:
    """Narrow a noisy multi-control crop to the likely exact-text row.

    Punctuation errors are allowed only for this read-only localization step.
    The returned crop is deliberately wider than the OCR glyph box so a fresh
    OCR pass sees field context and trailing characters. It never verifies text
    or authorizes a follow-up action by itself.
    """

    target = "".join(
        character.casefold()
        for character in intended
        if character.isalnum()
    )
    screen_width, screen_height = dims
    if (
        len(target) < PRECISE_LOCATE_MIN_CHARS
        or screen_width <= 0
        or screen_height <= 0
    ):
        return None

    for line in result.lines:
        line_region = ocr_line_region(
            line,
            (
                math.ceil(container.width),
                math.ceil(container.height),
            ),
            pad=0,
        )
        if line_region is None or not line.text:
            continue
        source_positions = [
            (character.casefold(), index)
            for index, character in enumerate(line.text)
            if character.isalnum()
        ]
        source = "".join(character for character, _index in source_positions)
        intended_filename = (
            intended.rsplit(".", 1)
            if _SAFE_FILENAME.fullmatch(intended)
            else None
        )
        observed_text = line.text.strip()
        observed_filename = (
            observed_text.rsplit(".", 1)
            if _SAFE_FILENAME.fullmatch(observed_text)
            else None
        )
        same_filename_stem = bool(
            intended_filename is not None
            and observed_filename is not None
            and intended_filename[0].casefold()
            == observed_filename[0].casefold()
        )
        if same_filename_stem:
            # Low-resolution Save As crops repeatedly preserve the basename
            # while confusing every short extension glyph (for example the
            # measured ``text-01.bd`` for ``text-01.txt``). Use that exact,
            # safe stem only to localize the filename row. The next OCR pass
            # still has to independently read the complete intended filename;
            # this branch never verifies or submits it.
            target_index = 0
            matched_length = len(source)
        else:
            target_index = source.find(target)
            matched_length = len(target)
        if target_index < 0:
            # A noisy punctuation-free OCR pass may still identify which row to
            # re-read. Keep this bounded to two alphanumeric edits and use it
            # only for crop localization; the second OCR must independently
            # match every intended character before the caller can continue.
            tolerance = min(2, max(1, math.ceil(len(target) * 0.20)))
            best: tuple[int, int, int] | None = None
            for start in range(len(source)):
                for candidate_length in range(
                    max(1, len(target) - tolerance),
                    min(
                        len(source) - start,
                        len(target) + tolerance,
                    )
                    + 1,
                ):
                    distance = levenshtein(
                        target,
                        source[start : start + candidate_length],
                        tolerance,
                    )
                    candidate = (distance, start, candidate_length)
                    if distance <= tolerance and (
                        best is None or candidate < best
                    ):
                        best = candidate
            if best is None:
                continue
            _distance, target_index, matched_length = best

        raw_start = source_positions[target_index][1]
        raw_end = (
            source_positions[target_index + matched_length - 1][1] + 1
        )
        line_length = max(1, len(line.text))
        line_x0 = float(line_region.x)
        line_width = max(1.0, float(line_region.width))
        estimated_start = line_x0 + line_width * raw_start / line_length
        estimated_width = max(
            1.0,
            line_width * (raw_end - raw_start) / line_length,
        )
        # Tiny exact-text rows need enough surrounding pixels for punctuation
        # whose marks sit above/below the letter body.  A 16 px crop around a
        # 12 px Windows Run row preserved the letters but clipped the two dots
        # of ":"; Paddle then confidently read ``ms-settingsabout``.  Keep the
        # refinement bounded to the same row, but retain at least six pixels of
        # vertical context on either side. Keep two additional pixels below the
        # row because a Windows field border at the crop edge can make Paddle
        # merge the final narrow glyph into its neighbour.
        vertical_padding = max(6, round(line_region.height * 0.50))
        x = max(
            0,
            math.floor(container.x + estimated_start - 2),
        )
        y = max(
            0,
            math.floor(container.y + line_region.y - vertical_padding),
        )
        container_right = min(
            screen_width,
            math.ceil(container.x + container.width),
        )
        desired_width = max(
            200,
            math.ceil(estimated_width + 96),
        )
        x2 = min(container_right, x + desired_width)
        y2 = min(
            screen_height,
            math.ceil(
                container.y
                + line_region.y
                + line_region.height
                + vertical_padding
                + 2
            ),
        )
        return Region(
            x=x,
            y=y,
            width=max(1, x2 - x),
            height=max(1, y2 - y),
        )
    return None


# --------------------------------------------------------------------------- #
# Chunking.
# --------------------------------------------------------------------------- #


def chunk_text(s: str) -> list[str]:
    """Word-boundary chunks of ~CHUNK_TARGET chars (never split a short word).

    A word longer than the cap is hard-split into CHUNK_TARGET-char slices.
    Invariant: ``"".join(chunk_text(s)) == s``.
    """
    if len(s) <= CHUNK_TARGET:
        return [s] if s else []
    out: list[str] = []
    buf = ""
    # split keeping the whitespace separators (re.split with a capturing group).
    for word in re.split(r"(\s+)", s):
        if not word:
            continue
        if buf and len(buf) + len(word) > CHUNK_TARGET and buf.strip():
            out.append(buf)
            buf = ""
        if len(word) > CHUNK_TARGET:
            if buf:
                out.append(buf)
                buf = ""
            for i in range(0, len(word), CHUNK_TARGET):
                out.append(word[i : i + CHUNK_TARGET])
            continue
        buf += word
    if buf:
        out.append(buf)
    # A cooperative stop may leave the guest containing exactly the chunks
    # already acknowledged. Keep separators at the beginning of the next chunk
    # so an interrupted prose prefix never ends in invisible whitespace that a
    # later continuation can accidentally duplicate.
    for index in range(len(out) - 1):
        match = re.search(r"\s+$", out[index])
        if match is None or match.start() == 0:
            continue
        separator = match.group()
        out[index] = out[index][: match.start()]
        out[index + 1] = separator + out[index + 1]
    return out


# --------------------------------------------------------------------------- #
# Field localisation (pixel-diff).
# --------------------------------------------------------------------------- #


def locate_changed_bbox(
    before_grid: np.ndarray,
    after_grid: np.ndarray,
    dims: Any,
    cols: int = GRID_COLS,
    rows: int = GRID_ROWS,
) -> Region | None:
    """Bounding box of the grid cells that changed between two frames.

    Returns ``None`` when too little changed (keystrokes didn't land / not focused)
    or when the change is taller than ``MAX_BOX_HEIGHT_FRAC`` of the screen (a
    full-screen repaint, not a field). ``before_grid`` / ``after_grid`` are flat
    row-major uint8 arrays of length ``cols * rows`` (see ``frame_diff.grid``).
    """
    width, height = _dims_wh(dims)
    kept = _pruned_changed_cells(before_grid, after_grid, cols, rows)
    if kept is None:
        return None

    ys, xs = np.nonzero(kept)
    min_c, max_c = int(xs.min()), int(xs.max())
    min_r, max_r = int(ys.min()), int(ys.max())

    cw = width / cols
    ch = height / rows
    x = max(0.0, (min_c - 1) * cw)
    y = max(0.0, (min_r - 1) * ch)
    w = min(width - x, (max_c - min_c + 3) * cw)
    h = min(height - y, (max_r - min_r + 3) * ch)
    if h > height * MAX_BOX_HEIGHT_FRAC:
        return None  # whole-screen repaint, not a field
    return Region(x=x, y=y, width=w, height=h)


def locate_dense_changed_bbox(
    before_image: bytes,
    after_image: bytes,
    dims: Any,
) -> Region | None:
    """Locate a narrow text-line change hidden by the coarse luminance grid.

    Replacing selected text can preserve the average brightness of every
    96x54 grid cell even though hundreds of full-resolution pixels changed.
    This fallback accepts only a coherent, horizontal, text-line-sized delta;
    isolated caret blinking and large window repaints remain non-evidence.
    """

    width, height = _dims_wh(dims)
    if width <= 0 or height <= 0 or not before_image or not after_image:
        return None
    try:
        before = np.asarray(
            Image.open(io.BytesIO(before_image)).convert("RGB"),
            dtype=np.int16,
        )
        after = np.asarray(
            Image.open(io.BytesIO(after_image)).convert("RGB"),
            dtype=np.int16,
        )
    except Exception:
        return None
    if before.shape != after.shape or before.ndim != 3:
        return None
    changed = np.max(np.abs(after - before), axis=2) > DENSE_PIXEL_DELTA
    changed_count = int(changed.sum())
    if changed_count < DENSE_MIN_CHANGED_PIXELS or changed_count > 50_000:
        return None

    remaining = {
        (int(y), int(x))
        for y, x in zip(*np.nonzero(changed), strict=True)
    }
    components: list[tuple[int, int, int, int, int]] = []
    while remaining:
        seed = remaining.pop()
        stack = [seed]
        count = 0
        min_y = max_y = seed[0]
        min_x = max_x = seed[1]
        while stack:
            y, x = stack.pop()
            count += 1
            min_y, max_y = min(min_y, y), max(max_y, y)
            min_x, max_x = min(min_x, x), max(max_x, x)
            for neighbour_y in range(y - 1, y + 2):
                for neighbour_x in range(x - 1, x + 2):
                    neighbour = (neighbour_y, neighbour_x)
                    if neighbour in remaining:
                        remaining.remove(neighbour)
                        stack.append(neighbour)
        if count >= 4:
            components.append((count, min_x, min_y, max_x + 1, max_y + 1))

    candidates_by_box: dict[tuple[int, int, int, int], int] = {}
    for seed in components:
        count, x0, y0, x1, y1 = seed
        for component in components:
            if component == seed:
                continue
            other_count, other_x0, other_y0, other_x1, other_y1 = component
            vertical_gap = max(0, max(y0, other_y0) - min(y1, other_y1))
            horizontal_gap = max(0, max(x0, other_x0) - min(x1, other_x1))
            if vertical_gap <= 4 and horizontal_gap <= 64:
                count += other_count
                x0, y0 = min(x0, other_x0), min(y0, other_y0)
                x1, y1 = max(x1, other_x1), max(y1, other_y1)
        box_width = x1 - x0
        box_height = y1 - y0
        if (
            count >= DENSE_MIN_CHANGED_PIXELS
            and box_width >= DENSE_MIN_WIDTH
            and box_height >= DENSE_MIN_HEIGHT
            and box_height <= min(DENSE_MAX_HEIGHT, height * 0.1)
            and box_width >= box_height * 1.25
        ):
            box = (x0, y0, x1, y1)
            candidates_by_box[box] = max(candidates_by_box.get(box, 0), count)
    candidates = [
        (count, *box)
        for box, count in candidates_by_box.items()
    ]
    if not candidates:
        return None
    candidates.sort(reverse=True)
    count, x0, y0, x1, y1 = candidates[0]
    if len(candidates) > 1 and count < candidates[1][0] * 2:
        return None
    pad = 8
    x = max(0, x0 - pad)
    y = max(0, y0 - pad)
    x2 = min(width, x1 + pad)
    y2 = min(height, y1 + pad)
    return Region(
        x=x,
        y=y,
        width=max(1, x2 - x),
        height=max(1, y2 - y),
    )


def locate_capture_change(
    before_grid: np.ndarray | None,
    before_frame: CapturedFrame | None,
    after_frame: CapturedFrame | None,
    dims: Any,
) -> Region | None:
    """Locate the causal delta from immediately before typing to one fresh frame."""

    if after_frame is None or not after_frame.data:
        return None
    after_grid: np.ndarray | None = None
    try:
        after_grid = grid(after_frame.data)
    except Exception:
        pass
    if before_grid is not None and after_grid is not None:
        changed = locate_changed_bbox(before_grid, after_grid, dims)
        if changed is not None:
            return changed
    if before_frame is None or not before_frame.data:
        return None
    return locate_dense_changed_bbox(
        before_frame.data,
        after_frame.data,
        dims,
    )


def _pruned_changed_cells(
    before_grid: np.ndarray,
    after_grid: np.ndarray,
    cols: int = GRID_COLS,
    rows: int = GRID_ROWS,
) -> np.ndarray | None:
    """Return clustered changed cells, excluding cursor/stream speckle."""

    a = np.asarray(before_grid)
    b = np.asarray(after_grid)
    if a.shape != b.shape or a.size != cols * rows:
        return None
    diff = np.abs(b.astype(np.int32) - a.astype(np.int32))
    changed = (diff > CELL_DELTA).reshape(rows, cols)
    if int(changed.sum()) < MIN_CHANGED_CELLS:
        return None
    up = np.zeros_like(changed)
    dn = np.zeros_like(changed)
    lf = np.zeros_like(changed)
    rt = np.zeros_like(changed)
    up[1:, :] = changed[:-1, :]
    dn[:-1, :] = changed[1:, :]
    lf[:, 1:] = changed[:, :-1]
    rt[:, :-1] = changed[:, 1:]
    kept = changed & (up | dn | lf | rt)
    if int(kept.sum()) < MIN_CHANGED_CELLS:
        return None
    return kept


def _substantial_change_outside_region(
    before_grid: np.ndarray,
    after_grid: np.ndarray,
    region: Region,
    dims: Any,
    cols: int = GRID_COLS,
    rows: int = GRID_ROWS,
) -> bool:
    """Detect an unexpected window/change away from the established field."""

    kept = _pruned_changed_cells(before_grid, after_grid, cols, rows)
    if kept is None:
        return False
    width, height = _dims_wh(dims)
    if width <= 0 or height <= 0:
        # A protocol-compatible backend may expose only screenshot dimensions.
        # Without a stable screen geometry we cannot map the protected region
        # onto the grid, so leave this signal unknown instead of crashing.
        return False
    cw = width / cols
    ch = height / rows
    x0 = max(0, int(region.x / cw) - 1)
    x1 = min(cols, int(np.ceil((region.x + region.width) / cw)) + 1)
    y0 = max(0, int(region.y / ch) - 1)
    y1 = min(rows, int(np.ceil((region.y + region.height) / ch)) + 1)
    inside = np.zeros_like(kept)
    inside[y0:y1, x0:x1] = True
    outside = int((kept & ~inside).sum())
    total = int(kept.sum())
    return outside >= MIN_CHANGED_CELLS and outside * 2 >= total


# --------------------------------------------------------------------------- #
# The typer.
# --------------------------------------------------------------------------- #


class WatchedTyper:
    """Watched, self-correcting typing over an injected backend + OCR provider."""

    def __init__(self, backend: TypingBackend, ocr: TypingOCR) -> None:
        self.backend = backend
        self.ocr = ocr
        self._last_readback_frame_sha256 = ""
        self._semantic_spacing_normalized = False
        self._last_read_semantic_spacing = False
        self._last_grid_frame: CapturedFrame | None = None
        self._last_read_screen_frame: CapturedFrame | None = None
        self._last_field_ocr_result = OCRResult()
        self._refined_readback_region: Region | None = None
        self._refined_readback_intended = ""

    # ---- capture/read helpers -------------------------------------------- #

    def _dims(self) -> tuple[int, int]:
        get = getattr(self.backend, "get_dimensions", None)
        if callable(get):
            return _dims_wh(get())
        # Fall back to a captured frame's reported size — handled by callers that
        # already hold a frame; default to 0x0 (locate then declines).
        return (0, 0)

    async def _grid(self) -> np.ndarray | None:
        """Full-frame grayscale grid for the pixel-diff, or ``None`` on failure."""
        self._last_grid_frame = None
        try:
            frame = await self.backend.screenshot()
        except Exception:
            return None
        if not frame or not frame.data:
            return None
        self._last_grid_frame = frame
        return await asyncio.to_thread(grid, frame.data)

    async def _read_field(
        self,
        region: Region,
        *,
        intended: str | None = None,
        precise: bool = False,
        allow_semantic_spacing: bool = False,
        allow_blind_fallback: bool = False,
        minimum_confidence: float = MIN_MISMATCH_OCR_CONFIDENCE,
    ) -> str:
        """OCR the field. Capture the FULL frame and pass the region to the OCR
        provider so it reads the field crop on every backend: file OCR
        (tesseract) crops the saved frame by region, while live PiKVM OCR reads
        that region on the live screen — never the whole frame. ``""`` on failure."""
        self._last_read_semantic_spacing = False
        self._last_field_ocr_result = OCRResult()
        try:
            frame = await self.backend.screenshot()
        except Exception:
            return ""
        if not frame or not frame.data:
            return ""
        frame_sha256 = str(frame.sha256 or "").lower()
        self._last_readback_frame_sha256 = (
            frame_sha256
            if re.fullmatch(r"[0-9a-f]{64}", frame_sha256)
            else hashlib.sha256(frame.data).hexdigest()
        )
        tmp: Path | None = None
        try:
            requested_region = region
            refined_region: Region | None = None
            fd = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            fd.write(frame.data)
            fd.close()
            tmp = Path(fd.name)
            precise_ocr = getattr(self.ocr, "ocr_precise", None)
            if precise and callable(precise_ocr):
                result = await precise_ocr(tmp, region=region)
            else:
                result = await self.ocr.ocr(tmp, region=region)
            if precise and intended:
                # Auto-located short fields may need surrounding dialog
                # context because typing also enables a nearby button. If one
                # complete canonical OCR row is already the exact intended
                # text, retain that whole row instead of paying for a second
                # refined OCR pass. This never carves a matching substring:
                # ``calcc`` remains ``calcc`` and therefore cannot verify.
                exact_rows = [
                    line
                    for line in result.lines
                    if (
                        line.text == intended
                        and (
                            line.confidence is None
                            or float(line.confidence)
                            >= MIN_GROUNDED_EXACT_OCR_CONFIDENCE
                        )
                    )
                ]
                if len(exact_rows) == 1:
                    exact_spacing_verified = any(
                        candidate.evidence_kind == "spacing"
                        and candidate.text == exact_rows[0].text
                        for candidate in result.alternatives
                    )
                    result = OCRResult(
                        lines=exact_rows,
                        alternatives=result.alternatives,
                        spacing_evidence=(
                            "verified"
                            if exact_spacing_verified
                            else result.spacing_evidence
                        ),
                    )
            if (
                precise
                and intended
                and callable(precise_ocr)
                and compute_verdict(intended, result.text, True)
                != "match"
            ):
                refined_region = precise_readback_candidate_region(
                    result,
                    intended,
                    region,
                    self._dims(),
                )
                if refined_region is not None:
                    result = await precise_ocr(
                        tmp,
                        region=refined_region,
                    )
                    # The refinement is read-only geometry derived from the
                    # current field crop. Reuse it for later OCR passes in this
                    # typing transaction instead of falling back to the noisy
                    # multi-control crop. It never verifies or submits text.
                    self._refined_readback_region = refined_region
                    self._refined_readback_intended = intended
            if (
                precise
                and intended
                and allow_blind_fallback
                and compute_verdict(intended, result.text, True)
                != "match"
            ):
                blind_precise_ocr = getattr(
                    self.ocr,
                    "ocr_precise_fallback",
                    None,
                )
                if callable(blind_precise_ocr):
                    blind_region = refined_region or region
                    blind_path = tmp
                    native_tmp: Path | None = None
                    try:
                        native_frame = await self.backend.screenshot(
                            region=blind_region,
                        )
                        if (
                            native_frame
                            and native_frame.data
                            and native_frame.width > 0
                            and native_frame.height > 0
                        ):
                            native_file = tempfile.NamedTemporaryFile(
                                suffix=".png",
                                delete=False,
                            )
                            native_file.write(native_frame.data)
                            native_file.close()
                            native_tmp = Path(native_file.name)
                            blind_path = native_tmp
                            blind_region = Region(
                                x=0,
                                y=0,
                                width=native_frame.width,
                                height=native_frame.height,
                            )
                            DEBUG.event(
                                "typing.field_readback_native_fallback",
                                width=native_frame.width,
                                height=native_frame.height,
                            )
                        blind_result = await blind_precise_ocr(
                            blind_path,
                            region=blind_region,
                        )
                    except Exception:
                        blind_result = OCRResult()
                    finally:
                        if native_tmp is not None:
                            native_tmp.unlink(missing_ok=True)
                    if blind_result.text:
                        result = blind_result
                        DEBUG.event(
                            "typing.field_readback_fallback",
                            provider="blind_model_consensus",
                            observed_characters=len(result.text),
                            line_count=len(result.lines),
                        )
            self._last_field_ocr_result = result
            confidences = [
                float(line.confidence)
                for line in result.lines
                if line.confidence is not None
            ]
            DEBUG.event(
                "typing.field_readback",
                precise=precise,
                intended_characters=len(intended or ""),
                observed_characters=len(result.text),
                line_count=len(result.lines),
                alternative_count=len(result.alternatives),
                mean_confidence=(
                    round(sum(confidences) / len(confidences), 4)
                    if confidences
                    else None
                ),
                verdict=(
                    compute_verdict(intended, result.text, precise)
                    if intended
                    else None
                ),
                requested_region=requested_region.model_dump(),
                refined_region=(
                    refined_region.model_dump()
                    if refined_region is not None
                    else None
                ),
            )
            if (
                precise
                and intended
            ):
                spacing_candidates = [
                    alternative.text
                    for alternative in result.alternatives
                    if alternative.evidence_kind == "spacing"
                ]
                for candidate in spacing_candidates:
                    if has_whitespace_only_difference(intended, candidate):
                        return candidate
                # Alternative OCR scales/engines are useful recheck evidence,
                # but selecting whichever candidate equals the intended text
                # would make the checksum circular. Exact completion uses the
                # provider's canonical complete-field text. A spacing candidate
                # may only veto it by exposing a visible whitespace mismatch.
                if (
                    any(character in intended for character in (" ", "\t", "\n"))
                    and result.spacing_evidence != "verified"
                ):
                    if (
                        allow_semantic_spacing
                        and norm(intended, precise)
                        == norm(
                            strip_prompt(result.text),
                            precise,
                        )
                    ):
                        self._last_read_semantic_spacing = True
                    else:
                        # Ordinary OCR collapses whitespace. Exact completion
                        # needs independently repeated, calibrated word-gap
                        # evidence. A terminal command whose argv contains no
                        # quoting or shell syntax is the sole exception because
                        # repeated token separators are semantically identical.
                        return ""
            if (
                confidences
                and sum(confidences) / len(confidences)
                < minimum_confidence
                and not (
                    precise
                    and intended
                    and compute_verdict(
                        intended,
                        result.text,
                        True,
                    )
                    == "match"
                )
            ):
                # Low-confidence OCR may still be useful to a human, but it is
                # not strong enough evidence to clear/retype a field or stop a
                # correct command as "failed".
                return ""
            return result.text
        except Exception:
            return ""
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    async def _read_screen(self, *, precise: bool = False) -> OCRResult:
        """OCR one exact full-frame capture for localization/readback fallback."""
        self._last_read_screen_frame = None
        try:
            frame = await self.backend.screenshot()
        except Exception:
            return OCRResult()
        if not frame or not frame.data:
            return OCRResult()
        self._last_read_screen_frame = frame
        frame_sha256 = str(frame.sha256 or "").lower()
        self._last_readback_frame_sha256 = (
            frame_sha256
            if re.fullmatch(r"[0-9a-f]{64}", frame_sha256)
            else hashlib.sha256(frame.data).hexdigest()
        )
        tmp: Path | None = None
        try:
            fd = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            fd.write(frame.data)
            fd.close()
            tmp = Path(fd.name)
            precise_ocr = getattr(self.ocr, "ocr_precise", None)
            if precise and callable(precise_ocr):
                return await precise_ocr(tmp)
            return await self.ocr.ocr(tmp)
        except Exception:
            return OCRResult()
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def _locate_ocr_candidate(
        self,
        result: OCRResult,
        intended: str,
        dims: tuple[int, int],
        *,
        precise: bool = False,
    ) -> Region | None:
        """Ground an OCR line containing the just-typed text."""
        # Word-boundary chunks commonly end in a space. OCR omits that invisible
        # boundary and may render the adjacent caret as punctuation, so retain
        # the exact visible characters while excluding only outer whitespace
        # from this localisation probe.
        if precise:
            # Code, commands, paths, and URLs retain exact punctuation here;
            # later verification remains the final authority on case as well.
            needle = intended.strip().casefold()
            normalize_line = str.casefold
        else:
            # Rich editors commonly replace straight quotes with smart quotes
            # before the first verification frame. Use the same family-aware
            # prose normalization as final read-back, without confusable or
            # edit-distance tolerance.
            needle = norm(intended, False)
            normalize_line = lambda value: norm(value, False)
        width, height = dims
        if not needle or width <= 0 or height <= 0:
            return None
        for line in result.lines:
            if needle not in normalize_line(line.text):
                continue
            if (
                line.confidence is not None
                and float(line.confidence) < MIN_MISMATCH_OCR_CONFIDENCE
            ):
                continue
            region = ocr_line_region(line, dims)
            if region is not None:
                return region
        return None

    @staticmethod
    def _full_screen_exact_line_candidate(
        result: OCRResult,
        intended: str,
        dims: tuple[int, int],
        *,
        allow_semantic_spacing: bool,
    ) -> tuple[str, Region, bool] | None:
        """Return grounded complete terminal input from one or more OCR rows.

        This is deliberately narrower than substring localization: the
        prompt-stripped OCR text itself must reconstruct the complete emitted
        command. Up to three vertically adjacent rows are considered because a
        terminal can hard-wrap inside a token. Its union geometry is retained
        so callers can require overlap with the changed-pixel field instead of
        accepting matching task text elsewhere.
        """

        width, height = dims
        target = intended.strip()
        if not target or width <= 0 or height <= 0:
            return None
        prefix = target[: min(20, len(target))]
        for start in range(len(result.lines) - 1, -1, -1):
            line = result.lines[start]
            region = ocr_line_region(line, dims)
            confidence = (
                float(line.confidence)
                if line.confidence is not None
                else 1.0
            )
            prefix_index = line.text.find(prefix)
            if (
                region is None
                or confidence < MIN_GROUNDED_EXACT_OCR_CONFIDENCE
                or prefix_index < 0
            ):
                continue
            observed = line.text[prefix_index:].strip()
            if observed == target:
                return (observed, region, False)
            if (
                confidence >= MIN_MISMATCH_OCR_CONFIDENCE
                and allow_semantic_spacing
                and _SIMPLE_TERMINAL_ARGV.fullmatch(target) is not None
                and norm(observed, True) == norm(target, True)
            ):
                return (observed, region, True)
            if not target.startswith(observed):
                continue

            combined = observed
            combined_region = region
            previous_region = region
            for continuation in result.lines[start + 1 : start + 3]:
                continuation_region = ocr_line_region(continuation, dims)
                continuation_confidence = (
                    float(continuation.confidence)
                    if continuation.confidence is not None
                    else 1.0
                )
                if (
                    continuation_region is None
                    or continuation_confidence
                    < MIN_GROUNDED_EXACT_OCR_CONFIDENCE
                ):
                    break
                if not vertically_adjacent_rows(
                    previous_region,
                    continuation_region,
                ):
                    break

                next_combined = ""
                continuation_text = continuation.text.strip()
                for separator in ("", " "):
                    candidate = combined + separator + continuation_text
                    if target.startswith(candidate):
                        next_combined = candidate
                        break
                if not next_combined:
                    break
                combined = next_combined
                combined_region = union_region(
                    combined_region,
                    continuation_region,
                )
                previous_region = continuation_region
                if combined == target:
                    return (combined, combined_region, False)
        return None

    @staticmethod
    def _full_screen_exact_prefix_region(
        result: OCRResult,
        intended: str,
        dims: tuple[int, int],
    ) -> Region | None:
        """Locate wrapped terminal rows by an exact command prefix for re-OCR."""

        width, height = dims
        target = intended.strip()
        if not target or width <= 0 or height <= 0:
            return None
        prefix = target[: min(20, len(target))]
        for start in range(len(result.lines) - 1, -1, -1):
            line = result.lines[start]
            line_region = ocr_line_region(line, dims)
            confidence = (
                float(line.confidence)
                if line.confidence is not None
                else 1.0
            )
            if (
                line_region is None
                or confidence < MIN_GROUNDED_EXACT_OCR_CONFIDENCE
                or prefix not in line.text
            ):
                continue
            region = line_region
            previous_region = line_region
            for continuation in result.lines[start + 1 : start + 3]:
                continuation_region = ocr_line_region(continuation, dims)
                if (
                    continuation_region is None
                    or not vertically_adjacent_rows(
                        previous_region,
                        continuation_region,
                    )
                ):
                    break
                region = union_region(region, continuation_region)
                previous_region = continuation_region

            # Full-screen PSM can merge the desktop's left-edge dock glyphs
            # into a terminal row. Re-OCR just the grounded rows with a small
            # screen-edge inset; the command itself begins after its prompt.
            x2 = min(width, math.ceil(region.x + region.width))
            inset_x = max(int(region.x), round(width * 0.03))
            return Region(
                x=inset_x,
                y=max(0, int(region.y)),
                width=max(1, x2 - inset_x),
                height=max(
                    1,
                    min(
                        height,
                        math.ceil(region.y + region.height),
                    )
                    - max(0, int(region.y)),
                ),
            )
        return None

    def _locate_wrapped_prose_tail(
        self,
        result: OCRResult,
        intended: str,
        dims: tuple[int, int],
    ) -> Region | None:
        """Locate the final words of acknowledged prose across OCR line wraps."""

        words = intended.split()
        if len(words) < 2:
            return None
        probe = " ".join(words[-min(8, len(words)) :])
        width, height = dims
        if width <= 0 or height <= 0:
            return None

        eligible: list[tuple[OCRLine, Region]] = []
        for line in result.lines:
            if (
                line.confidence is not None
                and float(line.confidence) < MIN_MISMATCH_OCR_CONFIDENCE
            ):
                continue
            line_region = ocr_line_region(line, dims)
            if line_region is None:
                continue
            eligible.append((line, line_region))

        for start in range(len(eligible)):
            window: list[OCRLine] = []
            region: Region | None = None
            for line, line_region in eligible[start : start + 3]:
                window.append(line)
                region = (
                    line_region
                    if region is None
                    else union_region(region, line_region)
                )
                if self._full_screen_prose_candidate(
                    OCRResult(lines=window),
                    probe,
                ):
                    return region
        return None

    @staticmethod
    def _typed_candidate(read_back: str, intended: str, precise: bool) -> str:
        """Retain the complete OCR field used for exact comparison.

        Earlier code carved an intended substring out of surrounding OCR. That
        made a duplicate suffix disappear before hashing. Precise verification
        must see every character returned for the grounded field, even when
        that makes the result conservatively ambiguous. The sole exception is
        a filename field whose next OCR row is one exact, known Save As file-
        type control; that row is dialog chrome rather than editable content.
        """
        if (
            precise
            and _SAFE_FILENAME.fullmatch(intended)
        ):
            lines = [
                line.strip()
                for line in read_back.splitlines()
                if line.strip()
            ]
            known_save_as_type_rows = {
                "all files (*.*)",
                "text documents (*.txt)",
            }
            if (
                len(lines) == 2
                and lines[0] == intended
                and lines[1].casefold() in known_save_as_type_rows
            ):
                return intended
        return read_back

    @staticmethod
    def _full_screen_prose_candidate(
        result: OCRResult,
        intended: str,
    ) -> str:
        """Select a bounded OCR line window for long natural-language prose.

        A word processor can wrap one print burst across several lines while
        the grid-diff crop still covers only the first changed line. Full-screen
        OCR may also join adjacent words or confuse a few small glyphs. Search
        only consecutive line windows near the intended length and retain the
        normal prose verifier's eight-percent edit ceiling. This path is never
        used for precise code, commands, paths, URLs, or secrets.
        """

        visible_intended = " ".join(intended.split())
        folded_intended = visible_intended.casefold()
        if (
            not folded_intended
            or len(folded_intended) != len(visible_intended)
        ):
            return ""

        sources = [
            [line.text for line in result.lines],
            *(
                candidate.text.splitlines()
                for candidate in result.alternatives
            ),
        ]
        for lines in sources:
            read_back = " ".join(" ".join(lines).split())
            visible_read_back = " ".join(read_back.split())
            folded_read_back = visible_read_back.casefold()
            if len(folded_read_back) != len(visible_read_back):
                continue
            index = folded_read_back.rfind(folded_intended)
            if index >= 0:
                return visible_read_back[
                    index : index + len(visible_intended)
                ]

        max_distance = max(1, math.ceil(len(folded_intended) * 0.08))
        max_window_length = (
            len(visible_intended)
            + max_distance
            + (MAX_PROSE_EDGE_CONTEXT_CHARS * 2)
        )
        best = ""
        best_distance = max_distance + 1

        def consider(candidate: str) -> None:
            nonlocal best, best_distance
            if (
                not candidate
                or abs(len(candidate) - len(visible_intended))
                > max_distance
            ):
                return
            folded_candidate = candidate.casefold()
            if len(folded_candidate) != len(candidate):
                return
            distance = levenshtein(
                folded_intended,
                folded_candidate,
                max_distance,
            )
            if distance < best_distance:
                best = candidate
                best_distance = distance

        for lines in sources:
            visible_lines = [
                " ".join(line.split())
                for line in lines
                if line.strip()
            ]
            for start in range(len(visible_lines)):
                window = ""
                for line in visible_lines[start : start + 12]:
                    window = f"{window} {line}".strip()
                    if len(window) > max_window_length:
                        break
                    if len(window) < len(visible_intended) - max_distance:
                        continue
                    consider(window)

                    # A continuation can begin or end inside an OCR line because
                    # the editor already held text on that line before this call.
                    # Trim only at word boundaries and only within the existing
                    # prose error budget at either edge. This finds the newly
                    # appended suffix without accepting an arbitrary distant
                    # paragraph or weakening exact paths/code verification.
                    boundaries = [
                        match.end()
                        for match in re.finditer(r" +", window)
                    ]
                    left_edges = [
                        0,
                        *(
                            boundary
                            for boundary in boundaries
                            if boundary <= MAX_PROSE_EDGE_CONTEXT_CHARS
                        ),
                    ]
                    right_edges = [
                        *(
                            boundary - 1
                            for boundary in boundaries
                            if len(window) - (boundary - 1)
                            <= MAX_PROSE_EDGE_CONTEXT_CHARS
                        ),
                        len(window),
                    ]
                    for left in left_edges:
                        for right in right_edges:
                            if left or right != len(window):
                                consider(window[left:right].strip())
        if (
            best
            and best_distance <= max_distance
            and compute_verdict(intended, best, False)
            in {"match", "contains"}
        ):
            return best
        return ""

    # ---- corrective primitives ------------------------------------------- #

    async def _clear_from_start(self, n_chars: int) -> None:
        """Clear the field from the START: Home, then forward-Delete×N.

        NEVER Ctrl+A (in a terminal that means line-start ⇒ would duplicate) and
        NEVER Enter. Delete past the end is a no-op so an over-count is safe.
        """
        n = min(MAX_BACKSPACES, max(0, n_chars) + 4)
        await self.backend.press_key("Home")
        for _ in range(n):
            await self.backend.press_key("Delete")

    async def _clear_recent_input(self, n_chars: int) -> None:
        """Remove only the input this watcher just emitted, preserving prior text."""
        for _ in range(min(MAX_BACKSPACES, max(0, n_chars))):
            await self.backend.press_key("Backspace")

    # ---- public API ------------------------------------------------------ #

    async def type_text(
        self,
        text: str,
        *,
        region: Region | None = None,
        code: bool = False,
        prose: bool = False,
        exact: bool | None = None,
        secret: bool = False,
        context: str = "",
        should_continue: Callable[[], bool] | None = None,
    ) -> WatchedTypingResult:
        """Type ``text`` while watching the field; verify (and at most once correct).

        ``should_continue`` (when given) is polled between word-boundary chunks: if it
        ever returns False — an abort / panic / steer bumped the controller epoch — the
        typer drops any held keys and stops MID-text instead of running the whole string
        to completion. This makes a long ``type_text`` interruptible, not just the gaps
        between transactions."""
        delivery_text = flatten_line_breaks(text)
        self._last_readback_frame_sha256 = ""
        self._semantic_spacing_normalized = False
        self._last_read_semantic_spacing = False
        self._last_read_screen_frame = None
        self._refined_readback_region = None
        self._refined_readback_intended = ""
        precise = (
            exact
            if exact is not None
            else code or (is_exact_text(delivery_text) and not prose)
        )
        total = len(delivery_text)
        allow_semantic_spacing = (
            context.casefold() == "terminal"
            and _SIMPLE_TERMINAL_ARGV.fullmatch(delivery_text) is not None
        )

        # FAST TRANSPORT: long prose and exact simple terminal argv can use the
        # server-side keymap printer, while remaining chunked, interruptible,
        # visually read back, and never auto-submitted. Exact terminal text is
        # restricted to the no-metacharacter grammar above; its separate Enter
        # remains a later guarded action. Caps-on and secrets always stay on
        # the compensating per-key transport.
        print_text = getattr(self.backend, "print_text", None)
        caps_on = self.backend.get_caps_lock()
        guarded_terminal_print = (
            precise
            and allow_semantic_spacing
            and total >= FAST_TERMINAL_PRINT_MIN
            and bool(
                getattr(
                    self.backend,
                    "guarded_exact_print",
                    False,
                )
            )
        )
        guarded_prose_print = (
            not code
            and (prose or not is_exact_text(delivery_text))
            and total > FAST_PRINT_MIN
        )
        guarded_editor_print = (
            precise
            and not code
            and context.casefold() == "editor"
            and total >= FAST_EDITOR_PRINT_MIN
            and bool(
                getattr(
                    self.backend,
                    "guarded_exact_print",
                    False,
                )
            )
        )
        if should_continue is not None and not should_continue():
            await self._release_all_quietly()
            return self._halted_result(
                status="blocked_by_policy",
                field_text="",
                corrected=False,
                typed_characters=0,
                intended_characters=len(delivery_text),
                used_fast_path=False,
                summary=INTERRUPTED_SUMMARY,
                intended_text=delivery_text,
            )
        if (
            callable(print_text)
            and not secret
            and (
                guarded_terminal_print
                or guarded_editor_print
                or guarded_prose_print
            )
            and caps_on is not True
        ):
            return await self._humanized(
                delivery_text,
                region=region,
                code=code,
                secret=secret,
                precise=precise,
                single_line_field=context.casefold() == "field",
                allow_semantic_spacing=allow_semantic_spacing,
                should_continue=should_continue,
                fast_print=True,
            )

        return await self._humanized(
            delivery_text,
            region=region,
            code=code,
            secret=secret,
            precise=precise,
            single_line_field=context.casefold() == "field",
            allow_semantic_spacing=allow_semantic_spacing,
            should_continue=should_continue,
        )

    # ---- watched per-chunk path ------------------------------------------ #

    async def _humanized(
        self,
        text: str,
        *,
        region: Region | None,
        code: bool,
        secret: bool,
        precise: bool,
        single_line_field: bool,
        allow_semantic_spacing: bool,
        should_continue: Callable[[], bool] | None = None,
        fast_print: bool = False,
    ) -> WatchedTypingResult:
        dims = self._dims()
        # Short exact fields (notably allowlisted Windows Run targets) are one
        # bounded emission. Splitting a 17–20 character URI after character 16
        # lets its own autocomplete popup look like external focus theft before
        # the final glyph. Exact OCR still gates every following action.
        chunks = [text] if precise and len(text) <= 20 else chunk_text(text)
        total = len(text)
        explicit_region = region is not None
        located = explicit_region
        cur_region: Region | None = region
        typed_so_far = ""
        corrections = 0
        delivery_retries = 0
        last_read = ""
        verified_clean = False
        can_vision = not secret and (
            total > 4
            or (precise and total >= 3)
        )
        emitted_parts: list[str] = []

        async def emit_text(value: str) -> None:
            emitted_parts.append(value)
            if fast_print:
                printer = getattr(self.backend, "print_text", None)
                if not callable(printer):
                    raise RuntimeError("fast print became unavailable")
                await printer(value)
            else:
                await self.backend.type_text(
                    value,
                    code=code,
                    secret=secret,
                )

        def cadence(i: int) -> bool:
            if not can_vision or cur_region is None:
                return False
            if i == 0:
                return True  # catch wrong layout / autocorrect EARLY
            if total <= 20:
                return False  # short: first + final only
            return i % 3 == 0  # longer: periodic

        def current_readback_region(
            intended_snapshot: str | None = None,
        ) -> Region:
            assert cur_region is not None
            desired_text = (
                text
                if intended_snapshot is None
                else intended_snapshot
            )
            if (
                self._refined_readback_region is not None
                and self._refined_readback_intended == desired_text
            ):
                return self._refined_readback_region
            return readback_region(
                cur_region,
                dims,
                explicit=explicit_region,
                vertical_context=(
                    precise
                    and not explicit_region
                    and total <= PRECISE_LOCATE_MIN_CHARS
                ),
            )

        stable_field_read_performed = False

        async def read_current_field(intended_snapshot: str) -> str:
            """Read a complete exact field once without trusting its caret.

            A focused Windows field can render ``calc|`` as ``cald``. Short
            fields without spaces can move focus for an independent read, but
            long drafts and fields with spaces move their caret to the start
            because a Windows address bar may discard unsubmitted text on
            focus loss and selected text is materially harder to OCR.
            """

            nonlocal stable_field_read_performed
            assert cur_region is not None
            should_stabilize = (
                precise
                and single_line_field
                and not stable_field_read_performed
                and intended_snapshot == text
                and (
                    (
                        not explicit_region
                        and PRECISE_LOCATE_MIN_CHARS <= total <= 20
                    )
                    or total > 20
                )
            )
            should_reposition_caret = bool(
                should_stabilize
                and (
                    total > 20
                    or any(
                        character.isspace()
                        for character in intended_snapshot
                    )
                )
            )
            should_blur = should_stabilize and not should_reposition_caret
            if not should_blur and not should_reposition_caret:
                return await self._read_field(
                    current_readback_region(intended_snapshot),
                    intended=intended_snapshot,
                    precise=precise,
                    allow_semantic_spacing=allow_semantic_spacing,
                    allow_blind_fallback=intended_snapshot == text,
                )
            if should_continue is not None and not should_continue():
                return ""
            stable_field_read_performed = True
            if should_reposition_caret:
                # Some focused Windows fields draw the caret through the final
                # glyph, while moving focus with Tab can discard an address-bar
                # draft. Move the caret before the first glyph without changing,
                # selecting, or submitting text. This preserves normal text
                # contrast for OCR. Exact OCR must still verify the whole field
                # before any caller can press Enter.
                DEBUG.event(
                    "typing.caret_stabilizer",
                    method="caret_home",
                    character_count=len(intended_snapshot),
                    stage="started",
                )
                await self.backend.press_key("Home")
                await asyncio.sleep(_CLEAR_SETTLE_S)
                repositioned_region = current_readback_region(
                    intended_snapshot
                )
                repositioned_read = await self._read_field(
                    repositioned_region,
                    intended=intended_snapshot,
                    precise=precise,
                    allow_semantic_spacing=allow_semantic_spacing,
                    allow_blind_fallback=True,
                )
                selected_confidences = [
                    float(line.confidence)
                    for line in self._last_field_ocr_result.lines
                    if line.confidence is not None
                ]
                DEBUG.event(
                    "typing.caret_stabilizer",
                    method="caret_home",
                    character_count=len(intended_snapshot),
                    stage="completed",
                    readback_available=bool(repositioned_read),
                    readback_region=repositioned_region.model_dump(),
                    ocr_line_count=len(self._last_field_ocr_result.lines),
                    ocr_character_count=len(
                        self._last_field_ocr_result.text
                    ),
                    ocr_spacing_evidence=(
                        self._last_field_ocr_result.spacing_evidence
                    ),
                    ocr_mean_confidence=(
                        round(
                            sum(selected_confidences)
                            / len(selected_confidences),
                            4,
                        )
                        if selected_confidences
                        else None
                    ),
                )
                return repositioned_read
            moved_focus = False
            try:
                blurred_region = current_readback_region(
                    intended_snapshot
                )
                DEBUG.event(
                    "typing.caret_stabilizer",
                    method="blur",
                    character_count=len(intended_snapshot),
                    stage="started",
                    readback_region=blurred_region.model_dump(),
                )
                await self.backend.keypress(["Tab"])
                moved_focus = True
                await asyncio.sleep(_CLEAR_SETTLE_S)
                blurred_read = await self._read_field(
                    blurred_region,
                    intended=intended_snapshot,
                    precise=precise,
                    allow_semantic_spacing=allow_semantic_spacing,
                    allow_blind_fallback=True,
                )
                DEBUG.event(
                    "typing.caret_stabilizer",
                    method="blur",
                    character_count=len(intended_snapshot),
                    stage="completed",
                    readback_available=bool(blurred_read),
                )
                return blurred_read
            finally:
                if moved_focus:
                    with contextlib.suppress(Exception):
                        await self.backend.keypress(["ShiftLeft", "Tab"])
                    await asyncio.sleep(_CLEAR_SETTLE_S)

        async def maybe_correct(read_back: str, intended_snapshot: str) -> None:
            nonlocal corrections, last_read, verified_clean
            read_back = self._typed_candidate(read_back, intended_snapshot, precise)
            last_read = read_back
            semantic_spacing_match = (
                allow_semantic_spacing
                and self._last_read_semantic_spacing
                and norm(intended_snapshot, precise)
                == norm(strip_prompt(read_back), precise)
            )
            if norm(intended_snapshot, precise) == norm(text, precise):
                self._semantic_spacing_normalized = semantic_spacing_match
            if corrections >= MAX_TOTAL_CORRECTIONS:
                return
            # A correction re-types everything typed so far; don't start it if control
            # was just taken away.
            if should_continue is not None and not should_continue():
                return
            read_verdict = (
                "match"
                if semantic_spacing_match
                else compute_verdict(
                    intended_snapshot,
                    read_back,
                    precise,
                )
            )
            kind = classify_mismatch(intended_snapshot, read_back, precise)
            strong_precise_transport_mismatch = False
            credible_one_edit_read = False
            one_character_prefix_read = False
            long_precise_layout_like_read = (
                precise
                and single_line_field
                and len(intended_snapshot) > 20
                and kind in {"layout", "case"}
            )
            if long_precise_layout_like_read:
                # The final long single-line draft was already selected for a
                # same-focus OCR pass. Never move focus or replay it: Windows
                # Save As discards an unsubmitted address-bar draft on Tab.
                return
            if (
                precise
                and intended_snapshot == text
                and PRECISE_LOCATE_MIN_CHARS
                <= len(intended_snapshot)
                <= 20
                and "\n" not in read_back
                and levenshtein(
                    norm(intended_snapshot, True),
                    norm(read_back, True),
                    1,
                )
                == 1
            ):
                canonical_lines = [
                    line
                    for line in self._last_field_ocr_result.lines
                    if line.text.strip()
                ]
                credible_one_edit_read = (
                    len(canonical_lines) == 1
                    and canonical_lines[0].confidence is not None
                    and float(canonical_lines[0].confidence)
                    >= MIN_ONE_EDIT_RECHECK_CONFIDENCE
                    and canonical_lines[0].text.strip() == read_back.strip()
                )
                strong_precise_transport_mismatch = (
                    credible_one_edit_read
                    and float(canonical_lines[0].confidence) >= 0.95
                )
                one_character_prefix_read = (
                    len(read_back) + 1 == len(intended_snapshot)
                    and intended_snapshot.startswith(read_back)
                )
            if (
                single_line_field
                and (
                    strong_precise_transport_mismatch
                    or credible_one_edit_read
                    or one_character_prefix_read
                )
            ):
                # A focused single-line field can permanently include the
                # caret in a remote framebuffer frame. Temporarily move focus
                # to the next control, read the unchanged field without its
                # caret, then restore focus. This is reversible and provides
                # better evidence than erasing text based on one fused glyph
                # or a one-character OCR truncation.
                if should_continue is not None and not should_continue():
                    return
                moved_focus = False
                try:
                    await self.backend.keypress(["Tab"])
                    moved_focus = True
                    await asyncio.sleep(_CLEAR_SETTLE_S)
                    rechecked = self._typed_candidate(
                        await self._read_field(
                            current_readback_region(intended_snapshot),
                            intended=intended_snapshot,
                            precise=precise,
                            allow_semantic_spacing=allow_semantic_spacing,
                        ),
                        intended_snapshot,
                        precise,
                    )
                except Exception:
                    rechecked = ""
                finally:
                    if moved_focus:
                        with contextlib.suppress(Exception):
                            await self.backend.keypress(["ShiftLeft", "Tab"])
                        await asyncio.sleep(_CLEAR_SETTLE_S)
                if (
                    compute_verdict(
                        intended_snapshot,
                        rechecked,
                        precise,
                    )
                    in {"match", "contains"}
                ):
                    last_read = rechecked
                    if norm(intended_snapshot, precise) == norm(text, precise):
                        verified_clean = True
                    return
                if one_character_prefix_read:
                    last_read = rechecked or read_back
                    return
                if (
                    credible_one_edit_read
                    and not strong_precise_transport_mismatch
                ):
                    # A medium-confidence one-edit mismatch is enough to
                    # justify one reversible read with the caret blurred, but
                    # never enough to erase or replay the field.
                    last_read = rechecked or read_back
                    return
                repeated_lines = [
                    line
                    for line in self._last_field_ocr_result.lines
                    if line.text.strip()
                ]
                strong_precise_transport_mismatch = (
                    rechecked.strip() == read_back.strip()
                    and len(repeated_lines) == 1
                    and repeated_lines[0].confidence is not None
                    and float(repeated_lines[0].confidence) >= 0.95
                    and repeated_lines[0].text.strip() == rechecked.strip()
                )
                if not strong_precise_transport_mismatch:
                    last_read = rechecked
                    return
            elif strong_precise_transport_mismatch:
                # A focused Windows text field can visually fuse its blinking
                # caret to the final glyph: ``calc|`` is then read as ``cald``
                # with very high confidence. Never erase and replay from one
                # such frame. Sample a different caret phase first; an exact
                # second read proves the original delivery without more HID,
                # while a correction still requires the same strong mismatch
                # to persist independently.
                await asyncio.sleep(_CARET_BLINK_RECHECK_S)
                rechecked = self._typed_candidate(
                    await self._read_field(
                        current_readback_region(intended_snapshot),
                        intended=intended_snapshot,
                        precise=precise,
                        allow_semantic_spacing=allow_semantic_spacing,
                    ),
                    intended_snapshot,
                    precise,
                )
                if (
                    compute_verdict(
                        intended_snapshot,
                        rechecked,
                        precise,
                    )
                    in {"match", "contains"}
                ):
                    last_read = rechecked
                    if norm(intended_snapshot, precise) == norm(text, precise):
                        verified_clean = True
                    return
                repeated_lines = [
                    line
                    for line in self._last_field_ocr_result.lines
                    if line.text.strip()
                ]
                strong_precise_transport_mismatch = (
                    rechecked.strip() == read_back.strip()
                    and len(repeated_lines) == 1
                    and repeated_lines[0].confidence is not None
                    and float(repeated_lines[0].confidence) >= 0.95
                    and repeated_lines[0].text.strip() == rechecked.strip()
                )
                if not strong_precise_transport_mismatch:
                    last_read = rechecked
                    return
            if kind is None and not strong_precise_transport_mismatch:
                # A prefix-only OCR read has no confident mismatch kind, but it
                # is not a clean verification. Only an actual match/containment
                # can skip the final settled reread.
                if (
                    read_verdict in {"match", "contains"}
                    and norm(intended_snapshot, precise) == norm(text, precise)
                ):
                    verified_clean = True
                return
            if fast_print:
                # A long prose mismatch is not permission to clear and replay
                # an entire field. Stop with the observed evidence instead.
                return
            if (
                precise
                and kind not in {"layout", "case"}
                and not strong_precise_transport_mismatch
            ):
                # Exact code/commands are load-bearing, but noisy OCR is not
                # permission to erase them. Only strong layout/case signatures
                # or one strongly grounded short-field substitution may
                # self-correct. The corrected field must still read back exact.
                return
            if cur_region is None:
                return  # nothing to crop against — leave it to the agent
            corrections += 1
            if kind == "layout":
                cur = self.backend.get_layout()
                nxt = "uk" if cur == "us" else "us"
                self.backend.set_layout(nxt)
            elif kind == "case":
                # RFB does not expose the guest LED state.  A pure case inversion
                # is therefore stronger evidence than the adapter's cached state.
                await self.backend.press_key("CapsLock")
            await self._clear_recent_input(len(typed_so_far))
            await asyncio.sleep(_CLEAR_SETTLE_S)
            await emit_text(typed_so_far)

        grid_prev = await self._grid()
        dense_prev = self._last_grid_frame
        if (
            dense_prev is not None
            and dense_prev.width > 0
            and dense_prev.height > 0
        ):
            # PiKVM/VNC may advertise the previous boot resolution until the
            # first post-reboot frame arrives. Geometry derived from that stale
            # cache can place an otherwise correct OCR crop below the real
            # image. The captured frame is the source of truth for every
            # changed-pixel coordinate used by this typing transaction.
            dims = (dense_prev.width, dense_prev.height)
        emission_start_grid = grid_prev
        emission_start_frame = dense_prev

        for i, chunk in enumerate(chunks):
            # Cooperative cancellation: an abort / panic / steer between chunks stops the
            # type MID-text. Drop any held keys first so a half-finished combo/modifier
            # doesn't stick on the target.
            if should_continue is not None and not should_continue():
                await self._release_all_quietly()
                return self._halted_result(
                    status="blocked_by_policy",
                    field_text=last_read,
                    corrected=corrections > 0,
                    correction_count=corrections,
                    delivery_retries=delivery_retries,
                    typed_characters=len(typed_so_far),
                    intended_characters=len(text),
                    used_fast_path=fast_print,
                    summary=INTERRUPTED_SUMMARY,
                    intended_text=text,
                    emitted_text="".join(emitted_parts),
                    readback_frame_sha256=self._last_readback_frame_sha256,
                )
            if (
                i > 0
                and can_vision
                and cur_region is not None
                and grid_prev is not None
            ):
                preflight_grid = await self._grid()
                preflight_frame = self._last_grid_frame
                if (
                    preflight_grid is not None
                    and _substantial_change_outside_region(
                        grid_prev,
                        preflight_grid,
                        cur_region,
                        dims,
                    )
                ):
                    relocated = None
                    if (
                        fast_print
                        and not precise
                        and not secret
                        and not explicit_region
                    ):
                        # Rich editors can reflow the page while a paragraph is
                        # growing. Treat that as expected movement only when a
                        # fresh full-screen read independently relocates the
                        # most recently acknowledged exact chunk. Never weaken
                        # the explicit-region, code, command, or secret paths.
                        screen = await self._read_screen()
                        relocated = self._locate_ocr_candidate(
                            screen,
                            chunks[i - 1],
                            dims,
                            precise=False,
                        )
                        if relocated is None:
                            relocated = self._locate_wrapped_prose_tail(
                                screen,
                                typed_so_far,
                                dims,
                            )
                        moved_region = locate_changed_bbox(
                            grid_prev,
                            preflight_grid,
                            dims,
                        )
                        if (
                            relocated is not None
                            and (
                                moved_region is None
                                or not regions_overlap(
                                    relocated,
                                    moved_region,
                                )
                            )
                        ):
                            relocated = None
                    if relocated is not None:
                        cur_region = relocated
                        located = True
                    else:
                        await self._release_all_quietly()
                        return self._halted_result(
                            status="failed_focus_lost",
                            field_text=last_read,
                            corrected=corrections > 0,
                            correction_count=corrections,
                            delivery_retries=delivery_retries,
                            typed_characters=len(typed_so_far),
                            intended_characters=len(text),
                            used_fast_path=fast_print,
                            summary=FOCUS_CHANGED_SUMMARY,
                            intended_text=text,
                            emitted_text="".join(emitted_parts),
                            readback_frame_sha256=self._last_readback_frame_sha256,
                        )
                if preflight_grid is not None:
                    grid_prev = preflight_grid
                    dense_prev = preflight_frame
            await emit_text(chunk)
            typed_so_far += chunk
            grid_now = await self._grid()
            dense_now = self._last_grid_frame
            if (
                dense_now is not None
                and dense_now.width > 0
                and dense_now.height > 0
            ):
                dims = (dense_now.width, dense_now.height)
            chunk_change = (
                locate_changed_bbox(grid_prev, grid_now, dims)
                if grid_prev is not None and grid_now is not None
                else None
            )
            if (
                chunk_change is None
                and dense_prev is not None
                and dense_now is not None
            ):
                chunk_change = await asyncio.to_thread(
                    locate_dense_changed_bbox,
                    dense_prev.data,
                    dense_now.data,
                    dims,
                )

            # Keyboard input is not idempotent. A stale frame cannot distinguish
            # "nothing landed" from "the boundary space landed but the glyphs
            # have not painted yet", so replaying this chunk can duplicate text.
            # Keep the original emission at-most-once and let the settled
            # read-back below stop the transaction as unverified if it is short.

            # Auto-locate the field from the changed pixels (skipped if the caller
            # gave an explicit region); grow the box each chunk so it spans the line.
            locate_min_chars = (
                PRECISE_LOCATE_MIN_CHARS
                if precise
                else LOCATE_MIN_CHARS
            )
            if not explicit_region and len(typed_so_far) >= locate_min_chars:
                loc = chunk_change
                if loc is not None:
                    # Search boxes and autocomplete fields can repaint a large
                    # results panel after the first chunk. The grid delta then
                    # describes the whole dynamic surface rather than the text
                    # field, so a cropped read sees unrelated result text. If
                    # full-screen OCR can locate the exact emitted text, narrow
                    # this suspiciously tall delta to that line before read-back.
                    max_field_height = max(
                        MAX_AUTODETECTED_FIELD_HEIGHT,
                        dims[1] * MAX_AUTODETECTED_FIELD_HEIGHT_FRAC,
                    )
                    if not secret and loc.height > max_field_height:
                        ocr_loc = self._locate_ocr_candidate(
                            await self._read_screen(),
                            typed_so_far,
                            dims,
                            precise=precise,
                        )
                        if ocr_loc is not None:
                            loc = ocr_loc
                    DEBUG.event(
                        "typing.field_located",
                        precise=precise,
                        typed_characters=len(typed_so_far),
                        suspicious_tall=loc.height > max_field_height,
                        region=loc.model_dump(),
                    )
                    cur_region = union_region(cur_region, loc) if located else loc
                    located = True
                elif (
                    not located
                    and not secret
                    and len(typed_so_far) >= locate_min_chars
                ):
                    # Remote VNC video can trail acknowledged HID by seconds.
                    # Take bounded, read-only samples before
                    # concluding that the field was not focused. Never emit
                    # more text here: continue only when pixels or exact
                    # grounded OCR prove that this first chunk landed.
                    for settle_s in (
                        _VIDEO_RETRY_SETTLE_S,
                        _PRINT_SETTLE_S,
                        _SLOW_VIDEO_RETRY_SETTLE_S,
                        _VERY_SLOW_VIDEO_RETRY_SETTLE_S,
                    ):
                        await asyncio.sleep(settle_s)
                        grid_retry = await self._grid()
                        dense_retry = self._last_grid_frame
                        retry_loc = (
                            locate_changed_bbox(grid_prev, grid_retry, dims)
                            if grid_prev is not None and grid_retry is not None
                            else None
                        )
                        if (
                            retry_loc is None
                            and dense_prev is not None
                            and dense_retry is not None
                        ):
                            retry_loc = await asyncio.to_thread(
                                locate_dense_changed_bbox,
                                dense_prev.data,
                                dense_retry.data,
                                dims,
                            )
                        if retry_loc is not None:
                            cur_region = retry_loc
                            located = True
                            grid_now = grid_retry
                            dense_now = dense_retry
                            break

                        # Some VNC encoders quantize small dark-theme glyph
                        # changes below the grid threshold. Accept only grounded
                        # OCR evidence that the just-typed text is on screen.
                        ocr_loc = self._locate_ocr_candidate(
                            await self._read_screen(),
                            typed_so_far,
                            dims,
                            precise=precise,
                        )
                        if ocr_loc is not None:
                            cur_region = ocr_loc
                            located = True
                            break

                    if (
                        not located
                        and len(typed_so_far) >= ABORT_MIN_CHARS
                    ):
                        # No pixel or OCR evidence ⇒ wrong target.
                        return self._halted_result(
                            status="failed_focus_lost",
                            field_text="",
                            corrected=False,
                            correction_count=corrections,
                            delivery_retries=delivery_retries,
                            used_fast_path=fast_print,
                            typed_characters=len(typed_so_far),
                            intended_characters=len(text),
                            summary=NO_FOCUS_SUMMARY,
                            intended_text=text,
                            emitted_text="".join(emitted_parts),
                            readback_frame_sha256=self._last_readback_frame_sha256,
                        )
            if grid_now is not None:
                grid_prev = grid_now
                dense_prev = dense_now

            if cadence(i) and cur_region is not None:
                rb = await read_current_field(typed_so_far)
                await maybe_correct(rb, typed_so_far)
                if corrections > 0:
                    grid_prev = await self._grid()  # field changed under us
                    dense_prev = self._last_grid_frame

        # Final correctness check if we never got a clean read mid-stream.
        if not verified_clean and cur_region is not None and can_vision:
            corrections_before = corrections
            rb = await read_current_field(text)
            await maybe_correct(rb, text)
            if corrections > corrections_before:
                # The final read triggered a clear+retype — re-read so the verdict
                # reflects the corrected field, not the pre-correction mismatch.
                last_read = await self._read_field(
                    current_readback_region(),
                    intended=text,
                    precise=precise,
                    allow_semantic_spacing=allow_semantic_spacing,
                    allow_blind_fallback=True,
                )
            elif (
                precise
                and compute_verdict(text, last_read, precise) == "unverified"
            ):
                # VNC/X11 can acknowledge all HID events before the final glyphs
                # are painted. A prefix-only read is therefore not yet proof of
                # truncation. Take three bounded, increasingly delayed reads,
                # grow the auto-located crop if late pixels appear, and accept
                # only exact/semantically safe evidence. This never emits more
                # HID, and Enter remains the caller's separate action.
                for settle_s in _PRECISE_READBACK_SETTLES_S:
                    await asyncio.sleep(settle_s)
                    if not explicit_region:
                        settled_grid = await self._grid()
                        settled_frame = self._last_grid_frame
                        late_region = (
                            locate_changed_bbox(grid_prev, settled_grid, dims)
                            if grid_prev is not None
                            and settled_grid is not None
                            else None
                        )
                        if (
                            late_region is None
                            and dense_prev is not None
                            and settled_frame is not None
                        ):
                            late_region = await asyncio.to_thread(
                                locate_dense_changed_bbox,
                                dense_prev.data,
                                settled_frame.data,
                                dims,
                            )
                        if late_region is not None:
                            cur_region = union_region(cur_region, late_region)
                    settled_read = self._typed_candidate(
                        await self._read_field(
                            current_readback_region(),
                            intended=text,
                            precise=precise,
                            allow_semantic_spacing=allow_semantic_spacing,
                            allow_blind_fallback=True,
                        ),
                        text,
                        precise,
                    )
                    semantic_spacing_match = (
                        allow_semantic_spacing
                        and self._last_read_semantic_spacing
                        and norm(text, precise)
                        == norm(strip_prompt(settled_read), precise)
                    )
                    if (
                        semantic_spacing_match
                        or compute_verdict(text, settled_read, precise)
                        in {"match", "contains"}
                    ):
                        last_read = settled_read
                        self._semantic_spacing_normalized = (
                            semantic_spacing_match
                        )
                        break

        if (
            precise
            and not explicit_region
            and cur_region is not None
            and compute_verdict(text, last_read, precise)
            not in {"match", "contains"}
        ):
            # A thin changed-pixel crop can miss exact text in editors and
            # terminals even when the full payload is legible. Take a fresh
            # precise full-frame read and accept only a complete line whose
            # bbox overlaps the field changed by this exact emission. This is
            # read-only; any subsequent commit remains a separate action.
            for settle_s in _PRECISE_FULL_SCREEN_SETTLES_S:
                if settle_s:
                    await asyncio.sleep(settle_s)
                full_screen_result = await self._read_screen(precise=True)
                full_screen_frame = self._last_read_screen_frame
                full_screen_match = self._full_screen_exact_line_candidate(
                    full_screen_result,
                    text,
                    dims,
                    allow_semantic_spacing=allow_semantic_spacing,
                )
                capture_change = await asyncio.to_thread(
                    locate_capture_change,
                    emission_start_grid,
                    emission_start_frame,
                    full_screen_frame,
                    dims,
                )

                def grounded(candidate_region: Region) -> bool:
                    return (
                        regions_overlap(
                            candidate_region,
                            current_readback_region(),
                        )
                        or (
                            capture_change is not None
                            and regions_overlap(
                                candidate_region,
                                capture_change,
                            )
                        )
                    )

                if (
                    full_screen_match is not None
                    and grounded(full_screen_match[1])
                ):
                    last_read = full_screen_match[0]
                    self._semantic_spacing_normalized = (
                        full_screen_match[2]
                    )
                    break

                prefix_region = self._full_screen_exact_prefix_region(
                    full_screen_result,
                    text,
                    dims,
                )
                if prefix_region is None or not grounded(prefix_region):
                    continue
                cropped_read = await self._read_field(
                    prefix_region,
                    precise=True,
                    minimum_confidence=(
                        MIN_GROUNDED_EXACT_OCR_CONFIDENCE
                    ),
                )
                def exact_crop_candidate(
                    candidate_text: str,
                ) -> tuple[str, Region, bool] | None:
                    candidate_lines = candidate_text.splitlines()
                    if not candidate_lines:
                        return None
                    row_height = max(
                        1,
                        round(
                            prefix_region.height
                            / len(candidate_lines)
                        ),
                    )
                    candidate_result = OCRResult(
                        lines=[
                            OCRLine(
                                text=line,
                                confidence=1.0,
                                bbox=[
                                    int(prefix_region.x),
                                    (
                                        int(prefix_region.y)
                                        + index * row_height
                                    ),
                                    int(
                                        prefix_region.x
                                        + prefix_region.width
                                    ),
                                    min(
                                        dims[1],
                                        int(prefix_region.y)
                                        + (index + 1) * row_height,
                                    ),
                                ],
                            )
                            for index, line in enumerate(candidate_lines)
                        ]
                    )
                    return self._full_screen_exact_line_candidate(
                        candidate_result,
                        text,
                        dims,
                        allow_semantic_spacing=allow_semantic_spacing,
                    )

                cropped_match = exact_crop_candidate(cropped_read)
                if cropped_match is None:
                    # Selected OCR can retain a one-character suffix artifact
                    # while two independent scale reads agree exactly. The
                    # crop is already grounded to the causal terminal rows;
                    # require two exact alternatives before trusting either.
                    alternative_matches = [
                        match
                        for alternative in (
                            self._last_field_ocr_result.alternatives
                        )
                        if (
                            match := exact_crop_candidate(
                                alternative.text
                            )
                        )
                        is not None
                    ]
                    if len(alternative_matches) >= 2:
                        cropped_match = alternative_matches[0]
                if cropped_match is not None:
                    last_read = cropped_match[0]
                    self._semantic_spacing_normalized = cropped_match[2]
                    break

        if (
            fast_print
            and compute_verdict(text, last_read, precise)
            not in {"match", "contains"}
        ):
            # Rich editors wrap prose beyond the first changed-line crop. Do
            # one read-only full-screen pass and accept only a complete exact
            # occurrence, never an approximate OCR similarity.
            screen_candidate = self._full_screen_prose_candidate(
                await self._read_screen(),
                text,
            )
            if screen_candidate:
                last_read = screen_candidate

        verdict = compute_verdict(text, last_read, precise)
        if self._semantic_spacing_normalized:
            verdict = "match"
        corrected = corrections > 0 or delivery_retries > 0
        return self._finalise(
            text,
            last_read,
            verdict,
            corrected,
            used_fast_path=fast_print,
            precise=precise,
            correction_count=corrections,
            delivery_retries=delivery_retries,
            emitted_text="".join(emitted_parts),
        )

    # ---- result assembly -------------------------------------------------- #

    async def _release_all_quietly(self) -> None:
        """Best-effort drop of every held key/button (the backend exposes it; the
        fake does too). Used when typing is interrupted so nothing stays pressed."""
        rel = getattr(self.backend, "release_all", None)
        if callable(rel):
            with contextlib.suppress(Exception):
                await rel()

    @staticmethod
    def _halted_result(
        *,
        status: VerificationStatus,
        field_text: str,
        corrected: bool,
        used_fast_path: bool,
        summary: str,
        typed_characters: int,
        intended_characters: int,
        correction_count: int = 0,
        delivery_retries: int = 0,
        intended_text: str = "",
        emitted_text: str = "",
        readback_frame_sha256: str = "",
    ) -> WatchedTypingResult:
        return WatchedTypingResult(
            verdict="mismatch",
            ok=False,
            status=status,
            field_text=field_text,
            corrected=corrected,
            used_fast_path=used_fast_path,
            summary=summary,
            typed_characters=typed_characters,
            intended_characters=intended_characters,
            correction_count=correction_count,
            delivery_retries=delivery_retries,
            emitted_characters=len(emitted_text),
            emitted_sha256=(
                hashlib.sha256(emitted_text.encode("utf-8")).hexdigest()
                if emitted_text
                else ""
            ),
            emitted_exactly_once=bool(
                emitted_text
                and intended_text
                and emitted_text == intended_text
            ),
            readback_frame_sha256=readback_frame_sha256,
        )

    def _finalise(
        self,
        intended: str,
        field_text: str,
        verdict: Verdict,
        corrected: bool,
        *,
        used_fast_path: bool,
        precise: bool,
        correction_count: int,
        delivery_retries: int,
        emitted_text: str,
    ) -> WatchedTypingResult:
        # Reuse the verifier for the authoritative status (the only thing allowed to
        # declare typed text verified or failed). Verdict drives the summary text.
        vr: VerificationResult = verify_text(intended, field_text, code=precise)
        status = vr.status
        if self._semantic_spacing_normalized:
            status = "verified_safe_normalized"

        head = "Typed (fast)" if used_fast_path else "Typed"
        if verdict == "mismatch":
            summary = f"{head}, but read-back still doesn't match — check the field."
        elif corrected and vr.safe_to_continue:
            summary = f"{head} and self-corrected (verified the field)."
        elif corrected:
            summary = (
                f"{head} and self-corrected, but read-back is still ambiguous."
            )
        elif verdict == "unverified":
            summary = (
                f"{head}; read-back only verified part of the field."
                if field_text
                else f"{head}."
            )
        elif self._semantic_spacing_normalized:
            summary = (
                f"{head} and verified the terminal argv with safe "
                "whitespace normalization."
            )
        else:
            summary = f"{head} and verified the field reads correctly."

        return WatchedTypingResult(
            verdict=verdict,
            # ``ok`` remains the legacy "not a confirmed mismatch" signal.
            # Callers that may continue a transaction must use ``status`` /
            # VerificationResult.verified, never this compatibility field.
            ok=verdict != "mismatch",
            status=status,
            field_text=field_text,
            corrected=corrected,
            used_fast_path=used_fast_path,
            summary=summary,
            typed_characters=len(intended),
            intended_characters=len(intended),
            correction_count=correction_count,
            delivery_retries=delivery_retries,
            emitted_characters=len(emitted_text),
            emitted_sha256=hashlib.sha256(
                emitted_text.encode("utf-8")
            ).hexdigest(),
            emitted_exactly_once=emitted_text == intended,
            readback_frame_sha256=self._last_readback_frame_sha256,
        )
