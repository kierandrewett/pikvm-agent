"""TesseractOcrProvider — file OCR via the system ``tesseract`` CLI.

A real, zero-Python-dependency OCRProvider: it shells out to tesseract with TSV
output (words + boxes + confidence) and groups words into lines. Works on the
exact saved frame, so OCR boxes are grounded on the frame we parsed. Used as the
default local OCR when the binary is present (PaddleOCR is the optional upgrade).
"""

from __future__ import annotations

import asyncio
import io
import shutil
import tempfile
from pathlib import Path

from PIL import Image

from pikvm_agent.core.models import OCRLine, OCRResult, Region


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _parse_tsv(tsv: str) -> list[OCRLine]:
    rows = tsv.splitlines()
    if not rows:
        return []
    header = rows[0].split("\t")
    idx = {name: i for i, name in enumerate(header)}
    # group words by (block, par, line)
    groups: dict[tuple[int, int, int], dict] = {}
    for row in rows[1:]:
        cols = row.split("\t")
        if len(cols) < len(header):
            continue
        try:
            conf = float(cols[idx["conf"]])
        except (ValueError, KeyError):
            continue
        text = cols[idx["text"]].strip() if "text" in idx else ""
        if conf < 0 or not text:
            continue
        key = (int(cols[idx["block_num"]]), int(cols[idx["par_num"]]), int(cols[idx["line_num"]]))
        left, top = int(cols[idx["left"]]), int(cols[idx["top"]])
        width, height = int(cols[idx["width"]]), int(cols[idx["height"]])
        g = groups.setdefault(key, {"words": [], "x0": left, "y0": top, "x1": left + width,
                                    "y1": top + height, "confs": []})
        g["words"].append(text)
        g["x0"] = min(g["x0"], left)
        g["y0"] = min(g["y0"], top)
        g["x1"] = max(g["x1"], left + width)
        g["y1"] = max(g["y1"], top + height)
        g["confs"].append(conf)
    lines: list[OCRLine] = []
    for g in groups.values():
        confs = g["confs"]
        lines.append(
            OCRLine(
                text=" ".join(g["words"]),
                confidence=(sum(confs) / len(confs) / 100.0) if confs else None,
                bbox=[g["x0"], g["y0"], g["x1"], g["y1"]],
            )
        )
    return lines


class TesseractOcrProvider:
    def __init__(self, lang: str = "eng", psm: int = 6) -> None:
        self.lang = lang
        self.psm = psm

    async def ocr(self, image_path: Path, region: Region | None = None) -> OCRResult:
        src = Path(image_path)
        tmp: Path | None = None
        if region is not None:
            img = Image.open(src).convert("RGB")
            x = max(0, int(region.x))
            y = max(0, int(region.y))
            box = (x, y, x + max(1, int(region.width)), y + max(1, int(region.height)))
            crop_img = img.crop(box)
            fd = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            crop_img.save(fd, format="PNG")
            fd.close()
            tmp = Path(fd.name)
            src = tmp
        try:
            proc = await asyncio.create_subprocess_exec(
                "tesseract", str(src), "stdout", "-l", self.lang, "--psm", str(self.psm), "tsv",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        return OCRResult(lines=_parse_tsv(out.decode("utf-8", "replace")))


def _readable_font(size: int):
    from PIL import ImageFont

    for path in (
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_text_image(text: str, size: tuple[int, int] = (720, 240), font_size: int = 30) -> bytes:
    """Render black-on-white text with a readable TrueType font — for tests/smoke
    without a real screenshot (so tesseract reads it reliably)."""
    from PIL import ImageDraw

    img = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = _readable_font(font_size)
    y = 12
    for line in (text.splitlines() or [text]):
        draw.text((12, y), line, fill=(0, 0, 0), font=font)
        y += font_size + 12
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
