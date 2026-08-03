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
    editor_caret_column_proves_leading_whitespace,
    editor_row_candidate_above_disjoint_effect,
    editor_punctuation_transport_substitution,
    editor_single_glyph_transport_deletion,
    editor_status_proves_single_line_payload,
    editor_status_proves_visible_multiline_payload,
    editor_status_search_region,
    is_caps_lock_case_inversion,
    is_disjoint_editor_effect,
    is_standalone_i_autocorrect,
    locate_capture_change,
    locate_changed_bbox,
    locate_dense_changed_bbox,
    locate_dense_changed_candidates,
    ocr_line_screen_region,
    precise_readback_candidate_region,
    readback_region,
    regions_overlap,
    standalone_i_autocorrect_navigation,
    standalone_i_autocorrect_suffix_length,
    structural_editor_readback_band,
    structural_editor_row_above_status_effect,
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


def test_editor_field_region_excludes_disjoint_status_bar_repaint() -> None:
    row = Region(x=37, y=103, width=163, height=33)

    assert is_disjoint_editor_effect(
        row,
        Region(x=72, y=473, width=48, height=31),
    )
    assert not is_disjoint_editor_effect(
        row,
        Region(x=196, y=104, width=42, height=29),
    )
    assert not is_disjoint_editor_effect(
        row,
        Region(x=40, y=137, width=180, height=30),
    )


def test_editor_row_candidate_recovers_nearest_row_above_status_effect() -> None:
    status = Region(x=89, y=488, width=87, height=32)
    row = Region(x=53, y=176, width=59, height=28)

    assert editor_row_candidate_above_disjoint_effect(
        Region(x=89, y=492, width=87, height=28),
        [status, row],
        (1280, 800),
    ) == row
    assert editor_row_candidate_above_disjoint_effect(
        row,
        [status, row],
        (1280, 800),
    ) is None


def test_structural_editor_row_recovers_code06_v5_causal_pair() -> None:
    status = Region(x=99, y=511, width=61, height=25)
    row = Region(x=80, y=181, width=28, height=28)
    unrelated_taskbar = Region(x=1240, y=768, width=24, height=24)

    assert structural_editor_row_above_status_effect(
        [status, row, unrelated_taskbar],
        (1280, 800),
    ) == row


def test_structural_editor_row_rejects_ambiguous_causal_rows() -> None:
    status = Region(x=99, y=511, width=61, height=25)

    assert structural_editor_row_above_status_effect(
        [
            status,
            Region(x=80, y=181, width=28, height=28),
            Region(x=78, y=214, width=30, height=25),
        ],
        (1280, 800),
    ) is None


def test_structural_editor_readback_band_selects_lowest_changed_text_row() -> None:
    before = Image.new("RGB", (128, 80), "#202020")
    after = before.copy()
    draw = ImageDraw.Draw(after)
    draw.rectangle((12, 20, 52, 26), fill="#f0f0f0")
    draw.rectangle((10, 36, 32, 43), fill="#f0f0f0")
    draw.point((20, 51), fill="#f0f0f0")
    before_output = io.BytesIO()
    after_output = io.BytesIO()
    before.save(before_output, "PNG")
    after.save(after_output, "PNG")

    assert structural_editor_readback_band(
        before_output.getvalue(),
        after_output.getvalue(),
        Region(x=5, y=15, width=60, height=42),
        (128, 80),
    ) == Region(x=5, y=33, width=60, height=14)


def test_structural_editor_readback_band_rejects_caret_only_noise() -> None:
    before = Image.new("RGB", (128, 80), "#202020")
    after = before.copy()
    draw = ImageDraw.Draw(after)
    draw.line((28, 30, 28, 44), fill="#f0f0f0", width=1)
    before_output = io.BytesIO()
    after_output = io.BytesIO()
    before.save(before_output, "PNG")
    after.save(after_output, "PNG")

    assert (
        structural_editor_readback_band(
            before_output.getvalue(),
            after_output.getvalue(),
            Region(x=20, y=20, width=24, height=36),
            (128, 80),
        )
        is None
    )


async def test_structural_editor_row_enables_caret_stabilized_exact_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intended = "  };"
    backend = FakeBackend(width=1280, height=800)
    backend.guarded_exact_print = True  # type: ignore[attr-defined]
    status = Region(x=99, y=511, width=61, height=25)
    row = Region(x=80, y=181, width=28, height=28)
    taskbar = Region(x=1240, y=768, width=24, height=24)

    async def exact_printing(text: str) -> None:
        assert text == intended
        backend.calls.append(("print_exact_text", {"text": text}))
        backend.set_screen("structural row and status changed")

    class StructuralOCR:
        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is not None and region.y >= 300:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="Ln 6, Col 5",
                            confidence=0.99,
                            bbox=[80, 10, 150, 25],
                        )
                    ]
                )
            caret_moved = any(
                method == "press_key" and kwargs.get("code") == "Home"
                for method, kwargs in backend.calls
            )
            return OCRResult(
                lines=[
                    OCRLine(
                        text="};" if caret_moved else "}5",
                        confidence=0.99,
                        bbox=[2, 2, 24, 22],
                    )
                ]
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(
        typing_module,
        "locate_changed_bbox",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        typing_module,
        "locate_dense_changed_candidates",
        lambda *_args, **_kwargs: [status, row, taskbar],
    )
    backend.print_exact_text = exact_printing  # type: ignore[attr-defined]

    result = await WatchedTyper(backend, StructuralOCR()).type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert result.emitted_exactly_once is True
    assert [
        kwargs["text"]
        for method, kwargs in backend.calls
        if method == "print_exact_text"
    ] == [intended]
    assert [
        kwargs["code"]
        for method, kwargs in backend.calls
        if method == "press_key"
    ] == ["Home", "End"]
    _assert_no_enter(backend)


def test_nearest_editor_status_row_proves_leading_whitespace() -> None:
    intended = "    result = []"
    row = Region(x=65, y=100, width=58, height=14)
    result = OCRResult(
        lines=[
            OCRLine(
                text="Ln 2, Col 16",
                confidence=0.99,
                bbox=[48, 484, 94, 496],
            ),
            OCRLine(
                text="Ln 2, Col 15",
                confidence=0.99,
                bbox=[64, 502, 110, 514],
            ),
            OCRLine(
                text="Ln 2, Col 16",
                confidence=0.99,
                bbox=[145, 582, 191, 594],
            ),
        ]
    )

    assert editor_caret_column_proves_leading_whitespace(
        result,
        intended,
        row,
        (1280, 800),
    )


def test_conflicting_nearest_editor_status_row_fails_closed() -> None:
    intended = "    result = []"
    row = Region(x=65, y=100, width=58, height=14)
    result = OCRResult(
        lines=[
            OCRLine(
                text="Ln 2, Col 15",
                confidence=0.99,
                bbox=[48, 484, 94, 496],
            ),
            OCRLine(
                text="Ln 2, Col 16",
                confidence=0.99,
                bbox=[64, 502, 110, 514],
            ),
        ]
    )

    assert not editor_caret_column_proves_leading_whitespace(
        result,
        intended,
        row,
        (1280, 800),
    )


def test_conflicting_independent_reads_of_same_status_row_fail_closed() -> None:
    intended = "    result = []"
    row = Region(x=65, y=100, width=58, height=14)
    status_region = Region(x=0, y=414, width=512, height=100)
    result = OCRResult(
        lines=[
            OCRLine(
                text="Ln 2, Col 15",
                confidence=0.99,
                bbox=[48, 28, 94, 40],
            )
        ],
        evidence_lines=[
            OCRLine(
                text="Ln 2, Col 16",
                confidence=0.99,
                bbox=[49, 30, 95, 42],
            )
        ],
    )

    assert not editor_caret_column_proves_leading_whitespace(
        result,
        intended,
        row,
        (1280, 800),
        container_region=status_region,
    )


def test_editor_status_search_region_is_bounded_below_causal_row() -> None:
    row = Region(x=65, y=100, width=58, height=14)

    region = editor_status_search_region(
        row,
        (1280, 800),
    )

    assert region == Region(x=0, y=364, width=512, height=150)
    bounded = OCRResult(
        lines=[
            OCRLine(
                text="Ln 2, Col 16",
                confidence=0.94,
                bbox=[15, 29, 57, 41],
            ),
            OCRLine(
                text="Ln 5, Col 17",
                confidence=0.97,
                bbox=[32, 47, 81, 57],
            )
        ]
    )
    assert editor_caret_column_proves_leading_whitespace(
        bounded,
        "    result = []",
        row,
        (1280, 800),
        container_region=region,
    )


def test_editor_status_search_region_caps_tall_causal_box() -> None:
    row = Region(x=37, y=103, width=163, height=401)

    region = editor_status_search_region(row, (1280, 800))

    assert region == Region(x=0, y=417, width=512, height=150)


def test_editor_status_search_region_keeps_foreground_stacked_row_visible() -> None:
    """Do not clip the status row at the top edge of the deepest crop."""

    row = Region(x=80, y=176, width=200, height=30)

    region = editor_status_search_region(row, (1280, 800))

    assert region == Region(x=0, y=456, width=512, height=150)
    assert region.y < 500 < region.y + region.height


def test_editor_status_search_region_keeps_low_foreground_row_visible() -> None:
    """Keep the foreground row below a high causal code line in the crop."""

    row = Region(x=58, y=94, width=70, height=26)

    region = editor_status_search_region(row, (1280, 800))

    assert region == Region(x=0, y=370, width=512, height=150)
    assert region.y < 489 < region.y + region.height


def test_editor_status_search_region_retains_complete_low_status_glyphs() -> None:
    """The Code-06 v4 crop must not clip the causal status repaint."""

    row = Region(
        x=40,
        y=29.62962962962963,
        width=733.3333333333334,
        height=88.88888888888889,
    )
    status_effect = Region(x=72, y=477, width=85, height=27)

    region = editor_status_search_region(row, (1280, 800))

    assert region is not None
    candidates = [
        region,
        *typing_module.editor_status_search_subregions(region),
    ]
    assert any(
        candidate.y <= status_effect.y
        and candidate.y + candidate.height
        >= status_effect.y + status_effect.height
        for candidate in candidates
    )


def test_editor_status_search_subregions_cover_broad_crop() -> None:
    region = Region(x=0, y=368, width=512, height=150)

    assert typing_module.editor_status_search_subregions(region) == [
        Region(x=0, y=454, width=512, height=128),
        Region(x=0, y=368, width=512, height=64),
        Region(x=0, y=411, width=512, height=64),
        Region(x=0, y=454, width=512, height=64),
    ]


def test_compact_status_crop_proves_single_line_from_document_invariants() -> None:
    """Two OCR scales can retain Col/count after corrupting only Ln 1."""

    intended = "function Get-FileDigest {"
    row = Region(x=40, y=74, width=133, height=44)
    region = Region(x=0, y=454, width=512, height=64)
    bounded = OCRResult(
        lines=[
            OCRLine(
                text="be pe pens",
                confidence=0.489,
                bbox=[68, 58, 192, 64],
            )
        ],
        alternatives=[
            OCRCandidate(
                text="1n4,Col26 — 25 characters\nPisin text",
                mean_confidence=0.551,
            ),
            OCRCandidate(
                text="tn1Col26 25 characters\nPain text",
                mean_confidence=0.359,
            ),
        ],
    )

    assert editor_status_proves_single_line_payload(
        bounded,
        intended,
        row,
        (1280, 800),
        container_region=region,
    )


def test_split_status_items_prove_single_line_document_invariants() -> None:
    """Paddle emits Notepad's position and character count as two boxes."""

    intended = "<!doctype html>"
    row = Region(
        x=40,
        y=74.07407407407408,
        width=93.33333333333334,
        height=44.44444444444444,
    )
    region = Region(x=0, y=368.5185185185185, width=512, height=150)
    bounded = OCRResult(
        lines=[
            OCRLine(
                text="Ln 1, Col 16",
                confidence=0.9232,
                bbox=[47, 116, 89, 127],
            ),
            OCRLine(
                text="15 characters",
                confidence=0.9973,
                bbox=[99, 117, 147, 127],
            ),
            OCRLine(
                text="Ln 8, Col 1",
                confidence=0.8937,
                bbox=[64, 132, 101, 142],
            ),
            OCRLine(
                text="154 characters",
                confidence=0.9971,
                bbox=[117, 133, 167, 142],
            ),
        ]
    )

    assert editor_status_proves_single_line_payload(
        bounded,
        intended,
        row,
        (1280, 800),
        container_region=region,
    )


def test_trailing_status_crop_uses_foreground_row_from_code06_v4() -> None:
    """A lower background Notepad row cannot override the causal foreground."""

    intended = "function debounce(fn, delay) {"
    row = Region(
        x=40,
        y=29.62962962962963,
        width=733.3333333333334,
        height=88.88888888888889,
    )
    trailing = Region(
        x=0,
        y=429.6296296296296,
        width=512,
        height=128,
    )
    result = OCRResult(
        lines=[
            OCRLine(
                text="Ln 1, Col 31",
                confidence=0.9278,
                bbox=[48, 55, 89, 65],
            ),
            OCRLine(
                text="30 characters",
                confidence=0.9730,
                bbox=[98, 54, 149, 66],
            ),
            OCRLine(
                text="Ln 9, Col 4",
                confidence=0.9508,
                bbox=[80, 88, 120, 98],
            ),
            OCRLine(
                text="128 characters",
                confidence=0.9989,
                bbox=[132, 88, 184, 98],
            ),
        ]
    )

    assert editor_status_proves_single_line_payload(
        result,
        intended,
        row,
        (1280, 800),
        container_region=trailing,
    )


def test_split_status_items_reject_count_from_another_status_row() -> None:
    intended = "<!doctype html>"
    row = Region(x=40, y=74, width=93, height=44)
    region = Region(x=0, y=368, width=512, height=150)
    bounded = OCRResult(
        lines=[
            OCRLine(
                text="Ln 1, Col 16",
                confidence=0.99,
                bbox=[47, 116, 89, 127],
            ),
            OCRLine(
                text="15 characters",
                confidence=0.99,
                bbox=[99, 133, 147, 143],
            ),
        ]
    )

    assert not editor_status_proves_single_line_payload(
        bounded,
        intended,
        row,
        (1280, 800),
        container_region=region,
    )


def test_compact_status_crop_rejects_conflicting_document_invariants() -> None:
    intended = "function Get-FileDigest {"
    row = Region(x=40, y=74, width=133, height=44)
    region = Region(x=0, y=454, width=512, height=64)
    bounded = OCRResult(
        alternatives=[
            OCRCandidate(
                text="1n1,Col26 — 25 characters",
                mean_confidence=0.72,
            ),
            OCRCandidate(
                text="tn1Col25 25 characters",
                mean_confidence=0.71,
            ),
        ],
    )

    assert not editor_status_proves_single_line_payload(
        bounded,
        intended,
        row,
        (1280, 800),
        container_region=region,
    )


def test_inflated_recovery_box_accepts_foreground_status_geometry() -> None:
    """The validator must use the same bounded row height as its status crop."""

    intended = "    for number in range(1, limit + 1):"
    row = Region(x=79, y=126, width=1184, height=666)
    region = editor_status_search_region(row, (1280, 800))
    assert region == Region(x=0, y=440, width=512, height=150)
    bounded = OCRResult(
        lines=[
            OCRLine(
                text="Ln 3, Col 39",
                confidence=0.9597,
                bbox=[64, 10, 108, 21],
            ),
            OCRLine(
                text="Ln 1, Col 21",
                confidence=0.9399,
                bbox=[80, 27, 120, 38],
            ),
        ]
    )

    assert editor_caret_column_proves_leading_whitespace(
        bounded,
        intended,
        row,
        (1280, 800),
        container_region=region,
    )


def test_compact_status_crop_accepts_notepad_ln_ocr_confusable() -> None:
    intended = "    for number in range(1, limit + 1):"
    row = Region(x=37, y=99, width=211, height=37)
    region = editor_status_search_region(row, (1280, 800))
    assert region == Region(x=0, y=386, width=512, height=150)
    bounded = OCRResult(
        lines=[
            OCRLine(
                text="in3.Col39 75 characters",
                confidence=0.735,
                bbox=[45, 10, 142, 18],
            ),
            OCRLine(
                text="Ln 1, Col 21 20 characters",
                confidence=0.74,
                bbox=[78, 44, 174, 50],
            ),
        ]
    )

    assert editor_caret_column_proves_leading_whitespace(
        bounded,
        intended,
        row,
        (1280, 800),
        container_region=region,
    )


def test_compact_status_crop_accepts_unique_precise_ocr_alternative() -> None:
    intended = "    result = []"
    row = Region(x=65, y=100, width=58, height=14)
    region = editor_status_search_region(row, (1280, 800))
    bounded = OCRResult(
        lines=[
            OCRLine(
                text="in2 Colt 36 characters",
                confidence=0.726,
                bbox=[17, 32, 114, 42],
            ),
            OCRLine(
                text="Pisin text",
                confidence=0.871,
                bbox=[198, 32, 231, 42],
            ),
        ],
        alternatives=[
            OCRCandidate(
                text="Ln 2, Col 16\n36 characters\nPlain text",
                mean_confidence=0.984,
            )
        ],
    )

    assert editor_caret_column_proves_leading_whitespace(
        bounded,
        intended,
        row,
        (1280, 800),
        container_region=region,
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("Ln 4, Col 12 53 characters", True),
        ("Ln 4, Col 11 53 characters", False),
        ("Ln 1, Col 12 11 characters", False),
    ],
)
def test_multiline_status_proves_visible_single_gap_row(
    status: str,
    expected: bool,
) -> None:
    result = OCRResult(
        lines=[
            OCRLine(
                text=status,
                confidence=0.99,
                bbox=[8, 8, 180, 20],
            )
        ]
    )

    assert (
        editor_status_proves_visible_multiline_payload(
            result,
            "FROM orders",
            Region(x=40, y=120, width=140, height=18),
            (1280, 800),
            container_region=Region(
                x=0,
                y=410,
                width=512,
                height=150,
            ),
        )
        is expected
    )


