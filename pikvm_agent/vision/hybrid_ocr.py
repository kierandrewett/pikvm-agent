"""Fast primary OCR with an independent precise-read evidence provider."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from pikvm_agent.core.models import OCRCandidate, OCRResult, Region
from pikvm_agent.core.ports import OCRProvider


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


def _merge_precise_evidence(
    primary: OCRResult,
    secondary: OCRResult,
) -> OCRResult:
    """Keep primary boxes while retaining every unique independent read."""

    candidates: list[OCRCandidate] = []
    seen = {primary.text}
    for candidate in primary.alternatives:
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
        text=secondary.text,
        mean_confidence=_mean_confidence(secondary),
    )
    for candidate in secondary.alternatives:
        _append_candidate(
            candidates,
            seen,
            text=candidate.text,
            mean_confidence=candidate.mean_confidence,
            evidence_kind=candidate.evidence_kind,
        )
    return OCRResult(
        lines=primary.lines,
        alternatives=candidates,
        spacing_evidence=primary.spacing_evidence,
    )


class HybridOcrProvider:
    """Use a fast primary normally and both engines for exact read-back.

    The secondary engine supplies evidence only. It cannot select text or
    authorize a follow-up action. Known-intent verification compares every
    independent candidate against the intended text and otherwise abstains.
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
        )

    def diagnostics(self) -> dict[str, int]:
        """Expose aggregate engine participation without case text."""

        return {
            "precise_calls": self._precise_calls,
            "secondary_attempted": self._secondary_attempted,
            "secondary_completed": self._secondary_completed,
            "secondary_skipped_busy": self._secondary_skipped_busy,
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
