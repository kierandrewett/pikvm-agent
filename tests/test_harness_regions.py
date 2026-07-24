from __future__ import annotations

import io

from PIL import Image, ImageDraw

from pikvm_agent.harness.regions import detect_observer_editor


def _observer_like_frame() -> bytes:
    image = Image.new("RGB", (1000, 700), (12, 30, 55))
    draw = ImageDraw.Draw(image)
    draw.rectangle((130, 80, 870, 620), fill=(242, 242, 242))
    draw.rectangle((145, 150, 855, 510), outline=(125, 125, 125), fill="white")
    draw.text((152, 158), "typed prose fragments the first bright rows", fill="black")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=90)
    return out.getvalue()


def test_detect_observer_editor_uses_pixels_not_fixed_coordinates() -> None:
    region = detect_observer_editor(_observer_like_frame())

    assert region is not None
    x, y, width, height = region
    assert 135 <= x <= 155
    assert 145 <= y <= 165
    assert 690 <= width <= 730
    assert 340 <= height <= 380


def test_detect_observer_editor_returns_none_without_large_bright_field() -> None:
    image = Image.new("RGB", (640, 400), (15, 25, 35))
    out = io.BytesIO()
    image.save(out, "JPEG")

    assert detect_observer_editor(out.getvalue()) is None
