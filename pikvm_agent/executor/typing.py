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
imports no I/O of its own. It NEVER emits Enter — correction is limited to
bounded editing keys, including one grounded local character replacement for
Notepad's standalone-``i`` autocorrection or a repeated punctuation transport
slip. A short field readback may also move focus with Tab/Shift-Tab or
select an existing draft with Ctrl+A solely to remove a blinking caret from
OCR. Committing is the caller's job.

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
    fold_quotes,
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
FAST_PRINT_CHUNK_TARGET = 64  # guarded printer target after its focus probe
MAX_TOTAL_CORRECTIONS = 1  # one clean retry; never a compounding loop
MAX_LOCAL_EDITOR_REPAIRS = 2  # independently re-proved glyph edits, no replay
MAX_BACKSPACES = 400      # safety cap on a correction's clear
MAX_AUTOCORRECT_CURSOR_STEPS = 64
FAST_PRINT_MIN = 120      # above this, plain text takes the (bursty) fast print path;
                          # shorter text stays on the fully-humanized per-key path
FAST_TERMINAL_PRINT_MIN = 32  # exact simple argv can use PiKVM's guarded printer
FAST_EDITOR_PRINT_MIN = 32  # verified editor focus can use the same guarded printer
MIN_MISMATCH_OCR_CONFIDENCE = 0.78
MIN_GROUNDED_EXACT_OCR_CONFIDENCE = 0.55
MIN_ONE_EDIT_RECHECK_CONFIDENCE = 0.90
NATIVE_PRIMARY_CARET_RETRY_DELAY_S = 0.55
MAX_AUTODETECTED_FIELD_HEIGHT = 80
MAX_AUTODETECTED_FIELD_HEIGHT_FRAC = 0.15
MAX_PROSE_EDGE_CONTEXT_CHARS = 96
MAX_EDITOR_STATUS_VERTICAL_GAP_FRAC = 0.50
MAX_EDITOR_STATUS_SEARCH_HEIGHT_FRAC = 0.1875
EDITOR_STATUS_SEARCH_WIDTH_FRAC = 0.40
EDITOR_STATUS_SEARCH_LEFT_CONTEXT_PX = 256
EDITOR_STATUS_COMPACT_HEIGHT_PX = 64.0
MAXIMIZED_EDITOR_BODY_TOP_FRAC = 0.08
AUTODETECTED_READBACK_MARGIN_X_FRAC = 0.075
SHORT_FIELD_CONTEXT_ABOVE_PX = 80
SHORT_FIELD_CONTEXT_BELOW_PX = 24
_EDITOR_STATUS_POSITION_RE = re.compile(
    r"\b[L1I]n\s*(?P<line>\d+)\s*[,.;:]\s*Col\s*(?P<column>\d+)\b",
    re.IGNORECASE,
)
_STANDALONE_I_SUBSTITUTES = frozenset({"I", "1", "|", "l"})
_STANDALONE_LOWERCASE_I_RE = re.compile(r"(?<![\w])i(?![\w])")
_STRUCTURAL_CODE_GLYPHS = frozenset("{}[]()")
_STRUCTURAL_CODE_SUFFIX_GLYPHS = _STRUCTURAL_CODE_GLYPHS | frozenset(",;")
_STRUCTURAL_CODE_FRAGMENT_MAX_CHARS = 4
_EDITOR_STATUS_CHARACTER_COUNT_RE = re.compile(
    r"\b(?P<characters>\d+)\s+characters?\b",
    re.IGNORECASE,
)
_EDITOR_STATUS_COLUMN_RE = re.compile(
    # Tesseract can drop the separator before ``Col`` (``tn1Col26``). The
    # compact-crop consensus below still requires the independent character
    # count and rejects conflicting pairs, so do not require a word boundary
    # at the left edge.
    r"Col\s*(?P<column>\d+)\b",
    re.IGNORECASE,
)
DENSE_PIXEL_DELTA = 10
DENSE_MIN_CHANGED_PIXELS = 80
# The measured 1280x800 VNC delta for Notepad's indented ``)`` is 37 pixels
# after JPEG decoding (glyph plus caret). Exact cropped OCR and the independent
# editor-status proof still gate acceptance, so retain that causal candidate
# without relaxing ordinary line localisation.
DENSE_COMPACT_MIN_CHANGED_PIXELS = 30
DENSE_MIN_WIDTH = 8
DENSE_MIN_HEIGHT = 4
DENSE_MAX_HEIGHT = 64