@pytest.mark.parametrize(
    "alternative",
    [
        OCRCandidate(
            text="Ln 2, Col 16\nLn 1, Col 21",
            mean_confidence=0.99,
        ),
        OCRCandidate(
            text="Ln 2, Col 16\n36 characters",
            mean_confidence=0.89,
        ),
    ],
)
def test_compact_status_crop_rejects_ambiguous_precise_ocr_alternative(
    alternative: OCRCandidate,
) -> None:
    intended = "    result = []"
    row = Region(x=65, y=100, width=58, height=14)
    region = editor_status_search_region(row, (1280, 800))
    bounded = OCRResult(
        lines=[
            OCRLine(
                text="in2 Colt 36 characters",
                confidence=0.726,
                bbox=[17, 32, 114, 42],
            )
        ],
        alternatives=[alternative],
    )

    assert not editor_caret_column_proves_leading_whitespace(
        bounded,
        intended,
        row,
        (1280, 800),
        container_region=region,
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


class _IndentedRowOCR:
    def __init__(
        self,
        expanded_text: str,
        expanded_spacing: str,
    ) -> None:
        self.expanded_text = expanded_text
        self.expanded_spacing = expanded_spacing
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
        if region.width < 120:
            return OCRResult(
                lines=[
                    OCRLine(
                        text="result = []",
                        confidence=0.99,
                        bbox=[2, 2, 62, 14],
                    )
                ],
                spacing_evidence="uncertain",
            )
        return OCRResult(
            lines=[
                OCRLine(
                    text=self.expanded_text,
                    confidence=0.99,
                    bbox=[20, 2, 88, 14],
                )
            ],
            spacing_evidence=self.expanded_spacing,
        )

    async def ocr_precise(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        return await self.ocr(image_path, region)


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
    (
        "intended",
        "visible_text",
        "spacing_evidence",
        "spacing_alternative_text",
        "expected_status",
    ),
    [
        ("2. Act", "2. Act", "verified", None, "verified_exact"),
        ("2. Act", "2. Act", "uncertain", "2. Act", "verified_exact"),
        ("2. Act", "2. Act", "uncertain", None, "unverified_ambiguous"),
        (
            "alpha  beta",
            "alpha beta",
            "uncertain",
            "alpha  beta",
            "verified_exact",
        ),
        (
            "alpha  beta",
            "alpha beta",
            "uncertain",
            "1\nalpha  beta",
            "verified_exact",
        ),
        (
            "alpha  beta",
            "alpha beta",
            "uncertain",
            None,
            "unverified_ambiguous",
        ),
    ],
)
async def test_short_exact_typing_checks_all_causal_lines_when_coarse_crop_is_wrong(
    monkeypatch: pytest.MonkeyPatch,
    intended: str,
    visible_text: str,
    spacing_evidence: str,
    spacing_alternative_text: str | None,
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
                lines=[OCRLine(text=visible_text, confidence=0.99)],
                alternatives=(
                    [
                        OCRCandidate(
                            text=spacing_alternative_text,
                            evidence_kind="spacing",
                        )
                    ]
                    if spacing_alternative_text is not None
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
        intended,
        exact=True,
        context="editor",
        code="  " in intended,
    )

    assert result.status == expected_status
    if expected_status.startswith("unverified"):
        assert "verified the field" not in result.summary
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


async def test_late_causal_exact_row_skips_redundant_settled_field_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _ambiguous_dense_typing_backend(monkeypatch)
    candidate_calls = 0

    def delayed_candidates(*_args, **_kwargs) -> list[Region]:
        nonlocal candidate_calls
        candidate_calls += 1
        if candidate_calls == 1:
            return []
        return [Region(x=40, y=96, width=80, height=24)]

    monkeypatch.setattr(
        typing_module,
        "locate_dense_changed_candidates",
        delayed_candidates,
    )

    class LateExactGlyphOCR:
        def __init__(self) -> None:
            self.causal_reads = 0
            self.background_reads = 0

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is not None and region.y < 200:
                self.causal_reads += 1
                return OCRResult(
                    lines=[OCRLine(text="}", confidence=0.99)],
                )
            self.background_reads += 1
            return OCRResult(
                lines=[OCRLine(text="Ln 6, Col 2", confidence=0.99)],
            )

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr(image_path, region)

        async def ocr_precise_fallback(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr(image_path, region)

    ocr = LateExactGlyphOCR()
    result = await WatchedTyper(backend, ocr).type_text(
        "}",
        exact=True,
        context="editor",
        code=True,
    )

    assert result.status == "verified_exact"
    assert result.field_text == "}"
    assert result.emitted_exactly_once is True
    assert ocr.causal_reads == 1
    assert ocr.background_reads == 0


@pytest.mark.parametrize(
    ("expanded_text", "expanded_spacing", "expected_status"),
    [
        ("    result = []", "verified", "verified_exact"),
        ("result = []", "uncertain", "unverified_whitespace"),
    ],
)
async def test_causal_code_row_uses_trimmed_glyphs_only_to_localize(
    monkeypatch: pytest.MonkeyPatch,
    expanded_text: str,
    expanded_spacing: str,
    expected_status: str,
) -> None:
    backend = _ambiguous_dense_typing_backend(monkeypatch)
    intended = "    result = []"
    ocr = _IndentedRowOCR(expanded_text, expanded_spacing)
    result = await WatchedTyper(backend, ocr).type_text(
        intended,
        exact=True,
        context="editor",
        code=True,
    )

    assert result.status == expected_status
    assert result.emitted_exactly_once is True
    assert result.emitted_characters == len(intended)
    assert any(
        region is not None and region.y < 200 and region.width < 120
        for region in ocr.regions
    )
    assert any(
        region is not None and region.y < 200 and region.width >= 120
        for region in ocr.regions
    )


@pytest.mark.parametrize("autocorrect_glyph", [None, "I", "1"])
async def test_bounded_indented_editor_line_uses_causal_dense_row(
    monkeypatch: pytest.MonkeyPatch,
    autocorrect_glyph: str | None,
) -> None:
    """A status repaint must not hide one bounded exact code-row delivery."""

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        typing_module,
        "locate_changed_bbox",
        lambda *_args, **_kwargs: None,
    )
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
    intended = "        if i % 15 == 0:"

    def visible_text() -> str:
        correction_applied = any(
            method == "press_key" and call.get("code") == "Backspace"
            for method, call in backend.calls
        )
        if autocorrect_glyph is not None and not correction_applied:
            return intended.replace(
                " if i ",
                f" if {autocorrect_glyph} ",
                1,
            )
        return intended

    class CurrentEditorRowOCR:
        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is None:
                return OCRResult()
            if region.y >= 200:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="Ln 4, Col 24",
                            confidence=0.99,
                            bbox=[12, region.height - 18, 90, region.height - 6],
                        )
                    ]
                )
            if region.width < 120:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=visible_text().strip(),
                            confidence=0.99,
                            bbox=[4, 4, 72, 18],
                        )
                    ],
                    spacing_evidence="uncertain",
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=visible_text(),
                        confidence=0.99,
                        bbox=[12, 4, 112, 18],
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

    result = await WatchedTyper(backend, CurrentEditorRowOCR()).type_text(
        intended,
        exact=True,
        context="editor",
        code=True,
    )

    assert result.status == "verified_exact", result
    assert result.correction_count == int(autocorrect_glyph is not None)
    assert result.emitted_exactly_once is (autocorrect_glyph is None)
    assert [
        call["text"]
        for method, call in backend.calls
        if method == "type_text" and call["text"] not in {"_", "i"}
    ] == [intended]
    if autocorrect_glyph is not None:
        pressed = [
            call.get("code")
            for method, call in backend.calls
            if method == "press_key"
        ]
        assert pressed[0] == "End"
        assert pressed.index("Home") > pressed.index("Backspace")
        assert pressed[-1] == "End"
        suffix_length = standalone_i_autocorrect_suffix_length(
            intended,
            intended.replace(
                " if i ",
                f" if {autocorrect_glyph} ",
                1,
            ),
        )
        assert suffix_length is not None
        assert pressed.count("ArrowLeft") == suffix_length + 1


