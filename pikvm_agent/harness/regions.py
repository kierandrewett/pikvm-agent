"""Image-only grounding helpers for the benchmark observer."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image


def _runs(values: np.ndarray) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    previous: int | None = None
    for raw in values:
        value = int(raw)
        if start is None or previous is None or value != previous + 1:
            if start is not None and previous is not None:
                runs.append((start, previous))
            start = value
        previous = value
    if start is not None and previous is not None:
        runs.append((start, previous))
    return runs


def detect_observer_editor(jpeg: bytes) -> tuple[int, int, int, int] | None:
    """Find the observer's large white multiline edit control from pixels."""
    rgb = np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB"))
    height, width = rgb.shape[:2]
    bright = (rgb.min(axis=2) >= 245) & (
        (rgb.max(axis=2) - rgb.min(axis=2)) <= 12
    )

    row_candidates = np.flatnonzero(bright.sum(axis=1) >= width * 0.42)
    row_runs = [
        run for run in _runs(row_candidates) if run[1] - run[0] + 1 >= height * 0.12
    ]
    if not row_runs:
        return None
    y0, y1 = max(
        row_runs,
        key=lambda run: (
            run[1] - run[0] + 1,
            float(bright[run[0] : run[1] + 1].sum()),
        ),
    )

    column_candidates = np.flatnonzero(
        bright[y0 : y1 + 1].mean(axis=0) >= 0.82
    )
    column_runs = [
        run
        for run in _runs(column_candidates)
        if run[1] - run[0] + 1 >= width * 0.30
    ]
    if not column_runs:
        return None
    x0, x1 = max(column_runs, key=lambda run: run[1] - run[0])

    # Text fragments the first bright rows. Recover the edit control's top
    # border by looking upward for its long, low-variance grey border.
    gray = rgb.mean(axis=2)
    search_start = max(0, y0 - round(height * 0.15))
    for y in range(y0 - 1, search_start - 1, -1):
        line = gray[y, x0 : x1 + 1]
        if 80 <= float(line.mean()) <= 210 and float(line.std()) < 35:
            y0 = y + 1
            break

    x0 = max(0, x0 - 2)
    x1 = min(width - 1, x1 + 12)
    y1 = min(height - 1, y1 + 1)
    detected_width = x1 - x0 + 1
    detected_height = y1 - y0 + 1
    if detected_width < width * 0.30 or detected_height < height * 0.12:
        return None
    return (x0, y0, detected_width, detected_height)
