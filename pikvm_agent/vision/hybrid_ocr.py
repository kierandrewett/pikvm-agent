"""Fast primary OCR with an independent precise-read evidence provider."""

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image

from pikvm_agent.core.models import OCRCandidate, OCRLine, OCRResult, Region
from pikvm_agent.core.ports import OCRProvider

_SECONDARY_MAX_AREA_FRAC = 0.20
_SECONDARY_MAX_WIDTH_FRAC = 0.80
_SECONDARY_MAX_HEIGHT_FRAC = 0.45
_WARMUP_MAX_WIDTH = 384
_WARMUP_MAX_HEIGHT = 160
_SECONDARY_SELECTION_MIN_CONFIDENCE = 0.85
_SECONDARY_MIN_CONFIDENCE = 0.90
_GEOMETRIC_SPACING_MIN_CONFIDENCE = 0.90
_SECONDARY_SINGLE_LINE_ADVANTAGE = 0.08
_SECONDARY_MULTILINE_ADVANTAGE = 0.20


def _image_size(image_path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(image_path) as image:
            return image.size
    except (OSError, ValueError):
        return None


def _secondary_region_is_bounded(
    image_path: Path,
    region: Region | None,
) -> bool:
    """Keep the heavyweight engine away from whole-screen OCR."""

    size = _image_size(image_path)
    if size is None:
        # Synthetic providers and compatibility fixtures may not be real images.
        return True
    if region is None:
        return False
    width, height = size
    if width <= 0 or height <= 0:
        return False
    return (
        region.width * region.height
        <= width * height * _SECONDARY_MAX_AREA_FRAC
        and region.width <= width * _SECONDARY_MAX_WIDTH_FRAC
        and region.height <= height * _SECONDARY_MAX_HEIGHT_FRAC
    )


def _warmup_region(image_path: Path) -> Region | None:
    size = _image_size(image_path)
    if size is None:
        return None
    width, height = size
    crop_width = min(width, _WARMUP_MAX_WIDTH)
    crop_height = min(height, _WARMUP_MAX_HEIGHT)
    return Region(
        x=max(0, (width - crop_width) // 2),
        y=max(0, (height - crop_height) // 2),
        width=crop_width,
        height=crop_height,
    )


def _mean_confidence(result: OCRResult) -> float | None:
    confidences = [
        float(line.confidence)
        for line in result.lines
        if line.confidence is not None
    ]
    if not confidences:
        return None
    return sum(confidences) / len(confidences)


def _append_candidate(
    candidates: list[OCRCandidate],
    seen: set[str],
    *,
    text: str,
    mean_confidence: float | None,
    evidence_kind: Literal["generic", "spacing"] = "generic",
) -> None:
    if not text or text in seen:
        return
    seen.add(text)
    candidates.append(
        OCRCandidate(
            text=text,
            mean_confidence=mean_confidence,
            evidence_kind=evidence_kind,
        )
    )


def _line_rectangle(
    line: OCRLine,
) -> tuple[float, float, float, float] | None:
    box = line.bbox
    if not isinstance(box, list) or len(box) < 4:
        return None
    if all(isinstance(value, (int, float)) for value in box[:4]):
        x1, y1, x2, y2 = (float(value) for value in box[:4])
        return (x1, y1, x2, y2) if x2 > x1 and y2 > y1 else None
    points = [
        point
        for point in box
        if (
            isinstance(point, list)
            and len(point) >= 2
            and isinstance(point[0], (int, float))
            and isinstance(point[1], (int, float))
        )
    ]
    if len(points) < 2:
        return None
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    rectangle = (min(xs), min(ys), max(xs), max(ys))
    return (
        rectangle
        if rectangle[2] > rectangle[0] and rectangle[3] > rectangle[1]
        else None
    )


def _aligned_secondary_row(
    primary: OCRResult,
    secondary: OCRResult,
    *,
    region: Region | None,
) -> OCRResult:
    """Isolate one independently aligned row from a noisy secondary crop."""

    if len(primary.lines) != 1 or len(secondary.lines) <= 1:
        return secondary
    primary_line = primary.lines[0]
    primary_rectangle = _line_rectangle(primary_line)
    central_matches: list[OCRLine] = []
    if region is not None and region.height > 0:
        region_midpoint = region.height / 2
        for line in secondary.lines:
            rectangle = _line_rectangle(line)
            confidence = (
                float(line.confidence)
                if line.confidence is not None
                else 0.0
            )
            if rectangle is None or confidence < 0.90:
                continue
            _x1, y1, _x2, y2 = rectangle
            if (
                abs(((y1 + y2) / 2) - region_midpoint)
                <= region.height * 0.25
                and y2 - y1 <= region.height * 0.60
            ):
                central_matches.append(line)
    if len(central_matches) == 1:
        return OCRResult(
            lines=central_matches,
            alternatives=secondary.alternatives,
            spacing_evidence=secondary.spacing_evidence,
        )

    matches: list[OCRLine] = []
    if primary_rectangle is not None and primary_line.text.strip():
        px1, py1, px2, py2 = primary_rectangle
        for line in secondary.lines:
            rectangle = _line_rectangle(line)
            confidence = (
                float(line.confidence)
                if line.confidence is not None
                else 0.0
            )
            if (
                rectangle is None
                or confidence < 0.90
                or SequenceMatcher(
                    None,
                    primary_line.text.strip().casefold(),
                    line.text.strip().casefold(),
                    autojunk=False,
                ).ratio()
                < 0.75
            ):
                continue
            sx1, sy1, sx2, sy2 = rectangle
            horizontal_overlap = max(
                0.0,
                min(px2, sx2) - max(px1, sx1),
            )
            vertical_overlap = max(
                0.0,
                min(py2, sy2) - max(py1, sy1),
            )
            if (
                horizontal_overlap
                >= min(px2 - px1, sx2 - sx1) * 0.60
                and vertical_overlap
                >= min(py2 - py1, sy2 - sy1) * 0.50
            ):
                matches.append(line)
    if len(matches) != 1:
        return secondary
    return OCRResult(
        lines=matches,
        alternatives=secondary.alternatives,
        spacing_evidence=secondary.spacing_evidence,
    )


def _has_aligned_single_space_consensus(
    selected: OCRResult,
    other: OCRResult,
) -> bool:
    """Accept one ordinary space only when two engines independently agree."""

    if len(selected.lines) != 1:
        return False
    selected_line = selected.lines[0]
    if (
        selected_line.text.count(" ") != 1
        or selected_line.text.strip() != selected_line.text
        or any(character in selected_line.text for character in "\t\r\n")
        or selected_line.confidence is None
        or float(selected_line.confidence) < _SECONDARY_MIN_CONFIDENCE
    ):
        return False
    selected_rectangle = _line_rectangle(selected_line)
    if selected_rectangle is None:
        return False

    matches = [
        line
        for line in other.lines
        if (
            line.text == selected_line.text
            and line.confidence is not None
            and float(line.confidence) >= 0.75
        )
    ]
    if len(matches) != 1:
        return False
    other_rectangle = _line_rectangle(matches[0])
    if other_rectangle is None:
        return False

    sx1, sy1, sx2, sy2 = selected_rectangle
    ox1, oy1, ox2, oy2 = other_rectangle
    horizontal_overlap = max(0.0, min(sx2, ox2) - max(sx1, ox1))
    vertical_overlap = max(0.0, min(sy2, oy2) - max(sy1, oy1))
    return (
        horizontal_overlap >= min(sx2 - sx1, ox2 - ox1) * 0.80
        and vertical_overlap >= min(sy2 - sy1, oy2 - oy1) * 0.60
    )


def _has_visible_single_space_gap(
    image_path: Path,
    region: Region | None,
    selected: OCRResult,
) -> bool:
    """Confirm one OCR space from an independently visible glyph gap."""

    if len(selected.lines) != 1:
        return False
    line = selected.lines[0]
    tokens = line.text.split(" ")
    if (
        len(tokens) != 2
        or not all(token.isalnum() for token in tokens)
        or line.confidence is None
        or float(line.confidence) < _GEOMETRIC_SPACING_MIN_CONFIDENCE
    ):
        return False
    rectangle = _line_rectangle(line)
    if rectangle is None:
        return False
    x1, y1, x2, y2 = rectangle
    offset_x = float(region.x) if region is not None else 0.0
    offset_y = float(region.y) if region is not None else 0.0
    try:
        with Image.open(image_path) as image:
            left = max(0, round(offset_x + x1))
            top = max(0, round(offset_y + y1))
            right = min(image.width, round(offset_x + x2))
            bottom = min(image.height, round(offset_y + y2))
            if right - left < 8 or bottom - top < 6:
                return False
            grayscale = np.asarray(
                image.convert("L").crop((left, top, right, bottom)),
                dtype=np.float32,
            )
    except (OSError, ValueError):
        return False

    background = float(np.median(grayscale))
    ink = np.abs(grayscale - background) >= 28
    minimum_column_ink = max(2, round(grayscale.shape[0] * 0.12))
    active = np.count_nonzero(ink, axis=0) >= minimum_column_ink
    if active.size < 3:
        return False
    active[0] = False
    active[-1] = False
    active_columns = np.flatnonzero(active)
    if active_columns.size < 4:
        return False

    gaps = [
        (int(right_column - left_column - 1), left_column, right_column)
        for left_column, right_column in zip(
            active_columns,
            active_columns[1:],
            strict=False,
        )
        if right_column > left_column + 1
    ]
    if not gaps:
        return False
    gaps.sort(reverse=True)
    largest, gap_left, gap_right = gaps[0]
    runner_up = gaps[1][0] if len(gaps) > 1 else 0
    line_height = grayscale.shape[0]
    if (
        largest < max(2, round(line_height * 0.12))
        or largest > max(3, round(line_height * 0.40))
        or largest <= runner_up
    ):
        return False

    ink_left = int(active_columns[0])
    ink_right = int(active_columns[-1])
    ink_width = ink_right - ink_left
    if ink_width <= 0:
        return False
    observed_gap_position = (
        ((gap_left + gap_right) / 2) - ink_left
    ) / ink_width
    expected_gap_position = len(tokens[0]) / sum(map(len, tokens))
    return abs(observed_gap_position - expected_gap_position) <= 0.18


def _with_visible_spacing_evidence(
    result: OCRResult,
    *,
    image_path: Path,
    region: Region | None,
) -> OCRResult:
    """Retain bounded pixel-gap proof when the second engine is unavailable."""

    if (
        region is None
        or result.spacing_evidence == "verified"
    ):
        return result
    if _has_visible_single_space_gap(image_path, region, result):
        return OCRResult(
            lines=result.lines,
            alternatives=result.alternatives,
            spacing_evidence="verified",
        )

    alternatives = list(result.alternatives)
    known_spacing = {
        candidate.text
        for candidate in alternatives
        if candidate.evidence_kind == "spacing"
    }
    for line in result.lines:
        isolated = OCRResult(lines=[line])
        if (
            line.text not in known_spacing
            and _has_visible_single_space_gap(
                image_path,
                region,
                isolated,
            )
        ):
            alternatives.append(
                OCRCandidate(
                    text=line.text,
                    mean_confidence=(
                        float(line.confidence)
                        if line.confidence is not None
                        else None
                    ),
                    evidence_kind="spacing",
                )
            )
            known_spacing.add(line.text)
    if alternatives == result.alternatives:
        return result
    return OCRResult(
        lines=result.lines,
        alternatives=alternatives,
        spacing_evidence=result.spacing_evidence,
    )


def _merge_precise_evidence(
    primary: OCRResult,
    secondary: OCRResult,
    *,
    image_path: Path,
    region: Region | None,
) -> OCRResult:
    """Select an engine without ground truth and retain the other as evidence.

    A high-confidence, single-line secondary read may replace a low-confidence
    primary row. The choice depends only on OCR geometry/confidence, never the
    intended string, so later exact-text comparison remains independent.
    """

    aligned_secondary = _aligned_secondary_row(
        primary,
        secondary,
        region=region,
    )
    primary_confidence = _mean_confidence(primary)
    secondary_confidence = _mean_confidence(aligned_secondary)
    confidence_advantage = (
        None
        if primary_confidence is None or secondary_confidence is None
        else secondary_confidence - primary_confidence
    )
    use_secondary = bool(
        secondary_confidence is not None
        and secondary_confidence >= _SECONDARY_SELECTION_MIN_CONFIDENCE
        and (
            primary_confidence is None
            or (
                len(aligned_secondary.lines) == 1
                and confidence_advantage is not None
                and confidence_advantage
                >= _SECONDARY_SINGLE_LINE_ADVANTAGE
            )
            or (
                region is not None
                and len(aligned_secondary.lines) > 1
                and confidence_advantage is not None
                and confidence_advantage
                >= _SECONDARY_MULTILINE_ADVANTAGE
            )
        )
    )
    selected = aligned_secondary if use_secondary else primary
    other = primary if use_secondary else secondary
    candidates: list[OCRCandidate] = []
    seen = {selected.text}
    for candidate in selected.alternatives:
        _append_candidate(
            candidates,
            seen,
            text=candidate.text,
            mean_confidence=candidate.mean_confidence,
            evidence_kind=candidate.evidence_kind,
        )
    _append_candidate(
        candidates,
        seen,
        text=other.text,
        mean_confidence=_mean_confidence(other),
    )
    for candidate in other.alternatives:
        _append_candidate(
            candidates,
            seen,
            text=candidate.text,
            mean_confidence=candidate.mean_confidence,
            evidence_kind=candidate.evidence_kind,
        )
    spacing_evidence = selected.spacing_evidence
    if (
        spacing_evidence != "verified"
        and (
            _has_aligned_single_space_consensus(selected, other)
            or _has_visible_single_space_gap(
                image_path,
                region,
                selected,
            )
        )
    ):
        spacing_evidence = "verified"
    merged = OCRResult(
        lines=selected.lines,
        alternatives=candidates,
        spacing_evidence=spacing_evidence,
    )
    return _with_visible_spacing_evidence(
        merged,
        image_path=image_path,
        region=region,
    )


class HybridOcrProvider:
    """Use a fast primary normally and both engines for exact read-back.

    The provider selects a canonical read using engine confidence and geometry
    only. It never sees the intended text; the verifier remains the sole place
    that can authorize a follow-up action.
    """

    def __init__(
        self,
        primary: OCRProvider,
        secondary: OCRProvider,
        *,
        secondary_timeout_s: float = 5.0,
        warmup_timeout_s: float | None = None,
    ) -> None:
        if secondary_timeout_s <= 0:
            raise ValueError("secondary_timeout_s must be positive")
        if warmup_timeout_s is not None and warmup_timeout_s <= 0:
            raise ValueError("warmup_timeout_s must be positive")
        self.primary = primary
        self.secondary = secondary
        self.secondary_timeout_s = secondary_timeout_s
        self.warmup_timeout_s = (
            warmup_timeout_s
            if warmup_timeout_s is not None
            else max(60.0, secondary_timeout_s * 4)
        )
        self._warmup_started = False
        self._warmup_complete = asyncio.Event()
        self._warmup_succeeded = False
        self._warmup_timed_out = 0
        self._precise_waited_for_warmup = 0
        self._precise_calls = 0
        self._secondary_attempted = 0
        self._secondary_completed = 0
        self._secondary_skipped_busy = 0
        self._secondary_skipped_unbounded = 0
        self._secondary_failed_or_timed_out = 0
        self._secondary_timeout_restarts = 0
        self._secondary_timeout_retries = 0

    async def ocr(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        """Keep ordinary screen parsing on the low-latency primary engine."""

        return await self.primary.ocr(image_path, region=region)

    async def ocr_precise(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        """Run independent reads concurrently and retain their candidates."""

        self._precise_calls += 1
        primary_precise = getattr(self.primary, "ocr_precise", None)
        primary_call = (
            primary_precise(image_path, region=region)
            if callable(primary_precise)
            else self.primary.ocr(image_path, region=region)
        )
        if not _secondary_region_is_bounded(image_path, region):
            self._secondary_skipped_unbounded += 1
            return await primary_call
        if self._warmup_started and not self._warmup_complete.is_set():
            self._precise_waited_for_warmup += 1
            try:
                await asyncio.wait_for(
                    self._warmup_complete.wait(),
                    timeout=self.warmup_timeout_s + 1,
                )
            except TimeoutError:
                pass
        secondary_busy = getattr(self.secondary, "busy", None)
        if callable(secondary_busy) and secondary_busy():
            self._secondary_skipped_busy += 1
            return _with_visible_spacing_evidence(
                await primary_call,
                image_path=image_path,
                region=region,
            )
        self._secondary_attempted += 1
        primary_result, secondary_result = await asyncio.gather(
            primary_call,
            asyncio.wait_for(
                self.secondary.ocr(image_path, region=region),
                timeout=self.secondary_timeout_s,
            ),
            return_exceptions=True,
        )
        if isinstance(primary_result, asyncio.CancelledError):
            raise primary_result
        if isinstance(secondary_result, asyncio.CancelledError):
            raise secondary_result
        if isinstance(secondary_result, TimeoutError):
            restart = getattr(
                self.secondary,
                "restart_after_timeout",
                None,
            )
            if callable(restart):
                self._secondary_timeout_restarts += 1
                try:
                    await restart()
                    self._secondary_timeout_retries += 1
                    secondary_result = await asyncio.wait_for(
                        self.secondary.ocr(image_path, region=region),
                        timeout=self.secondary_timeout_s,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    secondary_result = exc
        if isinstance(secondary_result, BaseException):
            self._secondary_failed_or_timed_out += 1
        else:
            self._secondary_completed += 1
        if isinstance(primary_result, BaseException):
            if isinstance(secondary_result, BaseException):
                raise primary_result
            return _with_visible_spacing_evidence(
                secondary_result,
                image_path=image_path,
                region=region,
            )
        if isinstance(secondary_result, BaseException):
            return _with_visible_spacing_evidence(
                primary_result,
                image_path=image_path,
                region=region,
            )
        return _merge_precise_evidence(
            primary_result,
            secondary_result,
            image_path=image_path,
            region=region,
        )

    async def warmup(self, image_path: Path) -> bool:
        """Warm the heavy secondary worker without delaying an exact action."""

        self._warmup_started = True
        self._warmup_succeeded = False
        self._warmup_complete.clear()
        secondary_busy = getattr(self.secondary, "busy", None)
        if callable(secondary_busy) and secondary_busy():
            self._warmup_complete.set()
            return False
        try:
            await asyncio.wait_for(
                self.secondary.ocr(
                    image_path,
                    region=_warmup_region(image_path),
                ),
                timeout=self.warmup_timeout_s,
            )
        except TimeoutError:
            self._warmup_timed_out += 1
            restart = getattr(
                self.secondary,
                "restart_after_timeout",
                None,
            )
            if callable(restart):
                try:
                    await restart()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            return False
        except RuntimeError:
            return False
        else:
            self._warmup_succeeded = True
            return True
        finally:
            self._warmup_complete.set()

    def diagnostics(self) -> dict[str, int]:
        """Expose aggregate engine participation without case text."""

        return {
            "warmup_started": int(self._warmup_started),
            "warmup_succeeded": int(self._warmup_succeeded),
            "warmup_timed_out": self._warmup_timed_out,
            "precise_waited_for_warmup": self._precise_waited_for_warmup,
            "precise_calls": self._precise_calls,
            "secondary_attempted": self._secondary_attempted,
            "secondary_completed": self._secondary_completed,
            "secondary_skipped_busy": self._secondary_skipped_busy,
            "secondary_skipped_unbounded": (
                self._secondary_skipped_unbounded
            ),
            "secondary_failed_or_timed_out": (
                self._secondary_failed_or_timed_out
            ),
            "secondary_timeout_restarts": (
                self._secondary_timeout_restarts
            ),
            "secondary_timeout_retries": (
                self._secondary_timeout_retries
            ),
        }

    async def aclose(self) -> None:
        """Close any provider-owned native workers without double-closing."""

        seen: set[int] = set()
        for provider in (self.primary, self.secondary):
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            closer = getattr(provider, "aclose", None)
            if callable(closer):
                await closer()