async def test_causal_spacing_row_ignores_unchanged_editor_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _ambiguous_dense_typing_backend(monkeypatch)
    intended = "gamma   delta"

    class ContextualSpacingOCR:
        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is None or region.y >= 200:
                return OCRResult()
            return OCRResult(
                lines=[
                    OCRLine(text="alpha beta", confidence=0.95),
                    OCRLine(text="gamma delta", confidence=0.80),
                ],
                alternatives=[
                    OCRCandidate(
                        text="alpha  beta\ngamma   delta",
                        evidence_kind="spacing",
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

    result = await WatchedTyper(backend, ContextualSpacingOCR()).type_text(
        intended,
        exact=True,
        context="editor",
        code=True,
    )

    assert result.status == "verified_exact"
    assert result.field_text == intended
    assert result.emitted_exactly_once is True


async def test_causal_spacing_reocrs_only_the_matching_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _ambiguous_dense_typing_backend(monkeypatch)
    intended = "alpha  beta"

    class NoisySpacingOCR:
        def __init__(self) -> None:
            self.regions: list[Region] = []

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is None or region.y >= 200:
                return OCRResult()
            self.regions.append(region)
            if region.width >= 70:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="alpha",
                            confidence=0.9996,
                            bbox=[16, 9, 44, 19],
                        ),
                        OCRLine(
                            text="beta",
                            confidence=0.9998,
                            bbox=[51, 10, 73, 19],
                        ),
                    ],
                    spacing_evidence="not_evaluated",
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text="alpha",
                        confidence=0.9995,
                        bbox=[2, 2, 30, 10],
                    ),
                    OCRLine(
                        text="beta",
                        confidence=0.9997,
                        bbox=[37, 2, 56, 10],
                    ),
                ],
                alternatives=[
                    OCRCandidate(
                        text=intended,
                        evidence_kind="spacing",
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

    ocr = NoisySpacingOCR()
    result = await WatchedTyper(backend, ocr).type_text(
        intended,
        exact=True,
        context="editor",
        code=True,
    )

    assert result.status == "verified_exact"
    assert result.emitted_exactly_once is True
    assert any(region.width < 70 for region in ocr.regions)


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


async def test_exact_editor_code_uses_guarded_fast_print() -> None:
    """Bounded code rows use the backend's reliable at-most-once printer."""

    backend = FakeBackend()
    backend.guarded_exact_print = True  # type: ignore[attr-defined]
    line = "        elif number % 3 == 0:"
    orig_print = backend.print_text

    async def printing(text: str) -> None:
        await orig_print(text)
        backend.set_screen(line)

    backend.print_text = printing  # type: ignore[method-assign]
    result = await WatchedTyper(
        backend,
        ScriptedOCR(line),
    ).type_text(
        line,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.used_fast_path is True
    assert result.status == "verified_exact"
    assert result.emitted_exactly_once is True
    assert [
        kwargs["text"]
        for method, kwargs in backend.calls
        if method == "print_text"
    ] == [line]
    assert not any(method == "type_text" for method, _ in backend.calls)
    _assert_no_enter(backend)


async def test_exact_editor_code_prefers_ordered_exact_printer() -> None:
    backend = FakeBackend()
    backend.guarded_exact_print = True  # type: ignore[attr-defined]
    line = "    for number in range(1, limit + 1):"
    exact_calls: list[str] = []

    async def exact_printing(text: str) -> None:
        exact_calls.append(text)
        backend.set_screen(line)

    async def unordered_printing(text: str) -> None:
        raise AssertionError(f"unordered printer used for exact code: {text}")

    backend.print_exact_text = exact_printing  # type: ignore[attr-defined]
    backend.print_text = unordered_printing  # type: ignore[method-assign]
    result = await WatchedTyper(
        backend,
        ScriptedOCR(line),
    ).type_text(
        line,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact"
    assert result.emitted_exactly_once is True
    assert exact_calls == [line]
    _assert_no_enter(backend)


async def test_guarded_editor_symbol_repair_uses_independent_per_key_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not retry one printer-specific symbol slip through that printer."""

    async def no_sleep(_seconds: float) -> None:
        return None

    backend = FakeBackend()
    backend.guarded_exact_print = True  # type: ignore[attr-defined]
    intended = 'result.append("FizzBuzz")'
    observed = 'result.append("FizzBuzz"0'
    orig_print = backend.print_text
    orig_type = backend.type_text

    async def printing(text: str) -> None:
        await orig_print(text)
        backend.set_screen(observed)

    async def typing(
        text: str,
        *,
        code: bool = False,
        secret: bool = False,
    ) -> None:
        await orig_type(text, code=code, secret=secret)
        if text == ")":
            backend.set_screen(intended)

    class PrinterSlipOCR:
        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            corrected = any(
                method == "type_text" and kwargs.get("text") == ")"
                for method, kwargs in backend.calls
            )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=intended if corrected else observed,
                        confidence=0.99,
                    )
                ]
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    backend.print_text = printing  # type: ignore[method-assign]
    backend.type_text = typing  # type: ignore[method-assign]

    result = await WatchedTyper(backend, PrinterSlipOCR()).type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert result.used_fast_path is True
    assert result.correction_count == 1
    assert result.emitted_exactly_once is False
    assert [
        kwargs["text"]
        for method, kwargs in backend.calls
        if method == "print_text"
    ] == [intended]
    assert [
        kwargs["text"]
        for method, kwargs in backend.calls
        if method == "type_text"
    ] == [")"]
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


async def test_indented_editor_layout_mismatch_never_replays_across_line_boundary() -> None:
    """A dropped append character must not let Backspace consume its newline."""

    backend = FakeBackend()
    intended = '            results.append("FizzBuzz")'
    observed = "            results.append(@FizzBuzz@)"

    result = await WatchedTyper(
        backend,
        ScriptedOCR(observed),
    ).type_text(
        intended,
        region=Region(x=10, y=10, width=500, height=40),
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "failed_keyboard_layout", result
    assert result.correction_count == 0
    assert result.emitted_exactly_once is True
    assert backend.layout == "us"
    assert not any(
        method == "press_key" and kwargs.get("code") == "Backspace"
        for method, kwargs in backend.calls
    )
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


def test_case_correction_signatures_are_narrow() -> None:
    assert is_standalone_i_autocorrect("for i in", "for I in")
    assert is_standalone_i_autocorrect("if i % 15 == 0:", "if 1 % 15 == 0:")
    assert is_standalone_i_autocorrect("if i % 15 == 0:", "if | % 15 == 0:")
    assert is_standalone_i_autocorrect("if i % 15 == 0:", "if l % 15 == 0:")
    assert not is_standalone_i_autocorrect("limit", "lImit")
    assert not is_standalone_i_autocorrect("for i in", "FOR I IN")
    assert is_caps_lock_case_inversion("HARNESSE2E42", "harnesse2e42")
    assert is_caps_lock_case_inversion("MyVar", "mYvAR")
    assert not is_caps_lock_case_inversion("MyVar", "myVar")
    assert not is_caps_lock_case_inversion("for i in", "for I in")
    assert standalone_i_autocorrect_suffix_length("for i in", "for I in") == 3
    assert (
        standalone_i_autocorrect_suffix_length(
            "    i  = i + 1",
            "i = I + 1",
        )
        == 4
    )
    assert standalone_i_autocorrect_suffix_length("limit", "lImit") is None
    assert standalone_i_autocorrect_navigation("for i in", "for I in") == (
        "End",
        "ArrowLeft",
        3,
    )
    assert standalone_i_autocorrect_navigation(
        "    for i in range(1, limit + 1):",
        "for I in range(1, limit + 1):",
    ) == ("End", "ArrowLeft", 24)
    assert standalone_i_autocorrect_navigation(
        "for i in range(1, limit + 1):",
        "for I in range(1, limit + 1):",
    ) == ("End", "ArrowLeft", 24)
    assert editor_punctuation_transport_substitution(
        '            result.append("FizzBuzz")',
        'result.append("FizzBuzz"0',
    ) == (0, ")", "0")
    assert (
        editor_punctuation_transport_substitution(
            '            result.append("FizzBuzz")',
            'result.append("FuzzBuzz"0',
        )
        is None
    )
    assert (
        editor_punctuation_transport_substitution(
            "    return result",
            "return resu1t",
        )
        is None
    )
    intended = "        elif number % 3 == 0:"
    assert editor_single_glyph_transport_deletion(
        intended,
        "1if number % 3 == 0:",
    ) == (20, "e")
    assert (
        editor_single_glyph_transport_deletion(
            intended,
            "11f number % 3 == 0:",
        )
        is None
    )
    assert editor_single_glyph_transport_deletion(
        intended,
        "elif number % 3 == 0:",
    ) == (21, " ")
    assert editor_single_glyph_transport_deletion(
        "        else:",
        "else:",
    ) == (5, " ")


@pytest.mark.parametrize(
    "reads",
    [
        ("for I in", "for I in", "for i in"),
        (
            "result = []\nfor I in",
            "result = []\nfor I in",
            "result = []\nfor i in",
        ),
    ],
)
async def test_editor_standalone_i_autocorrect_is_replaced_locally(
    reads: tuple[str, str, str],
) -> None:
    backend = FakeBackend()
    intended = "for i in"
    ocr = ScriptedOCR(*reads)

    result = await WatchedTyper(backend, ocr).type_text(
        intended,
        region=Region(x=10, y=10, width=400, height=40),
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert result.correction_count == 1
    assert result.emitted_exactly_once is False
    assert [
        kwargs
        for method, kwargs in backend.calls
        if method == "type_text"
    ] == [
        {"text": intended, "code": True, "secret": False},
        {"text": "_", "code": True, "secret": False},
        {"text": "i", "code": True, "secret": False},
    ]
    pressed = [
        kwargs.get("code")
        for method, kwargs in backend.calls
        if method == "press_key"
    ]
    assert pressed == [
        "End",
        "ArrowLeft",
        "ArrowLeft",
        "ArrowLeft",
        "Backspace",
        "ArrowLeft",
        "ArrowRight",
        "Backspace",
        "End",
        "Home",
        "End",
    ]
    assert not any(
        method == "press_key" and kwargs.get("code") == "CapsLock"
        for method, kwargs in backend.calls
    )
    _assert_no_enter(backend)


async def test_editor_autocorrect_readback_moves_caret_off_final_punctuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caret-obscured colon after local repair must not trigger another edit."""

    async def no_sleep(_seconds: float) -> None:
        return None

    backend = FakeBackend()
    intended = "if i % 15 == 0:"

    class CaretObscuredAutocorrectOCR:
        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            emitted = [
                kwargs.get("text")
                for method, kwargs in backend.calls
                if method == "type_text"
            ]
            moved_home = any(
                method == "press_key" and kwargs.get("code") == "Home"
                for method, kwargs in backend.calls
            )
            if "i" not in emitted:
                visible = intended.replace(" i ", " I ", 1)
            elif moved_home:
                visible = intended
            else:
                visible = f"{intended[:-1]};"
            return OCRResult(
                lines=[OCRLine(text=visible, confidence=0.99)],
                spacing_evidence="verified",
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    typer = WatchedTyper(backend, CaretObscuredAutocorrectOCR())
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[8:10, 5:25] = 200
    grids = [flat, changed.reshape(-1)]

    async def changed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = changed_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert result.field_text == intended
    assert result.correction_count == 1
    assert [
        kwargs["text"]
        for method, kwargs in backend.calls
        if method == "type_text"
    ] == [intended, "_", "i"]
    pressed = [
        kwargs.get("code")
        for method, kwargs in backend.calls
        if method == "press_key"
    ]
    assert "Home" in pressed
    assert pressed[-1] == "End"
    _assert_no_enter(backend)


async def test_indented_editor_autocorrect_uses_status_proof_after_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    backend = FakeBackend()
    intended = "    for i in"

    class IndentedAutocorrectOCR:
        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if (
                region is not None
                and region.x == 0
                and region.width == 512
                and region.height >= 90
                and region.y > 140
            ):
                foreground_y = region.height - 20
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="Ln 3, Col 13",
                            confidence=0.99,
                            bbox=[20, foreground_y, 82, foreground_y + 12],
                        )
                    ]
                )
            corrected = any(
                method == "press_key" and kwargs.get("code") == "Backspace"
                for method, kwargs in backend.calls
            )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=(
                            "result = []\nfor i in"
                            if corrected
                            else "result = []\nfor I in"
                        ),
                        confidence=0.99,
                    )
                ],
                spacing_evidence="not_evaluated",
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    typer = WatchedTyper(backend, IndentedAutocorrectOCR())
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[8:10, 5:14] = 200
    grids = [flat, changed.reshape(-1)]

    async def changed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = changed_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert result.field_text == intended
    assert result.correction_count == 1
    assert result.emitted_exactly_once is False
    assert [
        kwargs
        for method, kwargs in backend.calls
        if method == "type_text"
    ] == [
        {"text": intended, "code": True, "secret": False},
        {"text": "_", "code": True, "secret": False},
        {"text": "i", "code": True, "secret": False},
    ]
    _assert_no_enter(backend)


