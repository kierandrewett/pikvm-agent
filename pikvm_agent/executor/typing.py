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
correction are Home / Delete / Backspace / End / CapsLock. Committing is the
caller's job.

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
ABORT_MIN_CHARS = 8       # only HARD-fail "no focus" when ≥ this typed
MAX_BOX_HEIGHT_FRAC = 0.6  # a change taller than this frac of screen = repaint
CHUNK_TARGET = 16         # word-boundary chunk target length
MAX_TOTAL_CORRECTIONS = 1  # one clean retry; never a compounding loop
MAX_BACKSPACES = 400      # safety cap on a correction's clear
FAST_PRINT_MIN = 120      # above this, plain text takes the (bursty) fast print path;
                          # shorter text stays on the fully-humanized per-key path
MIN_MISMATCH_OCR_CONFIDENCE = 0.78
MAX_AUTODETECTED_FIELD_HEIGHT = 80
MAX_AUTODETECTED_FIELD_HEIGHT_FRAC = 0.15
MAX_PROSE_EDGE_CONTEXT_CHARS = 96
AUTODETECTED_READBACK_MARGIN_X_FRAC = 0.075
DENSE_PIXEL_DELTA = 10
DENSE_MIN_CHANGED_PIXELS = 80
DENSE_MIN_WIDTH = 8
DENSE_MIN_HEIGHT = 4
DENSE_MAX_HEIGHT = 64

# Pauses (seconds) — let a print / clear land and the video settle before reading.
_PRINT_SETTLE_S = 0.45
_CLEAR_SETTLE_S = 0.15
_VIDEO_RETRY_SETTLE_S = 0.20
_PRECISE_READBACK_SETTLES_S = (0.45, 0.90, 1.80)

