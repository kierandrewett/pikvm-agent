"""OmniParser integration — UI element grounding.

``OmniParserClient`` is a thin HTTP client that normalizes the OmniParser
server's varied response shapes. ``OmniParserProvider`` adapts it to our
``ScreenElementProvider`` port, classifying each detection into an
``ElementKind`` and converting boxes to frame pixels. When the server is
disabled or unreachable, ``parse_elements`` returns an empty ``ElementMap`` so
the pipeline degrades to OCR-only evidence rather than failing.

``NullElementProvider`` is the default when OmniParser is off.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

import httpx
from PIL import Image
from pydantic import BaseModel

from pikvm_agent.core.models import BBox, ElementKind, ElementMap, VisualElement

log = logging.getLogger("pikvm_agent.vision.omniparser")


class OmniParserElement(BaseModel):
    id: str | None = None
    bbox: list[float] | list[int]
    text: str | None = None
    caption: str | None = None
    type: str | None = None
    confidence: float | None = None
    raw: dict[str, Any] = {}


class OmniParserResult(BaseModel):
    elements: list[OmniParserElement]
    raw: dict[str, Any]


class OmniParserClient:
    def __init__(self, base_url: str = "http://127.0.0.1:47625",
                 health_url: str | None = None, timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.health_url = health_url or f"{self.base_url}/probe"
        self.timeout_s = timeout_s

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(self.health_url)
                return resp.status_code < 500
        except Exception:  # noqa: BLE001
            return False

    async def parse_image(self, image_path: Path) -> OmniParserResult:
        image_b64 = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(f"{self.base_url}/parse", json={"image": image_b64})
            resp.raise_for_status()
            raw = resp.json()
        return self._normalize(raw)

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> OmniParserResult:
        candidates = (
            raw.get("elements")
            or raw.get("parsed_content")
            or raw.get("parsed_content_list")
            or raw.get("boxes")
            or raw.get("detections")
            or []
        )
        elements: list[OmniParserElement] = []
        for i, item in enumerate(candidates):
            if not isinstance(item, dict):
                continue
            bbox = item.get("bbox") or item.get("box") or item.get("coordinates")
            if not bbox:
                continue
            elements.append(
                OmniParserElement(
                    id=item.get("id") or f"omni_{i}",
                    bbox=bbox,
                    text=item.get("text") or item.get("content"),
                    caption=item.get("caption") or item.get("description"),
                    type=item.get("type") or item.get("label"),
                    confidence=item.get("confidence") or item.get("score"),
                    raw=item,
                )
            )
        return OmniParserResult(elements=elements, raw=raw)


def classify_kind(type_: str | None, caption: str | None, text: str | None) -> ElementKind:
    blob = " ".join(filter(None, [type_, caption, text])).lower()
    if not blob:
        return "unknown"
    if "close" in blob or "dismiss" in blob:
        return "close_button"
    if "send" in blob:
        return "send_button"
    if "button" in blob or "btn" in blob:
        return "button"
    if any(k in blob for k in ("textbox", "input", "field", "search box", "combobox")):
        return "input"
    if "editor" in blob:
        return "editor"
    if "terminal" in blob or "prompt" in blob:
        return "terminal_prompt"
    if "menu" in blob:
        return "menu_item"
    if "tab" in blob:
        return "tab"
    if "toast" in blob:
        return "toast"
    if "modal" in blob or "dialog" in blob:
        return "modal"
    if "notification" in blob:
        return "notification"
    return "unknown"


def bbox_to_pixels(bbox: list[float] | list[int], width: int, height: int) -> BBox:
    """Convert an [x1,y1,x2,y2] box (normalized 0..1 or pixel) to a pixel BBox."""
    x1, y1, x2, y2 = (float(v) for v in bbox[:4])
    if max(x1, y1, x2, y2) <= 1.0:  # normalized
        x1, y1, x2, y2 = x1 * width, y1 * height, x2 * width, y2 * height
    x, y = int(min(x1, x2)), int(min(y1, y2))
    return BBox(x=x, y=y, w=int(abs(x2 - x1)), h=int(abs(y2 - y1)))


class OmniParserProvider:
    """ScreenElementProvider backed by an OmniParser server.

    When ``required`` is False a parse failure degrades to an empty element map
    (OCR-only evidence). When ``required`` is True a failure RAISES — OmniParser
    is the primary perception and the operator must not act blind on its absence.
    """

    def __init__(self, client: OmniParserClient, *, required: bool = False) -> None:
        self._client = client
        self._required = required

    async def parse_elements(self, image_path: Path, frame_id: int, world_version: int) -> ElementMap:
        try:
            result = await self._client.parse_image(image_path)
        except Exception as exc:  # noqa: BLE001
            if self._required:
                from pikvm_agent.core.errors import BackendError

                raise BackendError(
                    f"OmniParser is required but unreachable at {self._client.base_url}: {exc}"
                ) from exc
            log.warning("OmniParser parse failed (%s); returning empty element map", exc)
            return ElementMap(frame_id=frame_id, world_version=world_version)
        with Image.open(image_path) as im:
            width, height = im.size
        elements: list[VisualElement] = []
        for i, el in enumerate(result.elements):
            elements.append(
                VisualElement(
                    id=f"e{i}",
                    frame_id=frame_id,
                    world_version=world_version,
                    bbox=bbox_to_pixels(el.bbox, width, height),
                    kind=classify_kind(el.type, el.caption, el.text),
                    text=el.text,
                    caption=el.caption,
                    confidence=el.confidence or 0.5,
                    source=["omniparser"],
                )
            )
        return ElementMap(frame_id=frame_id, world_version=world_version, elements=elements)


class NullElementProvider:
    """Default element provider when OmniParser is disabled — yields nothing."""

    async def parse_elements(self, image_path: Path, frame_id: int, world_version: int) -> ElementMap:
        return ElementMap(frame_id=frame_id, world_version=world_version)
