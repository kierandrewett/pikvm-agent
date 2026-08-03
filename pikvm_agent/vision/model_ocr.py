"""Blind multimodal OCR consensus for exact-readback fallback.

The model never receives the intended text. Three independent, tool-disabled
transcriptions see the same context-preserving frame with an external locator;
at least two must return the identical high-confidence string before this
provider emits any OCR evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import tempfile
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageDraw, ImageOps
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pikvm_agent.core.models import OCRLine, OCRResult, Region
from pikvm_agent.harness.agent_models import ModelRequest, ModelResponse


class _ModelProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class _BlindTranscription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    uncertain_characters: list[int] = Field(default_factory=list)


_PROMPT = (
    "Blind OCR task. Transcribe every visible character inside the red "
    "rectangle in the upper context image exactly from left to right. The "
    "lower panel is an enlarged contextual copy; only its inner red rectangle "
    "is the same exact target. The rectangles are only locators and are not "
    "text. Preserve case and punctuation. Do not "
    "normalize, autocorrect, complete, or infer a likely value. Return "
    "zero-based character positions that are genuinely unclear. Distinguish "
    "straight from curly quotation marks and distinguish the visible glyphs "
    "hyphen-minus (-), en dash (–), and em dash (—); never substitute one for "
    "another. In monospaced editor text, a horizontal dash that spans most of "
    "one character cell is an em dash, even when the font renders it shorter "
    "than its typographic name suggests. In editors, visual line wrapping is "
    "layout, not a newline character: join soft-wrapped rows without adding "
    "or removing a visible space. Preserve run-together words exactly; never "
    "insert a plausible missing space."
)
_LOCATOR_PADDING = 12
_LOCATOR_WIDTH = 2
_DETAIL_PADDING = 6
_DETAIL_SCALE = 6
_PANEL_GAP = 12
_CACHE_LIMIT = 16


class BlindModelOcrProvider:
    """Return only a two-of-three blind model transcription consensus."""

    def __init__(
        self,
        provider: _ModelProvider,
        *,
        minimum_confidence: float = 0.97,
        samples: int = 3,
    ) -> None:
        if not 0.0 <= minimum_confidence <= 1.0:
            raise ValueError("minimum_confidence must be between 0 and 1")
        if samples < 2:
            raise ValueError("samples must be at least 2")
        self.provider = provider
        self.minimum_confidence = minimum_confidence
        self.samples = samples
        self._cache: OrderedDict[str, OCRResult] = OrderedDict()
        self._cache_lock = asyncio.Lock()

    async def ocr(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        del image_path, region
        return OCRResult()

    @staticmethod
    def _cache_key(image_path: Path, region: Region | None) -> str:
        digest = hashlib.sha256(image_path.read_bytes())
        if region is None:
            digest.update(b":full")
        else:
            digest.update(
                (
                    f":{region.x}:{region.y}:"
                    f"{region.width}:{region.height}"
                ).encode()
            )
        return digest.hexdigest()

    @staticmethod
    def _annotated_frame(
        image_path: Path,
        region: Region,
    ) -> Path:
        with Image.open(image_path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
        left = max(0, math.floor(region.x) - _LOCATOR_PADDING)
        top = max(0, math.floor(region.y) - _LOCATOR_PADDING)
        right = min(
            image.width - 1,
            math.ceil(region.x + region.width) + _LOCATOR_PADDING,
        )
        bottom = min(
            image.height - 1,
            math.ceil(region.y + region.height) + _LOCATOR_PADDING,
        )
        ImageDraw.Draw(image).rectangle(
            (left, top, right, bottom),
            outline=(255, 64, 64),
            width=_LOCATOR_WIDTH,
        )
        detail_left = max(0, math.floor(region.x) - _DETAIL_PADDING)
        detail_top = max(0, math.floor(region.y) - _DETAIL_PADDING)
        detail_right = min(
            image.width,
            math.ceil(region.x + region.width) + _DETAIL_PADDING,
        )
        detail_bottom = min(
            image.height,
            math.ceil(region.y + region.height) + _DETAIL_PADDING,
        )
        with Image.open(image_path) as detail_source:
            detail = ImageOps.exif_transpose(
                detail_source
            ).convert("RGB").crop(
                (
                    detail_left,
                    detail_top,
                    detail_right,
                    detail_bottom,
                )
            )
        detail = detail.resize(
            (
                detail.width * _DETAIL_SCALE,
                detail.height * _DETAIL_SCALE,
            ),
            Image.Resampling.LANCZOS,
        )
        canvas = Image.new(
            "RGB",
            (
                max(image.width, detail.width + _PANEL_GAP * 2),
                image.height + detail.height + _PANEL_GAP * 2,
            ),
            (20, 20, 20),
        )
        canvas.paste(image, (0, 0))
        canvas.paste(detail, (_PANEL_GAP, image.height + _PANEL_GAP))
        target_left = max(detail_left, math.floor(region.x))
        target_top = max(detail_top, math.floor(region.y))
        target_right = min(
            detail_right,
            math.ceil(region.x + region.width),
        )
        target_bottom = min(
            detail_bottom,
            math.ceil(region.y + region.height),
        )
        ImageDraw.Draw(canvas).rectangle(
            (
                _PANEL_GAP
                + (target_left - detail_left) * _DETAIL_SCALE,
                image.height
                + _PANEL_GAP
                + (target_top - detail_top) * _DETAIL_SCALE,
                _PANEL_GAP
                + (target_right - detail_left) * _DETAIL_SCALE,
                image.height
                + _PANEL_GAP
                + (target_bottom - detail_top) * _DETAIL_SCALE,
            ),
            outline=(255, 64, 64),
            width=_LOCATOR_WIDTH,
        )
        detail.close()
        image.close()
        image = canvas
        temporary = tempfile.NamedTemporaryFile(
            suffix=".png",
            delete=False,
        )
        path = Path(temporary.name)
        try:
            image.save(temporary, format="PNG", optimize=True)
        finally:
            temporary.close()
            image.close()
        return path

    async def _transcribe(
        self,
        *,
        image_path: Path,
        sample: int,
        cache_key: str,
    ) -> _BlindTranscription | None:
        try:
            response = await self.provider.complete(
                ModelRequest(
                    role="verifier",
                    prompt=_PROMPT,
                    output_schema=_BlindTranscription.model_json_schema(),
                    image_path=str(image_path),
                    run_id=f"blind-ocr-{cache_key[:16]}-{sample}",
                    metadata={"image_detail": "original"},
                )
            )
            return _BlindTranscription.model_validate(response.data)
        except (RuntimeError, ValidationError):
            return None

    async def ocr_precise(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        if region is None or region.width <= 0 or region.height <= 0:
            return OCRResult()
        key = await asyncio.to_thread(
            self._cache_key,
            Path(image_path),
            region,
        )
        async with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached.model_copy(deep=True)

            annotated = await asyncio.to_thread(
                self._annotated_frame,
                Path(image_path),
                region,
            )
            try:
                values = list(
                    await asyncio.gather(
                        *(
                            self._transcribe(
                                image_path=annotated,
                                sample=sample,
                                cache_key=key,
                            )
                            for sample in range(min(2, self.samples))
                        )
                    )
                )
                first_counts = Counter(
                    value.text
                    for value in values
                    if (
                        value is not None
                        and value.text
                        and value.confidence >= self.minimum_confidence
                        and not value.uncertain_characters
                    )
                )
                if (
                    not first_counts
                    or first_counts.most_common(1)[0][1] < 2
                ):
                    values.extend(
                        await asyncio.gather(
                            *(
                                self._transcribe(
                                    image_path=annotated,
                                    sample=sample,
                                    cache_key=key,
                                )
                                for sample in range(2, self.samples)
                            )
                        )
                    )
            finally:
                annotated.unlink(missing_ok=True)

            eligible = [
                value
                for value in values
                if (
                    value is not None
                    and value.text
                    and value.confidence >= self.minimum_confidence
                    and not value.uncertain_characters
                )
            ]
            counts = Counter(value.text for value in eligible)
            text, count = counts.most_common(1)[0] if counts else ("", 0)
            if count < 2:
                result = OCRResult()
            else:
                confidence = min(
                    value.confidence
                    for value in eligible
                    if value.text == text
                )
                result = OCRResult(
                    lines=[
                        OCRLine(
                            text=text,
                            confidence=confidence,
                            bbox=[
                                math.floor(region.x),
                                math.floor(region.y),
                                math.ceil(region.x + region.width),
                                math.ceil(region.y + region.height),
                            ],
                        )
                    ],
                    spacing_evidence="verified",
                )
            self._cache[key] = result
            self._cache.move_to_end(key)
            while len(self._cache) > _CACHE_LIMIT:
                self._cache.popitem(last=False)
            return result.model_copy(deep=True)

    async def aclose(self) -> None:
        closer = getattr(self.provider, "aclose", None)
        if callable(closer):
            await closer()


class PreciseFallbackOcrProvider:
    """Keep normal/local OCR fast and expose a separate blind precise fallback."""

    def __init__(
        self,
        primary: object,
        fallback: BlindModelOcrProvider,
    ) -> None:
        self.primary = primary
        self.fallback = fallback

    async def ocr(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        return await self.primary.ocr(image_path, region=region)

    async def ocr_precise(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        precise = getattr(self.primary, "ocr_precise", None)
        if callable(precise):
            return await precise(image_path, region=region)
        return await self.primary.ocr(image_path, region=region)

    async def ocr_precise_fallback(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        return await self.fallback.ocr_precise(
            image_path,
            region=region,
        )

    async def warmup(self, image_path: Path) -> bool:
        warmup = getattr(self.primary, "warmup", None)
        if not callable(warmup):
            return False
        return bool(await warmup(image_path))

    def diagnostics(self) -> dict[str, int]:
        diagnostics = getattr(self.primary, "diagnostics", None)
        return dict(diagnostics()) if callable(diagnostics) else {}

    async def aclose(self) -> None:
        seen: set[int] = set()
        for provider in (self.primary, self.fallback):
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            closer = getattr(provider, "aclose", None)
            if callable(closer):
                await closer()
