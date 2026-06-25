"""PaddleOCRProvider — optional local PP-OCRv5 OCR (the ``[vision]`` extra).

PaddleOCR is imported lazily so the package installs and runs without it. It
produces text, confidence, and boxes; our verifier classifies the result —
PaddleOCR never decides whether typing succeeded.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pikvm_agent.core.models import OCRLine, OCRResult, Region


def paddleocr_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("paddleocr") is not None


class PaddleOCRProvider:
    def __init__(self, lang: str = "en", device: str | None = None) -> None:
        from paddleocr import PaddleOCR  # lazy: only when actually used

        kwargs: dict[str, Any] = {
            "lang": lang,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
        }
        if device:
            kwargs["device"] = device
        self._ocr = PaddleOCR(**kwargs)

    def _predict(self, image_path: Path) -> OCRResult:
        output = self._ocr.predict(str(image_path))
        lines: list[OCRLine] = []
        for res in output:
            if hasattr(res, "json") and callable(res.json):
                data = res.json()
            elif hasattr(res, "to_json") and callable(res.to_json):
                data = res.to_json()
            else:
                data = getattr(res, "res", None) or {}
            raw_res = data.get("res", data) if isinstance(data, dict) else {}
            texts = raw_res.get("rec_texts") or []
            scores = raw_res.get("rec_scores") or []
            boxes = raw_res.get("rec_boxes") or raw_res.get("rec_polys") or []
            for i, text in enumerate(texts):
                box = boxes[i] if i < len(boxes) else None
                if hasattr(box, "tolist"):
                    box = box.tolist()
                lines.append(
                    OCRLine(
                        text=str(text),
                        confidence=float(scores[i]) if i < len(scores) else None,
                        bbox=box,
                    )
                )
        return OCRResult(lines=lines)

    async def ocr(self, image_path: Path, region: Region | None = None) -> OCRResult:
        # Region cropping is left to the caller's saved crop; PaddleOCR runs on the
        # whole image. Heavy + synchronous, so push off the event loop.
        return await asyncio.to_thread(self._predict, Path(image_path))