_SIMPLE_TERMINAL_ARGV = re.compile(
    r"[A-Za-z0-9_./:@=+-]+(?: [A-Za-z0-9_./:@=+-]+)*"
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


def readback_region(
    region: Region,
    dims: tuple[int, int],
    *,
    explicit: bool,
) -> Region:
    """Add OCR context without weakening the narrower focus-theft guard."""

    if explicit:
        return region
    width, height = dims
    if width <= 0 or height <= 0:
        return region
    margin_x = max(16, round(width * AUTODETECTED_READBACK_MARGIN_X_FRAC))
    x = max(0, int(region.x) - margin_x)
    x2 = min(width, math.ceil(region.x + region.width) + margin_x)
    y = max(0, int(region.y))
    y2 = min(height, math.ceil(region.y + region.height))
    return Region(
        x=x,
        y=y,
        width=max(1, x2 - x),
        height=max(1, y2 - y),
    )


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
    ) -> str:
        """OCR the field. Capture the FULL frame and pass the region to the OCR
        provider so it reads the field crop on every backend: file OCR
        (tesseract) crops the saved frame by region, while live PiKVM OCR reads
        that region on the live screen — never the whole frame. ``""`` on failure."""
        self._last_read_semantic_spacing = False
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
            fd = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            fd.write(frame.data)
            fd.close()
            tmp = Path(fd.name)
            precise_ocr = getattr(self.ocr, "ocr_precise", None)
            if precise and callable(precise_ocr):
                result = await precise_ocr(tmp, region=region)
            else:
                result = await self.ocr.ocr(tmp, region=region)
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
            confidences = [
                float(line.confidence)
                for line in result.lines
                if line.confidence is not None
            ]
            if (
                confidences
                and sum(confidences) / len(confidences)
                < MIN_MISMATCH_OCR_CONFIDENCE
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
        try:
            frame = await self.backend.screenshot()
        except Exception:
            return OCRResult()
        if not frame or not frame.data:
            return OCRResult()
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
        """Return one grounded complete line matching exact terminal input.

        This is deliberately narrower than substring localization: the
        prompt-stripped OCR line itself must be the complete emitted command.
        Its geometry is retained so callers can require overlap with the
        changed-pixel field instead of accepting matching task text elsewhere.
        """

        width, height = dims
        target = intended.strip()
        if not target or width <= 0 or height <= 0:
            return None
        for line in result.lines:
            if (
                line.confidence is not None
                and float(line.confidence) < MIN_MISMATCH_OCR_CONFIDENCE
            ):
                continue
            region = ocr_line_region(line, dims)
            if region is None:
                continue
            observed = strip_prompt(line.text).strip()
            spacing_normalized = False
            if observed != target:
                spacing_normalized = (
                    allow_semantic_spacing
                    and _SIMPLE_TERMINAL_ARGV.fullmatch(target) is not None
                    and norm(observed, True) == norm(target, True)
                )
                if not spacing_normalized:
                    continue
            return (observed, region, spacing_normalized)
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
        that makes the result conservatively ambiguous.
        """
        del intended, precise
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

        # FAST TRANSPORT: long, plain (non-exact, non-secret) prose uses the
        # server-side keymap printer, but remains chunked and visually guarded.
        # Caps-on disables it (the printer cannot compensate per letter);
        # Commands/code, short text, and secrets stay on the per-key transport.
        # Exact natural-language verification may still use guarded printer
        # chunks: transport choice and OCR strictness are separate concerns.
        print_text = getattr(self.backend, "print_text", None)
        caps_on = self.backend.get_caps_lock()
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
            and not code
            and (prose or not is_exact_text(delivery_text))
            and total > FAST_PRINT_MIN
            and caps_on is not True
        ):
            return await self._humanized(
                delivery_text,
                region=region,
                code=code,
                secret=secret,
                precise=precise,
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
        allow_semantic_spacing: bool,
        should_continue: Callable[[], bool] | None = None,
        fast_print: bool = False,
    ) -> WatchedTypingResult:
        dims = self._dims()
        chunks = chunk_text(text)
        total = len(text)
        explicit_region = region is not None
        located = explicit_region
        cur_region: Region | None = region
        typed_so_far = ""
        corrections = 0
        delivery_retries = 0
        last_read = ""
        verified_clean = False
        can_vision = not secret and total > 4
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

        def current_readback_region() -> Region:
            assert cur_region is not None
            return readback_region(
                cur_region,
                dims,
                explicit=explicit_region,
            )

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
            if kind is None:
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
            if precise and kind not in {"layout", "case"}:
                # Exact code/commands are load-bearing, but noisy OCR is not
                # permission to erase them. Only strong layout/case signatures
                # may self-correct.
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
            if not explicit_region and len(typed_so_far) >= LOCATE_MIN_CHARS:
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
                    cur_region = union_region(cur_region, loc) if located else loc
                    located = True
                elif not located and not secret and len(typed_so_far) >= ABORT_MIN_CHARS:
                    # Remote VNC video can trail acknowledged HID by more than
                    # one frame. Take two bounded, read-only samples before
                    # concluding that the field was not focused. Never emit
                    # more text here: continue only when pixels or exact
                    # grounded OCR prove that this first chunk landed.
                    for settle_s in (
                        _VIDEO_RETRY_SETTLE_S,
                        _PRINT_SETTLE_S,
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

                    if not located:
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
                rb = await self._read_field(
                    current_readback_region(),
                    intended=typed_so_far,
                    precise=precise,
                    allow_semantic_spacing=allow_semantic_spacing,
                )
                await maybe_correct(rb, typed_so_far)
                if corrections > 0:
                    grid_prev = await self._grid()  # field changed under us
                    dense_prev = self._last_grid_frame

        # Final correctness check if we never got a clean read mid-stream.
        if not verified_clean and cur_region is not None and can_vision:
            corrections_before = corrections
            rb = await self._read_field(
                current_readback_region(),
                intended=text,
                precise=precise,
                allow_semantic_spacing=allow_semantic_spacing,
            )
            await maybe_correct(rb, text)
            if corrections > corrections_before:
                # The final read triggered a clear+retype — re-read so the verdict
                # reflects the corrected field, not the pre-correction mismatch.
                last_read = await self._read_field(
                    current_readback_region(),
                    intended=text,
                    precise=precise,
                    allow_semantic_spacing=allow_semantic_spacing,
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
            and allow_semantic_spacing
            and not explicit_region
            and cur_region is not None
            and compute_verdict(text, last_read, precise)
            not in {"match", "contains"}
        ):
            # A thin changed-pixel crop can miss dark-theme terminal glyphs
            # even when the full command is legible. Take one fresh precise
            # full-frame read and accept only a complete prompt-stripped line
            # whose bbox overlaps the field changed by this exact emission.
            # This is read-only; Enter remains a separate guarded action.
            full_screen_match = self._full_screen_exact_line_candidate(
                await self._read_screen(precise=True),
                text,
                dims,
                allow_semantic_spacing=allow_semantic_spacing,
            )
            if (
                full_screen_match is not None
                and regions_overlap(
                    full_screen_match[1],
                    current_readback_region(),
                )
            ):
                last_read = full_screen_match[0]
                self._semantic_spacing_normalized = full_screen_match[2]

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
