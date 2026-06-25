"""PiKVMOcrProvider — OCR via PiKVM's built-in tesseract endpoint.

Zero local cost: the Pi OCRs the live frame server-side. It returns plain text
(no per-word boxes), so it contributes ``ocr_text`` evidence but not positioned
elements. Because it reads the LIVE screen (not a saved file), ``image_path`` is
ignored — use it right after a capture so live ≈ the frame you parsed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pikvm_agent.core.models import OCRLine, OCRResult, Region


class PiKVMOcrProvider:
    def __init__(self, backend: Any, langs: str = "eng") -> None:
        self._backend = backend
        self._langs = langs

    async def ocr(self, image_path: Path | None = None, region: Region | None = None) -> OCRResult:
        text = await self._backend.ocr(region, self._langs)
        lines = [OCRLine(text=ln) for ln in text.splitlines() if ln.strip()]
        return OCRResult(lines=lines)