async def test_editor_autocorrect_then_punctuation_slip_are_repaired_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two independently grounded glyph repairs may compose without replay."""

    async def no_sleep(_seconds: float) -> None:
        return None

    backend = FakeBackend()
    intended = "if i % 15 == 0:"

    class CompoundEditorSlipOCR:
        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            typed = [
                kwargs.get("text")
                for method, kwargs in backend.calls
                if method == "type_text"
            ]
            if ":" in typed:
                visible = intended
            elif "i" in typed:
                visible = f"{intended[:-1]};"
            else:
                visible = intended.replace(" i ", " I ", 1)
            return OCRResult(
                lines=[OCRLine(text=visible, confidence=0.99)],
                spacing_evidence="verified",
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    typer = WatchedTyper(backend, CompoundEditorSlipOCR())
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[8:10, 5:25] = 200
    grids = [flat, changed.reshape(-1)]

    async def changed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = changed_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert result.field_text == intended
    assert result.correction_count == 2
    assert result.emitted_exactly_once is False
    assert [
        kwargs["text"]
        for method, kwargs in backend.calls
        if method == "type_text"
    ] == [intended, "_", "i", ":"]
    _assert_no_enter(backend)


async def test_indented_editor_one_glyph_transport_slip_is_repaired_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grounded ``)`` -> ``0`` HID slip must not replay the code row."""

    async def no_sleep(_seconds: float) -> None:
        return None

    backend = FakeBackend()
    intended = '            result.append("FizzBuzz")'
    visible_intended = intended.lstrip()
    visible_slip = visible_intended[:-1] + "0"

    class IndentedTransportSlipOCR:
        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if (
                region is not None
                and region.x == 0
                and region.width == 512
                and region.height >= 90
                and region.y > 140
            ):
                foreground_y = region.height - 20
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="Ln 5, Col 38",
                            confidence=0.99,
                            bbox=[20, foreground_y, 90, foreground_y + 12],
                        )
                    ]
                )
            corrected = any(
                method == "press_key" and kwargs.get("code") == "Backspace"
                for method, kwargs in backend.calls
            )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=visible_intended if corrected else visible_slip,
                        confidence=0.99,
                    )
                ],
                spacing_evidence="not_evaluated",
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    typer = WatchedTyper(backend, IndentedTransportSlipOCR())
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[8:10, 5:28] = 200
    grids = [flat, changed.reshape(-1)]

    async def changed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = changed_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert result.field_text == intended
    assert result.correction_count == 1
    assert result.emitted_exactly_once is False
    assert [
        kwargs["text"]
        for method, kwargs in backend.calls
        if method == "type_text"
    ] == [intended, ")"]
    pressed = [
        kwargs.get("code")
        for method, kwargs in backend.calls
        if method == "press_key"
    ]
    assert pressed[-3:] == ["End", "Backspace", "End"]
    _assert_no_enter(backend)


@pytest.mark.parametrize(
    ("reported_column", "expected_status"),
    [
        (20, "verified_exact"),
        (21, "unverified_ambiguous"),
    ],
)
async def test_editor_one_missing_glyph_is_inserted_locally(
    monkeypatch: pytest.MonkeyPatch,
    reported_column: int,
    expected_status: str,
) -> None:
    """A repeated, status-proved HID deletion inserts only the missing glyph."""

    async def no_sleep(_seconds: float) -> None:
        return None

    backend = FakeBackend()
    intended = "def fizzbuzz(limit):"
    observed = "def fizzbuzz(imit):"

    class MissingGlyphOCR:
        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if (
                region is not None
                and region.x == 0
                and region.width == 512
                and region.height >= 90
                and region.y > 140
            ):
                foreground_y = region.height - 20
                repaired = any(
                    method == "type_text" and kwargs.get("text") == "l"
                    for method, kwargs in backend.calls
                )
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=(
                                "Ln 1, Col 21   20 characters"
                                if repaired
                                else (
                                    f"Ln 1, Col {reported_column}   "
                                    "19 characters"
                                )
                            ),
                            confidence=0.99,
                            bbox=[20, foreground_y, 148, foreground_y + 12],
                        )
                    ]
                )
            repaired = any(
                method == "type_text" and kwargs.get("text") == "l"
                for method, kwargs in backend.calls
            )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=intended if repaired else observed,
                        confidence=0.99,
                        bbox=[44, 84, 190, 105],
                    )
                ]
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    typer = WatchedTyper(backend, MissingGlyphOCR())
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[8:10, 5:25] = 200
    grids = [flat, changed.reshape(-1)]

    async def changed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = changed_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == expected_status, result
    assert result.correction_count == int(reported_column == 20)
    assert result.emitted_exactly_once is (reported_column == 21)
    emitted = [
        kwargs["text"]
        for method, kwargs in backend.calls
        if method == "type_text"
    ]
    pressed = [
        kwargs.get("code")
        for method, kwargs in backend.calls
        if method == "press_key"
    ]
    if reported_column == 20:
        assert result.field_text == intended
        assert emitted == [intended, "l"]
        assert pressed[:8] == ["End", *(["ArrowLeft"] * 6), "Home"]
        assert pressed[-1] == "End"
    else:
        assert emitted == [intended]
        assert "ArrowLeft" not in pressed
    _assert_no_enter(backend)


