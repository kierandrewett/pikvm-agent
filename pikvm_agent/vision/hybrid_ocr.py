"""Fast primary OCR with an independent precise-read evidence provider."""

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from PIL import Image

from pikvm_agent.core.models import OCRCandidate, OCRLine, OCRResult, Region
from pikvm_agent.core.ports import OCRProvider

_SECONDARY_MAX_AREA_FRAC = 0.20
_SECONDARY_MAX_WIDTH_FRAC = 0.80
_SECONDARY_MAX_HEIGHT_FRAC = 0.45
_WARMUP_MAX_WIDTH = 384
_WARMUP_MAX_HEIGHT = 160


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


def _merge_precise_evidence(
    primary: OCRResult,
    secondary: OCRResult,
    *,
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
    use_secondary = bool(
        len(aligned_secondary.lines) == 1
        and secondary_confidence is not None
        and secondary_confidence >= 0.90
        and (
            primary_confidence is None
            or secondary_confidence - primary_confidence >= 0.15
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
    return OCRResult(
        lines=selected.lines,
        alternatives=candidates,
        spacing_evidence=selected.spacing_evidence,
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
    ) -> None:
        if secondary_timeout_s <= 0:
            raise ValueError("secondary_timeout_s must be positive")
        self.primary = primary
        self.secondary = secondary
        self.secondary_timeout_s = secondary_timeout_s
        self._precise_calls = 0
        self._secondary_attempted = 0
        self._secondary_completed = 0
        self._secondary_skipped_busy = 0
        self._secondary_skipped_unbounded = 0
        self._secondary_failed_or_timed_out = 0

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
        secondary_busy = getattr(self.secondary, "busy", None)
        if callable(secondary_busy) and secondary_busy():
            self._secondary_skipped_busy += 1
            return await primary_call
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
        if isinstance(secondary_result, BaseException):
            self._secondary_failed_or_timed_out += 1
        else:
            self._secondary_completed += 1
        if isinstance(primary_result, BaseException):
            if isinstance(secondary_result, BaseException):
                raise primary_result
            return secondary_result
        if isinstance(secondary_result, BaseException):
            return primary_result
        return _merge_precise_evidence(
            primary_result,
            secondary_result,
            region=region,
        )

    async def warmup(self, image_path: Path) -> bool:
        """Warm the heavy secondary worker without delaying an exact action."""

        secondary_busy = getattr(self.secondary, "busy", None)
        if callable(secondary_busy) and secondary_busy():
            return False
        try:
            await asyncio.wait_for(
                self.secondary.ocr(
                    image_path,
                    region=_warmup_region(image_path),
                ),
                timeout=self.secondary_timeout_s,
            )
        except (TimeoutError, RuntimeError):
            return False
        return True

    def diagnostics(self) -> dict[str, int]:
        """Expose aggregate engine participation without case text."""

        return {
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
