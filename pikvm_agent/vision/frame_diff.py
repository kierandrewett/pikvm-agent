"""Perceptual fingerprint + diff — the programmatic world-change signal.

Ported faithfully from the TS implementation (``src/ambient/fingerprint.ts``).
A frame is reduced to a tiny grayscale bitmap (mean of R,G,B per pixel); two
frames are compared by mean-absolute-difference normalized to 0..1. The
thresholds below were tuned against this exact reduction — keep them.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

# Single source of truth for the fingerprint-diff thresholds (0..1).
FP_MOVE = 0.04        # above this, the screen is actively changing
FP_SETTLE = 0.015     # below this, it has settled
FP_MEANINGFUL = 0.05  # a settled frame must differ from baseline by >= this
# Mean delta alone misses large dark-on-dark surfaces such as the Windows
# Search panel. A change covering this much of the tiny fingerprint is also
# meaningful even when its average luminance stays close to the wallpaper.
FP_CELL_DELTA = 0.10
FP_CELL_MEANINGFUL_FRACTION = 0.08
BLANK_VARIANCE = 6.0  # std-dev (0..255) below this ⇒ blank / no-signal

FP_SIZE = 16          # fingerprint is FP_SIZE x FP_SIZE
GRID_COLS = 96        # field-localisation / region-watch grid
GRID_ROWS = 54


def _gray_mean(jpeg: bytes, w: int, h: int) -> np.ndarray:
    """Resize to w x h and reduce to grayscale by the MEAN of R,G,B (matching the
    TS reduction — not PIL's luminosity weights)."""
    img = Image.open(io.BytesIO(jpeg)).convert("RGB").resize((w, h), Image.BILINEAR)
    arr = np.asarray(img, dtype=np.float32)  # (h, w, 3)
    return (arr.mean(axis=2)).astype(np.uint8).reshape(-1)  # row-major (h*w,)


def fingerprint(jpeg: bytes) -> np.ndarray:
    """16x16 grayscale fingerprint, 256-element uint8, row-major."""
    return _gray_mean(jpeg, FP_SIZE, FP_SIZE)


def grid(jpeg: bytes, cols: int = GRID_COLS, rows: int = GRID_ROWS) -> np.ndarray:
    """cols x rows grayscale grid, row-major uint8 (length cols*rows)."""
    return _gray_mean(jpeg, cols, rows)


def fp_diff(a: np.ndarray | None, b: np.ndarray | None) -> float:
    """Mean absolute difference, normalized to 0..1. Returns 1.0 if either is
    missing or lengths mismatch (treat as fully changed)."""
    if a is None or b is None:
        return 1.0
    n = min(len(a), len(b))
    if n == 0:
        return 1.0
    a32 = a[:n].astype(np.int32)
    b32 = b[:n].astype(np.int32)
    return float(np.abs(a32 - b32).sum()) / n / 255.0


def fp_meaningful_change(
    a: np.ndarray | None,
    b: np.ndarray | None,
    *,
    mean_threshold: float = FP_MEANINGFUL,
    cell_delta: float = FP_CELL_DELTA,
    cell_fraction: float = FP_CELL_MEANINGFUL_FRACTION,
) -> bool:
    """Detect either a large average change or a coherent changed surface.

    The second signal prevents a modal, menu, or side panel with a similar
    overall luminance from retaining a stale world version. Tiny cursor and
    stream artefacts remain below the changed-cell fraction.
    """

    if a is None or b is None:
        return True
    n = min(len(a), len(b))
    if n == 0:
        return True
    delta = np.abs(
        a[:n].astype(np.int32) - b[:n].astype(np.int32)
    ) / 255.0
    return bool(
        float(delta.mean()) > mean_threshold
        or float((delta > cell_delta).mean()) > cell_fraction
    )


def fp_variance(a: np.ndarray) -> float:
    """Population std-dev of the fingerprint bytes. Near-zero ⇒ blank screen."""
    return float(np.asarray(a, dtype=np.float32).std())


def is_blank(a: np.ndarray) -> bool:
    return fp_variance(a) < BLANK_VARIANCE


def screen_hash(fp: np.ndarray) -> str:
    """A stable hex digest of a fingerprint, for the frame record."""
    return fp.astype(np.uint8).tobytes().hex()


def screen_hashes_match_surface(left: str, right: str) -> bool:
    """Compare serialized fingerprints with the normal world-change policy."""

    try:
        left_fp = np.frombuffer(bytes.fromhex(left), dtype=np.uint8)
        right_fp = np.frombuffer(bytes.fromhex(right), dtype=np.uint8)
    except ValueError:
        return False
    return bool(
        len(left_fp) == FP_SIZE * FP_SIZE
        and len(right_fp) == FP_SIZE * FP_SIZE
        and not fp_meaningful_change(left_fp, right_fp)
    )