async def test_editor_missing_leading_glyph_tolerates_l_one_ocr_confusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caret-proved deletion may survive OCR confusing ``l`` with ``1``."""

    async def no_sleep(_seconds: float) -> None:
        return None

    backend = FakeBackend()
    intended = "        elif number % 3 == 0:"
    visible_intended = intended.lstrip(" ")
    observed = "1if number % 3 == 0:"

    class MissingLeadingGlyphOCR:
        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            repaired = any(
                method == "type_text" and kwargs.get("text") == "e"
                for method, kwargs in backend.calls
            )
            if (
                region is not None
                and region.x == 0
                and region.width == 512
                and region.height >= 90
                and region.y > 140
            ):
                foreground_y = region.height - 20
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=(
                                "Ln 6, Col 30   171 characters"
                                if repaired
                                else "Ln 6, Col 29   170 characters"
                            ),
                            confidence=0.99,
                            bbox=[20, foreground_y, 158, foreground_y + 12],
                        )
                    ]
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=visible_intended if repaired else observed,
                        confidence=0.99,
                        bbox=[80, 142, 200, 162],
                    )
                ],
                spacing_evidence="verified",
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    typer = WatchedTyper(backend, MissingLeadingGlyphOCR())
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[8:10, 5:25] = 200
    grids = [flat, changed.reshape(-1)]

    async def changed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = changed_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert result.field_text == intended
    assert result.correction_count == 1
    assert result.emitted_exactly_once is False
    assert [
        kwargs["text"]
        for method, kwargs in backend.calls
        if method == "type_text"
    ] == [intended, "e"]
    pressed = [
        kwargs.get("code")
        for method, kwargs in backend.calls
        if method == "press_key"
    ]
    first_left = pressed.index("ArrowLeft")
    assert pressed[first_left - 1] == "End"
    assert pressed[first_left : first_left + 20] == ["ArrowLeft"] * 20
    assert pressed[first_left + 20] == "Home"
    assert pressed[-1] == "End"
    _assert_no_enter(backend)


@pytest.mark.parametrize(
    ("reported_characters", "expected_status"),
    [
        (20, "verified_exact"),
        (19, "unverified_ambiguous"),
    ],
)
async def test_single_line_editor_replacement_uses_status_character_count_for_spacing(
    monkeypatch: pytest.MonkeyPatch,
    reported_characters: int,
    expected_status: str,
) -> None:
    """A tall Ctrl+A repaint must not hide an exact one-line replacement.

    OCR sees the complete rendered row, but its generic whitespace calibrator
    cannot evaluate one short gap.  Foreground Notepad independently reports
    line 1, the end column, and the exact document character count.
    """

    async def no_sleep(_seconds: float) -> None:
        return None

    intended = "def fizzbuzz(limit):"

    class ReplacementOCR:
        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if (
                region is not None
                and region.x == 0
                and region.width == 512
                and region.height >= 90
                and region.y > 140
            ):
                foreground_y = region.height - 20
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=(
                                "Ln 1, Col 21 "
                                f"{reported_characters} characters"
                            ),
                            confidence=0.99,
                            bbox=[
                                20,
                                foreground_y,
                                160,
                                foreground_y + 12,
                            ],
                        )
                    ]
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=intended,
                        confidence=0.99,
                    )
                ],
                spacing_evidence="not_evaluated",
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    backend = FakeBackend()
    typer = WatchedTyper(backend, ReplacementOCR())
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[5:10, 2:17] = 200
    grids = [flat, changed.reshape(-1)]

    async def changed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = changed_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        exact=True,
        context="editor",
    )

    assert result.status == expected_status, result
    assert result.field_text == (
        intended
        if expected_status == "verified_exact"
        else ""
    )
    assert result.emitted_exactly_once is True
    _assert_no_enter(backend)


async def test_visible_multiline_editor_row_uses_caret_column_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact later row with one visible gap stays on grounded local OCR."""

    async def no_sleep(_seconds: float) -> None:
        return None

    intended = "FROM orders"

    class MultilineStatusOCR:
        def __init__(self) -> None:
            self.fallback_reads = 0

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if (
                region is not None
                and region.x == 0
                and region.width == 512
                and region.height >= 90
                and region.y > 140
            ):
                foreground_y = region.height - 20
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="Ln 4, Col 12   53 characters",
                            confidence=0.99,
                            bbox=[
                                20,
                                foreground_y,
                                180,
                                foreground_y + 12,
                            ],
                        )
                    ]
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=intended,
                        confidence=0.99,
                    )
                ],
                spacing_evidence="not_evaluated",
            )

        async def ocr_precise_fallback(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            self.fallback_reads += 1
            return OCRResult()

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    backend = FakeBackend()
    ocr = MultilineStatusOCR()
    typer = WatchedTyper(backend, ocr)
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[5:10, 2:17] = 200
    grids = [flat, changed.reshape(-1)]

    async def changed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = changed_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert result.field_text == intended
    assert result.emitted_exactly_once is True
    assert ocr.fallback_reads == 0
    _assert_no_enter(backend)


async def test_single_line_editor_uses_compact_status_consensus_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured v8 row/status shape must finish on fast local OCR."""

    async def no_sleep(_seconds: float) -> None:
        return None

    intended = "function Get-FileDigest {"

    class CompactStatusOCR:
        def __init__(self) -> None:
            self.compact_reads = 0
            self.fallback_reads = 0
            self.field_reads = 0

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if (
                region is not None
                and region.width == 512
                and region.height == 64
            ):
                self.compact_reads += 1
                if self.compact_reads < 3:
                    return OCRResult()
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="be pe pens",
                            confidence=0.489,
                        )
                    ],
                    alternatives=[
                        OCRCandidate(
                            text="1n4,Col26 — 25 characters",
                            mean_confidence=0.551,
                        ),
                        OCRCandidate(
                            text="tn1Col26 25 characters",
                            mean_confidence=0.359,
                        ),
                    ],
                )
            if (
                region is not None
                and region.width == 512
                and region.height > 64
            ):
                return OCRResult()
            self.field_reads += 1
            if self.field_reads == 1:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="function Get-FileDicest {",
                            confidence=0.643,
                            bbox=[7, 0, 171, 27],
                        )
                    ],
                    spacing_evidence="uncertain",
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text="function Get-FileDigest",
                        confidence=0.998,
                        bbox=[28, 29, 146, 41],
                    )
                ],
                evidence_lines=[
                    OCRLine(
                        text=intended,
                        confidence=0.763,
                        bbox=[28, 31, 153, 41],
                    )
                ],
                spacing_evidence="not_evaluated",
            )

        async def ocr_precise_fallback(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            self.fallback_reads += 1
            return OCRResult()

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    backend = FakeBackend()
    ocr = CompactStatusOCR()
    typer = WatchedTyper(backend, ocr)
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[5:10, 2:17] = 200
    grids = [flat, changed.reshape(-1)]

    async def changed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = changed_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert result.field_text == intended
    assert result.emitted_exactly_once is True
    assert ocr.field_reads >= 2
    assert ocr.compact_reads == 3
    assert ocr.fallback_reads == 0
    _assert_no_enter(backend)


@pytest.mark.parametrize("status_lane", ["local", "fallback"])
async def test_four_space_editor_autocorrect_preserves_final_status_proof(
    monkeypatch: pytest.MonkeyPatch,
    status_lane: str,
) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    intended = "    for i in range(1, limit + 1):"

    class StatusCropBackend(FakeBackend):
        async def screenshot(
            self,
            region: Region | None = None,
        ):
            if region is None:
                return await super().screenshot()
            width = max(1, round(region.width))
            height = max(1, round(region.height))
            output = io.BytesIO()
            Image.new("RGB", (width, height), "navy").save(output, "PNG")
            return to_captured_frame(output.getvalue(), width, height)

    backend = StatusCropBackend()

    class FourSpaceAutocorrectOCR:
        def __init__(self) -> None:
            self.status_fallback_sizes: list[tuple[int, int]] = []

        @staticmethod
        def rendered_text() -> str:
            original_parts = [
                str(kwargs["text"])
                for method, kwargs in backend.calls
                if (
                    method == "type_text"
                    and kwargs.get("text") not in {"_", "i"}
                )
            ]
            rendered = "".join(original_parts).lstrip(" ")
            corrected = any(
                method == "press_key" and kwargs.get("code") == "Backspace"
                for method, kwargs in backend.calls
            )
            if not corrected:
                rendered = rendered.replace("for i in", "for I in", 1)
            return rendered

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            rendered = self.rendered_text()
            if (
                region is not None
                and region.x == 0
                and region.width == 512
                and region.height >= 90
                and region.y > 140
            ):
                foreground_y = region.height - 20
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=(
                                f"Ln 3, Col {len(rendered) + 5}"
                                if status_lane == "local"
                                else "Pisin text"
                            ),
                            confidence=0.99,
                            bbox=[20, foreground_y, 92, foreground_y + 12],
                        )
                    ]
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=rendered,
                        confidence=0.99,
                    )
                ],
                spacing_evidence="not_evaluated",
            )

        async def ocr_precise_fallback(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del region
            size = Image.open(image_path).size
            if size[0] == 512 and size[1] >= 90:
                self.status_fallback_sizes.append(size)
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="Ln 3, Col 34",
                            confidence=0.99,
                            bbox=[20, size[1] - 20, 92, size[1] - 8],
                        )
                    ]
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=self.rendered_text(),
                        confidence=0.99,
                    )
                ]
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    ocr = FourSpaceAutocorrectOCR()
    typer = WatchedTyper(backend, ocr)
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[8:10, 5:28] = 200
    grids = [flat, changed.reshape(-1)]

    async def changed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = changed_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert result.field_text == intended
    assert result.correction_count == 1
    assert result.emitted_exactly_once is False
    assert bool(ocr.status_fallback_sizes) is (status_lane == "fallback")
    _assert_no_enter(backend)


async def test_failed_editor_autocorrect_replacement_stops_without_replaying_line() -> None:
    backend = FakeBackend()
    intended = "for i in range(1, limit + 1):"
    observed = "for I in range(1, limit + 1):"

    result = await WatchedTyper(
        backend,
        ScriptedOCR(observed, observed, observed),
    ).type_text(
        intended,
        region=Region(x=10, y=10, width=400, height=40),
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "failed_case_mismatch", result
    assert result.correction_count == 1
    assert result.typed_characters == len(intended)
    assert result.intended_characters == len(intended)
    assert result.emitted_exactly_once is False
    assert [
        kwargs
        for method, kwargs in backend.calls
        if method == "type_text"
    ] == [
        {"text": intended, "code": True, "secret": False},
        {"text": "_", "code": True, "secret": False},
        {"text": "i", "code": True, "secret": False},
    ]
    assert not any(
        method == "press_key" and kwargs.get("code") == "CapsLock"
        for method, kwargs in backend.calls
    )
    _assert_no_enter(backend)


async def test_failed_editor_punctuation_replacement_stops_without_replaying_line() -> None:
    backend = FakeBackend()
    intended = 'result.append("FizzBuzz")'
    observed = 'result.append("FizzBuzz"0'

    class PersistentPunctuationSlipOCR:
        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(
                        text=observed,
                        confidence=0.99,
                    )
                ]
            )

    result = await WatchedTyper(
        backend,
        PersistentPunctuationSlipOCR(),
    ).type_text(
        intended,
        region=Region(x=10, y=10, width=400, height=40),
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "failed_symbol_mismatch", result
    assert result.correction_count == 1
    assert result.typed_characters == len(intended)
    assert result.intended_characters == len(intended)
    assert result.emitted_exactly_once is False
    assert [
        kwargs["text"]
        for method, kwargs in backend.calls
        if method == "type_text"
    ] == [intended, ")"]
    _assert_no_enter(backend)


@pytest.mark.parametrize(
    ("intended", "observed", "expected_status"),
    [
        ("MyVar", "myVar", "failed_case_mismatch"),
        (
            "    for number in range(1, limit + 1):",
            "    for number in range(1, limit + 1):",
            "verified_exact",
        ),
        (
            "        if i % 15 == 0:",
            "        if i % 15 == 0:",
            "verified_exact",
        ),
    ],
)
async def test_exact_editor_code_is_one_delivery_without_caps_replay(
    intended: str,
    observed: str,
    expected_status: str,
) -> None:
    backend = FakeBackend()

    result = await WatchedTyper(
        backend,
        ScriptedOCR(observed),
    ).type_text(
        intended,
        region=Region(x=10, y=10, width=400, height=40),
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == expected_status, result
    assert result.correction_count == 0
    assert result.emitted_exactly_once is True
    assert [
        kwargs
        for method, kwargs in backend.calls
        if method == "type_text"
    ] == [{"text": intended, "code": True, "secret": False}]
    assert not any(
        method == "press_key" and kwargs.get("code") == "CapsLock"
        for method, kwargs in backend.calls
    )
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


async def test_causal_partial_editor_row_is_truncated_not_focus_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A landed suffix must not be reported as an unfocused field.

    The retained Code-06 VNC trace issued ``  let timer;`` once, then painted
    only the final ``mer;`` on the otherwise blank Notepad row.  The coarse
    luminance grid did not move, but the full-resolution causal diff retained
    the editor row.  That is evidence of partial transport delivery, not
    evidence that the field never had focus.
    """

    async def no_sleep(_seconds: float) -> None:
        return None

    intended = "  let timer;"
    visible_suffix = "mer;"
    before_bytes, after_bytes = _ambiguous_dense_line_frames()
    backend = FakeBackend(width=1280, height=800)
    backend.guarded_exact_print = True  # type: ignore[attr-defined]
    backend.set_frame_bytes(before_bytes)
    original_print = backend.print_text

    async def partially_delivered_print(text: str) -> None:
        await original_print(text)
        backend.set_frame_bytes(after_bytes)

    backend.print_exact_text = partially_delivered_print  # type: ignore[attr-defined]

    class PartialEditorOCR:
        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is not None and region.y < 200:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=visible_suffix,
                            confidence=0.99,
                            bbox=[8, 8, 48, 26],
                        )
                    ]
                )
            return OCRResult()

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

        async def ocr_precise_fallback(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        typing_module,
        "locate_changed_bbox",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        typing_module,
        "locate_dense_changed_candidates",
        lambda *_args, **_kwargs: [
            Region(x=36, y=88, width=64, height=32),
            Region(x=72, y=472, width=88, height=32),
        ],
    )

    result = await WatchedTyper(backend, PartialEditorOCR()).type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "unverified_truncated", result
    assert result.field_text == visible_suffix
    assert result.emitted_exactly_once is True
    assert result.correction_count == 0
    assert [
        kwargs["text"]
        for method, kwargs in backend.calls
        if method == "print_text"
    ] == [intended]
    _assert_no_enter(backend)


