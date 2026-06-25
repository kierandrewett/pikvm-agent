"""Frame post-processing: dimension parsing, downscale, crop, hashing.

The HTTP fetch lives in the client; this module is the pure pixel work so it is
trivially testable. Full frames are downscaled so the long edge is at most
``MAX_SCREENSHOT_DIM`` (the documented vision-model click-accuracy sweet spot),
held fixed per session so the model's pixel sense stays calibrated.
"""

from __future__ import annotations

import hashlib
import io
import time
from datetime import datetime, timezone

from PIL import Image

from pikvm_agent.core.models import CapturedFrame, Region

MAX_SCREENSHOT_DIM = 1280


def jpeg_size(buf: bytes) -> tuple[int, int] | None:
    """Parse width/height from JPEG SOF markers (fallback when headers absent)."""
    i = 2
    n = len(buf)
    while i + 9 < n:
        if buf[i] != 0xFF:
            i += 1
            continue
        marker = buf[i + 1]
        # SOF0..SOF15 carry frame dimensions, except DHT(c4)/DAC(cc)/RSTn.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = (buf[i + 5] << 8) | buf[i + 6]
            width = (buf[i + 7] << 8) | buf[i + 8]
            return width, height
        if i + 3 >= n:
            break
        length = (buf[i + 2] << 8) | buf[i + 3]
        i += 2 + length
    return None


def _encode(img: Image.Image, quality: int) -> bytes:
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=quality)
    return out.getvalue()


def downscale(jpeg: bytes, w: int, h: int, max_dim: int = MAX_SCREENSHOT_DIM, quality: int = 78) -> tuple[bytes, int, int]:
    """Downscale so the long edge is at most ``max_dim``. No-op if already small."""
    long_edge = max(w, h)
    if long_edge <= max_dim:
        return jpeg, w, h
    scale = max_dim / long_edge
    nw, nh = round(w * scale), round(h * scale)
    img = Image.open(io.BytesIO(jpeg))
    img = img.resize((nw, nh), Image.LANCZOS)
    return _encode(img, quality), nw, nh


def crop(jpeg: bytes, fw: int, fh: int, region: Region, quality: int = 88) -> tuple[bytes, int, int]:
    """Crop a full-res frame to a clamped region, then cap its size."""
    x = max(0, min(round(region.x), fw - 1))
    y = max(0, min(round(region.y), fh - 1))
    w = max(1, min(round(region.width), fw - x))
    h = max(1, min(round(region.height), fh - y))
    img = Image.open(io.BytesIO(jpeg)).crop((x, y, x + w, y + h))
    return downscale(_encode(img, quality), w, h, quality=quality)


def to_captured_frame(data: bytes, width: int, height: int, mime_type: str = "image/jpeg") -> CapturedFrame:
    """Wrap raw bytes into a CapturedFrame with a content hash + timestamps."""
    return CapturedFrame(
        data=data,
        width=width,
        height=height,
        mime_type=mime_type,
        sha256=hashlib.sha256(data).hexdigest(),
        captured_at=datetime.now(timezone.utc).isoformat(),
        monotonic_ms=int(time.monotonic() * 1000),
    )
