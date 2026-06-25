"""Set-of-marks overlay — a debug image with numbered boxes over each element.

Purely diagnostic: it visualises the ElementMap the operator is shown (the
element ids it must choose between). It never affects control flow.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from pikvm_agent.core.models import ElementMap

_KIND_COLOR = {
    "button": (80, 180, 250),
    "send_button": (250, 120, 80),
    "close_button": (250, 90, 90),
    "input": (120, 230, 140),
    "editor": (160, 200, 120),
    "terminal_prompt": (200, 200, 120),
    "menu_item": (200, 150, 240),
    "tab": (140, 200, 240),
    "modal": (250, 80, 160),
    "toast": (250, 200, 90),
    "notification": (250, 180, 90),
    "text": (150, 160, 175),
    "unknown": (170, 170, 170),
}


def _font(size: int = 13):
    from pikvm_agent.vision.tesseract_ocr import _readable_font

    return _readable_font(size)


def draw_set_of_marks(image_path: str | Path, element_map: ElementMap,
                      out_path: str | Path | None = None) -> Path:
    src = Path(image_path)
    out = Path(out_path) if out_path is not None else src.with_suffix(".marks.png")
    img = Image.open(src).convert("RGB")
    draw = ImageDraw.Draw(img)
    font = _font()
    for el in element_map.elements:
        b = el.bbox
        color = _KIND_COLOR.get(el.kind, _KIND_COLOR["unknown"])
        draw.rectangle([b.x, b.y, b.x + b.w, b.y + b.h], outline=color, width=2)
        label = el.id
        tx, ty = b.x + 1, max(0, b.y - 15)
        draw.rectangle([tx, ty, tx + 8 * len(label) + 6, ty + 14], fill=color)
        draw.text((tx + 3, ty + 1), label, fill=(15, 18, 24), font=font)
    img.save(out, format="PNG")
    return out