@pytest.mark.parametrize(
    ("intended", "noisy_row", "visible_row", "status_row"),
    [
        (
            "    result = []",
            "result = [J",
            "result = []",
            "Ln 2, Col 16",
        ),
        (
            "        if i % 15 == 0:",
            "if i % 15 = 0:",
            "if I % 15 == 0:",
            "Ln 4, Col 24",
        ),
    ],
)
async def test_precise_autolocate_rechecks_noisy_editor_punctuation(
    monkeypatch: pytest.MonkeyPatch,
    intended: str,
    noisy_row: str,
    visible_row: str,
    status_row: str,
) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    class NoisyFullScreenExactCropOCR:
        def __init__(self) -> None:
            self.screen_calls = 0
            self.regions: list[Region | None] = []

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            self.regions.append(region)
            if region is None:
                self.screen_calls += 1
                if self.screen_calls > 1:
                    return OCRResult(
                        lines=[
                            OCRLine(
                                text="in2,Colt6 36 characters",
                                confidence=0.57,
                                bbox=[50, 470, 149, 484],
                            )
                        ]
                    )
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=noisy_row,
                            confidence=0.91,
                            bbox=[64, 101, 121, 112],
                        )
                    ],
                    spacing_evidence="not_evaluated",
                )
            if (
                region.width == 512
                and region.height <= 160
                and region.y > 140
            ):
                foreground_y = region.height - 20
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=status_row,
                            confidence=0.94,
                            bbox=[
                                15,
                                foreground_y,
                                57,
                                foreground_y + 12,
                            ],
                        )
                    ]
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=visible_row,
                        confidence=0.99,
                    )
                ],
                spacing_evidence="verified",
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    backend = FakeBackend()
    typer = WatchedTyper(backend, NoisyFullScreenExactCropOCR())
    ocr = typer.ocr
    flat = _flat_grid()

    def current_visible_row() -> str:
        corrected = any(
            method == "press_key" and call.get("code") == "Backspace"
            for method, call in backend.calls
        )
        if (
            corrected
            and is_standalone_i_autocorrect(intended, visible_row)
        ):
            return intended.strip()
        return visible_row

    original_precise_ocr = ocr.ocr_precise

    async def current_precise_ocr(
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        result = await original_precise_ocr(image_path, region)
        if (
            region is not None
            and not (
                region.width == 512
                and region.height <= 160
                and region.y > 140
            )
            and result.lines
            and result.lines[0].text == visible_row
        ):
            result.lines[0] = result.lines[0].model_copy(
                update={"text": current_visible_row()}
            )
        return result

    ocr.ocr_precise = current_precise_ocr  # type: ignore[method-assign]

    async def unchanged_grid() -> np.ndarray:
        return flat

    typer._grid = unchanged_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact"
    assert result.field_text == intended
    expected_correction = is_standalone_i_autocorrect(
        intended,
        visible_row,
    )
    assert result.correction_count == int(expected_correction)
    assert result.emitted_exactly_once is (not expected_correction)
    assert any(
        region is not None
        and region.width == 512
        and region.height <= 160
        and region.y > 140
        for region in ocr.regions
    )
    typed_calls = [
        kwargs
        for method, kwargs in backend.calls
        if method == "type_text"
    ]
    assert typed_calls[0] == {
        "text": intended,
        "code": True,
        "secret": False,
    }
    assert typed_calls[1:] == (
        [
            {"text": "_", "code": True, "secret": False},
            {"text": "i", "code": True, "secret": False},
        ]
        if expected_correction
        else []
    )
    _assert_no_enter(backend)


@pytest.mark.parametrize(
    ("intended", "line_number", "correct_column", "character_count"),
    [
        ('            result.append("FizzBuzz")', 5, 38, 142),
        ("    result = []", 2, 16, 36),
    ],
)
@pytest.mark.parametrize(
    ("column_delta", "expected_status", "expected_corrections"),
    [
        (0, "verified_exact", 0),
        (-1, "verified_exact", 1),
        (-2, "unverified_whitespace", 0),
    ],
)
async def test_long_indented_editor_suffix_uses_status_alternative(
    monkeypatch: pytest.MonkeyPatch,
    intended: str,
    line_number: int,
    correct_column: int,
    character_count: int,
    column_delta: int,
    expected_status: str,
    expected_corrections: int,
) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    suffix = intended.lstrip(" ")
    regions: list[Region | None] = []

    class ExactVisibleSuffixOCR:
        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            regions.append(region)
            repaired = any(
                method == "type_text" and kwargs.get("text") == " "
                for method, kwargs in backend.calls
            )
            if region is None:
                return OCRResult()
            if (
                region is not None
                and region.x == 0
                and region.width == 512
                and region.height >= 90
                and region.y > 140
            ):
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="in5 Colt 142 characters",
                            confidence=0.73,
                            bbox=[17, 32, 120, 42],
                        )
                    ],
                    alternatives=[
                        OCRCandidate(
                            text=(
                                f"Ln {line_number}, Col "
                                f"{correct_column if repaired else correct_column + column_delta}\n"
                                f"{character_count if repaired else character_count + column_delta} "
                                "characters\nPlain text"
                            ),
                            mean_confidence=0.98,
                        )
                    ],
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=suffix,
                        confidence=0.99,
                        bbox=[103, 130, 315, 153],
                    )
                ]
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    backend = FakeBackend()
    typer = WatchedTyper(backend, ExactVisibleSuffixOCR())
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[8:10, 5:25] = 200
    grids = [flat, changed.reshape(-1)]

    async def changed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = changed_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == expected_status, regions
    assert result.field_text == (
        intended if expected_status == "verified_exact" else suffix
    )
    assert result.correction_count == expected_corrections
    assert result.emitted_exactly_once is (expected_corrections == 0)
    assert [
        kwargs["text"]
        for method, kwargs in backend.calls
        if method == "type_text"
    ] == [intended, *([" "] if expected_corrections else [])]
    _assert_no_enter(backend)