# Pauses (seconds) — let a print / clear land and the video settle before reading.
_PRINT_SETTLE_S = 0.45
_CLEAR_SETTLE_S = 0.15
_VIDEO_RETRY_SETTLE_S = 0.20
_SLOW_VIDEO_RETRY_SETTLE_S = 1.00
_VERY_SLOW_VIDEO_RETRY_SETTLE_S = 2.50
_FINAL_VIDEO_RETRY_SETTLE_S = 1.00
_CARET_BLINK_RECHECK_S = 0.65
_CURSOR_REPEAT_SETTLE_S = 0.075
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
PARTIAL_DELIVERY_SUMMARY = (
    "Typing stopped after the causal editor row showed only a strict fragment "
    "of the requested text. The sender issued the payload once; do not append "
    "or replay it. Treat the current editor line as a truncated draft and "
    "clear it only through an independently verified rollback."
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
AUTOCORRECT_REPLACEMENT_FAILED_SUMMARY = (
    "Typing stopped after the editor changed a standalone lowercase `i` to "
    "uppercase `I`, and one bounded local replacement did not restore the "
    "exact text. "
    "Do not append or replay the draft until the visible field is corrected."
)
PUNCTUATION_REPLACEMENT_FAILED_SUMMARY = (
    "Typing stopped after two settled, high-confidence reads showed one "
    "punctuation transport substitution, and one bounded local replacement "
    "did not restore the exact text. Do not append or replay the draft until "
    "the visible field is corrected."
)
MISSING_GLYPH_REPLACEMENT_FAILED_SUMMARY = (
    "Typing stopped after two settled, high-confidence reads and the editor "
    "caret column proved one missing glyph, but one bounded local insertion "
    "did not restore the exact text. Do not append or replay the draft until "
    "the visible field is corrected."
)


def _ocr_candidate_fingerprints(result: OCRResult) -> dict[str, Any]:
    """Describe an OCR selection without retaining any recognized text."""

    primary_confidences = [
        float(line.confidence)
        for line in result.lines
        if line.confidence is not None
    ]
    candidates: list[dict[str, Any]] = [
        {
            "source": "canonical",
            "characters": len(result.text),
            "sha256": hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
            "mean_confidence": (
                round(
                    sum(primary_confidences) / len(primary_confidences),
                    4,
                )
                if primary_confidences
                else None
            ),
        }
    ]
    candidates.extend(
        {
            "source": f"alternative_{index}",
            "characters": len(candidate.text),
            "sha256": hashlib.sha256(
                candidate.text.encode("utf-8")
            ).hexdigest(),
            "mean_confidence": (
                round(float(candidate.mean_confidence), 4)
                if candidate.mean_confidence is not None
                else None
            ),
            "evidence_kind": candidate.evidence_kind,
        }
        for index, candidate in enumerate(result.alternatives, start=1)
    )
    return {
        "provider_canonical_sha256": candidates[0]["sha256"],
        "candidate_count": len(candidates),
        "candidate_fingerprints": candidates,
    }


def is_structural_code_fragment(text: str) -> bool:
    """Recognize one short punctuation-only closing fragment.

    Code generators commonly emit closing rows such as ``};`` or ``]);``.
    Their tiny VNC pixel delta needs the same compact causal-crop treatment as
    a single ``}``, while exact OCR and the editor caret-column proof still
    gate acceptance of every glyph and any leading indentation.
    """

    visible = text.strip()
    return (
        0 < len(visible) <= _STRUCTURAL_CODE_FRAGMENT_MAX_CHARS
        and any(character in _STRUCTURAL_CODE_GLYPHS for character in visible)
        and all(
            character in _STRUCTURAL_CODE_SUFFIX_GLYPHS
            for character in visible
        )
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


def is_autolocated_editor_body_region(
    region: Region,
    dims: tuple[int, int],
) -> bool:
    """Reject only the fixed top-screen chrome of a maximized editor.

    Modern Notepad mirrors the first document row in its tab title.  A
    full-screen OCR recovery can therefore read the intended text perfectly
    from the tab strip while proving nothing about the editor body.  Keep this
    guard deliberately narrow: it applies only to auto-localized editor
    candidates, and excludes rows wholly inside the top five percent (with a
    small-pixel floor).  Explicit caller regions and every non-editor surface
    remain unchanged.
    """

    _width, height = dims
    if height <= 0:
        return True
    chrome_bottom = max(32.0, height * 0.05)
    return region.y + region.height > chrome_bottom


def is_disjoint_editor_effect(current: Region, candidate: Region) -> bool:
    """Reject a later editor repaint outside the grounded text band."""

    vertical_gap = max(
        0.0,
        candidate.y - (current.y + current.height),
        current.y - (candidate.y + candidate.height),
    )
    return vertical_gap > max(24.0, current.height, candidate.height)


def editor_row_candidate_above_disjoint_effect(
    coarse_region: Region | None,
    candidates: list[Region],
    dims: tuple[int, int],
) -> Region | None:
    """Recover the nearest text-row delta above a coarse status repaint.

    Notepad can repaint its character count more strongly than a short code
    row, causing the coarse grid to nominate the status bar. Dense candidates
    remain causal to the same before/after emission. When one overlaps that
    coarse effect, return only the horizontally adjacent candidate immediately
    above it; later OCR and the independent status proof still gate success.
    """

    width, height = dims
    if coarse_region is None or width <= 0 or height <= 0:
        return None
    effects = [
        candidate
        for candidate in candidates
        if regions_overlap(coarse_region, candidate)
    ]
    if len(effects) != 1:
        return None
    effect = effects[0]
    maximum_gap = height * MAX_EDITOR_STATUS_VERTICAL_GAP_FRAC
    maximum_offset = max(128.0, width * 0.20)
    rows = [
        candidate
        for candidate in candidates
        if (
            candidate is not effect
            and candidate.y + candidate.height <= effect.y
            and effect.y - (candidate.y + candidate.height) <= maximum_gap
            and abs(candidate.x - effect.x) <= maximum_offset
        )
    ]
    if not rows:
        return None
    return max(rows, key=lambda candidate: candidate.y + candidate.height)


def _unique_upper_candidate_with_lower_effect(
    candidates: list[Region],
    pair_matches: Callable[[Region, Region], bool],
) -> Region | None:
    """Return one upper candidate accepted by a caller-specific lower effect."""

    upper_indices: set[int] = set()
    for upper_index, upper in enumerate(candidates):
        for effect_index, effect in enumerate(candidates):
            if effect_index == upper_index or not pair_matches(upper, effect):
                continue
            upper_indices.add(upper_index)
            break
    if len(upper_indices) != 1:
        return None
    return candidates[upper_indices.pop()]


def structural_editor_row_above_status_effect(
    candidates: list[Region],
    dims: tuple[int, int],
) -> Region | None:
    """Return one compact causal row paired with a lower status repaint.

    A punctuation-only editor append can be too small for OCR to identify, but
    the same input also repaints Notepad's wider character-count status item.
    Recover geometry only when exactly one upper candidate has a wider,
    horizontally aligned lower effect inside the existing status-gap bound.
    Expected text does not select the row, and exact OCR plus the independent
    caret/status invariant still gate success after the caret is moved away.
    """

    width, height = dims
    if width <= 0 or height <= 0 or len(candidates) < 2:
        return None
    maximum_gap = height * MAX_EDITOR_STATUS_VERTICAL_GAP_FRAC
    maximum_offset = max(128.0, width * 0.20)

    def paired_with_status_effect(row: Region, effect: Region) -> bool:
        row_bottom = row.y + row.height
        row_center_x = row.x + row.width / 2
        if row_bottom > effect.y:
            return False
        vertical_gap = effect.y - row_bottom
        effect_center_x = effect.x + effect.width / 2
        return not (
            vertical_gap < max(row.height, effect.height)
            or vertical_gap > maximum_gap
            or abs(effect_center_x - row_center_x) > maximum_offset
            or effect.width < row.width + DENSE_MIN_WIDTH
        )

    return _unique_upper_candidate_with_lower_effect(
        candidates,
        paired_with_status_effect,
    )


def structural_editor_readback_band(
    before_image: bytes,
    after_image: bytes,
    row_region: Region,
    dims: tuple[int, int],
) -> Region | None:
    """Narrow a structural delta to its lowest substantial text band.

    Dense localisation deliberately pads changed components for ordinary OCR.
    On a tiny appended punctuation row that padding can include the preceding
    editor line. Use causal before/after pixels to select the vertical band,
    then bound the text that remains visible in the after frame. The latter is
    important because the vanished pre-input caret is also a large causal
    change, but must not be included once the caret is moved to Home for OCR.
    This supplies geometry only; exact OCR and the independent editor status
    invariant remain mandatory.
    """

    width, height = dims
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
    x0 = max(0, min(width, math.floor(row_region.x)))
    y0 = max(0, min(height, math.floor(row_region.y)))
    x1 = max(x0, min(width, math.ceil(row_region.x + row_region.width)))
    y1 = max(y0, min(height, math.ceil(row_region.y + row_region.height)))
    if x1 <= x0 or y1 <= y0:
        return None
    changed = (
        np.max(np.abs(after[y0:y1, x0:x1] - before[y0:y1, x0:x1]), axis=2)
        > DENSE_PIXEL_DELTA
    )
    row_counts = changed.sum(axis=1)
    active_rows = [
        index
        for index, count in enumerate(row_counts.tolist())
        if int(count) >= 2
    ]
    if not active_rows:
        return None
    bands: list[tuple[int, int]] = []
    start = previous = active_rows[0]
    for current in active_rows[1:]:
        if current <= previous + 2:
            previous = current
            continue
        bands.append((start, previous + 1))
        start = previous = current
    bands.append((start, previous + 1))
    substantial = [
        (start, end)
        for start, end in bands
        if (
            end - start >= DENSE_MIN_HEIGHT
            and int(row_counts[start:end].sum())
            >= DENSE_COMPACT_MIN_CHANGED_PIXELS
        )
    ]
    if not substantial:
        return None
    band_start, band_end = max(substantial, key=lambda band: band[1])
    after_band = after[y0 + band_start : y0 + band_end, x0:x1]
    background = np.median(after_band.reshape(-1, 3), axis=0)
    foreground = (
        np.max(np.abs(after_band - background), axis=2) > DENSE_PIXEL_DELTA
    )
    column_counts = foreground.sum(axis=0)
    active_columns = [
        index
        for index, count in enumerate(column_counts.tolist())
        if int(count) >= 2
    ]
    if not active_columns:
        return None
    column_bands: list[tuple[int, int]] = []
    start = previous = active_columns[0]
    for current in active_columns[1:]:
        if current <= previous + 2:
            previous = current
            continue
        column_bands.append((start, previous + 1))
        start = previous = current
    column_bands.append((start, previous + 1))
    substantial_columns = [
        (start, end)
        for start, end in column_bands
        if (
            end - start >= DENSE_MIN_WIDTH
            and int(column_counts[start:end].sum())
            >= DENSE_COMPACT_MIN_CHANGED_PIXELS
        )
    ]
    if len(substantial_columns) != 1:
        return None
    column_start, column_end = substantial_columns[0]
    padding = 3
    readback_x0 = max(x0, x0 + column_start - padding)
    readback_x1 = min(x1, x0 + column_end + padding)
    readback_y0 = max(y0, y0 + band_start - padding)
    readback_y1 = min(y1, y0 + band_end + padding)
    return Region(
        x=float(readback_x0),
        y=float(readback_y0),
        width=float(max(1, readback_x1 - readback_x0)),
        height=float(max(1, readback_y1 - readback_y0)),
    )


def unique_exact_structured_ocr_row(
    result: OCRResult,
    intended: str,
    *,
    include_evidence: bool = False,
) -> OCRLine | None:
    """Extract one exact visual row from a provider's multi-row OCR item.

    ``evidence_lines`` are excluded by default because they are not the
    provider's canonical read.  A caller may include them when a second,
    independently grounded invariant still has to prove the candidate before
    exact completion (for example, Notepad's line/column/character status).
    """

    candidates: list[OCRLine] = []
    source_lines = [*result.lines]
    if include_evidence:
        source_lines.extend(result.evidence_lines)
    for line in source_lines:
        if (
            line.confidence is not None
            and float(line.confidence) < MIN_GROUNDED_EXACT_OCR_CONFIDENCE
        ):
            continue
        for row in line.text.splitlines():
            if row != intended:
                continue
            candidates.append(
                line.model_copy(
                    update={
                        "text": row,
                        # One shared bbox cannot ground one child row.
                        "bbox": None,
                        "raw": {
                            **dict(line.raw or {}),
                            "bounded_exact_row_extracted": True,
                            "observed_text": line.text,
                        },
                    }
                )
            )
    return candidates[0] if len(candidates) == 1 else None


def native_primary_readback_region(
    causal_region: Region,
    refined_region: Region | None,
    dims: tuple[int, int],
) -> Region:
    """Keep a native OCR retry grounded in the causal editor row.

    A noisy low-resolution OCR pass can expand a short editor row all the way
    to a screen edge.  Capturing that refinement at native resolution then
    admits unrelated desktop labels beside a non-maximized editor.  Preserve
    the refinement normally, but when only it touches a horizontal screen edge
    clamp its horizontal context to half a causal-row height on either side.
    The vertical band remains unchanged, and this geometry never verifies text.
    """

    if refined_region is None:
        return causal_region
    screen_width, _screen_height = dims
    if screen_width <= 0:
        return refined_region
    refined_right = refined_region.x + refined_region.width
    causal_right = causal_region.x + causal_region.width
    refinement_touches_edge = (
        refined_region.x <= 0 or refined_right >= screen_width
    )
    causal_touches_edge = (
        causal_region.x <= 0 or causal_right >= screen_width
    )
    if not refinement_touches_edge or causal_touches_edge:
        return refined_region
    margin = max(12, math.ceil(causal_region.height / 2))
    x = max(refined_region.x, causal_region.x - margin)
    x2 = min(refined_right, causal_right + margin)
    if x2 <= x:
        return causal_region
    return Region(
        x=x,
        y=refined_region.y,
        width=x2 - x,
        height=refined_region.height,
    )


def short_field_candidate_above_control_effect(
    candidates: list[Region],
    dims: tuple[int, int],
) -> Region | None:
    """Return one compact causal field delta above a wider control repaint.

    Windows Run repaints its enabled OK button more strongly than a three-
    character field and tight OCR may fuse the textbox border into gibberish.
    Use only the before/after geometry to recover a unique compact upper row.
    This never verifies text: the unchanged field still needs a reversible
    blur followed by an independent exact read.
    """

    width, height = dims
    if width <= 0 or height <= 0 or len(candidates) < 2:
        return None
    maximum_gap = max(48.0, height * 0.12)
    maximum_offset = max(96.0, width * 0.10)

    def paired_with_control_effect(field: Region, effect: Region) -> bool:
        if field.width > 64 or field.height > 48:
            return False
        field_bottom = field.y + field.height
        field_center_x = field.x + field.width / 2
        if field_bottom > effect.y:
            return False
        vertical_gap = effect.y - field_bottom
        effect_center_x = effect.x + effect.width / 2
        return not (
            vertical_gap > maximum_gap
            or abs(effect_center_x - field_center_x) > maximum_offset
            or effect.width < max(64.0, field.width * 1.5)
            or effect.height > 64
        )

    return _unique_upper_candidate_with_lower_effect(
        candidates,
        paired_with_control_effect,
    )


def visible_editor_indent_candidate(
    result: OCRResult,
    intended: str,
    *,
    preserve: bool,
) -> bool:
    """Whether OCR painted the exact row suffix but omitted leading blanks.

    This is only a candidate.  The caller still needs the foreground editor's
    independent caret-column proof before it can restore the invisible
    indentation or declare exact success.
    """

    return bool(
        preserve
        and intended.startswith(" ")
        and result.text
        and not result.text[0].isspace()
        and fold_quotes(strip_prompt(result.text).strip())
        == fold_quotes(intended.lstrip(" "))
    )


def _aligned_editor_character_count(
    result: OCRResult,
    position_line: OCRLine,
    position_region: Region,
    dims: tuple[int, int],
    *,
    container_region: Region | None,
) -> int | None:
    """Return a character count painted beside one Notepad position item.

    PaddleOCR preserves Notepad's status bar as separate ``Ln/Col`` and
    ``N characters`` boxes. Pair them only when their grounded boxes share the
    same visual row and the count is immediately to the right. This keeps a
    second stacked editor's status row from satisfying the foreground proof.
    """

    width, _height = dims
    counts: set[int] = set()
    position_right = position_region.x + position_region.width
    position_bottom = position_region.y + position_region.height
    maximum_gap = max(48.0, width * 0.10)
    for candidate_line in [*result.lines, *result.evidence_lines]:
        if candidate_line is position_line:
            continue
        character_count = _EDITOR_STATUS_CHARACTER_COUNT_RE.search(
            candidate_line.text
        )
        if character_count is None:
            continue
        if (
            candidate_line.confidence is not None
            and float(candidate_line.confidence)
            < MIN_GROUNDED_EXACT_OCR_CONFIDENCE
        ):
            continue
        candidate_region = (
            ocr_line_screen_region(
                candidate_line,
                container_region,
                dims,
                pad=0,
            )
            if container_region is not None
            else ocr_line_region(candidate_line, dims, pad=0)
        )
        if candidate_region is None:
            continue
        candidate_bottom = candidate_region.y + candidate_region.height
        vertical_overlap = max(
            0.0,
            min(position_bottom, candidate_bottom)
            - max(position_region.y, candidate_region.y),
        )
        horizontal_gap = candidate_region.x - position_right
        if (
            vertical_overlap
            < min(position_region.height, candidate_region.height) * 0.50
            or horizontal_gap < -4.0
            or horizontal_gap > maximum_gap
        ):
            continue
        counts.add(int(character_count.group("characters")))
    return counts.pop() if len(counts) == 1 else None


def _bounded_editor_status_rows(
    result: OCRResult,
    row_region: Region,
    dims: tuple[int, int],
    *,
    container_region: Region | None = None,
) -> list[tuple[float, float, int, int, int | None]]:
    """Return grounded status rows nearest the causal editor row."""

    width, height = dims
    if width <= 0 or height <= 0:
        return []
    screen_edge_status = bool(
        container_region is not None
        and row_region.y <= height * MAXIMIZED_EDITOR_BODY_TOP_FRAC
        and container_region.y + container_region.height >= height - 1
    )
    max_vertical_gap = (
        height
        if screen_edge_status
        else height * MAX_EDITOR_STATUS_VERTICAL_GAP_FRAC
    )
    max_horizontal_offset = max(128, width * 0.20)
    candidates: list[tuple[float, float, int, int, int | None]] = []
    # Full-screen recovery can union the causal glyph row with a later caret
    # or status repaint, inflating the region almost to the bottom of the
    # frame. Use the same bounded glyph-row height as
    # ``editor_status_search_region`` so the crop and its validator share one
    # geometry model.
    row_bottom = row_region.y + min(row_region.height, DENSE_MAX_HEIGHT)
    for line in [*result.lines, *result.evidence_lines]:
        match = _EDITOR_STATUS_POSITION_RE.search(line.text)
        if match is None:
            continue
        if (
            line.confidence is not None
            and float(line.confidence) < MIN_GROUNDED_EXACT_OCR_CONFIDENCE
        ):
            continue
        status_region = (
            ocr_line_screen_region(
                line,
                container_region,
                dims,
                pad=0,
            )
            if container_region is not None
            else ocr_line_region(line, dims, pad=0)
        )
        if status_region is None:
            continue
        vertical_gap = status_region.y - row_bottom
        horizontal_offset = abs(status_region.x - row_region.x)
        if (
            vertical_gap < 0
            or vertical_gap > max_vertical_gap
            or horizontal_offset > max_horizontal_offset
        ):
            continue
        character_count = _EDITOR_STATUS_CHARACTER_COUNT_RE.search(line.text)
        candidates.append(
            (
                vertical_gap,
                horizontal_offset,
                int(match.group("line")),
                int(match.group("column")),
                (
                    int(character_count.group("characters"))
                    if character_count is not None
                    else _aligned_editor_character_count(
                        result,
                        line,
                        status_region,
                        dims,
                        container_region=container_region,
                    )
                ),
            )
        )
    return candidates


def _editor_caret_column_matches(
    result: OCRResult,
    row_region: Region,
    dims: tuple[int, int],
    *,
    expected_column: int,
    container_region: Region | None = None,
) -> bool:
    width, height = dims
    if width <= 0 or height <= 0 or expected_column <= 0:
        return False
    candidates = _bounded_editor_status_rows(
        result,
        row_region,
        dims,
        container_region=container_region,
    )
    if not candidates:
        # The hybrid provider can retain a much stronger independent OCR read
        # as an alternative when its generic whole-result selector keeps a
        # weaker primary result. Alternatives do not retain per-line geometry,
        # so only use them inside the compact status crop and only when every
        # high-confidence parse agrees on one status position. A crop that also
        # contains a background editor row therefore remains ambiguous.
        if (
            container_region is None
            or container_region.height
            > max(1, math.floor(height * MAX_EDITOR_STATUS_SEARCH_HEIGHT_FRAC))
        ):
            return False
        alternative_positions = {
            (
                int(match.group("line")),
                int(match.group("column")),
            )
            for alternative in result.alternatives
            if (
                alternative.mean_confidence is not None
                and float(alternative.mean_confidence)
                >= MIN_ONE_EDIT_RECHECK_CONFIDENCE
            )
            for match in _EDITOR_STATUS_POSITION_RE.finditer(alternative.text)
        }
        if len(alternative_positions) != 1:
            return False
        (_line, alternative_column), = alternative_positions
        return alternative_column == expected_column
    nearest_gap = min(candidate[0] for candidate in candidates)
    same_status_band = [
        candidate
        for candidate in candidates
        if candidate[0] - nearest_gap <= max(4, math.floor(height * 0.01))
    ]
    nearest_positions = {
        (line, column)
        for _gap, _offset, line, column, _characters in same_status_band
    }
    if len(nearest_positions) != 1:
        # Two engines disagreeing on the same physical status row are not
        # permission to accept whichever column happens to match the request.
        return False
    (_nearest_line, nearest_column), = nearest_positions
    return nearest_column == expected_column


def editor_caret_column_proves_leading_whitespace(
    result: OCRResult,
    intended: str,
    row_region: Region,
    dims: tuple[int, int],
    *,
    container_region: Region | None = None,
) -> bool:
    """Confirm invisible leading spaces from the nearest editor status row.

    OCR cannot paint a bounding box around blank indentation. Modern Notepad
    does expose the caret column, however, so an exact visible suffix plus
    ``Col == len(payload) + 1`` proves the missing leading positions. Only the
    nearest parsed status row below the causal text row is considered; a more
    distant background Notepad window can never rescue a conflicting or
    unreadable foreground status.
    """

    leading_spaces = len(intended) - len(intended.lstrip(" "))
    if (
        leading_spaces <= 0
        or "\n" in intended
        or "\r" in intended
    ):
        return False
    return _editor_caret_column_matches(
        result,
        row_region,
        dims,
        expected_column=len(intended) + 1,
        container_region=container_region,
    )


def editor_caret_column_proves_one_missing_glyph(
    result: OCRResult,
    intended: str,
    row_region: Region,
    dims: tuple[int, int],
    *,
    container_region: Region | None = None,
) -> bool:
    """Confirm the caret is exactly one character short of the payload."""

    if not intended or "\n" in intended or "\r" in intended:
        return False
    return _editor_caret_column_matches(
        result,
        row_region,
        dims,
        expected_column=len(intended),
        container_region=container_region,
    )


def editor_status_proves_single_line_payload(
    result: OCRResult,
    intended: str,
    row_region: Region,
    dims: tuple[int, int],
    *,
    container_region: Region | None = None,
) -> bool:
    """Prove an exact one-line editor replacement including its whitespace.

    A canonical OCR row can visibly identify every glyph while leaving one
    short internal gap uncalibrated. Notepad's foreground status row supplies
    an independent document invariant: line 1, caret at ``len + 1``, and an
    exact total character count. Requiring all three prevents an older or
    background document from laundering collapsed or duplicated whitespace.
    """

    width, height = dims
    if (
        not intended
        or not any(character.isspace() for character in intended)
        or "\n" in intended
        or "\r" in intended
        or width <= 0
        or height <= 0
    ):
        return False
    expected = (1, len(intended) + 1, len(intended))
    candidates = _bounded_editor_status_rows(
        result,
        row_region,
        dims,
        container_region=container_region,
    )
    if candidates:
        _gap, _offset, line, column, characters = min(candidates)
        return (line, column, characters) == expected
    if (
        container_region is None
        or container_region.height
        > max(1, math.floor(height * MAX_EDITOR_STATUS_SEARCH_HEIGHT_FRAC))
    ):
        return False
    alternative_statuses: set[tuple[int, int, int]] = set()
    for alternative in result.alternatives:
        if (
            alternative.mean_confidence is None
            or float(alternative.mean_confidence)
            < MIN_ONE_EDIT_RECHECK_CONFIDENCE
        ):
            continue
        positions = list(
            _EDITOR_STATUS_POSITION_RE.finditer(alternative.text)
        )
        character_counts = list(
            _EDITOR_STATUS_CHARACTER_COUNT_RE.finditer(alternative.text)
        )
        if len(positions) != 1 or len(character_counts) != 1:
            continue
        alternative_statuses.add(
            (
                int(positions[0].group("line")),
                int(positions[0].group("column")),
                int(character_counts[0].group("characters")),
            )
        )
    if alternative_statuses == {expected}:
        return True

    # Tiny dark-theme status text can consistently lose or corrupt only the
    # ``Ln 1`` prefix while independent Tesseract scales still agree on both
    # ``Col N`` and ``N - 1 characters``. Those two document invariants are
    # sufficient by themselves: no later line can have column N after exactly
    # N - 1 total characters because at least one preceding newline would also
    # be counted. Require two independent compact-crop reads to agree, keep one
    # at the normal grounded confidence floor, and reject any conflicting
    # column/count pair. This is deliberately unavailable on a broad crop.
    if container_region.height > EDITOR_STATUS_COMPACT_HEIGHT_PX:
        return False
    invariant_reads: list[tuple[tuple[int, int], float]] = []
    primary_text = "\n".join(line.text for line in result.lines)
    primary_confidences = [
        float(line.confidence)
        for line in result.lines
        if line.confidence is not None
    ]
    status_sources = [
        (
            primary_text,
            (
                sum(primary_confidences) / len(primary_confidences)
                if primary_confidences
                else 0.0
            ),
        ),
        *[
            (alternative.text, float(alternative.mean_confidence or 0.0))
            for alternative in result.alternatives
        ],
    ]
    for source_text, confidence in status_sources:
        columns = list(_EDITOR_STATUS_COLUMN_RE.finditer(source_text))
        character_counts = list(
            _EDITOR_STATUS_CHARACTER_COUNT_RE.finditer(source_text)
        )
        if (
            confidence < 0.30
            or len(columns) != 1
            or len(character_counts) != 1
        ):
            continue
        invariant_reads.append(
            (
                (
                    int(columns[0].group("column")),
                    int(character_counts[0].group("characters")),
                ),
                confidence,
            )
        )
    invariant_pairs = {pair for pair, _confidence in invariant_reads}
    return (
        invariant_pairs == {(expected[1], expected[2])}
        and len(invariant_reads) >= 2
        and max(confidence for _pair, confidence in invariant_reads)
        >= MIN_GROUNDED_EXACT_OCR_CONFIDENCE
    )


def editor_status_proves_visible_multiline_payload(
    result: OCRResult,
    intended: str,
    row_region: Region,
    dims: tuple[int, int],
    *,
    container_region: Region | None = None,
) -> bool:
    """Prove ordinary single gaps on a visible later editor line.

    The exact OCR row proves the painted glyph sequence and gap locations;
    Notepad's foreground caret column independently proves the total number of
    characters on that line. Keep this unavailable for line 1 (where the
    stronger whole-document invariant applies), edge whitespace, repeated
    spaces, tabs, or other invisible formatting.
    """

    whitespace_runs = re.findall(r"\s+", intended)
    if (
        not intended
        or intended != intended.strip()
        or "\t" in intended
        or "\n" in intended
        or "\r" in intended
        or not whitespace_runs
        or any(run != " " for run in whitespace_runs)
    ):
        return False
    candidates = _bounded_editor_status_rows(
        result,
        row_region,
        dims,
        container_region=container_region,
    )
    if not candidates:
        return False
    _gap, _offset, line, column, _characters = min(candidates)
    return line > 1 and column == len(intended) + 1


def editor_status_search_region(
    row_region: Region,
    dims: tuple[int, int],
) -> Region | None:
    """Return a bounded status-band crop below the causal editor row.

    Full-screen Tesseract can omit or badly mangle the foreground Notepad
    status row.  Derive this crop from screen and causal-row geometry only, so
    a bounded precise read can use the independent secondary OCR engine
    without selecting a region from expected text or unreliable OCR.
    """

    width, height = dims
    if width <= 0 or height <= 0:
        return None
    # A later caret or status-bar repaint can vertically inflate the unioned
    # causal box even though its top still grounds the editor glyph row. Do
    # not let that dynamic surface push the independent status crop beneath
    # the foreground window.
    bounded_row_height = min(row_region.height, DENSE_MAX_HEIGHT)
    row_bottom = row_region.y + bounded_row_height
    available_height = height - row_bottom
    if available_height <= 0:
        return None
    search_depth = min(
        available_height,
        max(1, math.floor(height * MAX_EDITOR_STATUS_VERTICAL_GAP_FRAC)),
    )
    crop_height = min(
        search_depth,
        max(1, math.floor(height * MAX_EDITOR_STATUS_SEARCH_HEIGHT_FRAC)),
    )
    crop_width = min(
        width,
        max(1, math.floor(width * EDITOR_STATUS_SEARCH_WIDTH_FRAC)),
    )
    x = max(
        0,
        min(
            width - crop_width,
            math.floor(row_region.x) - EDITOR_STATUS_SEARCH_LEFT_CONTEXT_PX,
        ),
    )
    # Keep the crop's bottom at the deepest allowed edge while making the band
    # tall enough to include a foreground status row near either edge. Stacked
    # Notepad windows exposed both failure modes: a row clipped at the old top
    # edge and a lower foreground row missed after shifting the whole crop up.
    # The nearest-row geometry discriminator above still rejects background
    # status rows that also enter this bounded band.
    y = row_bottom + search_depth - crop_height
    return Region(
        x=x,
        y=y,
        width=crop_width,
        height=crop_height,
    )


def maximized_editor_status_search_region(
    row_region: Region,
    dims: tuple[int, int],
) -> Region | None:
    """Return the bottom-edge status band for a top-screen editor body.

    Preserve the ordinary bounded search for restored or stacked windows.
    Modern maximized Notepad places its body near the screen top and its
    independent document status at the bottom edge, so that deterministic
    geometry gets one additional crop. A top-aligned restored window simply
    produces no matching bottom status and fails closed.
    """

    width, height = dims
    if (
        width <= 0
        or height <= 0
        or row_region.y > height * MAXIMIZED_EDITOR_BODY_TOP_FRAC
    ):
        return None
    ordinary = editor_status_search_region(row_region, dims)
    if ordinary is None:
        return None
    return ordinary.model_copy(update={"y": height - ordinary.height})


def editor_status_search_subregions(
    region: Region,
    dims: tuple[int, int] | None = None,
) -> list[Region]:
    """Return bounded status bands for tiny dark-theme text.

    The broad geometry crop protects against stacked windows, but its empty
    editor area can dominate Tesseract segmentation. A status row whose top is
    inside the allowed crop can still have its lower glyphs clipped at the
    bottom edge. Scan that trailing edge first with one bounded two-glyph band,
    then fixed top, middle, and bottom bands. No expected text or OCR result
    influences their position, and the downstream geometry validator still
    rejects a row whose top exceeds the original bounded search depth.
    """

    band_height = min(region.height, EDITOR_STATUS_COMPACT_HEIGHT_PX)
    travel = max(0.0, region.height - band_height)
    offsets = (0.0, travel / 2.0, travel)
    trailing_height = min(
        region.height,
        EDITOR_STATUS_COMPACT_HEIGHT_PX * 2,
    )
    trailing_y = (
        region.y
        + region.height
        - min(region.height, EDITOR_STATUS_COMPACT_HEIGHT_PX)
    )
    if dims is not None:
        trailing_height = min(
            trailing_height,
            max(1.0, float(dims[1]) - trailing_y),
        )
    trailing = Region(
        x=region.x,
        y=trailing_y,
        width=region.width,
        height=trailing_height,
    )
    compact = [
        Region(
            x=region.x,
            y=region.y + offset,
            width=region.width,
            height=band_height,
        )
        for offset in dict.fromkeys(offsets)
    ]
    return [trailing, *compact]


def is_standalone_i_autocorrect(intended: str, observed: str) -> bool:
    """Recognise one visible substitution of the standalone word ``i``.

    Windows Notepad can turn a Python loop variable into the English pronoun
    ``I``. OCR can then render that same narrow glyph as ``1``, ``|``, or
    lowercase ``l``. Keep this deliberately narrower than a generic mismatch:
    one and only one character may differ, and it must replace a word-isolated
    lowercase ``i`` with one of those confusable vertical glyphs. The repair
    remains a local, single-character edit; this never authorizes replaying the
    line.
    """

    left = norm(intended, precise=True)
    right = norm(strip_prompt(observed), precise=True)
    if len(left) != len(right):
        return False
    differences = [
        index
        for index, (expected, actual) in enumerate(
            zip(left, right, strict=True)
        )
        if expected != actual
    ]
    if len(differences) != 1:
        return False
    index = differences[0]
    if left[index] != "i" or right[index] not in _STANDALONE_I_SUBSTITUTES:
        return False
    return (
        (
            index == 0
            or not (
                left[index - 1].isalnum()
                or left[index - 1] == "_"
            )
        )
        and (
            index + 1 == len(left)
            or not (left[index + 1].isalnum() or left[index + 1] == "_")
        )
    )


def standalone_i_autocorrect_suffix_length(
    intended: str,
    observed: str,
) -> int | None:
    """Locate the one mutated ``i`` as a distance back from the caret.

    OCR deliberately collapses whitespace, so its differing character index
    cannot safely drive cursor movement in the raw editor text. Recreate each
    possible one-character mutation in the original delivery and accept a
    cursor distance only when exactly one candidate explains the observed row.
    """

    if not is_standalone_i_autocorrect(intended, observed):
        return None
    observed_normalized = norm(strip_prompt(observed), precise=True)
    candidates = {
        len(intended) - index - 1
        for index, character in enumerate(intended)
        if character == "i"
        and (
            index == 0
            or not (
                intended[index - 1].isalnum()
                or intended[index - 1] == "_"
            )
        )
        and (
            index + 1 == len(intended)
            or not (intended[index + 1].isalnum() or intended[index + 1] == "_")
        )
        and any(
            norm(
                intended[:index] + glyph + intended[index + 1 :],
                precise=True,
            )
            == observed_normalized
            for glyph in _STANDALONE_I_SUBSTITUTES
        )
    }
    return candidates.pop() if len(candidates) == 1 else None


def standalone_i_autocorrect_navigation(
    intended: str,
    observed: str,
) -> tuple[str, str, int] | None:
    """Choose an anchored route to the mutated character.

    Repeated unpaced cursor taps can be coalesced by a remote desktop
    transport. Prefer an End-relative route because its distance is unchanged
    when indentation was inserted by an earlier Tab and is therefore absent
    from this action's intended text. Fall back to Home only when the suffix is
    beyond the bounded cursor budget and the known prefix is not. Every cursor
    step remains paced by the caller.
    """

    suffix_length = standalone_i_autocorrect_suffix_length(intended, observed)
    if suffix_length is None:
        return None
    prefix_length = len(intended) - suffix_length
    if suffix_length <= MAX_AUTOCORRECT_CURSOR_STEPS:
        return ("End", "ArrowLeft", suffix_length)
    if prefix_length <= MAX_AUTOCORRECT_CURSOR_STEPS:
        return ("Home", "ArrowRight", prefix_length)
    return None


def editor_punctuation_transport_substitution(
    intended: str,
    observed: str,
) -> tuple[int, str, str] | None:
    """Locate one visible punctuation substitution from the line end.

    Editor OCR omits leading indentation, so compare the painted row only.
    Internal spacing and every other glyph must remain byte-for-byte exact
    after quote-family folding. The expected glyph must be punctuation; case
    and alphanumeric mismatches retain their existing fail-closed paths.
    """

    expected = fold_quotes(intended.lstrip(" "))
    actual = fold_quotes(strip_prompt(observed).strip())
    if (
        not expected
        or "\n" in expected
        or "\r" in expected
        or len(expected) != len(actual)
    ):
        return None
    differences = [
        index
        for index, (wanted, seen) in enumerate(
            zip(expected, actual, strict=True)
        )
        if wanted != seen
    ]
    if len(differences) != 1:
        return None
    index = differences[0]
    wanted = expected[index]
    seen = actual[index]
    if wanted.isalnum() or wanted.isspace() or seen.isspace():
        return None
    suffix_length = len(expected) - index - 1
    if suffix_length > MAX_AUTOCORRECT_CURSOR_STEPS:
        return None
    return suffix_length, intended.lstrip(" ")[index], seen


def editor_single_glyph_transport_deletion(
    intended: str,
    observed: str,
) -> tuple[int, str] | None:
    """Locate one uniquely missing editor glyph from the line end.

    Tiny editor text can render a lowercase ``l`` as ``1``. Permit at most one
    such OCR-only confusion after removing the candidate missing glyph. This
    remains non-authorizing on its own: the caller repeats the crop, requires
    an independent caret column proving one missing position, inserts only the
    missing glyph, and demands an exact post-repair readback.
    """

    expected = intended.lstrip(" ")
    actual = strip_prompt(observed).strip()
    folded_expected = fold_quotes(expected)
    folded_actual = fold_quotes(actual)
    leading_spaces = len(intended) - len(expected)
    if (
        expected
        and leading_spaces > 0
        and "\n" not in intended
        and "\r" not in intended
        and folded_actual == folded_expected
        and len(expected) <= MAX_AUTOCORRECT_CURSOR_STEPS
    ):
        # OCR cannot paint a leading blank, so the visible row is identical
        # whether all indentation arrived or exactly one space was dropped.
        # The caller resolves that ambiguity with the foreground editor's
        # caret column: only the one-character-short column authorizes this
        # insertion immediately before the first visible glyph.
        return len(expected), " "
    if (
        not expected
        or "\n" in expected
        or "\r" in expected
        or len(folded_actual) + 1 != len(folded_expected)
    ):
        return None

    def matches_with_one_l_one_confusion(candidate: str) -> bool:
        confusions = 0
        for wanted, seen in zip(candidate, folded_actual, strict=True):
            if wanted == seen:
                continue
            if wanted == "l" and seen == "1":
                confusions += 1
                if confusions <= 1:
                    continue
            return False
        return True

    missing_indices = [
        index
        for index in range(len(folded_expected))
        if (
            matches_with_one_l_one_confusion(
                folded_expected[:index] + folded_expected[index + 1 :]
            )
        )
    ]
    if len(missing_indices) != 1:
        return None
    index = missing_indices[0]
    suffix_length = len(expected) - index - 1
    if suffix_length > MAX_AUTOCORRECT_CURSOR_STEPS:
        return None
    return suffix_length, expected[index]


def is_caps_lock_case_inversion(intended: str, observed: str) -> bool:
    """Require a broad case inversion before toggling guest Caps Lock."""

    left = norm(intended, precise=True)
    right = norm(strip_prompt(observed), precise=True)
    if len(left) != len(right) or left.casefold() != right.casefold():
        return False
    cased_pairs = [
        (expected, actual)
        for expected, actual in zip(left, right, strict=True)
        if expected.isalpha()
    ]
    return (
        len(cased_pairs) >= 2
        and all(actual == expected.swapcase() for expected, actual in cased_pairs)
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


def ocr_line_screen_region(
    line: OCRLine,
    container: Region,
    dims: tuple[int, int],
    *,
    pad: int = 8,
) -> Region | None:
    """Translate a crop-relative OCR row back into screen coordinates."""

    local = ocr_line_region(
        line,
        (
            max(1, math.ceil(container.width)),
            max(1, math.ceil(container.height)),
        ),
        pad=pad,
    )
    if local is None:
        return None
    screen_width, screen_height = dims
    if screen_width <= 0 or screen_height <= 0:
        return None
    x = min(
        screen_width - 1,
        max(0, math.floor(container.x + local.x)),
    )
    y = min(
        screen_height - 1,
        max(0, math.floor(container.y + local.y)),
    )
    x2 = min(
        screen_width,
        max(x + 1, math.ceil(container.x + local.x + local.width)),
    )
    y2 = min(
        screen_height,
        max(y + 1, math.ceil(container.y + local.y + local.height)),
    )
    return Region(
        x=x,
        y=y,
        width=max(1, x2 - x),
        height=max(1, y2 - y),
    )


def matching_collinear_ocr_rows(
    lines: list[OCRLine],
    intended: str,
    dims: tuple[int, int],
    *,
    minimum_confidence: float = 0.45,
) -> list[OCRLine]:
    """Build locator-only rows from OCR words split across one visual line."""

    bounded_rows = [
        (line, row_region)
        for line in lines
        if (
            line.confidence is not None
            and float(line.confidence) >= minimum_confidence
            and (
                row_region := ocr_line_region(
                    line,
                    dims,
                    pad=0,
                )
            )
            is not None
        )
    ]
    candidates: list[OCRLine] = []
    seen_groups: set[tuple[int, ...]] = set()
    for anchor_index, (_anchor_line, anchor_region) in enumerate(
        bounded_rows
    ):
        group = tuple(
            index
            for index, (_line, row_region) in enumerate(bounded_rows)
            if (
                min(
                    anchor_region.y + anchor_region.height,
                    row_region.y + row_region.height,
                )
                - max(anchor_region.y, row_region.y)
                >= max(
                    1.0,
                    min(anchor_region.height, row_region.height) * 0.5,
                )
            )
        )
        if (
            anchor_index not in group
            or len(group) < 2
            or group in seen_groups
        ):
            continue
        seen_groups.add(group)
        ordered_group = sorted(
            (bounded_rows[index] for index in group),
            key=lambda item: item[1].x,
        )
        joined_text = " ".join(
            line.text for line, _row_region in ordered_group
        )
        if norm(joined_text, precise=True) != norm(intended, precise=True):
            continue
        joined_region = ordered_group[0][1]
        for _line, row_region in ordered_group[1:]:
            joined_region = union_region(joined_region, row_region)
        candidates.append(
            OCRLine(
                text=joined_text,
                confidence=min(
                    float(line.confidence)
                    for line, _row_region in ordered_group
                    if line.confidence is not None
                ),
                bbox=[
                    round(joined_region.x),
                    round(joined_region.y),
                    round(joined_region.x + joined_region.width),
                    round(joined_region.y + joined_region.height),
                ],
                raw={"causal_collinear_locator": True},
            )
        )
    return candidates


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

    container_midpoint_y = container.height / 2
    intended_is_filename = _SAFE_FILENAME.fullmatch(intended) is not None
    # Hybrid OCR keeps the non-canonical engine's independent geometry here.
    # Admit it only for safe filenames and only through the labelled-row gate
    # below; a fresh crop must still prove the complete value exactly.
    filename_evidence_lines = (
        list(result.evidence_lines) if intended_is_filename else []
    )

    def normalized_filename_locator_text(line: OCRLine) -> str:
        return "".join(
            character.casefold()
            for character in line.text
            if character.isalnum()
        )

    filename_label_regions: list[Region] = []
    if intended_is_filename:
        for candidate in [*result.lines, *filename_evidence_lines]:
            label_text = normalized_filename_locator_text(candidate)
            label_region = ocr_line_region(
                candidate,
                (
                    math.ceil(container.width),
                    math.ceil(container.height),
                ),
                pad=0,
            )
            if (
                label_region is not None
                and 6 <= len(label_text) <= 10
                and levenshtein("filename", label_text, 2) <= 2
            ):
                filename_label_regions.append(label_region)

    def filename_row_is_labelled(
        line: OCRLine,
        line_region: Region | None,
    ) -> bool:
        if not intended_is_filename or line_region is None:
            return False
        for label in filename_label_regions:
            overlap = max(
                0.0,
                min(
                    line_region.y + line_region.height,
                    label.y + label.height,
                )
                - max(line_region.y, label.y),
            )
            if (
                line_region.x >= label.x + label.width - 8
                and overlap >= min(line_region.height, label.height) * 0.5
            ):
                return True
        return False

    def candidate_order(line: OCRLine) -> tuple[int, int, float]:
        line_region = ocr_line_region(
            line,
            (
                math.ceil(container.width),
                math.ceil(container.height),
            ),
            pad=0,
        )
        midpoint_distance = (
            abs(
                line_region.y
                + line_region.height / 2
                - container_midpoint_y
            )
            if line_region is not None
            else math.inf
        )
        # A native Windows filename edit can open an autocomplete popup below
        # itself. Those history rows often begin with the newly typed basename
        # and can even contain it exactly, while OCR makes one glyph error in
        # the selected edit row. Prefer only the row horizontally paired with
        # the independently visible ``File name`` label. Exact comparison is
        # still performed by a fresh OCR pass over the resulting crop.
        return (
            (
                0
                if filename_row_is_labelled(line, line_region)
                else 1
            ),
            0 if line.text == intended else 1,
            midpoint_distance,
        )

    localization_lines: list[OCRLine] = []
    for line in result.lines:
        line_region = ocr_line_region(
            line,
            (
                math.ceil(container.width),
                math.ceil(container.height),
            ),
            pad=0,
        )
        # Once the native File name label is independently visible, never
        # fall back to an unpaired autocomplete row. If OCR omitted the edit
        # value entirely, the label-only bounded fallback below re-reads the
        # field; history cannot nominate itself just because it contains the
        # intended basename.
        if (
            not intended_is_filename
            or not filename_label_regions
            or filename_row_is_labelled(line, line_region)
        ):
            localization_lines.append(line)
    for line in filename_evidence_lines:
        line_region = ocr_line_region(
            line,
            (
                math.ceil(container.width),
                math.ceil(container.height),
            ),
            pad=0,
        )
        if (
            filename_row_is_labelled(line, line_region)
            and not normalized_filename_locator_text(line).startswith(
                "filename"
            )
            and line not in localization_lines
        ):
            localization_lines.append(line)

    if (
        intended_is_filename
        and filename_label_regions
        and not localization_lines
    ):
        label = min(
            filename_label_regions,
            key=lambda candidate: (
                abs(
                    candidate.y
                    + candidate.height / 2
                    - container_midpoint_y
                ),
                candidate.x,
            ),
        )
        # A native Save/Open edit begins immediately to the right of its
        # visible label. This fallback is used only when OCR omitted the value
        # row; the resulting same-row crop still has to read the entire safe
        # filename exactly before any follow-up input can be authorized.
        x = max(
            0,
            math.floor(container.x + label.x + label.width - 2),
        )
        y = max(0, math.floor(container.y + label.y - 2))
        container_right = min(
            screen_width,
            math.ceil(container.x + container.width),
        )
        x2 = min(container_right, x + 200)
        y2 = min(
            screen_height,
            math.ceil(container.y + label.y + label.height + 2),
        )
        return Region(
            x=x,
            y=y,
            width=max(1, x2 - x),
            height=max(1, y2 - y),
        )

    # Notepad repeats the first document line in its tab title. Prefer a
    # punctuation-complete exact row, then the row nearest the causal crop's
    # centre. A lookalike title may still nominate a crop when it is the only
    # evidence, but it cannot outrank the exact editor row beneath it.
    for line in sorted(localization_lines, key=candidate_order):
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
        first_target_alnum = next(
            (
                index
                for index, character in enumerate(intended)
                if character.isalnum()
            ),
            0,
        )
        visible_prefix_characters = sum(
            1
            for character in intended[:first_target_alnum]
            if not character.isspace()
        )
        # The alphanumeric locator deliberately ignores punctuation, but the
        # refined crop must not. A measured JSON row was localized from the
        # first ``r`` in ``"retries"`` and clipped the opening quote; exact OCR
        # then rejected a row that the editor had received correctly. Back off
        # by the visible intended prefix while leaving leading whitespace to
        # the independent caret-column proof.
        visible_prefix_width = (
            line_width / line_length * visible_prefix_characters
        )
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
        # Consecutive editor rows have almost no inter-line gap. Symmetric
        # padding around an indented JSON row included the lower pixels of the
        # previous row, so the fallback joined both lines and exact read-back
        # failed closed. Keep enough lower context for commas and descenders,
        # but use a tight upper edge for indented rows. Invisible indentation
        # is proved later from the editor's caret column, not from this crop.
        top_padding = (
            min(2, vertical_padding)
            if intended.startswith((" ", "\t"))
            else vertical_padding
        )
        horizontal_padding = 4 if intended_is_filename else 2
        x = max(
            0,
            math.floor(
                container.x
                + estimated_start
                - visible_prefix_width
                - horizontal_padding
            ),
        )
        y = max(
            0,
            math.floor(container.y + line_region.y - top_padding),
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


def _guarded_print_chunks(s: str) -> list[str]:
    """Retain a small focus probe, then coalesce acknowledged prose chunks.

    The first normal chunk bounds the amount that can land in a wrongly
    focused field before changed pixels/OCR prove the destination. Once that
    proof exists, grouping adjacent word-boundary chunks cuts remote captures
    and OCR checkpoints while preserving exact order, at-most-once delivery,
    and a cooperative cancellation boundary at most 64 characters away.
    """

    chunks = chunk_text(s)
    if len(chunks) <= 1:
        return chunks
    coalesced = [chunks[0]]
    pending = ""
    for chunk in chunks[1:]:
        if (
            pending
            and len(pending) + len(chunk) > FAST_PRINT_CHUNK_TARGET
        ):
            coalesced.append(pending)
            pending = ""
        pending += chunk
    if pending:
        coalesced.append(pending)
    return coalesced


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


def _dense_changed_candidates(
    before_image: bytes,
    after_image: bytes,
    dims: Any,
    *,
    allow_compact: bool = False,
) -> list[tuple[int, Region]]:
    width, height = _dims_wh(dims)
    if width <= 0 or height <= 0 or not before_image or not after_image:
        return []
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
        return []
    if before.shape != after.shape or before.ndim != 3:
        return []
    changed = np.max(np.abs(after - before), axis=2) > DENSE_PIXEL_DELTA
    changed_count = int(changed.sum())
    minimum_changed_pixels = (
        DENSE_COMPACT_MIN_CHANGED_PIXELS
        if allow_compact
        else DENSE_MIN_CHANGED_PIXELS
    )
    if changed_count < minimum_changed_pixels or changed_count > 50_000:
        return []

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
        line_candidate = (
            count >= DENSE_MIN_CHANGED_PIXELS
            and box_width >= DENSE_MIN_WIDTH
            and box_height >= DENSE_MIN_HEIGHT
            and box_height <= min(DENSE_MAX_HEIGHT, height * 0.1)
            and box_width >= box_height * 1.25
        )
        compact_candidate = (
            allow_compact
            and count >= DENSE_COMPACT_MIN_CHANGED_PIXELS
            and 4 <= box_width <= 32
            and 4 <= box_height <= 32
        )
        if line_candidate or compact_candidate:
            box = (x0, y0, x1, y1)
            candidates_by_box[box] = max(candidates_by_box.get(box, 0), count)
    candidates = [
        (count, *box)
        for box, count in candidates_by_box.items()
    ]
    if not candidates:
        return []
    candidates.sort(reverse=True)
    located: list[tuple[int, Region]] = []
    for count, x0, y0, x1, y1 in candidates:
        pad = 8
        x = max(0, x0 - pad)
        y = max(0, y0 - pad)
        x2 = min(width, x1 + pad)
        y2 = min(height, y1 + pad)
        located.append(
            (
                count,
                Region(
                    x=x,
                    y=y,
                    width=max(1, x2 - x),
                    height=max(1, y2 - y),
                ),
            )
        )
    return located


def locate_dense_changed_candidates(
    before_image: bytes,
    after_image: bytes,
    dims: Any,
    *,
    allow_compact: bool = False,
) -> list[Region]:
    """Return bounded line-shaped deltas in descending pixel-count order.

    These are OCR candidates, not proof that keyboard input caused a region.
    A caller choosing among multiple candidates must still verify the exact
    intended characters independently.
    """

    return [
        region
        for _count, region in _dense_changed_candidates(
            before_image,
            after_image,
            dims,
            allow_compact=allow_compact,
        )
    ]


def locate_dense_changed_bbox(
    before_image: bytes,
    after_image: bytes,
    dims: Any,
    *,
    allow_ambiguous: bool = False,
    allow_compact: bool = False,
) -> Region | None:
    """Locate a narrow text-line change hidden by the coarse luminance grid.

    Replacing selected text can preserve the average brightness of every
    96x54 grid cell even though hundreds of full-resolution pixels changed.
    This fallback accepts only a coherent, horizontal, text-line-sized delta;
    isolated caret blinking and large window repaints remain non-evidence.
    ``allow_ambiguous`` nominates the strongest plausible line without proving
    it is causal. Callers must still require an independent exact OCR match.
    """

    candidates = _dense_changed_candidates(
        before_image,
        after_image,
        dims,
        allow_compact=allow_compact,
    )
    if not candidates:
        return None
    count, region = candidates[0]
    if (
        not allow_ambiguous
        and len(candidates) > 1
        and count < candidates[1][0] * 2
    ):
        return None
    return region


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
        self._last_read_exact_unverified_spacing = False
        self._refined_readback_region: Region | None = None
        self._refined_readback_intended = ""
        self._causal_exact_spacing_region: Region | None = None
        self._causal_exact_spacing_intended = ""

    # ---- capture/read helpers -------------------------------------------- #

    def _dims(self) -> tuple[int, int]:
        get = getattr(self.backend, "get_dimensions", None)
        if callable(get):
            return _dims_wh(get())
        # Fall back to a captured frame's reported size — handled by callers that
        # already hold a frame; default to 0x0 (locate then declines).
        return (0, 0)

    def _with_causal_spacing_proof(
        self,
        result: OCRResult,
        *,
        intended: str | None,
        precise: bool,
        requested_region: Region,
    ) -> OCRResult:
        causal_region = self._causal_exact_spacing_region
        if (
            not precise
            or not intended
            or not any(character.isspace() for character in intended)
            or compute_verdict(intended, result.text, True) != "match"
            or re.sub(r"\s+", "", result.text)
            != re.sub(r"\s+", "", intended)
            or self._causal_exact_spacing_intended != intended
            or causal_region is None
            or not regions_overlap(requested_region, causal_region)
        ):
            return result
        # The immediately post-emission causal crop already proved this exact
        # row and its visible gap. A later settled OCR pass can dip just below
        # the spacing-confidence threshold while still reading every character
        # exactly. No HID occurs between those frames, so retain the earlier
        # bounded proof instead of turning the same row into an empty readback.
        DEBUG.event(
            "typing.causal_spacing_proof_reused",
            intended_characters=len(intended),
            requested_region=requested_region.model_dump(),
            causal_region=causal_region.model_dump(),
            canonicalized_visual_wrap=result.text != intended,
        )
        if result.text != intended:
            confidences = [
                line.confidence
                for line in result.lines
                if line.confidence is not None
            ]
            return OCRResult(
                lines=[
                    OCRLine(
                        text=intended,
                        confidence=(
                            min(confidences) if confidences else None
                        ),
                        bbox=(
                            result.lines[0].bbox
                            if len(result.lines) == 1
                            else None
                        ),
                        raw={
                            "causal_exact_row_canonicalized": True,
                            "observed_text": result.text,
                        },
                    )
                ],
                evidence_lines=result.evidence_lines,
                alternatives=result.alternatives,
                spacing_evidence="verified",
            )
        return OCRResult(
            lines=result.lines,
            evidence_lines=result.evidence_lines,
            alternatives=result.alternatives,
            spacing_evidence="verified",
        )

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
        allow_native_primary_fallback: bool = False,
        allow_empty_native_primary_fallback: bool = False,
        preserve_editor_indent_candidate: bool = False,
        extract_structured_exact_row: bool = False,
        minimum_confidence: float = MIN_MISMATCH_OCR_CONFIDENCE,
    ) -> str:
        """OCR the field. Capture the FULL frame and pass the region to the OCR
        provider so it reads the field crop on every backend: file OCR
        (tesseract) crops the saved frame by region, while live PiKVM OCR reads
        that region on the live screen — never the whole frame. ``""`` on failure."""
        self._last_read_semantic_spacing = False
        self._last_read_exact_unverified_spacing = False
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
        native_tmp: Path | None = None
        native_capture_region: Region | None = None
        native_local_region: Region | None = None

        async def native_crop(
            screen_region: Region,
            *,
            refresh: bool = False,
        ) -> tuple[Path, Region] | None:
            """Capture one lossless backend crop for the final exact re-read."""

            nonlocal native_capture_region, native_local_region, native_tmp
            if refresh:
                if native_tmp is not None:
                    native_tmp.unlink(missing_ok=True)
                native_tmp = None
                native_capture_region = None
                native_local_region = None
            if (
                native_tmp is not None
                and native_capture_region == screen_region
                and native_local_region is not None
            ):
                return native_tmp, native_local_region
            try:
                native_frame = await self.backend.screenshot(
                    region=screen_region,
                )
            except Exception:
                return None
            if (
                not native_frame
                or not native_frame.data
                or native_frame.width <= 0
                or native_frame.height <= 0
            ):
                return None
            if native_tmp is not None:
                native_tmp.unlink(missing_ok=True)
            native_file = tempfile.NamedTemporaryFile(
                suffix=".png",
                delete=False,
            )
            native_file.write(native_frame.data)
            native_file.close()
            native_tmp = Path(native_file.name)
            native_capture_region = screen_region
            native_local_region = Region(
                x=0,
                y=0,
                width=native_frame.width,
                height=native_frame.height,
            )
            DEBUG.event(
                "typing.field_readback_native_crop",
                width=native_frame.width,
                height=native_frame.height,
                image_sha256=hashlib.sha256(native_frame.data).hexdigest(),
                screen_region=screen_region.model_dump(),
            )
            return native_tmp, native_local_region

        def retain_unique_exact_row(candidate: OCRResult) -> OCRResult:
            if not extract_structured_exact_row or not intended:
                return candidate
            structured_row = unique_exact_structured_ocr_row(
                candidate,
                intended,
            )
            if structured_row is None and preserve_editor_indent_candidate:
                structured_row = unique_exact_structured_ocr_row(
                    candidate,
                    intended.lstrip(" \t"),
                )
            if structured_row is None:
                return candidate
            return OCRResult(
                lines=[structured_row],
                evidence_lines=candidate.evidence_lines,
                alternatives=candidate.alternatives,
                spacing_evidence=candidate.spacing_evidence,
            )

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
                if not exact_rows and extract_structured_exact_row:
                    structured_row = unique_exact_structured_ocr_row(
                        result,
                        intended,
                    )
                    if structured_row is not None:
                        exact_rows = [structured_row]
                if len(exact_rows) == 1:
                    exact_spacing_verified = any(
                        candidate.evidence_kind == "spacing"
                        and candidate.text == exact_rows[0].text
                        for candidate in result.alternatives
                    )
                    result = OCRResult(
                        lines=exact_rows,
                        evidence_lines=result.evidence_lines,
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
                    if extract_structured_exact_row:
                        # Hybrid OCR can choose a high-confidence canonical
                        # row that drops one terminal code glyph while its
                        # independently grounded evidence row retains the
                        # complete text. Preserve exactly one such row as a
                        # candidate instead of paying for blind model OCR.
                        # Whitespace remains deliberately unverified here;
                        # the editor status invariant must still prove it.
                        refined_row = unique_exact_structured_ocr_row(
                            result,
                            intended,
                            include_evidence=True,
                        )
                        if refined_row is not None:
                            result = OCRResult(
                                lines=[refined_row],
                                evidence_lines=result.evidence_lines,
                                alternatives=result.alternatives,
                                spacing_evidence=result.spacing_evidence,
                            )
                            DEBUG.event(
                                "typing.field_readback_refined_evidence",
                                observed_characters=len(refined_row.text),
                                confidence=refined_row.confidence,
                            )
            if (
                precise
                and intended
                and allow_blind_fallback
                and allow_native_primary_fallback
                and callable(precise_ocr)
                and (
                    bool(result.text)
                    or allow_empty_native_primary_fallback
                )
                and compute_verdict(intended, result.text, True)
                != "match"
                and re.sub(r"\s+", "", result.text)
                != re.sub(r"\s+", "", intended)
                and not visible_editor_indent_candidate(
                    result,
                    intended,
                    preserve=preserve_editor_indent_candidate,
                )
            ):
                native_screen_region = native_primary_readback_region(
                    region,
                    refined_region,
                    self._dims(),
                )
                native = await native_crop(native_screen_region)
                if native is not None:
                    native_path, native_region = native
                    try:
                        native_result = await precise_ocr(
                            native_path,
                            region=native_region,
                        )
                    except Exception:
                        native_result = OCRResult()
                    if native_result.text:
                        result = retain_unique_exact_row(native_result)
                        DEBUG.event(
                            "typing.field_readback_native_primary",
                            observed_characters=len(result.text),
                            line_count=len(result.lines),
                            selected_sha256=hashlib.sha256(
                                result.text.encode("utf-8")
                            ).hexdigest(),
                            verdict=compute_verdict(
                                intended,
                                result.text,
                                True,
                            ),
                            **_ocr_candidate_fingerprints(native_result),
                        )
                        if compute_verdict(
                            intended,
                            result.text,
                            True,
                        ) != "match":
                            # A terminal caret can overlap the final glyph in
                            # one otherwise exact native crop. Sample the
                            # opposite blink phase once, then accept only a
                            # complete exact read. This remains read-only and
                            # never replays the typed payload.
                            await asyncio.sleep(
                                NATIVE_PRIMARY_CARET_RETRY_DELAY_S
                            )
                            retry_native = await native_crop(
                                native_screen_region,
                                refresh=True,
                            )
                            retry_result = OCRResult()
                            if retry_native is not None:
                                retry_path, retry_region = retry_native
                                try:
                                    retry_result = await precise_ocr(
                                        retry_path,
                                        region=retry_region,
                                    )
                                except Exception:
                                    retry_result = OCRResult()
                            if retry_result.text:
                                retry_result = retain_unique_exact_row(
                                    retry_result
                                )
                                retry_verdict = compute_verdict(
                                    intended,
                                    retry_result.text,
                                    True,
                                )
                                DEBUG.event(
                                    "typing.field_readback_native_primary_retry",
                                    observed_characters=len(
                                        retry_result.text
                                    ),
                                    line_count=len(retry_result.lines),
                                    selected_sha256=hashlib.sha256(
                                        retry_result.text.encode("utf-8")
                                    ).hexdigest(),
                                    verdict=retry_verdict,
                                    **_ocr_candidate_fingerprints(
                                        retry_result
                                    ),
                                )
                                if retry_verdict == "match":
                                    result = retry_result
            if (
                precise
                and intended
                and allow_blind_fallback
                and compute_verdict(intended, result.text, True)
                != "match"
                and not visible_editor_indent_candidate(
                    result,
                    intended,
                    preserve=preserve_editor_indent_candidate,
                )
            ):
                blind_precise_ocr = getattr(
                    self.ocr,
                    "ocr_precise_fallback",
                    None,
                )
                if callable(blind_precise_ocr):
                    blind_path = tmp
                    blind_region = refined_region or region
                    native = await native_crop(blind_region)
                    if native is not None:
                        blind_path, blind_region = native
                        DEBUG.event(
                            "typing.field_readback_native_fallback",
                            width=blind_region.width,
                            height=blind_region.height,
                        )
                    try:
                        blind_result = await blind_precise_ocr(
                            blind_path,
                            region=blind_region,
                        )
                    except Exception:
                        blind_result = OCRResult()
                    if blind_result.text:
                        result = retain_unique_exact_row(blind_result)
                        DEBUG.event(
                            "typing.field_readback_fallback",
                            provider="blind_model_consensus",
                            observed_characters=len(result.text),
                            line_count=len(result.lines),
                        )
            result = self._with_causal_spacing_proof(
                result,
                intended=intended,
                precise=precise,
                requested_region=requested_region,
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
                    visible_indent_candidate = (
                        visible_editor_indent_candidate(
                            result,
                            intended,
                            preserve=preserve_editor_indent_candidate,
                        )
                    )
                    if (
                        allow_semantic_spacing
                        and norm(intended, precise)
                        == norm(
                            strip_prompt(result.text),
                            precise,
                        )
                    ):
                        self._last_read_semantic_spacing = True
                    elif not visible_indent_candidate:
                        # Ordinary OCR collapses whitespace. Exact completion
                        # needs independently repeated, calibrated word-gap
                        # evidence. A terminal command whose argv contains no
                        # quoting or shell syntax is the sole exception because
                        # repeated token separators are semantically identical.
                        self._last_read_exact_unverified_spacing = bool(
                            "\n" not in intended
                            and "\r" not in intended
                            and compute_verdict(
                                intended,
                                result.text,
                                True,
                            )
                            == "match"
                        )
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
            if native_tmp is not None:
                native_tmp.unlink(missing_ok=True)

    async def _read_screen(
        self,
        *,
        precise: bool = False,
        region: Region | None = None,
    ) -> OCRResult:
        """OCR one capture for localization or a bounded exact re-read."""
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
                return await precise_ocr(tmp, region=region)
            return await self.ocr.ocr(tmp, region=region)
        except Exception:
            return OCRResult()
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    async def _read_native_screen_with(
        self,
        region: Region,
        *,
        reader_name: str,
        debug_kind: str,
    ) -> OCRResult:
        """OCR one lossless crop and normalize boxes to screen coordinates."""

        reader = getattr(self.ocr, reader_name, None)
        if not callable(reader):
            return OCRResult()
        tmp: Path | None = None
        try:
            frame = await self.backend.screenshot(region=region)
            if (
                not frame
                or not frame.data
                or frame.width <= 0
                or frame.height <= 0
            ):
                return OCRResult()
            fd = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            fd.write(frame.data)
            fd.close()
            tmp = Path(fd.name)
            result = await reader(
                tmp,
                region=Region(
                    x=0,
                    y=0,
                    width=frame.width,
                    height=frame.height,
                ),
            )
            scale_x = region.width / frame.width
            scale_y = region.height / frame.height

            def normalize_line(line: OCRLine) -> OCRLine:
                if line.bbox is None or len(line.bbox) < 4:
                    return line
                left, top, right, bottom = line.bbox[:4]
                return line.model_copy(
                    update={
                        "bbox": [
                            float(left) * scale_x,
                            float(top) * scale_y,
                            float(right) * scale_x,
                            float(bottom) * scale_y,
                        ],
                        "raw": {
                            **dict(line.raw or {}),
                            "native_crop_geometry_normalized": True,
                        },
                    }
                )

            normalized = result.model_copy(
                update={
                    "lines": [normalize_line(line) for line in result.lines],
                    "evidence_lines": [
                        normalize_line(line)
                        for line in result.evidence_lines
                    ],
                }
            )
            DEBUG.event(
                debug_kind,
                width=frame.width,
                height=frame.height,
                screen_region=region.model_dump(),
                observed_characters=len(normalized.text),
                line_count=len(normalized.lines),
                **_ocr_candidate_fingerprints(normalized),
            )
            return normalized
        except Exception:
            return OCRResult()
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    async def _read_native_screen(self, region: Region) -> OCRResult:
        """Precisely OCR one lossless backend crop in screen coordinates.

        Region capture can have more pixels than the downscaled observation
        frame. The shared native reader normalizes its OCR boxes back into the
        requested screen crop before any foreground/status geometry uses it.
        """

        return await self._read_native_screen_with(
            region,
            reader_name="ocr_precise",
            debug_kind="typing.screen_readback_native_primary",
        )

    async def _read_precise_screen_fallback(
        self,
        region: Region,
    ) -> OCRResult:
        """Read one bounded screen crop with the blind precise OCR lane.

        The normal screen reader deliberately stays on the fast local OCR
        path. Tiny editor status text is the one place where a local miss can
        leave otherwise exact indentation unverifiable, so callers may
        explicitly escalate only that geometry-derived crop. The fallback
        receives the native crop rather than a downscaled full frame.
        """

        return await self._read_native_screen_with(
            region,
            reader_name="ocr_precise_fallback",
            debug_kind="typing.screen_readback_fallback",
        )

    def _locate_ocr_candidate(
        self,
        result: OCRResult,
        intended: str,
        dims: tuple[int, int],
        *,
        precise: bool = False,
        editor_field: bool = False,
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
            if (
                region is not None
                and (
                    not editor_field
                    or is_autolocated_editor_body_region(region, dims)
                )
            ):
                return region
        return None

    async def _recover_precise_ocr_candidate(
        self,
        result: OCRResult,
        intended: str,
        dims: tuple[int, int],
        *,
        editor_field: bool = False,
    ) -> tuple[Region, str] | None:
        """Use a noisy full-screen row only to choose an exact re-read crop.

        Dark-theme editor glyphs can fall below the frame-grid threshold while
        whole-screen OCR confuses small punctuation such as ``[]`` with
        ``[J``. A punctuation-tolerant row may therefore localize the field,
        but it is never focus evidence by itself: a fresh cropped OCR pass must
        independently match the complete intended text, the one bounded
        standalone-``i`` mutation that the editor repair path can correct, or
        one exact editor glyph row whose whitespace remains deliberately
        unverified.  That final case grounds focus only; the later read-back
        path must still prove spacing before reporting verified input.
        """

        width, height = dims
        if width <= 0 or height <= 0:
            return None
        localized_result = result
        if editor_field:
            body_lines = [
                line
                for line in result.lines
                if (
                    (line_region := ocr_line_region(line, dims)) is not None
                    and is_autolocated_editor_body_region(line_region, dims)
                )
            ]
            ignored_rows = len(result.lines) - len(body_lines)
            if ignored_rows:
                DEBUG.event(
                    "typing.editor_chrome_rows_ignored",
                    ignored_rows=ignored_rows,
                    retained_rows=len(body_lines),
                )
            localized_result = result.model_copy(update={"lines": body_lines})
        candidate_region = precise_readback_candidate_region(
            localized_result,
            intended,
            Region(x=0, y=0, width=width, height=height),
            dims,
        )
        if candidate_region is None:
            return None
        read_back = self._typed_candidate(
            await self._read_field(
                candidate_region,
                intended=intended,
                precise=True,
                allow_blind_fallback=True,
                # A maximized editor row may be legible only in the backend's
                # lossless crop even after the full-screen OCR has correctly
                # localized its body. Keep this editor-only and read-only:
                # the native lane still requires a complete exact canonical
                # read plus independent spacing proof and never replays HID.
                allow_native_primary_fallback=editor_field,
            ),
            intended,
            True,
        )
        confirmed = compute_verdict(intended, read_back, True) == "match"
        editor_autocorrect_localized = bool(
            editor_field
            and is_standalone_i_autocorrect(intended, read_back)
        )
        editor_spacing_pending_localized = bool(
            editor_field
            and not read_back
            and any(character.isspace() for character in intended)
            and "\n" not in intended
            and "\r" not in intended
            and len(
                [
                    line
                    for line in self._last_field_ocr_result.lines
                    if (
                        line.text == intended
                        and (
                            line.confidence is None
                            or float(line.confidence)
                            >= MIN_GROUNDED_EXACT_OCR_CONFIDENCE
                        )
                    )
                ]
            )
            == 1
        )
        DEBUG.event(
            "typing.precise_localization_recheck",
            confirmed=confirmed,
            editor_autocorrect_localized=editor_autocorrect_localized,
            editor_spacing_pending_localized=(
                editor_spacing_pending_localized
            ),
            intended_characters=len(intended),
            region=candidate_region.model_dump(),
        )
        return (
            (candidate_region, read_back)
            if (
                confirmed
                or editor_autocorrect_localized
                or editor_spacing_pending_localized
            )
            else None
        )

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
                if self._bounded_prose_candidate(
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
            known_filename_rows = {
                intended,
                f"File name: {intended}",
            }
            known_type_rows = known_save_as_type_rows | {
                f"save as type: {row}"
                for row in known_save_as_type_rows
            }
            known_trailing_chrome_rows = {"hide folders"}
            if (
                len(lines) in {2, 3}
                and lines[0] in known_filename_rows
                and lines[1].casefold() in known_type_rows
                and (
                    len(lines) == 2
                    or lines[2].casefold() in known_trailing_chrome_rows
                )
            ):
                return intended
        return read_back

    @staticmethod
    def _bounded_prose_candidate(
        result: OCRResult,
        intended: str,
    ) -> str:
        """Select a bounded OCR line window for long natural-language prose.

        A word processor can wrap one print burst across several lines while
        the grid-diff crop still covers only the first changed line. Grounded
        field or full-screen OCR may also join adjacent words or confuse a few
        small glyphs. Search only consecutive line windows near the intended
        length and retain the normal prose verifier's eight-percent edit
        ceiling. This path is never used for precise code, commands, paths,
        URLs, or secrets.
        """

        visible_intended = " ".join(intended.split())
        folded_intended = fold_quotes(visible_intended).casefold()
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
            folded_read_back = fold_quotes(visible_read_back).casefold()
            if len(folded_read_back) != len(visible_read_back):
                continue
            index = folded_read_back.rfind(folded_intended)
            if index >= 0:
                candidate = visible_read_back[
                    index : index + len(visible_intended)
                ]
                # Word processors can visually smarten the exact ASCII quote
                # bytes already acknowledged by the transport receipt. Keep
                # every observed character except a one-for-one glyph in the
                # same quote family, which is restored to the requested glyph.
                # Case, punctuation families, spacing, and lengths remain
                # exact; code/commands never enter this prose-only locator.
                candidate = "".join(
                    requested
                    if (
                        observed != requested
                        and fold_quotes(observed)
                        == fold_quotes(requested)
                    )
                    else observed
                    for observed, requested in zip(
                        candidate,
                        visible_intended,
                        strict=True,
                    )
                )
                # OCR normalizes an editor's visible word boundary but does not
                # retain which append transaction owned that space. Restore one
                # requested edge space only when the full document visibly
                # proves the same adjacent boundary. This lets an exact editor
                # suffix keep its byte receipt without accepting invisible or
                # repeated whitespace.
                if (
                    intended.startswith(" ")
                    and not intended.startswith("  ")
                    and index > 0
                    and visible_read_back[index - 1] == " "
                ):
                    candidate = f" {candidate}"
                end = index + len(visible_intended)
                if (
                    intended.endswith(" ")
                    and not intended.endswith("  ")
                    and end < len(visible_read_back)
                    and visible_read_back[end] == " "
                ):
                    candidate = f"{candidate} "
                return candidate

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
        self._causal_exact_spacing_region = None
        self._causal_exact_spacing_intended = ""
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

        # FAST TRANSPORT: long prose, exact editor text, and exact simple
        # terminal argv can use the server-side keymap printer, while remaining
        # chunked, interruptible, visually read back, and never auto-submitted.
        # Exact terminal text is restricted to the no-metacharacter grammar
        # above; its separate Enter remains a later guarded action. Caps-on and
        # secrets always stay on the compensating per-key transport.
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
            and context.casefold() == "editor"
            # Short exact headings can begin with layout-sensitive punctuation.
            # Keep them on the same guarded, keymap-aware printer as code rows;
            # exact visual readback still gates every later action.
            and total >= PRECISE_LOCATE_MIN_CHARS
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
                prose=prose,
                secret=secret,
                precise=precise,
                single_line_field=context.casefold() == "field",
                editor_field=context.casefold() == "editor",
                allow_semantic_spacing=allow_semantic_spacing,
                should_continue=should_continue,
                fast_print=True,
            )

        return await self._humanized(
            delivery_text,
            region=region,
            code=code,
            prose=prose,
            secret=secret,
            precise=precise,
            single_line_field=context.casefold() == "field",
            editor_field=context.casefold() == "editor",
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
        prose: bool,
        secret: bool,
        precise: bool,
        single_line_field: bool,
        editor_field: bool,
        allow_semantic_spacing: bool,
        should_continue: Callable[[], bool] | None = None,
        fast_print: bool = False,
    ) -> WatchedTypingResult:
        dims = self._dims()
        # Short exact fields (notably allowlisted Windows Run targets) are one
        # bounded emission. Splitting a 17–20 character URI after character 16
        # lets its own autocomplete popup look like external focus theft before
        # the final glyph. Exact OCR still gates every following action.
        chunks = (
            [text]
            if precise
            and (
                len(text) <= 20
                or (
                    editor_field
                    and code
                    and len(text) <= FAST_PRINT_CHUNK_TARGET
                )
            )
            else (
                _guarded_print_chunks(text)
                if fast_print
                else chunk_text(text)
            )
        )
        total = len(text)
        explicit_region = region is not None
        located = explicit_region
        cur_region: Region | None = region
        typed_so_far = ""
        corrections = 0
        delivery_retries = 0
        last_read = ""
        verified_clean = False
        causal_short_field_localized = False
        causal_partial_delivery = ""
        local_replacement_failure: tuple[VerificationStatus, str] | None = None
        bounded_editor_code = bool(
            editor_field
            and code
            and total <= FAST_PRINT_CHUNK_TARGET
        )
        structural_code_glyph = bool(
            precise
            and editor_field
            and code
            and is_structural_code_fragment(text)
        )
        defer_blind_editor_readback = bool(
            precise
            and editor_field
            and code
            and total <= FAST_PRINT_CHUNK_TARGET
            and not explicit_region
            # One-glyph structural appends already use repeated compact-crop
            # consensus. Deferring their blind fallback removes the repeated
            # reads needed to prove an indented closing glyph such as ``  ]``.
            and not structural_code_glyph
        )
        can_vision = not secret and (
            total > 4
            or (precise and total >= 3)
            or structural_code_glyph
        )
        emitted_parts: list[str] = []

        async def emit_text(value: str) -> None:
            emitted_parts.append(value)
            if fast_print:
                printer = (
                    getattr(self.backend, "print_exact_text", None)
                    if precise and editor_field and code
                    else None
                )
                if not callable(printer):
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

        async def emit_local_repair(value: str) -> None:
            """Insert one proven repair through an independent transport."""

            emitted_parts.append(value)
            await self.backend.type_text(
                value,
                code=code,
                secret=secret,
            )

        async def exact_dense_candidate(
            before_frame: CapturedFrame | None,
            after_frame: CapturedFrame | None,
            intended_snapshot: str,
            coarse_region: Region | None,
        ) -> Region | None:
            """Choose among causal line deltas only by exact cropped OCR."""

            nonlocal causal_short_field_localized
            nonlocal last_read, verified_clean
            if (
                not precise
                or (total > 20 and not bounded_editor_code)
                or explicit_region
                or (single_line_field and coarse_region is not None)
                or before_frame is None
                or after_frame is None
            ):
                return None
            candidates = await asyncio.to_thread(
                locate_dense_changed_candidates,
                before_frame.data,
                after_frame.data,
                dims,
                allow_compact=(
                    structural_code_glyph or single_line_field
                ),
            )
            if not candidates:
                return None
            ordered = sorted(
                enumerate(candidates),
                key=lambda indexed: (
                    0
                    if (
                        coarse_region is not None
                        and regions_overlap(coarse_region, indexed[1])
                    )
                    else 1,
                    (
                        indexed[1].y
                        if single_line_field
                        else indexed[0]
                    ),
                ),
            )
            checked = 0
            tmp: Path | None = None
            try:
                fd = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                fd.write(after_frame.data)
                fd.close()
                tmp = Path(fd.name)
                precise_ocr = getattr(self.ocr, "ocr_precise", None)
                reader = (
                    precise_ocr
                    if callable(precise_ocr)
                    else self.ocr.ocr
                )

                def has_spacing_proof(ocr_result: OCRResult) -> bool:
                    return (
                        ocr_result.spacing_evidence == "verified"
                        or any(
                            alternative.evidence_kind == "spacing"
                            and (
                                alternative.text == intended_snapshot
                                or (
                                    "\n" not in intended_snapshot
                                    and "\r" not in intended_snapshot
                                    and sum(
                                        row == intended_snapshot
                                        for row in (
                                            alternative.text.splitlines()
                                        )
                                    )
                                    == 1
                                )
                            )
                            for alternative in ocr_result.alternatives
                        )
                )

                for _index, candidate in ordered[:4]:
                    checked += 1
                    used_native_primary = False
                    # Dense candidates are already padded around only the
                    # pixels changed by this exact emission. Expanding them
                    # with the generic field margin can pull in editor borders,
                    # carets, or adjacent controls and turn a clean short row
                    # into OCR such as ``| alpha beta!``. Remove one pixel of
                    # change-detector padding instead and keep this proof
                    # causally bounded to the emitted line.
                    inset = (
                        1.0
                        if candidate.width > 2 and candidate.height > 2
                        else 0.0
                    )
                    candidate_readback_region = Region(
                        x=candidate.x + inset,
                        y=candidate.y + inset,
                        width=max(1.0, candidate.width - inset * 2),
                        height=max(1.0, candidate.height - inset * 2),
                    )
                    result = await reader(
                        tmp,
                        region=candidate_readback_region,
                    )
                    spacing_verified = has_spacing_proof(result)
                    if (
                        single_line_field
                        and compute_verdict(
                            intended_snapshot,
                            result.text,
                            True,
                        )
                        != "match"
                    ):
                        # A tiny field delta can be smaller than the enabled
                        # button repaint below it. Evaluate compact candidates
                        # in screen order and re-read each from one lossless
                        # backend crop. This remains causal geometry: expected
                        # text may verify a candidate but never selects it, and
                        # no HID is replayed when every crop is ambiguous.
                        native_read = await self._read_field(
                            candidate_readback_region,
                            intended=intended_snapshot,
                            precise=True,
                            allow_blind_fallback=True,
                            allow_native_primary_fallback=True,
                            allow_empty_native_primary_fallback=True,
                            extract_structured_exact_row=True,
                        )
                        if (
                            compute_verdict(
                                intended_snapshot,
                                native_read,
                                True,
                            )
                            == "match"
                        ):
                            result = self._last_field_ocr_result
                            spacing_verified = has_spacing_proof(result)
                            used_native_primary = True
                    if (
                        not spacing_verified
                        and any(
                            character.isspace()
                            for character in intended_snapshot
                        )
                    ):
                        provisional_rows = [
                            line
                            for line in result.lines
                            if (
                                norm(line.text, precise=True)
                                == norm(intended_snapshot, precise=True)
                                and line.confidence is not None
                                and float(line.confidence) >= 0.45
                            )
                        ]
                        if not provisional_rows:
                            provisional_rows = matching_collinear_ocr_rows(
                                result.lines,
                                intended_snapshot,
                                (
                                    max(
                                        1,
                                        math.ceil(
                                            candidate_readback_region.width
                                        ),
                                    ),
                                    max(
                                        1,
                                        math.ceil(
                                            candidate_readback_region.height
                                        ),
                                    ),
                                ),
                            )
                        if len(provisional_rows) == 1:
                            refined_region = ocr_line_screen_region(
                                provisional_rows[0],
                                candidate_readback_region,
                                dims,
                                pad=2,
                            )
                            if (
                                refined_region is not None
                                and refined_region.width
                                < candidate_readback_region.width
                            ):
                                refined_result = await reader(
                                    tmp,
                                    region=refined_region,
                                )
                                if has_spacing_proof(refined_result):
                                    result = refined_result
                                    candidate_readback_region = refined_region
                                    spacing_verified = True
                                    DEBUG.event(
                                        "typing.causal_row_refined",
                                        intended_characters=(
                                            len(intended_snapshot)
                                        ),
                                        region=refined_region.model_dump(),
                                    )
                    exact_rows = [
                        line
                        for line in result.lines
                        if (
                            line.text == intended_snapshot
                            and (
                                line.confidence is None
                                or float(line.confidence)
                                >= MIN_GROUNDED_EXACT_OCR_CONFIDENCE
                            )
                        )
                    ]
                    if not exact_rows and bounded_editor_code:
                        autocorrect_rows = [
                            line
                            for line in result.lines
                            if (
                                is_standalone_i_autocorrect(
                                    intended_snapshot,
                                    line.text,
                                )
                                and line.confidence is not None
                                and float(line.confidence)
                                >= MIN_GROUNDED_EXACT_OCR_CONFIDENCE
                            )
                        ]
                        if len(autocorrect_rows) == 1:
                            localized_region = candidate
                            screen_row_region = ocr_line_screen_region(
                                autocorrect_rows[0],
                                candidate_readback_region,
                                dims,
                                pad=2,
                            )
                            if screen_row_region is not None:
                                localized_region = screen_row_region
                            DEBUG.event(
                                "typing.causal_autocorrect_row_localized",
                                intended_characters=len(intended_snapshot),
                                region=localized_region.model_dump(),
                            )
                            DEBUG.event(
                                "typing.dense_candidate_scan",
                                candidate_count=len(candidates),
                                checked=checked,
                                matched=True,
                                verified=False,
                                region=localized_region.model_dump(),
                            )
                            return localized_region
                    if not exact_rows and spacing_verified:
                        spacing_rows = [
                            line
                            for line in result.lines
                            if (
                                norm(line.text, precise=True)
                                == norm(intended_snapshot, precise=True)
                                and (
                                    line.confidence is None
                                    or float(line.confidence)
                                    >= MIN_GROUNDED_EXACT_OCR_CONFIDENCE
                                )
                            )
                        ]
                        if not spacing_rows:
                            spacing_rows = matching_collinear_ocr_rows(
                                result.lines,
                                intended_snapshot,
                                (
                                    max(
                                        1,
                                        math.ceil(
                                            candidate_readback_region.width
                                        ),
                                    ),
                                    max(
                                        1,
                                        math.ceil(
                                            candidate_readback_region.height
                                        ),
                                    ),
                                ),
                                minimum_confidence=(
                                    MIN_GROUNDED_EXACT_OCR_CONFIDENCE
                                ),
                            )
                        if len(spacing_rows) == 1:
                            source_row = spacing_rows[0]
                            exact_rows = [
                                source_row.model_copy(
                                    update={
                                        "text": intended_snapshot,
                                        "raw": {
                                            **dict(source_row.raw or {}),
                                            "causal_spacing_canonicalized": True,
                                            "observed_text": source_row.text,
                                        },
                                    }
                                )
                            ]
                    if (
                        bounded_editor_code
                        and len(exact_rows) == 1
                        and any(
                            character.isspace()
                            for character in intended_snapshot
                        )
                        and not spacing_verified
                    ):
                        # Modern Notepad mirrors the first document row in its
                        # tab title. A punctuation-complete row inside this
                        # exact emission's dense pixel delta may ground the
                        # editor field, but uncalibrated OCR spacing cannot yet
                        # verify the payload. Retain only the causal location;
                        # the normal expanded read and independent Ln/Col/count
                        # status proof still have to establish exactness.
                        localized_region = candidate
                        screen_row_region = ocr_line_screen_region(
                            exact_rows[0],
                            candidate_readback_region,
                            dims,
                            pad=2,
                        )
                        if screen_row_region is not None:
                            localized_region = screen_row_region
                        DEBUG.event(
                            "typing.causal_exact_row_localized",
                            intended_characters=len(intended_snapshot),
                            spacing_verified=False,
                            region=localized_region.model_dump(),
                        )
                        DEBUG.event(
                            "typing.dense_candidate_scan",
                            candidate_count=len(candidates),
                            checked=checked,
                            matched=True,
                            verified=False,
                            region=localized_region.model_dump(),
                        )
                        return localized_region
                    if (
                        intended_snapshot != intended_snapshot.strip()
                        and not spacing_verified
                    ):
                        # A dense delta begins at the first painted glyph, so
                        # its crop cannot prove leading indentation.  It can
                        # still safely locate the row when one high-confidence
                        # OCR line matches every visible character exactly.
                        # Return only its causal region here; do not retain an
                        # exact receipt.  The normal expanded field read below
                        # must independently prove the whitespace before a
                        # later action can commit or append anything.
                        localization_rows = [
                            line
                            for line in result.lines
                            if (
                                line.text.strip()
                                == intended_snapshot.strip()
                                and line.confidence is not None
                                and float(line.confidence)
                                >= MIN_GROUNDED_EXACT_OCR_CONFIDENCE
                            )
                        ]
                        if len(localization_rows) == 1:
                            localized_region = candidate
                            screen_row_region = ocr_line_screen_region(
                                localization_rows[0],
                                candidate_readback_region,
                                dims,
                                pad=2,
                            )
                            if screen_row_region is not None:
                                localized_region = screen_row_region
                            DEBUG.event(
                                "typing.causal_exact_row_localized",
                                intended_characters=len(intended_snapshot),
                                spacing_verified=False,
                                region=localized_region.model_dump(),
                            )
                            DEBUG.event(
                                "typing.dense_candidate_scan",
                                candidate_count=len(candidates),
                                checked=checked,
                                matched=True,
                                verified=False,
                                region=localized_region.model_dump(),
                            )
                            return localized_region
                    if (
                        len(exact_rows) != 1
                        or (
                            any(
                                character.isspace()
                                for character in intended_snapshot
                            )
                            and not spacing_verified
                        )
                    ):
                        continue
                    matched_region = candidate
                    screen_row_region = ocr_line_screen_region(
                        exact_rows[0],
                        candidate_readback_region,
                        dims,
                        pad=2,
                    )
                    if screen_row_region is not None:
                        matched_region = screen_row_region
                    if spacing_verified:
                        self._causal_exact_spacing_region = matched_region
                        self._causal_exact_spacing_intended = intended_snapshot
                    self._last_field_ocr_result = OCRResult(
                        lines=exact_rows,
                        evidence_lines=result.evidence_lines,
                        alternatives=result.alternatives,
                        spacing_evidence=(
                            "verified"
                            if spacing_verified
                            else result.spacing_evidence
                        ),
                    )
                    self._last_read_semantic_spacing = spacing_verified
                    if not used_native_primary:
                        frame_sha256 = str(after_frame.sha256 or "").lower()
                        self._last_readback_frame_sha256 = (
                            frame_sha256
                            if re.fullmatch(
                                r"[0-9a-f]{64}",
                                frame_sha256,
                            )
                            else hashlib.sha256(after_frame.data).hexdigest()
                        )
                    # This OCR is already grounded to the before/after pixel
                    # delta from the current at-most-once emission. A unique
                    # exact row, confidence gate, and independently verified
                    # visible spacing are stronger evidence than a later
                    # multi-line crop. Retain it as the terminal receipt
                    # instead of paying for a noisier second OCR pass.
                    last_read = intended_snapshot
                    verified_clean = True
                    DEBUG.event(
                        "typing.causal_exact_row_retained",
                        intended_characters=len(intended_snapshot),
                        readback_frame_sha256=(
                            self._last_readback_frame_sha256
                        ),
                        region=matched_region.model_dump(),
                    )
                    DEBUG.event(
                        "typing.dense_candidate_scan",
                        candidate_count=len(candidates),
                        checked=checked,
                        matched=True,
                        region=matched_region.model_dump(),
                    )
                    return matched_region
                geometric_field = (
                    short_field_candidate_above_control_effect(
                        candidates,
                        dims,
                    )
                    if single_line_field
                    else None
                )
                if geometric_field is not None:
                    localized_region = geometric_field
                    DEBUG.event(
                        "typing.causal_short_field_localized",
                        intended_characters=len(intended_snapshot),
                        verified=False,
                        region=localized_region.model_dump(),
                    )
                    DEBUG.event(
                        "typing.dense_candidate_scan",
                        candidate_count=len(candidates),
                        checked=checked,
                        matched=True,
                        verified=False,
                        region=localized_region.model_dump(),
                    )
                    causal_short_field_localized = True
                    return localized_region
                DEBUG.event(
                    "typing.dense_candidate_scan",
                    candidate_count=len(candidates),
                    checked=checked,
                    matched=False,
                )
                return None
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)

        async def causal_partial_editor_candidate(
            before_frame: CapturedFrame | None,
            after_frame: CapturedFrame | None,
            intended_snapshot: str,
        ) -> tuple[str, Region] | None:
            """Return one causal, high-confidence strict editor fragment.

            A Windows RFB server can accept the HTTP print request while the
            guest paints only a suffix of the row.  That is not permission to
            replay anything, but it is enough to distinguish partial delivery
            from an unfocused field.  Candidate geometry comes only from the
            before/after emission frames; expected text may classify a strict
            fragment but never select pixels or authorize success.
            """

            if (
                not bounded_editor_code
                or before_frame is None
                or after_frame is None
                or not before_frame.data
                or not after_frame.data
            ):
                return None
            candidates = await asyncio.to_thread(
                locate_dense_changed_candidates,
                before_frame.data,
                after_frame.data,
                dims,
                allow_compact=structural_code_glyph,
            )
            visible_intended = intended_snapshot.strip()
            if len(visible_intended) < 4:
                return None
            matches: list[tuple[str, Region, OCRResult]] = []
            for candidate in candidates[:4]:
                result = await self._read_screen(
                    precise=True,
                    region=candidate,
                )
                rows = {
                    strip_prompt(line.text).strip()
                    for line in result.lines
                    if (
                        line.text.strip()
                        and line.confidence is not None
                        and float(line.confidence) >= 0.95
                    )
                }
                fragments = {
                    row
                    for row in rows
                    if (
                        3 <= len(row) < len(visible_intended)
                        and (
                            visible_intended.startswith(row)
                            or visible_intended.endswith(row)
                        )
                    )
                }
                if len(fragments) == 1:
                    matches.append((fragments.pop(), candidate, result))
            if len(matches) != 1:
                return None
            observed, region, result = matches[0]
            self._last_field_ocr_result = result
            DEBUG.event(
                "typing.causal_partial_editor_row",
                intended_characters=len(intended_snapshot),
                observed_characters=len(observed),
                fragment_kind=(
                    "prefix"
                    if visible_intended.startswith(observed)
                    else "suffix"
                ),
                region=region.model_dump(),
            )
            return observed, region

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

        def unique_editor_autocorrect_candidate(
            intended_snapshot: str,
            read_back: str,
        ) -> str:
            """Return one grounded row with only Notepad's ``i`` mutation."""

            if is_standalone_i_autocorrect(intended_snapshot, read_back):
                return read_back
            candidates = {
                row.strip()
                for line in self._last_field_ocr_result.lines
                if (
                    line.confidence is None
                    or float(line.confidence)
                    >= MIN_MISMATCH_OCR_CONFIDENCE
                )
                for row in line.text.splitlines()
                if is_standalone_i_autocorrect(
                    intended_snapshot,
                    row.strip(),
                )
            }
            if len(candidates) != 1:
                return ""
            candidate = candidates.pop()
            DEBUG.event(
                "typing.editor_autocorrect_row_localized",
                intended_characters=len(intended_snapshot),
            )
            return candidate

        def unique_editor_exact_candidate_after_correction(
            intended_snapshot: str,
            read_back: str,
        ) -> str:
            """Select an exact row only when no stale autocorrect row remains."""

            rows = [
                row.strip()
                for line in self._last_field_ocr_result.lines
                if (
                    line.confidence is None
                    or float(line.confidence)
                    >= MIN_GROUNDED_EXACT_OCR_CONFIDENCE
                )
                for row in line.text.splitlines()
                if row.strip()
            ]
            if any(
                is_standalone_i_autocorrect(intended_snapshot, row)
                for row in rows
            ):
                return read_back
            candidates = {
                row
                for row in rows
                if norm(row, True) == norm(intended_snapshot, True)
            }
            return candidates.pop() if len(candidates) == 1 else read_back

        def unique_editor_punctuation_substitution_candidate(
            intended_snapshot: str,
        ) -> str:
            """Return one high-confidence row with one punctuation HID slip."""

            candidates = {
                row.strip()
                for line in self._last_field_ocr_result.lines
                if (
                    line.confidence is not None
                    and float(line.confidence) >= 0.95
                )
                for row in line.text.splitlines()
                if editor_punctuation_transport_substitution(
                    intended_snapshot,
                    row.strip(),
                )
                is not None
            }
            return candidates.pop() if len(candidates) == 1 else ""

        def unique_editor_single_deletion_candidate(
            intended_snapshot: str,
        ) -> str:
            """Return one high-confidence row missing one requested glyph."""

            candidates = {
                row.strip()
                for line in self._last_field_ocr_result.lines
                if (
                    line.confidence is not None
                    and float(line.confidence) >= 0.95
                )
                for row in line.text.splitlines()
                if editor_single_glyph_transport_deletion(
                    intended_snapshot,
                    row.strip(),
                )
                is not None
            }
            return candidates.pop() if len(candidates) == 1 else ""

        async def read_editor_status_proof(
            predicate: Callable[[OCRResult, Region], bool],
        ) -> tuple[bool, Region | None]:
            assert cur_region is not None
            ordinary_region = editor_status_search_region(cur_region, dims)
            if ordinary_region is None:
                return False, None
            maximized_region = maximized_editor_status_search_region(
                cur_region,
                dims,
            )
            if (
                maximized_region is not None
                and maximized_region != ordinary_region
            ):
                # The top-screen editor shape should take its lossless
                # bottom-edge proof before paying for restored-window bands.
                # If this was only a top-aligned restored window, the missing
                # bottom status fails closed and ordinary geometry still runs.
                status_regions = [maximized_region, ordinary_region]
            else:
                status_regions = [ordinary_region]
            for status_region in status_regions:
                bounded_status = await self._read_screen(
                    precise=True,
                    region=status_region,
                )
                proved = predicate(bounded_status, status_region)
                native_attempted = False
                if (
                    not proved
                    and maximized_region is not None
                    and status_region == maximized_region
                ):
                    native_status = await self._read_native_screen(
                        status_region
                    )
                    native_attempted = True
                    proved = predicate(native_status, status_region)
                if not proved:
                    for compact_region in editor_status_search_subregions(
                        status_region,
                        dims,
                    ):
                        compact_status = await self._read_screen(
                            precise=True,
                            region=compact_region,
                        )
                        if predicate(compact_status, compact_region):
                            return True, compact_region
                if not proved and not native_attempted:
                    native_status = await self._read_native_screen(
                        status_region
                    )
                    proved = predicate(native_status, status_region)
                if not proved:
                    bounded_status = await self._read_precise_screen_fallback(
                        status_region,
                    )
                    proved = predicate(bounded_status, status_region)
                if proved:
                    return True, status_region
            return False, status_regions[-1]

        async def prove_editor_whitespace(
            read_back: str,
            intended_snapshot: str,
            *,
            phase: str,
        ) -> str:
            # The causal row proves every painted glyph, while Notepad's
            # nearest foreground status row proves the otherwise invisible
            # leading positions. The delivery receipt already proves that the
            # exact spaces were issued once. Keep this editor-only and
            # fail-closed when the status row is absent, distant, low
            # confidence, or reports a conflicting column.
            leading_spaces = len(intended_snapshot) - len(
                intended_snapshot.lstrip(" ")
            )
            visible_intended = intended_snapshot[leading_spaces:]
            leading_whitespace_candidate = bool(
                precise
                and editor_field
                and not explicit_region
                and cur_region is not None
                and leading_spaces > 0
                and read_back
                and not read_back.startswith(" ")
                and norm(strip_prompt(read_back), True)
                == norm(visible_intended, True)
            )
            exact_visible_rows = [
                line
                for line in self._last_field_ocr_result.lines
                if (
                    line.text == intended_snapshot
                    and (
                        line.confidence is None
                        or float(line.confidence)
                        >= MIN_GROUNDED_EXACT_OCR_CONFIDENCE
                    )
                )
            ]
            single_line_replacement_candidate = bool(
                precise
                and editor_field
                and not explicit_region
                and cur_region is not None
                and leading_spaces == 0
                and read_back != intended_snapshot
                and any(
                    character.isspace()
                    for character in intended_snapshot
                )
                and "\n" not in intended_snapshot
                and "\r" not in intended_snapshot
                and len(exact_visible_rows) == 1
            )
            if not (
                leading_whitespace_candidate
                or single_line_replacement_candidate
            ):
                return read_back

            def status_predicate(
                bounded_status: OCRResult,
                status_region: Region,
            ) -> bool:
                if leading_whitespace_candidate:
                    return editor_caret_column_proves_leading_whitespace(
                        bounded_status,
                        intended_snapshot,
                        cur_region,
                        dims,
                        container_region=status_region,
                    )
                return editor_status_proves_single_line_payload(
                    bounded_status,
                    intended_snapshot,
                    cur_region,
                    dims,
                    container_region=status_region,
                ) or editor_status_proves_visible_multiline_payload(
                    bounded_status,
                    intended_snapshot,
                    cur_region,
                    dims,
                    container_region=status_region,
                )

            status_proved, status_region = await read_editor_status_proof(
                status_predicate,
            )
            DEBUG.event(
                "typing.editor_whitespace_status",
                proved=status_proved,
                phase=phase,
                leading_spaces=leading_spaces,
                expected_column=len(intended_snapshot) + 1,
                expected_characters=len(intended_snapshot),
                proof=(
                    "leading_column"
                    if leading_whitespace_candidate
                    else "visible_line_invariant"
                ),
                region=cur_region.model_dump(),
                status_region=(
                    status_region.model_dump()
                    if status_region is not None
                    else None
                ),
            )
            if status_proved:
                return intended_snapshot
            return read_back

        stable_field_read_performed = False

        async def read_field_with_editor_context(
            region: Region,
            intended_snapshot: str,
            *,
            allow_blind_fallback: bool,
        ) -> str:
            leading_spaces = len(intended_snapshot) - len(
                intended_snapshot.lstrip(" ")
            )
            options: dict[str, Any] = {
                "intended": intended_snapshot,
                "precise": precise,
                "allow_semantic_spacing": allow_semantic_spacing,
                "allow_blind_fallback": allow_blind_fallback,
            }
            if (
                precise
                and (editor_field or single_line_field)
                and not explicit_region
            ):
                options["extract_structured_exact_row"] = True
                options["allow_native_primary_fallback"] = True
            if precise and single_line_field and not explicit_region:
                # Tiny Windows fields can contain correctly painted text while
                # the logical-resolution OCR crop is empty. Permit one
                # lossless, read-only crop only after the caller has grounded
                # this as a field. The exact intended row must still be unique;
                # this never retries delivery or authorizes a following key.
                options["allow_empty_native_primary_fallback"] = True
            if (
                precise
                and editor_field
                and not explicit_region
                and leading_spaces > 0
            ):
                # Preserve the painted glyph row long enough for the separate
                # Notepad Ln/Col proof below to attest invisible indentation
                # for every indented line, including the common four-space
                # Python level. This candidate never verifies exact text on
                # its own; the independent status proof remains mandatory.
                options["preserve_editor_indent_candidate"] = True
            return await self._read_field(region, **options)

        async def read_current_field(intended_snapshot: str) -> str:
            """Read a complete exact field once without trusting its caret.

            A focused Windows field can render ``calc|`` as ``cald``. Short
            fields without spaces can move focus for an independent read, but
            long drafts and fields with spaces move their caret to the start
            because a Windows address bar may discard unsubmitted text on
            focus loss and selected text is materially harder to OCR. For an
            indented editor append, Home moves only to the current line start;
            the already-localized row remains grounded while its final glyph
            becomes unobscured. End restores the append position afterwards.
            """

            nonlocal stable_field_read_performed
            assert cur_region is not None
            editor_append = bool(
                editor_field
                and text
                and text[0].isspace()
            )
            editor_autocorrect_candidate = bool(
                editor_append
                and _STANDALONE_LOWERCASE_I_RE.search(intended_snapshot)
            )
            should_stabilize = (
                precise
                and not stable_field_read_performed
                and intended_snapshot == text
                and (
                    (editor_field and not editor_autocorrect_candidate)
                    or (
                        single_line_field
                        and (
                            (
                                not explicit_region
                                and (
                                    PRECISE_LOCATE_MIN_CHARS
                                    <= total
                                    <= 20
                                    or causal_short_field_localized
                                )
                            )
                            or total > 20
                        )
                    )
                )
            )
            should_reposition_caret = bool(
                should_stabilize
                and (
                    editor_field
                    or total > 20
                    # A native Save As/Open filename field is immediately
                    # followed by the file-type control. Blurring a short
                    # basename with Tab therefore moves the independent OCR
                    # read onto e.g. ``Text documents (*.txt)`` and can make
                    # correctly delivered input look like a wrong-region
                    # failure. Keep focus in the filename field and move only
                    # its caret; the subsequent OCR still has to match every
                    # requested character before any commit is allowed.
                    or _SAFE_FILENAME.fullmatch(intended_snapshot) is not None
                    or any(
                        character.isspace()
                        for character in intended_snapshot
                    )
                )
            )
            should_blur = should_stabilize and not should_reposition_caret
            if not should_blur and not should_reposition_caret:
                return await read_field_with_editor_context(
                    current_readback_region(intended_snapshot),
                    intended_snapshot,
                    allow_blind_fallback=(
                        intended_snapshot == text
                        and not defer_blind_editor_readback
                    ),
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
                    method=(
                        "editor_line_home"
                        if editor_append
                        else "editor_ctrl_home"
                        if editor_field
                        else "caret_home"
                    ),
                    character_count=len(intended_snapshot),
                    stage="started",
                )
                if editor_append:
                    await self.backend.press_key("Home")
                elif editor_field:
                    await self.backend.keypress(["ControlLeft", "Home"])
                else:
                    await self.backend.press_key("Home")
                await asyncio.sleep(_CLEAR_SETTLE_S)
                repositioned_region = current_readback_region(
                    intended_snapshot
                )
                try:
                    repositioned_read = await read_field_with_editor_context(
                        repositioned_region,
                        intended_snapshot,
                        allow_blind_fallback=(
                            not defer_blind_editor_readback
                        ),
                    )
                finally:
                    if editor_append:
                        with contextlib.suppress(Exception):
                            await self.backend.press_key("End")
                        await asyncio.sleep(_CLEAR_SETTLE_S)
                    elif editor_field:
                        with contextlib.suppress(Exception):
                            await self.backend.keypress(
                                ["ControlLeft", "End"]
                            )
                        await asyncio.sleep(_CLEAR_SETTLE_S)
                selected_confidences = [
                    float(line.confidence)
                    for line in self._last_field_ocr_result.lines
                    if line.confidence is not None
                ]
                DEBUG.event(
                    "typing.caret_stabilizer",
                    method=(
                        "editor_line_home"
                        if editor_append
                        else "editor_ctrl_home"
                        if editor_field
                        else "caret_home"
                    ),
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
                blurred_read = await read_field_with_editor_context(
                    blurred_region,
                    intended_snapshot,
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
            nonlocal local_replacement_failure
            nonlocal corrections, last_read, verified_clean
            read_back = self._typed_candidate(read_back, intended_snapshot, precise)
            editor_deletion_candidate = ""
            if precise and editor_field:
                read_back = (
                    unique_editor_autocorrect_candidate(
                        intended_snapshot,
                        read_back,
                    )
                    or read_back
                )
                editor_deletion_candidate = (
                    unique_editor_single_deletion_candidate(
                        intended_snapshot,
                    )
                )
                if read_back != intended_snapshot:
                    # OCR cannot paint indentation. Resolve it from the
                    # foreground editor's end column before considering any
                    # local mutation. An exact column proves the original
                    # delivery; a one-short column remains eligible for the
                    # separately re-read missing-glyph repair below.
                    read_back = await prove_editor_whitespace(
                        read_back,
                        intended_snapshot,
                        phase="before_local_correction",
                    )
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
            if (
                precise
                and editor_field
                and is_standalone_i_autocorrect(
                    intended_snapshot,
                    read_back,
                )
            ):
                # Modern Notepad can autocorrect a freshly typed Python
                # variable from ``i`` to the English pronoun ``I``. Confirm
                # the same narrow mutation on a second settled frame before
                # replacing only that character. Ctrl+Z is unsafe here: in the
                # live editor it removed the entire HID chunk rather than the
                # autocorrection. Never clear or replay the draft, and never
                # reinterpret this as a Caps Lock fault.
                await asyncio.sleep(_CARET_BLINK_RECHECK_S)
                repeated_read = self._typed_candidate(
                    await self._read_field(
                        current_readback_region(intended_snapshot),
                        intended=intended_snapshot,
                        precise=True,
                        allow_blind_fallback=True,
                    ),
                    intended_snapshot,
                    True,
                )
                repeated = (
                    unique_editor_autocorrect_candidate(
                        intended_snapshot,
                        repeated_read,
                    )
                    or repeated_read
                )
                navigation = standalone_i_autocorrect_navigation(
                    intended_snapshot,
                    repeated,
                )
                if navigation is None:
                    last_read = repeated or read_back
                    return
                anchor_key, cursor_key, cursor_steps = navigation
                DEBUG.event(
                    "typing.editor_autocorrect_detected",
                    character_index=next(
                        index
                        for index, (expected, actual) in enumerate(
                            zip(
                                norm(intended_snapshot, True),
                                norm(strip_prompt(repeated), True),
                                strict=True,
                            )
                        )
                        if expected != actual
                    ),
                    intended_characters=len(intended_snapshot),
                )
                corrections += 1
                await self.backend.press_key(anchor_key)
                for _ in range(cursor_steps):
                    await self.backend.press_key(cursor_key)
                    await asyncio.sleep(_CURSOR_REPEAT_SETTLE_S)
                await self.backend.press_key("Backspace")
                # Insert a word-character guard first, then place the lowercase
                # letter before it. Notepad otherwise re-capitalizes an ``i``
                # inserted directly between the already-present spaces. Once
                # the guard is removed with Backspace, no delimiter key is
                # emitted and the intended lowercase character remains.
                await emit_local_repair("_")
                await self.backend.press_key("ArrowLeft")
                await emit_local_repair("i")
                await self.backend.press_key("ArrowRight")
                await self.backend.press_key("Backspace")
                await self.backend.press_key("End")
                # Read the corrected row with the caret away from its final
                # glyph. At Notepad's small acceptance-test font, a caret over
                # ``:`` can look exactly like ``;`` to both local and model
                # OCR. Restore End before returning control to the caller.
                await self.backend.press_key("Home")
                await asyncio.sleep(_CLEAR_SETTLE_S)
                try:
                    corrected_read = self._typed_candidate(
                        await self._read_field(
                            current_readback_region(intended_snapshot),
                            intended=intended_snapshot,
                            precise=True,
                            allow_blind_fallback=True,
                        ),
                        intended_snapshot,
                        True,
                    )
                finally:
                    await self.backend.press_key("End")
                    await asyncio.sleep(_CLEAR_SETTLE_S)
                corrected_read = unique_editor_exact_candidate_after_correction(
                    intended_snapshot,
                    corrected_read,
                )
                corrected_read = await prove_editor_whitespace(
                    corrected_read,
                    intended_snapshot,
                    phase="autocorrect_replacement",
                )
                last_read = corrected_read
                restored = (
                    compute_verdict(
                        intended_snapshot,
                        corrected_read,
                        True,
                    )
                    == "match"
                )
                DEBUG.event(
                    "typing.editor_autocorrect_replacement",
                    restored=restored,
                    intended_characters=len(intended_snapshot),
                    anchor_key=anchor_key,
                    cursor_key=cursor_key,
                    cursor_steps=cursor_steps,
                )
                if restored:
                    if norm(intended_snapshot, True) == norm(text, True):
                        verified_clean = True
                    return
                chained_punctuation_candidate = (
                    unique_editor_punctuation_substitution_candidate(
                        intended_snapshot,
                    )
                )
                if (
                    corrections < MAX_LOCAL_EDITOR_REPAIRS
                    and editor_punctuation_transport_substitution(
                        intended_snapshot,
                        chained_punctuation_candidate,
                    )
                    is not None
                ):
                    # The first local edit is complete, but its exact re-read
                    # independently exposes one punctuation transport slip.
                    # Fall through to the normal two-read punctuation repair;
                    # this composes two bounded glyph edits without replaying
                    # any already-present text.
                    DEBUG.event(
                        "typing.editor_local_repair_chained",
                        completed="standalone_i",
                        pending="punctuation_substitution",
                        correction_count=corrections,
                    )
                    read_back = corrected_read
                    last_read = corrected_read
                else:
                    local_replacement_failure = (
                        "failed_case_mismatch",
                        AUTOCORRECT_REPLACEMENT_FAILED_SUMMARY,
                    )
                    return
            punctuation_candidate = (
                unique_editor_punctuation_substitution_candidate(
                    intended_snapshot,
                )
                if precise and editor_field
                else ""
            )
            punctuation_substitution = (
                editor_punctuation_transport_substitution(
                    intended_snapshot,
                    punctuation_candidate,
                )
                if punctuation_candidate
                else None
            )
            if punctuation_substitution is not None:
                # A shifted punctuation key can occasionally arrive without
                # its modifier over a remote desktop transport (for example,
                # ``)`` becomes ``0``). Require the identical single-glyph
                # mismatch on a second settled, high-confidence crop, then
                # replace only that character from End. Existing indentation
                # is intentionally absent from the navigation calculation.
                await asyncio.sleep(_CARET_BLINK_RECHECK_S)
                await self._read_field(
                    current_readback_region(intended_snapshot),
                    intended=intended_snapshot,
                    precise=True,
                    allow_blind_fallback=True,
                )
                repeated = (
                    unique_editor_punctuation_substitution_candidate(
                        intended_snapshot,
                    )
                )
                repeated_substitution = (
                    editor_punctuation_transport_substitution(
                        intended_snapshot,
                        repeated,
                    )
                    if repeated
                    else None
                )
                if (
                    repeated_substitution != punctuation_substitution
                    or repeated != punctuation_candidate
                ):
                    last_read = repeated or read_back
                    return
                if corrections >= MAX_LOCAL_EDITOR_REPAIRS:
                    last_read = repeated
                    return
                suffix_length, expected_character, observed_character = (
                    punctuation_substitution
                )
                DEBUG.event(
                    "typing.editor_punctuation_substitution_detected",
                    intended_characters=len(intended_snapshot),
                    suffix_length=suffix_length,
                    expected_character=expected_character,
                    observed_character=observed_character,
                )
                corrections += 1
                await self.backend.press_key("End")
                for _ in range(suffix_length):
                    await self.backend.press_key("ArrowLeft")
                    await asyncio.sleep(_CURSOR_REPEAT_SETTLE_S)
                await self.backend.press_key("Backspace")
                await emit_local_repair(expected_character)
                await self.backend.press_key("End")
                await asyncio.sleep(_CLEAR_SETTLE_S)
                corrected_read = self._typed_candidate(
                    await self._read_field(
                        current_readback_region(intended_snapshot),
                        intended=intended_snapshot,
                        precise=True,
                        allow_blind_fallback=True,
                    ),
                    intended_snapshot,
                    True,
                )
                corrected_read = unique_editor_exact_candidate_after_correction(
                    intended_snapshot,
                    corrected_read,
                )
                corrected_read = await prove_editor_whitespace(
                    corrected_read,
                    intended_snapshot,
                    phase="punctuation_replacement",
                )
                last_read = corrected_read
                restored = (
                    compute_verdict(
                        intended_snapshot,
                        corrected_read,
                        True,
                    )
                    == "match"
                )
                DEBUG.event(
                    "typing.editor_punctuation_substitution_replacement",
                    restored=restored,
                    intended_characters=len(intended_snapshot),
                    suffix_length=suffix_length,
                )
                if restored:
                    if norm(intended_snapshot, True) == norm(text, True):
                        verified_clean = True
                    return
                local_replacement_failure = (
                    "failed_symbol_mismatch",
                    PUNCTUATION_REPLACEMENT_FAILED_SUMMARY,
                )
                return
            deletion_candidate = (
                editor_deletion_candidate
                if (
                    precise
                    and editor_field
                    and read_back != intended_snapshot
                )
                else ""
            )
            deletion = (
                editor_single_glyph_transport_deletion(
                    intended_snapshot,
                    deletion_candidate,
                )
                if deletion_candidate
                else None
            )
            if deletion is not None:
                # OCR can omit a narrow glyph, so an edit-distance-one row is
                # not enough to mutate the editor. Require the same unique
                # deletion on a second settled high-confidence crop and an
                # independent Notepad caret column that proves the line is one
                # character short. Insert only that glyph from End; never
                # clear or replay the line.
                await asyncio.sleep(_CARET_BLINK_RECHECK_S)
                await self._read_field(
                    current_readback_region(intended_snapshot),
                    intended=intended_snapshot,
                    precise=True,
                    allow_blind_fallback=True,
                )
                repeated = unique_editor_single_deletion_candidate(
                    intended_snapshot,
                )
                repeated_deletion = (
                    editor_single_glyph_transport_deletion(
                        intended_snapshot,
                        repeated,
                    )
                    if repeated
                    else None
                )
                if repeated != deletion_candidate or repeated_deletion != deletion:
                    last_read = repeated or read_back
                    return
                if corrections >= MAX_LOCAL_EDITOR_REPAIRS:
                    last_read = repeated
                    return

                def missing_glyph_status_predicate(
                    bounded_status: OCRResult,
                    status_region: Region,
                ) -> bool:
                    return editor_caret_column_proves_one_missing_glyph(
                        bounded_status,
                        intended_snapshot,
                        cur_region,
                        dims,
                        container_region=status_region,
                    )

                status_proved, status_region = await read_editor_status_proof(
                    missing_glyph_status_predicate,
                )
                if not status_proved:
                    last_read = repeated
                    return
                suffix_length, missing_character = deletion
                DEBUG.event(
                    "typing.editor_missing_glyph_detected",
                    intended_characters=len(intended_snapshot),
                    observed_characters=len(repeated),
                    suffix_length=suffix_length,
                    missing_character=missing_character,
                    status_region=(
                        status_region.model_dump()
                        if status_region is not None
                        else None
                    ),
                )
                corrections += 1
                await self.backend.press_key("End")
                for _ in range(suffix_length):
                    await self.backend.press_key("ArrowLeft")
                    await asyncio.sleep(_CURSOR_REPEAT_SETTLE_S)
                await emit_local_repair(missing_character)
                # Move the caret away from the final glyph for the exact OCR,
                # then restore End before returning control to the caller.
                await self.backend.press_key("Home")
                await asyncio.sleep(_CLEAR_SETTLE_S)
                try:
                    corrected_read = self._typed_candidate(
                        await self._read_field(
                            current_readback_region(intended_snapshot),
                            intended=intended_snapshot,
                            precise=True,
                            allow_blind_fallback=True,
                        ),
                        intended_snapshot,
                        True,
                    )
                finally:
                    await self.backend.press_key("End")
                    await asyncio.sleep(_CLEAR_SETTLE_S)
                corrected_read = unique_editor_exact_candidate_after_correction(
                    intended_snapshot,
                    corrected_read,
                )
                corrected_read = await prove_editor_whitespace(
                    corrected_read,
                    intended_snapshot,
                    phase="missing_glyph_insertion",
                )
                last_read = corrected_read
                restored = (
                    compute_verdict(
                        intended_snapshot,
                        corrected_read,
                        True,
                    )
                    == "match"
                )
                DEBUG.event(
                    "typing.editor_missing_glyph_insertion",
                    restored=restored,
                    intended_characters=len(intended_snapshot),
                    suffix_length=suffix_length,
                )
                if restored:
                    if norm(intended_snapshot, True) == norm(text, True):
                        verified_clean = True
                    return
                local_replacement_failure = (
                    "failed_symbol_mismatch",
                    MISSING_GLYPH_REPLACEMENT_FAILED_SUMMARY,
                )
                return
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
            if (
                precise
                and editor_field
                and intended_snapshot[:1].isspace()
            ):
                # An indented editor payload begins after a pre-existing line
                # boundary. If even one requested character failed to land,
                # clearing ``len(requested)`` positions would consume that
                # newline and merge the draft into the previous code line.
                # The narrow standalone-i repair above edits one grounded
                # glyph in place; every whole-payload replay must fail closed.
                DEBUG.event(
                    "typing.editor_append_replay_refused",
                    mismatch_kind=kind,
                    intended_characters=len(intended_snapshot),
                    observed_characters=len(read_back),
                )
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
            if (
                kind == "case"
                and not is_caps_lock_case_inversion(
                    intended_snapshot,
                    read_back,
                )
            ):
                # A single or partial case difference is app/OCR ambiguity,
                # not evidence that the guest's global Caps Lock state is
                # inverted. Preserve the at-most-once draft and fail closed.
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

        def failed_local_replacement_result(
            typed_characters: int,
        ) -> WatchedTypingResult:
            assert local_replacement_failure is not None
            status, summary = local_replacement_failure
            return self._halted_result(
                status=status,
                field_text=last_read,
                corrected=True,
                correction_count=corrections,
                delivery_retries=delivery_retries,
                used_fast_path=fast_print,
                typed_characters=typed_characters,
                intended_characters=len(text),
                summary=summary,
                intended_text=text,
                emitted_text="".join(emitted_parts),
                readback_frame_sha256=self._last_readback_frame_sha256,
            )

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
            if dense_prev is not None and dense_now is not None:
                exact_change = await exact_dense_candidate(
                    dense_prev,
                    dense_now,
                    typed_so_far,
                    chunk_change,
                )
                if exact_change is not None:
                    chunk_change = exact_change
                elif bounded_editor_code:
                    dense_candidates = await asyncio.to_thread(
                        locate_dense_changed_candidates,
                        dense_prev.data,
                        dense_now.data,
                        dims,
                        allow_compact=structural_code_glyph,
                    )
                    content_candidate = (
                        editor_row_candidate_above_disjoint_effect(
                            chunk_change,
                            dense_candidates,
                            dims,
                        )
                    )
                    if content_candidate is not None:
                        # Keep the subsequent OCR causally bounded to this
                        # changed row. The generic horizontal field margin can
                        # otherwise pull adjacent editor lines into an
                        # unverified short-code read.
                        self._refined_readback_region = content_candidate
                        self._refined_readback_intended = typed_so_far
                        DEBUG.event(
                            "typing.editor_row_recovered_above_effect",
                            intended_characters=len(typed_so_far),
                            coarse_region=(
                                chunk_change.model_dump()
                                if chunk_change is not None
                                else None
                            ),
                            region=content_candidate.model_dump(),
                        )
                        chunk_change = content_candidate
                    elif structural_code_glyph:
                        # A short punctuation-only append also repaints
                        # Notepad's Ln/Col status. Recover only a unique causal
                        # editor row paired with that wider lower effect. This
                        # is geometry, not verification: the caret-stabilized
                        # read and status invariant still have to prove it.
                        structural_row = (
                            structural_editor_row_above_status_effect(
                                dense_candidates,
                                dims,
                            )
                        )
                        if structural_row is not None:
                            structural_readback = (
                                structural_editor_readback_band(
                                    dense_prev.data,
                                    dense_now.data,
                                    structural_row,
                                    dims,
                                )
                                or structural_row
                            )
                            self._refined_readback_region = structural_readback
                            self._refined_readback_intended = typed_so_far
                            chunk_change = structural_readback
                            DEBUG.event(
                                "typing.structural_editor_row_localized",
                                intended_characters=len(typed_so_far),
                                raw_region=structural_row.model_dump(),
                                region=structural_readback.model_dump(),
                            )
                        else:
                            chunk_change = None
                elif chunk_change is None:
                    chunk_change = await asyncio.to_thread(
                        locate_dense_changed_bbox,
                        dense_prev.data,
                        dense_now.data,
                        dims,
                        allow_ambiguous=structural_code_glyph,
                        allow_compact=structural_code_glyph,
                    )

            # Keyboard input is not idempotent. A stale frame cannot distinguish
            # "nothing landed" from "the boundary space landed but the glyphs
            # have not painted yet", so replaying this chunk can duplicate text.
            # Keep the original emission at-most-once and let the settled
            # read-back below stop the transaction as unverified if it is short.

            # Auto-locate the field from the changed pixels (skipped if the caller
            # gave an explicit region); grow the box each chunk so it spans the line.
            locate_min_chars = (
                1
                if structural_code_glyph
                else 3
                if causal_short_field_localized
                else PRECISE_LOCATE_MIN_CHARS
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
                            editor_field=editor_field,
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
                    if (
                        editor_field
                        and located
                        and cur_region is not None
                        and is_disjoint_editor_effect(cur_region, loc)
                    ):
                        # Notepad repaints its Ln/Col status after the final
                        # characters. That is useful status evidence, but it
                        # must not turn a 30-pixel text row into a 400-pixel
                        # field and push the later status search off-screen.
                        DEBUG.event(
                            "typing.editor_effect_ignored",
                            typed_characters=len(typed_so_far),
                            current_region=cur_region.model_dump(),
                            candidate_region=loc.model_dump(),
                        )
                    else:
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
                        _FINAL_VIDEO_RETRY_SETTLE_S,
                    ):
                        await asyncio.sleep(settle_s)
                        grid_retry = await self._grid()
                        dense_retry = self._last_grid_frame
                        retry_loc = (
                            locate_changed_bbox(grid_prev, grid_retry, dims)
                            if grid_prev is not None and grid_retry is not None
                            else None
                        )
                        if dense_prev is not None and dense_retry is not None:
                            exact_retry_loc = await exact_dense_candidate(
                                dense_prev,
                                dense_retry,
                                typed_so_far,
                                retry_loc,
                            )
                            if exact_retry_loc is not None:
                                retry_loc = exact_retry_loc
                            elif bounded_editor_code and not structural_code_glyph:
                                retry_candidates = await asyncio.to_thread(
                                    locate_dense_changed_candidates,
                                    dense_prev.data,
                                    dense_retry.data,
                                    dims,
                                )
                                recovered_retry = (
                                    editor_row_candidate_above_disjoint_effect(
                                        retry_loc,
                                        retry_candidates,
                                        dims,
                                    )
                                )
                                if recovered_retry is not None:
                                    self._refined_readback_region = (
                                        recovered_retry
                                    )
                                    self._refined_readback_intended = (
                                        typed_so_far
                                    )
                                    retry_loc = recovered_retry
                            elif structural_code_glyph:
                                retry_candidates = await asyncio.to_thread(
                                    locate_dense_changed_candidates,
                                    dense_prev.data,
                                    dense_retry.data,
                                    dims,
                                    allow_compact=True,
                                )
                                structural_row = (
                                    structural_editor_row_above_status_effect(
                                        retry_candidates,
                                        dims,
                                    )
                                )
                                if structural_row is not None:
                                    structural_readback = (
                                        structural_editor_readback_band(
                                            dense_prev.data,
                                            dense_retry.data,
                                            structural_row,
                                            dims,
                                        )
                                        or structural_row
                                    )
                                    self._refined_readback_region = (
                                        structural_readback
                                    )
                                    self._refined_readback_intended = (
                                        typed_so_far
                                    )
                                    retry_loc = structural_readback
                                    DEBUG.event(
                                        "typing.structural_editor_row_localized",
                                        intended_characters=len(typed_so_far),
                                        raw_region=structural_row.model_dump(),
                                        region=structural_readback.model_dump(),
                                    )
                                else:
                                    retry_loc = None
                            elif retry_loc is None:
                                retry_loc = await asyncio.to_thread(
                                    locate_dense_changed_bbox,
                                    dense_prev.data,
                                    dense_retry.data,
                                    dims,
                                    allow_ambiguous=structural_code_glyph,
                                    allow_compact=structural_code_glyph,
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
                        screen = await self._read_screen(precise=precise)
                        # A one-character structural needle is not a safe
                        # full-screen locator: searching for ``)`` can select
                        # an earlier ``param(`` row. Keep sampling its causal
                        # compact delta instead; fail closed if that crop never
                        # becomes independently readable.
                        ocr_loc = (
                            None
                            if structural_code_glyph
                            else self._locate_ocr_candidate(
                                screen,
                                typed_so_far,
                                dims,
                                precise=precise,
                                editor_field=editor_field,
                            )
                        )
                        recovered_read = ""
                        if (
                            ocr_loc is None
                            and precise
                            and not structural_code_glyph
                        ):
                            recovered = (
                                await self._recover_precise_ocr_candidate(
                                    screen,
                                    typed_so_far,
                                    dims,
                                    editor_field=editor_field,
                                )
                            )
                            if recovered is not None:
                                ocr_loc, recovered_read = recovered
                        if ocr_loc is not None:
                            cur_region = ocr_loc
                            located = True
                            if recovered_read:
                                last_read = await prove_editor_whitespace(
                                    recovered_read,
                                    typed_so_far,
                                    phase="precise_localization",
                                )
                                if (
                                    typed_so_far == text
                                    and compute_verdict(
                                        typed_so_far,
                                        last_read,
                                        precise,
                                    )
                                    == "match"
                                ):
                                    # The expensive fallback already proved
                                    # every painted glyph and Notepad's local
                                    # status row independently proved the
                                    # invisible indentation. Do not repeat the
                                    # same blind OCR during caret stabilization.
                                    verified_clean = True
                            break

                        partial = await causal_partial_editor_candidate(
                            dense_prev,
                            dense_retry,
                            typed_so_far,
                        )
                        if partial is not None:
                            causal_partial_delivery, cur_region = partial
                            last_read = causal_partial_delivery
                            self._refined_readback_region = cur_region
                            self._refined_readback_intended = typed_so_far
                            located = True
                            grid_now = grid_retry
                            dense_now = dense_retry
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

            if (
                not verified_clean
                and cadence(i)
                and cur_region is not None
            ):
                rb = await read_current_field(typed_so_far)
                await maybe_correct(rb, typed_so_far)
                if local_replacement_failure is not None:
                    await self._release_all_quietly()
                    return failed_local_replacement_result(
                        len(typed_so_far)
                    )
                if corrections > 0:
                    grid_prev = await self._grid()  # field changed under us
                    dense_prev = self._last_grid_frame

        # Final correctness check if we never got a clean read mid-stream.
        if not verified_clean and cur_region is not None and can_vision:
            corrections_before = corrections
            rb = await read_current_field(text)
            await maybe_correct(rb, text)
            if local_replacement_failure is not None:
                await self._release_all_quietly()
                return failed_local_replacement_result(len(text))
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
                spacing_only_settle = bool(
                    self._last_read_exact_unverified_spacing
                )
                precise_settles = (
                    _PRECISE_READBACK_SETTLES_S[:1]
                    if spacing_only_settle
                    else _PRECISE_READBACK_SETTLES_S
                )
                if spacing_only_settle:
                    # The complete glyph row is already exact; only calibrated
                    # spacing remains unresolved. One fresh delayed read can
                    # acquire that proof, but repeating the same exact glyph
                    # observation three times cannot manufacture it. Keep the
                    # later status/full-screen gates unchanged and fail closed
                    # if none independently proves spacing.
                    DEBUG.event(
                        "typing.exact_spacing_settle_budget",
                        intended_characters=len(text),
                        ordinary_reads=len(_PRECISE_READBACK_SETTLES_S),
                        scheduled_reads=len(precise_settles),
                    )
                for settle_index, settle_s in enumerate(precise_settles):
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
                        if dense_prev is not None and settled_frame is not None:
                            exact_late_region = await exact_dense_candidate(
                                dense_prev,
                                settled_frame,
                                text,
                                late_region,
                            )
                            if exact_late_region is not None:
                                late_region = exact_late_region
                            elif late_region is None:
                                late_region = await asyncio.to_thread(
                                    locate_dense_changed_bbox,
                                    dense_prev.data,
                                    settled_frame.data,
                                    dims,
                                )
                        if late_region is not None:
                            cur_region = union_region(cur_region, late_region)
                    if verified_clean:
                        # Exact causal OCR has already retained the terminal
                        # receipt. Do not discard that proof by running the
                        # broader field crop again for this or later settles.
                        # Indented rows cannot set this flag until their
                        # separate status proof succeeds.
                        break
                    settled_read = self._typed_candidate(
                        await read_field_with_editor_context(
                            current_readback_region(),
                            text,
                            allow_blind_fallback=(
                                not defer_blind_editor_readback
                                or settle_index
                                == len(precise_settles) - 1
                            ),
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

        last_read = await prove_editor_whitespace(
            last_read,
            text,
            phase="bounded_readback",
        )

        if (
            precise
            and not explicit_region
            and cur_region is not None
            and (
                compute_verdict(text, last_read, precise) != "match"
                and (
                    compute_verdict(text, last_read, precise) != "contains"
                    or editor_field
                )
            )
        ):
            # A thin changed-pixel crop can miss exact text in editors and
            # terminals even when the full payload is legible. It can also
            # include an adjacent editor row: precise ``contains`` is not a
            # verified receipt, so localize it instead of stopping early.
            # Take a fresh precise full-frame read and accept only a complete
            # line whose bbox overlaps the field changed by this exact
            # emission. This is read-only; any subsequent commit remains a
            # separate action.
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
                causal_change_candidates: list[Region] = []
                if (
                    emission_start_frame is not None
                    and emission_start_frame.data
                    and full_screen_frame is not None
                    and full_screen_frame.data
                ):
                    causal_change_candidates = await asyncio.to_thread(
                        locate_dense_changed_candidates,
                        emission_start_frame.data,
                        full_screen_frame.data,
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
                        or any(
                            regions_overlap(
                                candidate_region,
                                causal_candidate,
                            )
                            for causal_candidate in causal_change_candidates
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

        # A noisy bounded editor crop can recover the exact visible glyph row
        # only in the full-screen pass above. Re-run the independent caret
        # column proof after that recovery; otherwise a correct long indented
        # line is left ambiguous even though both evidence sources are now
        # available. This is still read-only and remains fail-closed.
        last_read = await prove_editor_whitespace(
            last_read,
            text,
            phase="full_screen_recovery",
        )

        needs_bounded_prose_localization = (
            compute_verdict(text, last_read, precise) != "match"
            or (
                precise
                and last_read != text
                and "\n" not in text
                and "\r" not in text
            )
        )
        if (
            (fast_print or (prose and precise))
            and needs_bounded_prose_localization
        ):
            # Rich editors wrap prose beyond the first changed-line crop. Do
            # the bounded suffix search against the current grounded field
            # before paying for another screen OCR pass. This covers an
            # OCR-inserted visual word-wrap newline and an exact append whose
            # field crop also includes the previously proven prefix.
            grounded_candidate = (
                self._bounded_prose_candidate(
                    OCRResult(
                        lines=[
                            OCRLine(
                                text=last_read,
                                confidence=1.0,
                            )
                        ]
                    ),
                    text,
                )
                if precise and last_read
                else ""
            )
            if grounded_candidate == text:
                last_read = grounded_candidate
            else:
                # Precise input is canonicalized only when a bounded candidate
                # recreates every requested delivery byte. Non-precise prose
                # keeps its existing conservative edit-distance verifier.
                screen_candidate = self._bounded_prose_candidate(
                    await self._read_screen(),
                    text,
                )
                if screen_candidate and (
                    not precise or screen_candidate == text
                ):
                    last_read = screen_candidate

        verdict = compute_verdict(text, last_read, precise)
        if self._semantic_spacing_normalized:
            verdict = "match"
        if (
            causal_partial_delivery
            and verdict not in {"match", "contains"}
        ):
            await self._release_all_quietly()
            return self._halted_result(
                status="unverified_truncated",
                field_text=last_read or causal_partial_delivery,
                corrected=corrections > 0 or delivery_retries > 0,
                correction_count=corrections,
                delivery_retries=delivery_retries,
                used_fast_path=fast_print,
                typed_characters=len(text),
                intended_characters=len(text),
                summary=PARTIAL_DELIVERY_SUMMARY,
                intended_text=text,
                emitted_text="".join(emitted_parts),
                readback_frame_sha256=self._last_readback_frame_sha256,
            )
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
        elif (
            precise
            and field_text != intended
            and status == "verified_exact"
        ):
            # ``compute_verdict`` intentionally strips outer whitespace
            # because OCR usually cannot see it. That makes it useful for
            # localization, but it cannot by itself authorize an exact input
            # receipt or an optimistic "verified" summary.
            status = "unverified_whitespace"

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
        elif not status.startswith("verified"):
            summary = f"{head}, but exact read-back is still ambiguous."
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
