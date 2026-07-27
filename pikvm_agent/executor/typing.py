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
import math
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np
from pydantic import BaseModel

from pikvm_agent.core.models import (
    CapturedFrame,
    OCRResult,
    Region,
    VerificationResult,
    VerificationStatus,
)
from pikvm_agent.executor.verification import (
    Verdict,
    classify_mismatch,
    compute_verdict,
    is_exact_text,
    levenshtein,
    norm,
    verify_text,
)
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
MIN_EXPECTED_AWARE_EXACT_CHARS = 8
MAX_AUTODETECTED_FIELD_HEIGHT = 80
MAX_AUTODETECTED_FIELD_HEIGHT_FRAC = 0.15
MAX_PROSE_EDGE_CONTEXT_CHARS = 96

# Pauses (seconds) — let a print / clear land and the video settle before reading.
_PRINT_SETTLE_S = 0.45
_CLEAR_SETTLE_S = 0.15
_VIDEO_RETRY_SETTLE_S = 0.20

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
        try:
            frame = await self.backend.screenshot()
        except Exception:
            return None
        if not frame or not frame.data:
            return None
        return await asyncio.to_thread(grid, frame.data)

    async def _read_field(
        self,
        region: Region,
        *,
        intended: str | None = None,
        precise: bool = False,
    ) -> str:
        """OCR the field. Capture the FULL frame and pass the region to the OCR
        provider so it reads the field crop on every backend: file OCR
        (tesseract) crops the saved frame by region, while live PiKVM OCR reads
        that region on the live screen — never the whole frame. ``""`` on failure."""
        try:
            frame = await self.backend.screenshot()
        except Exception:
            return ""
        if not frame or not frame.data:
            return ""
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
                and len(norm(intended, True)) >= MIN_EXPECTED_AWARE_EXACT_CHARS
            ):
                for candidate in (
                    result.text,
                    *(alternative.text for alternative in result.alternatives),
                ):
                    if compute_verdict(intended, candidate, True) in {
                        "match",
                        "contains",
                    }:
                        return candidate
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

    async def _read_screen(self) -> OCRResult:
        """OCR one exact full-frame capture for localization fallback."""
        try:
            frame = await self.backend.screenshot()
        except Exception:
            return OCRResult()
        if not frame or not frame.data:
            return OCRResult()
        tmp: Path | None = None
        try:
            fd = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            fd.write(frame.data)
            fd.close()
            tmp = Path(fd.name)
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
            box = line.bbox
            if (
                not isinstance(box, list)
                or len(box) != 4
                or any(isinstance(value, list) for value in box)
            ):
                continue
            x0, y0, x1, y1 = (int(value) for value in box)
            pad = 8
            x = max(0, x0 - pad)
            y = max(0, y0 - pad)
            return Region(
                x=x,
                y=y,
                width=max(1, min(width, x1 + pad) - x),
                height=max(1, min(height, y1 + pad) - y),
            )
        return None

    @staticmethod
    def _typed_candidate(read_back: str, intended: str, precise: bool) -> str:
        """Extract the newest case-only occurrence from surrounding OCR text."""
        if not precise or not intended:
            return read_back
        folded_read = read_back.casefold()
        folded_intended = intended.casefold()
        if len(folded_read) != len(read_back) or len(folded_intended) != len(intended):
            return read_back
        index = folded_read.rfind(folded_intended)
        if index < 0:
            return read_back
        return read_back[index : index + len(intended)]

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
        secret: bool = False,
        should_continue: Callable[[], bool] | None = None,
    ) -> WatchedTypingResult:
        """Type ``text`` while watching the field; verify (and at most once correct).

        ``should_continue`` (when given) is polled between word-boundary chunks: if it
        ever returns False — an abort / panic / steer bumped the controller epoch — the
        typer drops any held keys and stops MID-text instead of running the whole string
        to completion. This makes a long ``type_text`` interruptible, not just the gaps
        between transactions."""
        precise = code or (is_exact_text(text) and not prose)
        total = len(text)

        # FAST TRANSPORT: long, plain (non-exact, non-secret) prose uses the
        # server-side keymap printer, but remains chunked and visually guarded.
        # Caps-on disables it (the printer cannot compensate per letter);
        # precise/short/secret text stays on the per-key transport.
        print_text = getattr(self.backend, "print_text", None)
        caps_on = self.backend.get_caps_lock()
        if should_continue is not None and not should_continue():
            await self._release_all_quietly()
            return self._halted_result(
                status="blocked_by_policy",
                field_text="",
                corrected=False,
                typed_characters=0,
                intended_characters=len(text),
                used_fast_path=False,
                summary=INTERRUPTED_SUMMARY,
            )
        if (
            callable(print_text)
            and not secret
            and not precise
            and total > FAST_PRINT_MIN
            and caps_on is not True
        ):
            return await self._humanized(
                text,
                region=region,
                code=code,
                secret=secret,
                precise=precise,
                should_continue=should_continue,
                fast_print=True,
            )

        return await self._humanized(
            text, region=region, code=code, secret=secret, precise=precise,
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

        async def emit_text(value: str) -> None:
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

        async def maybe_correct(read_back: str, intended_snapshot: str) -> None:
            nonlocal corrections, last_read, verified_clean
            read_back = self._typed_candidate(read_back, intended_snapshot, precise)
            last_read = read_back
            if corrections >= MAX_TOTAL_CORRECTIONS:
                return
            # A correction re-types everything typed so far; don't start it if control
            # was just taken away.
            if should_continue is not None and not should_continue():
                return
            read_verdict = compute_verdict(
                intended_snapshot,
                read_back,
                precise,
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
            await self.backend.type_text(typed_so_far, code=code, secret=secret)

        grid_prev = await self._grid()

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
                )
            if (
                i > 0
                and can_vision
                and cur_region is not None
                and grid_prev is not None
            ):
                preflight_grid = await self._grid()
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
                        relocated = self._locate_ocr_candidate(
                            await self._read_screen(),
                            chunks[i - 1],
                            dims,
                            precise=False,
                        )
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
                        )
                if preflight_grid is not None:
                    grid_prev = preflight_grid
            await emit_text(chunk)
            typed_so_far += chunk
            grid_now = await self._grid()
            chunk_change = (
                locate_changed_bbox(grid_prev, grid_now, dims)
                if grid_prev is not None and grid_now is not None
                else None
            )

            # A transport can acknowledge a chunk whose final key events never
            # reached the guest. Once the field is already grounded, no
            # meaningful pixel change is stronger evidence than OCR alone that
            # this exact chunk did not land. Retry that chunk once, never the
            # whole field, and never emit a commit key.
            if (
                (located or explicit_region)
                and i > 0
                and chunk_change is None
                and len(chunk.strip()) >= 2
                and precise
                and not secret
            ):
                await asyncio.sleep(_VIDEO_RETRY_SETTLE_S)
                settled_grid = await self._grid()
                settled_change = (
                    locate_changed_bbox(grid_prev, settled_grid, dims)
                    if grid_prev is not None and settled_grid is not None
                    else None
                )
                delivery_read = (
                    self._typed_candidate(
                        await self._read_field(
                            cur_region,
                            intended=typed_so_far,
                            precise=precise,
                        ),
                        typed_so_far,
                        precise,
                    )
                    if settled_change is None and cur_region is not None
                    else ""
                )
                previous_text = typed_so_far[: -len(chunk)]
                if (
                    settled_change is None
                    and compute_verdict(
                        previous_text,
                        delivery_read,
                        precise,
                    )
                    == "match"
                    and compute_verdict(
                        typed_so_far,
                        delivery_read,
                        precise,
                    )
                    == "unverified"
                    and (
                        should_continue is None
                        or should_continue()
                    )
                ):
                    await emit_text(chunk)
                    delivery_retries += 1
                    grid_now = await self._grid()
                    chunk_change = (
                        locate_changed_bbox(grid_prev, grid_now, dims)
                        if grid_prev is not None and grid_now is not None
                        else None
                    )
                elif settled_grid is not None:
                    grid_now = settled_grid
                    chunk_change = settled_change

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
                        retry_loc = (
                            locate_changed_bbox(grid_prev, grid_retry, dims)
                            if grid_prev is not None and grid_retry is not None
                            else None
                        )
                        if retry_loc is not None:
                            cur_region = retry_loc
                            located = True
                            grid_now = grid_retry
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
                        )
            if grid_now is not None:
                grid_prev = grid_now

            if cadence(i) and cur_region is not None:
                rb = await self._read_field(
                    cur_region,
                    intended=typed_so_far,
                    precise=precise,
                )
                await maybe_correct(rb, typed_so_far)
                if corrections > 0:
                    grid_prev = await self._grid()  # field changed under us

        # Final correctness check if we never got a clean read mid-stream.
        if not verified_clean and cur_region is not None and can_vision:
            corrections_before = corrections
            rb = await self._read_field(
                cur_region,
                intended=text,
                precise=precise,
            )
            await maybe_correct(rb, text)
            if corrections > corrections_before:
                # The final read triggered a clear+retype — re-read so the verdict
                # reflects the corrected field, not the pre-correction mismatch.
                last_read = await self._read_field(
                    cur_region,
                    intended=text,
                    precise=precise,
                )
            elif (
                precise
                and compute_verdict(text, last_read, precise) == "unverified"
            ):
                # VNC/X11 can acknowledge all HID events before the final glyphs
                # are painted. A prefix-only read is therefore not yet proof of
                # truncation. Take at most two delayed reads (R19 needed the
                # second capture), grow the auto-located crop if late pixels
                # appear, and accept only exact/containing evidence. This never
                # emits more HID, and Enter remains the caller's separate action.
                for _ in range(2):
                    await asyncio.sleep(_PRINT_SETTLE_S)
                    if not explicit_region:
                        settled_grid = await self._grid()
                        late_region = (
                            locate_changed_bbox(grid_prev, settled_grid, dims)
                            if grid_prev is not None
                            and settled_grid is not None
                            else None
                        )
                        if late_region is not None:
                            cur_region = union_region(cur_region, late_region)
                    settled_read = self._typed_candidate(
                        await self._read_field(
                            cur_region,
                            intended=text,
                            precise=precise,
                        ),
                        text,
                        precise,
                    )
                    if compute_verdict(text, settled_read, precise) in {
                        "match",
                        "contains",
                    }:
                        last_read = settled_read
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
    ) -> WatchedTypingResult:
        # Reuse the verifier for the authoritative status (the only thing allowed to
        # declare typed text verified or failed). Verdict drives the summary text.
        vr: VerificationResult = verify_text(intended, field_text, code=precise)
        status = vr.status

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
        else:
            summary = f"{head} and verified the field reads correctly."

        return WatchedTypingResult(
            verdict=verdict,
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
        )