@pytest.mark.parametrize(
    (
        "intended",
        "local_visible_row",
        "foreground_status",
        "pixel_change_visible",
        "expected_fallback_calls",
    ),
    [
        (
            "    for number in range(1, limit + 1):",
            "for number in range(1, limit + 1):",
            "Ln 3, Col 39",
            True,
            0,
        ),
        (
            "        if number % 15 == 0:",
            "if number % 15 == @:",
            "Ln 4, Col 29",
            False,
            1,
        ),
    ],
)
async def test_indented_editor_suffix_uses_one_grounded_glyph_read(
    monkeypatch: pytest.MonkeyPatch,
    intended: str,
    local_visible_row: str,
    foreground_status: str,
    pixel_change_visible: bool,
    expected_fallback_calls: int,
) -> None:
    """A grounded glyph read plus local Ln/Col proof must finish the line."""

    async def no_sleep(_seconds: float) -> None:
        return None

    suffix = intended.lstrip(" ")

    class GroundedSuffixOCR:
        def __init__(self) -> None:
            self.fallback_calls = 0

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is None:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=local_visible_row,
                            confidence=0.91,
                            bbox=[101, 136, 201, 153],
                        )
                    ]
                )
            if (
                region.x == 0
                and region.width == 512
                and region.height >= 90
                and region.y > 140
            ):
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="Ln 1, Col 21 20 characters",
                            confidence=0.72,
                            bbox=[83, 76, 179, 85],
                        )
                    ],
                    evidence_lines=[
                        OCRLine(
                            text=foreground_status,
                            confidence=0.996,
                            bbox=[64, 58, 120, 68],
                        ),
                        OCRLine(
                            text="Ln 1, Col 21",
                            confidence=0.995,
                            bbox=[81, 75, 122, 86],
                        ),
                    ],
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=local_visible_row,
                        confidence=0.91,
                        bbox=[103, 130, 315, 153],
                    )
                ]
            )

        async def ocr_precise_fallback(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            self.fallback_calls += 1
            return OCRResult(
                lines=[OCRLine(text=suffix, confidence=0.99)],
                spacing_evidence="verified",
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    backend = FakeBackend()
    ocr = GroundedSuffixOCR()
    typer = WatchedTyper(backend, ocr)
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[8:10, 5:25] = 200
    grids = [flat, changed.reshape(-1)]

    async def current_grid() -> np.ndarray:
        if not pixel_change_visible:
            return flat
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = current_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert result.field_text == intended
    assert result.emitted_exactly_once is True
    assert ocr.fallback_calls == expected_fallback_calls
    _assert_no_enter(backend)


async def test_indented_editor_append_moves_caret_off_final_punctuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read the final glyph with Home, then restore End for the next line."""

    async def no_sleep(_seconds: float) -> None:
        return None

    intended = '            result.append("FizzBuzz")'
    suffix = intended.lstrip(" ")

    class CaretObscuredEditorOCR:
        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if (
                region is not None
                and region.x == 0
                and region.width == 512
                and region.height >= 90
                and region.y > 140
            ):
                return OCRResult(
                    alternatives=[
                        OCRCandidate(
                            text="Ln 5, Col 38\n142 characters\nPlain text",
                            mean_confidence=0.99,
                        )
                    ]
                )
            if region is None:
                return OCRResult()
            pressed = [
                kwargs.get("code")
                for method, kwargs in backend.calls
                if method == "press_key"
            ]
            caret_moved_home = bool(pressed and pressed[-1] == "Home")
            return OCRResult(
                lines=[
                    OCRLine(
                        text=(suffix if caret_moved_home else f"{suffix[:-1]}8"),
                        confidence=0.99,
                        bbox=[103, 130, 315, 153],
                    )
                ]
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    backend = FakeBackend()
    typer = WatchedTyper(backend, CaretObscuredEditorOCR())
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[8:10, 5:25] = 200
    grids = [flat, changed.reshape(-1)]

    async def changed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = changed_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    pressed = [
        kwargs.get("code")
        for method, kwargs in backend.calls
        if method == "press_key"
    ]
    assert result.status == "verified_exact"
    assert result.field_text == intended
    assert result.emitted_exactly_once is True
    assert pressed[-2:] == ["Home", "End"]
    assert [
        kwargs["text"]
        for method, kwargs in backend.calls
        if method == "type_text"
    ] == [intended]
    _assert_no_enter(backend)


async def test_editor_exact_row_without_spacing_proof_keeps_focus_grounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact editor glyphs may locate the row before spacing is proven.

    A dark Notepad row can sit below the grid-diff threshold while precise OCR
    independently reads the complete line.  The OCR provider deliberately
    withholds exact spacing until a separate invariant proves it.  That must
    leave the once-emitted draft unverified, not misclassify it as lost focus
    and invite an unsafe replay.
    """

    async def no_sleep(_seconds: float) -> None:
        return None

    intended = "for number in range(1, limit + 1):"

    backend = FakeBackend()
    backend.guarded_exact_print = True  # type: ignore[attr-defined]

    def emitted_text() -> str:
        return "".join(
            kwargs["text"]
            for method, kwargs in backend.calls
            if method == "print_text"
        )

    class ExactGlyphsWithoutSpacingOCR:
        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            emitted = emitted_text()
            if region is None:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=emitted.replace("number", "nurnber"),
                            confidence=0.98,
                            bbox=[83, 106, 339, 132],
                        )
                    ],
                    spacing_evidence="not_evaluated",
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text=emitted,
                        confidence=0.98,
                        bbox=[0, 0, 256, 26],
                    )
                ],
                spacing_evidence=(
                    "not_evaluated"
                    if emitted == intended
                    else "verified"
                ),
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    typer = WatchedTyper(backend, ExactGlyphsWithoutSpacingOCR())
    flat = _flat_grid()

    async def unchanged_grid() -> np.ndarray:
        return flat

    typer._grid = unchanged_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        exact=True,
        context="editor",
    )

    assert result.status.startswith("unverified_"), (
        result,
        result.typed_characters,
        result.intended_characters,
        backend.calls,
    )
    assert result.status != "failed_focus_lost"
    assert result.emitted_exactly_once is True
    assert "".join(
        kwargs["text"]
        for method, kwargs in backend.calls
        if method == "print_text"
    ) == intended
    _assert_no_enter(backend)


