"""CompositeScreenParser — merge element grounding + OCR into one ElementMap.

Merge rule (from docs/PLAN.md):
  * OmniParser wins for interactable elements.
  * OCR wins for exact text.
  * An OCR box overlapping an interactable element attaches its text to that
    element (if the element has none).
  * OCR-only text is kept as non-clickable evidence (kind="text").
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pikvm_agent.core.models import BBox, ElementMap, VisualElement
from pikvm_agent.core.ports import OCRProvider, ScreenElementProvider

_INTERACTABLE = {"button", "input", "editor", "terminal_prompt", "menu_item",
                 "tab", "close_button", "send_button"}


def bbox_from_ocr(raw) -> BBox | None:
    if not raw:
        return None
    if isinstance(raw[0], (list, tuple)):  # polygon [[x,y],...]
        xs = [float(p[0]) for p in raw]
        ys = [float(p[1]) for p in raw]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    else:  # flat [x1,y1,x2,y2]
        x0, y0, x1, y1 = (float(v) for v in raw[:4])
    return BBox(x=int(min(x0, x1)), y=int(min(y0, y1)),
                w=int(abs(x1 - x0)), h=int(abs(y1 - y0)))


def iou(a: BBox, b: BBox) -> float:
    ix0, iy0 = max(a.x, b.x), max(a.y, b.y)
    ix1, iy1 = min(a.x + a.w, b.x + b.w), min(a.y + a.h, b.y + b.h)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    if inter == 0:
        return 0.0
    return inter / (a.area() + b.area() - inter)


class CompositeScreenParser:
    def __init__(self, element_provider: ScreenElementProvider, ocr_provider: OCRProvider) -> None:
        self.elements = element_provider
        self.ocr = ocr_provider

    async def parse(self, image_path: Path, frame_id: int, world_version: int) -> ElementMap:
        # Element grounding (OmniParser GPU/network) and OCR (thread/subprocess) are
        # independent reads of the same image — run them concurrently so parse latency is
        # max(omni, ocr), not omni + ocr.
        from pikvm_agent.debuglog import DEBUG

        async def _elements():
            with DEBUG.span("vision.omniparser", frame_id=frame_id) as r:
                em = await self.elements.parse_elements(image_path, frame_id, world_version)
                r(elements=len(em.elements))
                return em

        async def _ocr():
            with DEBUG.span("vision.ocr", frame_id=frame_id) as r:
                res = await self.ocr.ocr(image_path)
                r(lines=len(getattr(res, "lines", []) or []))
                return res

        em, ocr = await asyncio.gather(_elements(), _ocr())
        merged: list[VisualElement] = list(em.elements)
        source_tag = self._ocr_source()

        next_idx = len(merged)
        for line in ocr.lines:
            box = bbox_from_ocr(line.bbox)
            if box is None:
                continue
            attached = False
            for el in merged:
                if el.kind in _INTERACTABLE and iou(box, el.bbox) > 0.3:
                    # OCR text is authoritative for an interactable element's label.
                    # OmniParser/Florence captions for icons are frequently
                    # hallucinated (a calendar cell -> "14 September", an icon ->
                    # "Skype"), so when real OCR overlaps, it WINS — and the prior
                    # OmniParser guess is demoted to a low-trust `caption` hint
                    # (never discarded, so a label still survives when OCR can't
                    # reach it, e.g. box-less PiKVM OCR or an OCR miss).
                    if el.text and el.text != line.text and not el.caption:
                        el.caption = el.text
                    el.text = line.text
                    if source_tag not in el.source:
                        el.source.append(source_tag)
                    attached = True
                    break
            if attached:
                continue
            merged.append(
                VisualElement(
                    id=f"e{next_idx}",
                    frame_id=frame_id,
                    world_version=world_version,
                    bbox=box,
                    kind="text",
                    text=line.text,
                    confidence=line.confidence or 0.5,
                    source=[source_tag],
                )
            )
            next_idx += 1

        return ElementMap(
            frame_id=frame_id, world_version=world_version, elements=merged, ocr_text=ocr.text
        )

    def _ocr_source(self) -> str:
        name = type(self.ocr).__name__.lower()
        if "paddle" in name:
            return "paddleocr"
        if "tesseract" in name:
            return "tesseract"
        if "pikvm" in name:
            return "pikvm_ocr"
        return "ocr"
