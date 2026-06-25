"""Vision evidence: fingerprint/diff, frame store, OCR, element parsing, merge."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from pikvm_agent.core.models import BBox, ElementMap, Region, VisualElement
from pikvm_agent.pikvm.fake import FakeBackend
from pikvm_agent.store.frames import FrameStore
from pikvm_agent.vision.frame_diff import (
    FP_MEANINGFUL,
    fingerprint,
    fp_diff,
    grid,
    is_blank,
)
from pikvm_agent.vision.omniparser_client import (
    NullElementProvider,
    OmniParserClient,
    OmniParserProvider,
    bbox_to_pixels,
    classify_kind,
)
from pikvm_agent.vision.screen_parser import CompositeScreenParser, bbox_from_ocr, iou
from pikvm_agent.vision.tesseract_ocr import (
    TesseractOcrProvider,
    render_text_image,
    tesseract_available,
)

requires_tesseract = pytest.mark.skipif(not tesseract_available(), reason="tesseract CLI absent")


def _jpeg(color, size=(640, 360)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, format="JPEG", quality=90)
    return b.getvalue()


# ---- fingerprint ---------------------------------------------------------- #

def test_fingerprint_diff_thresholds() -> None:
    a, b, c = _jpeg((30, 60, 90)), _jpeg((30, 60, 90)), _jpeg((220, 40, 10))
    fa, fb, fc = fingerprint(a), fingerprint(b), fingerprint(c)
    assert len(fa) == 256
    assert fp_diff(fa, fb) < 0.01
    assert fp_diff(fa, fc) > FP_MEANINGFUL
    assert fp_diff(None, fa) == 1.0
    assert len(grid(a)) == 96 * 54
    assert is_blank(fingerprint(_jpeg((0, 0, 0)))) is True


# ---- frame store world-versioning ----------------------------------------- #

async def test_frame_store_world_versioning(tmp_path) -> None:
    be = FakeBackend()
    fs = FrameStore("sess", tmp_path, be)
    f1 = await fs.capture()
    assert (f1.frame_id, f1.world_version) == (1, 1)
    f2 = await fs.capture()  # unchanged screen
    assert (f2.frame_id, f2.world_version) == (2, 1)
    be.set_screen("modal", bg=(210, 30, 30))  # meaningful change
    assert (await fs.capture()).world_version == 2
    be.caps_lock = True  # keyboard change bumps world too
    assert (await fs.capture()).world_version == 3


# ---- OCR ------------------------------------------------------------------ #

@requires_tesseract
async def test_tesseract_ocr_reads_text_with_boxes(tmp_path) -> None:
    p = tmp_path / "s.png"
    p.write_bytes(render_text_image("Open the README file\nfind . -name README"))
    res = await TesseractOcrProvider().ocr(p)
    assert "readme" in res.text.lower()
    assert all(len(ln.bbox) == 4 for ln in res.lines)
    assert res.lines[0].confidence and res.lines[0].confidence > 0.5


# ---- OmniParser ----------------------------------------------------------- #

def test_omniparser_normalize_classify_bbox() -> None:
    r = OmniParserClient._normalize(
        {"parsed_content_list": [
            {"bbox": [0.1, 0.1, 0.3, 0.2], "content": "Send", "type": "button"},
            {"box": [100, 50, 200, 80], "caption": "close window"},
        ]}
    )
    assert len(r.elements) == 2 and r.elements[0].text == "Send"
    assert classify_kind("button", "", "Send") == "send_button"
    assert classify_kind("button", "Save", "Save") == "button"
    assert classify_kind(None, "close window", None) == "close_button"
    assert classify_kind("textbox", None, None) == "input"
    b = bbox_to_pixels([0.1, 0.1, 0.3, 0.2], 1000, 1000)
    assert (b.x, b.y, b.w, b.h) == (100, 100, 200, 100)


async def test_omniparser_provider_builds_and_degrades(tmp_path) -> None:
    img = tmp_path / "f.png"
    img.write_bytes(render_text_image("hi"))
    result = OmniParserClient._normalize(
        {"parsed_content_list": [{"bbox": [0.1, 0.1, 0.3, 0.2], "content": "Send", "type": "button"}]}
    )

    class Stub:
        async def parse_image(self, _p):
            return result

    em = await OmniParserProvider(Stub()).parse_elements(img, frame_id=7, world_version=3)
    assert len(em.elements) == 1 and em.frame_id == 7 and em.elements[0].kind == "send_button"

    class Boom:
        async def parse_image(self, _p):
            raise RuntimeError("server down")

    assert (await OmniParserProvider(Boom()).parse_elements(img, 1, 1)).elements == []
    assert (await NullElementProvider().parse_elements(img, 1, 1)).elements == []


async def test_omniparser_interactivity_maps_to_button(tmp_path) -> None:
    # OmniParser flags clickable elements; an interactable the keyword classifier
    # left "unknown" becomes a button (so the operator sees it as clickable).
    img = tmp_path / "f.png"
    img.write_bytes(render_text_image("x"))
    result = OmniParserClient._normalize(
        {"parsed_content_list": [
            {"bbox": [0.1, 0.1, 0.2, 0.2], "content": "Submit", "type": "text", "interactivity": True},
            {"bbox": [0.5, 0.5, 0.6, 0.6], "content": "just a label", "type": "text", "interactivity": False},
        ]}
    )

    class Stub:
        async def parse_image(self, _p):
            return result

    em = await OmniParserProvider(Stub()).parse_elements(img, 1, 1)
    assert em.elements[0].kind == "button" and em.elements[0].text == "Submit"
    assert em.elements[1].kind == "text"  # non-interactive stays text


# ---- composite parser ----------------------------------------------------- #

def test_bbox_from_ocr_and_iou() -> None:
    assert bbox_from_ocr([10, 20, 110, 40]) == BBox(x=10, y=20, w=100, h=20)
    assert bbox_from_ocr([[10, 20], [110, 20], [110, 40], [10, 40]]) == BBox(x=10, y=20, w=100, h=20)
    assert iou(BBox(x=0, y=0, w=10, h=10), BBox(x=0, y=0, w=10, h=10)) == 1.0
    assert iou(BBox(x=0, y=0, w=10, h=10), BBox(x=100, y=100, w=10, h=10)) == 0.0


@requires_tesseract
async def test_composite_parser_attaches_and_keeps(tmp_path) -> None:
    img = tmp_path / "f.png"
    img.write_bytes(render_text_image("Open the README\nfind . -name README"))

    # OCR-only: positioned text elements + ocr_text
    em = await CompositeScreenParser(NullElementProvider(), TesseractOcrProvider()).parse(img, 5, 2)
    assert "readme" in em.ocr_text.lower()
    assert em.elements and all(e.kind == "text" for e in em.elements)
    assert em.elements[0].source == ["tesseract"]

    # an interactable element overlapping the first line gets the text attached
    first = em.elements[0].bbox

    class StubEP:
        async def parse_elements(self, _p, fid, wv):
            el = VisualElement(
                id="e0", frame_id=fid, world_version=wv,
                bbox=BBox(x=first.x - 4, y=first.y - 4, w=first.w + 8, h=first.h + 8),
                kind="input", source=["omniparser"],
            )
            return ElementMap(frame_id=fid, world_version=wv, elements=[el])

    em2 = await CompositeScreenParser(StubEP(), TesseractOcrProvider()).parse(img, 5, 2)
    inp = next(e for e in em2.elements if e.kind == "input")
    assert inp.text and "open" in inp.text.lower() and "tesseract" in inp.source


# ---- set-of-marks overlay ------------------------------------------------- #

def test_set_of_marks_overlay(tmp_path) -> None:
    from pikvm_agent.vision.set_of_marks import draw_set_of_marks

    img = tmp_path / "f.png"
    img.write_bytes(render_text_image("hi"))
    with Image.open(img) as im:
        src_size = im.size
    em = ElementMap(
        frame_id=1, world_version=1,
        elements=[
            VisualElement(id="e0", frame_id=1, world_version=1,
                          bbox=BBox(x=10, y=10, w=80, h=30), kind="button", text="OK"),
            VisualElement(id="e1", frame_id=1, world_version=1,
                          bbox=BBox(x=10, y=60, w=120, h=24), kind="text", text="hello"),
        ],
    )
    out = draw_set_of_marks(img, em)
    assert out.exists()
    with Image.open(out) as im:
        assert im.size == src_size  # overlay preserves frame dimensions
    # empty map still yields a valid file at the requested path
    out2 = draw_set_of_marks(img, ElementMap(frame_id=1, world_version=1), tmp_path / "empty.png")
    assert out2.exists() and out2.name == "empty.png"