async def test_causal_editor_row_outranks_notepad_tab_title_for_status_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first document row and its mirrored tab title are not equivalent."""

    async def no_sleep(_seconds: float) -> None:
        return None

    intended = "<!doctype html>"
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
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        typing_module,
        "locate_changed_bbox",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        typing_module,
        "locate_dense_changed_bbox",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        typing_module,
        "locate_dense_changed_candidates",
        lambda *_args, **_kwargs: [
            Region(x=809, y=136, width=120, height=24),
            Region(x=142, y=186, width=120, height=24),
            Region(x=142, y=586, width=150, height=16),
        ],
    )

    class MirroredFirstLineOCR:
        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is None:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="!doctype html",
                            confidence=0.99,
                            bbox=[809, 136, 929, 160],
                        )
                    ]
                )
            if region.width == 512 and region.height >= 90:
                foreground_y = region.height - 24
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="Ln 1, Col 16",
                            confidence=0.99,
                            bbox=[20, foreground_y, 105, foreground_y + 12],
                        ),
                        OCRLine(
                            text="15 characters",
                            confidence=0.99,
                            bbox=[112, foreground_y, 198, foreground_y + 12],
                        ),
                    ]
                )
            if 170 <= region.y <= 230:
                return OCRResult(
                    lines=[OCRLine(text=intended, confidence=0.99)],
                    spacing_evidence="not_evaluated",
                )
            if 120 <= region.y < 170:
                return OCRResult(
                    lines=[OCRLine(text="!doctype html", confidence=0.99)],
                    spacing_evidence="not_evaluated",
                )
            return OCRResult()

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

        async def ocr_precise_fallback(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            return OCRResult()

    result = await WatchedTyper(backend, MirroredFirstLineOCR()).type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert result.field_text == intended
    assert result.emitted_exactly_once is True
    _assert_no_enter(backend)


async def test_editor_indentation_is_reproved_after_full_screen_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    intended = "    for number in range(1, limit + 1):"
    visible_intended = intended.lstrip(" ")
    locate_calls = 0

    def growing_editor_effect(*_args, **_kwargs) -> Region:
        nonlocal locate_calls
        locate_calls += 1
        if locate_calls == 1:
            return Region(x=64, y=106, width=260, height=24)
        return Region(x=0, y=106, width=1280, height=666)

    monkeypatch.setattr(
        typing_module,
        "locate_changed_bbox",
        growing_editor_effect,
    )

    class NoisyBoundedEditorOCR:
        def __init__(self) -> None:
            self.status_reads = 0
            self.full_screen_reads = 0

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if (
                region is not None
                and region.width == 512
                and region.height <= 160
                and region.y > 140
            ):
                self.status_reads += 1
                foreground_y = region.height - 20
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="Ln 3, Col 39",
                            confidence=0.99,
                            bbox=[20, foreground_y, 82, foreground_y + 12],
                        )
                    ]
                )
            if region is None:
                self.full_screen_reads += 1
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=visible_intended,
                            confidence=0.99,
                            bbox=[64, 106, 324, 130],
                        )
                    ]
                )
            return OCRResult(
                lines=[
                    OCRLine(
                        text="for nurnber in range(1, limit + 1):",
                        confidence=0.98,
                        bbox=[64, 10, 324, 34],
                    )
                ],
                spacing_evidence="not_evaluated",
            )

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    backend = FakeBackend()
    ocr = NoisyBoundedEditorOCR()
    typer = WatchedTyper(backend, ocr)
    flat = _flat_grid()
    changed = flat.copy().reshape(GRID_ROWS, GRID_COLS)
    changed[8:10, 5:25] = 200
    grids = [flat, changed.reshape(-1)]

    async def changed_grid() -> np.ndarray:
        return grids.pop(0) if grids else changed.reshape(-1)

    typer._grid = changed_grid  # type: ignore[method-assign]

    result = await typer.type_text(
        intended,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact"
    assert result.field_text == intended
    assert result.emitted_exactly_once is True
    assert ocr.full_screen_reads >= 1
    assert ocr.status_reads == 1
    emitted = [
        kwargs
        for method, kwargs in backend.calls
        if method == "type_text"
    ]
    assert "".join(call["text"] for call in emitted) == intended
    assert all(
        call["code"] is False and call["secret"] is False
        for call in emitted
    )
    _assert_no_enter(backend)


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
        "surrounding_crop",
        "expected_status",
    ),
    [
        pytest.param(
            [20, 72, 1040, 100],
            "",
            False,
            0,
            False,
            "verified_exact",
            id="grounded-complete-line",
        ),
        pytest.param(
            [20, 72, 1040, 100],
            "",
            False,
            1,
            False,
            "verified_exact",
            id="delayed-full-screen-frame",
        ),
        pytest.param(
            [20, 72, 1040, 100],
            "",
            True,
            0,
            False,
            "verified_exact",
            id="causal-delta-recovers-poisoned-crop",
        ),
        pytest.param(
            [20, 72, 1040, 100],
            "",
            False,
            0,
            True,
            "verified_exact",
            id="adjacent-editor-row-is-localized",
        ),
        pytest.param(
            [20, 400, 1040, 428],
            "",
            False,
            0,
            False,
            "unverified_ambiguous",
            id="matching-text-elsewhere",
        ),
        pytest.param(
            [20, 72, 1040, 100],
            "x",
            False,
            0,
            False,
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
    surrounding_crop: bool,
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
                if surrounding_crop:
                    return OCRResult(
                        lines=[
                            OCRLine(
                                text=(
                                    "previous editor row\n"
                                    f"{self.backend.visible}"
                                ),
                                confidence=0.99,
                            )
                        ]
                    )
                return OCRResult()
            return OCRResult()

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is not None:
                if surrounding_crop:
                    return OCRResult(
                        lines=[
                            OCRLine(
                                text=(
                                    "previous editor row\n"
                                    f"{self.backend.visible}"
                                ),
                                confidence=0.99,
                            )
                        ]
                    )
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


async def test_late_editor_paint_reocrs_secondary_causal_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh full-screen frame must get a fresh causal-row OCR pass."""

    async def no_sleep(_seconds: float) -> None:
        return None

    intended = "<body>"
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

    class LatePaintBackend(FakeBackend):
        async def type_text(
            self,
            text: str,
            *,
            code: bool = False,
            secret: bool = False,
        ) -> None:
            await super().type_text(text, code=code, secret=secret)
            self.set_screen(text)

    class LatePaintOCR:
        def __init__(self) -> None:
            self.full_screen_reads = 0
            self.blind_fallback_reads = 0

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is None:
                self.full_screen_reads += 1
                return OCRResult()
            if self.full_screen_reads and region.y < 300:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=intended,
                            confidence=0.99,
                        )
                    ]
                )
            return OCRResult()

        async def ocr_precise_fallback(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            self.blind_fallback_reads += 1
            return OCRResult(
                lines=[
                    OCRLine(
                        text="Ln 7, Col 7 105 characters Plain text",
                        confidence=0.99,
                    )
                ]
            )

    backend = LatePaintBackend()
    ocr = LatePaintOCR()
    result = await WatchedTyper(backend, ocr).type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert result.field_text == intended
    assert result.emitted_exactly_once is True
    assert ocr.full_screen_reads >= 1
    assert ocr.blind_fallback_reads == 0
    _assert_no_enter(backend)


async def test_short_editor_defers_blind_ocr_until_video_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale crop must not pay for model OCR before local video catches up."""

    async def no_sleep(_seconds: float) -> None:
        return None

    intended = "<body>"
    status_region = Region(x=72, y=474, width=88, height=30)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        typing_module,
        "locate_changed_bbox",
        lambda *_args, **_kwargs: status_region,
    )
    monkeypatch.setattr(
        typing_module,
        "locate_dense_changed_candidates",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        typing_module,
        "locate_dense_changed_bbox",
        lambda *_args, **_kwargs: None,
    )

    class DelayedLocalOCR:
        def __init__(self) -> None:
            self.local_reads = 0
            self.blind_fallback_reads = 0

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr_precise(image_path, region)

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is None:
                return OCRResult()
            self.local_reads += 1
            if self.local_reads < 3:
                return OCRResult()
            return OCRResult(
                lines=[OCRLine(text=intended, confidence=0.99)]
            )

        async def ocr_precise_fallback(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            self.blind_fallback_reads += 1
            return OCRResult(
                lines=[
                    OCRLine(
                        text="Ln 7, Col 7 105 characters Plain text",
                        confidence=0.99,
                    )
                ]
            )

    backend = FakeBackend()
    ocr = DelayedLocalOCR()
    result = await WatchedTyper(backend, ocr).type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact", result
    assert result.field_text == intended
    assert result.emitted_exactly_once is True
    assert ocr.local_reads >= 3
    assert ocr.blind_fallback_reads == 0
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


async def test_precise_field_read_extracts_one_exact_structured_ocr_row() -> None:
    intended = "def fizzbuzz(limit):"

    observed = await WatchedTyper(
        FakeBackend(),
        ScriptedOCR(f"File Edit View\n{intended}"),
    )._read_field(
        Region(x=40, y=80, width=260, height=80),
        intended=intended,
        precise=True,
        extract_structured_exact_row=True,
    )

    assert observed == intended


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


def test_precise_readback_prefers_exact_editor_row_over_tab_title() -> None:
    intended = "def fizzbuzz(limit):"
    result = OCRResult(
        lines=[
            OCRLine(
                text="def fizzbuzz(limit)",
                confidence=0.99,
                bbox=[102, 18, 167, 27],
            ),
            OCRLine(
                text=intended,
                confidence=0.85,
                bbox=[79, 64, 177, 74],
            ),
        ]
    )

    refined = precise_readback_candidate_region(
        result,
        intended,
        Region(x=0, y=59, width=286, height=90),
        (1280, 800),
    )

    assert refined == Region(x=77, y=117, width=200, height=24)


def test_precise_readback_retains_json_prefix_before_first_alphanumeric() -> None:
    intended = '  "retries": 3,'
    result = OCRResult(
        lines=[
            OCRLine(
                text='retries": 3,',
                confidence=0.9869,
                bbox=[60, 6, 140, 18],
            )
        ]
    )

    refined = precise_readback_candidate_region(
        result,
        intended,
        Region(x=0, y=112, width=291, height=32),
        (1280, 800),
    )

    assert refined == Region(x=51, y=116, width=200, height=22)


async def test_precise_readback_defers_visible_indent_to_editor_status() -> None:
    intended = '  "enabled": true,'

    class StackedEditorOCR:
        def __init__(self) -> None:
            self.precise_calls = 0
            self.fallback_calls = 0

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
                        text='"enabled": true,',
                        confidence=0.9869,
                        bbox=[60, 6, 140, 18],
                    )
                ]
            )

        async def ocr_precise_fallback(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            self.fallback_calls += 1
            return OCRResult(
                lines=[
                    OCRLine(
                        text=(
                            '"name": "pikvm-agent",\n'
                            '"enabled": true,'
                        ),
                        confidence=0.99,
                    )
                ],
                spacing_evidence="verified",
            )

    ocr = StackedEditorOCR()
    observed = await WatchedTyper(
        FakeBackend(width=1280, height=800),
        ocr,
    )._read_field(
        Region(x=0, y=98, width=301, height=37),
        intended=intended,
        precise=True,
        allow_blind_fallback=True,
        preserve_editor_indent_candidate=True,
        extract_structured_exact_row=True,
    )

    assert observed == '"enabled": true,'
    assert ocr.precise_calls == 1
    assert ocr.fallback_calls == 0


@pytest.mark.parametrize("intended", ["}", "  ]", "    )", "  };"])
async def test_short_structural_code_fragment_uses_grounded_compact_crop(
    monkeypatch: pytest.MonkeyPatch,
    intended: str,
) -> None:
    backend = FakeBackend(width=1280, height=800)
    before = Image.new("RGB", (1280, 800), "#202020")
    after = before.copy()
    draw = ImageDraw.Draw(after)
    draw.rectangle((44, 144, 55, 159), fill="#efefef", width=2)
    draw.rectangle((80, 484, 87, 495), fill="#efefef", width=1)
    before_output = io.BytesIO()
    after_output = io.BytesIO()
    before.save(before_output, format="PNG")
    after.save(after_output, format="PNG")
    before_bytes = before_output.getvalue()
    after_bytes = after_output.getvalue()
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
        lambda *_args, **_kwargs: None,
    )

    class ClosingGlyphOCR:
        compact_reads = 0

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is not None and region.height >= 150:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=(
                                f"Ln 9, Col {len(intended) + 1}"
                                if intended.startswith(" ")
                                else "Ln 10, Col 2"
                            ),
                            confidence=0.99,
                            bbox=[4, 8, 74, 20],
                        )
                    ]
                )
            if region is None:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="param(",
                            confidence=0.99,
                            bbox=[40, 80, 100, 100],
                        )
                    ]
                )
            if region.y < 250 and region.y + region.height > 120:
                self.compact_reads += 1
                if self.compact_reads < 3:
                    return OCRResult()
                return OCRResult(
                    lines=[
                        OCRLine(
                            text=intended.strip(),
                            confidence=0.99,
                            bbox=[4, 4, 18, 18],
                        )
                    ]
                )
            return OCRResult()

        async def ocr_precise_fallback(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(
                        text=(
                            '{\n  "enabled": true,\n'
                            f"{intended}\n}}"
                        ),
                        confidence=0.99,
                    )
                ]
            )

    ocr = ClosingGlyphOCR()
    result = await WatchedTyper(backend, ocr).type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact"
    assert result.field_text == intended
    assert result.emitted_exactly_once is True
    assert ocr.compact_reads >= 3


async def test_structural_fragment_uses_blind_exact_crop_after_local_ocr_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intended = "  };"
    backend = _ambiguous_dense_typing_backend(monkeypatch)

    class EmptyLocalExactBlindOCR:
        def __init__(self) -> None:
            self.fallback_calls = 0

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path
            if region is not None and region.y >= 200:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="Ln 8, Col 5",
                            confidence=0.99,
                            bbox=[4, 8, 74, 20],
                        )
                    ]
                )
            return OCRResult()

        async def ocr_precise(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            return await self.ocr(image_path, region)

        async def ocr_precise_fallback(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            self.fallback_calls += 1
            return OCRResult(
                lines=[OCRLine(text="};", confidence=0.99)],
                spacing_evidence="verified",
            )

    ocr = EmptyLocalExactBlindOCR()
    result = await WatchedTyper(backend, ocr).type_text(
        intended,
        code=True,
        exact=True,
        context="editor",
    )

    assert result.status == "verified_exact"
    assert result.field_text == intended
    assert result.emitted_exactly_once is True
    assert ocr.fallback_calls == 1


def test_dense_locator_nominates_compact_glyph_only_when_requested() -> None:
    before = Image.new("RGB", (1280, 800), "#202020")
    after = before.copy()
    ImageDraw.Draw(after).rectangle(
        (44, 144, 55, 159),
        fill="#efefef",
        width=2,
    )
    before_output = io.BytesIO()
    after_output = io.BytesIO()
    before.save(before_output, format="PNG")
    after.save(after_output, format="PNG")

    assert locate_dense_changed_bbox(
        before_output.getvalue(),
        after_output.getvalue(),
        (1280, 800),
    ) is None
    compact = locate_dense_changed_bbox(
        before_output.getvalue(),
        after_output.getvalue(),
        (1280, 800),
        allow_compact=True,
    )

    assert compact is not None
    assert compact.x <= 44 < compact.x + compact.width
    assert compact.y <= 144 < compact.y + compact.height


def test_dense_candidates_keep_low_pixel_structural_glyph_beside_status_repaint() -> None:
    before = Image.new("RGB", (1280, 800), "#202020")
    after = before.copy()
    draw = ImageDraw.Draw(after)
    # Reproduce the measured 37-pixel VNC delta for an indented closing
    # bracket: 24 glyph pixels plus a 13-pixel caret. This is below the former
    # compact threshold even though the exact glyph is independently readable.
    draw.line((56, 177, 56, 190), fill="#efefef", width=1)
    draw.line((56, 177, 61, 177), fill="#efefef", width=1)
    draw.line((56, 190, 61, 190), fill="#efefef", width=1)
    draw.line((44, 178, 44, 190), fill="#efefef", width=1)
    # Notepad's Ln/Col repaint is larger and must not erase the glyph from the
    # exact-OCR candidate list merely because it sorts first.
    draw.rectangle((80, 486, 87, 495), fill="#efefef")
    draw.rectangle((104, 487, 118, 495), fill="#efefef")
    before_output = io.BytesIO()
    after_output = io.BytesIO()
    before.save(before_output, format="PNG")
    after.save(after_output, format="PNG")

    ordinary = locate_dense_changed_candidates(
        before_output.getvalue(),
        after_output.getvalue(),
        (1280, 800),
    )
    structural = locate_dense_changed_candidates(
        before_output.getvalue(),
        after_output.getvalue(),
        (1280, 800),
        allow_compact=True,
    )

    assert not any(
        region.x <= 56 < region.x + region.width
        and region.y <= 177 < region.y + region.height
        for region in ordinary
    )
    assert any(
        region.x <= 56 < region.x + region.width
        and region.y <= 177 < region.y + region.height
        for region in structural
    )


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
        (
            f"File name: {intended}\n"
            "Save as type: Text documents (*.txt)\n"
            "Hide Folders"
        ),
        intended,
        True,
    ) == intended
    assert WatchedTyper._typed_candidate(
        (
            f"File name: {intended}\n"
            "Save as type: Text documents (*.txt)\n"
            "Unexpected adjacent text"
        ),
        intended,
        True,
    ) == (
        f"File name: {intended}\n"
        "Save as type: Text documents (*.txt)\n"
        "Unexpected adjacent text"
    )
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
