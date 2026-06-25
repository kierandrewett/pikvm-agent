"""FakeBackend — a hardware-free, structurally-compatible ComputerBackend.

Used for local development, the daemon smoke path, and (Phase 3) replay/eval
fixtures. It generates frames on demand and records every HID action so tests
can assert on them. Drop-in for :class:`PiKVMBackend` everywhere the runtime
consumes a backend.
"""

from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageDraw

from pikvm_agent.core.models import CapturedFrame, Region
from pikvm_agent.pikvm.keyboard_state import Layout
from pikvm_agent.pikvm.screenshot import to_captured_frame


def render_frame(text: str = "", size: tuple[int, int] = (1280, 720),
                 bg: tuple[int, int, int] = (24, 28, 36)) -> bytes:
    """Render a labelled JPEG frame — handy for distinct, diffable fake screens."""
    img = Image.new("RGB", size, bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, size[0] - 1, 28], fill=(45, 52, 64))
    if text:
        draw.text((12, 8), text, fill=(220, 226, 235))
        draw.text((20, 80), text, fill=(160, 200, 255))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


class FakeBackend:
    def __init__(self, width: int = 1280, height: int = 720, layout: Layout = "us") -> None:
        self.dims = {"width": width, "height": height}
        self.native_dims: tuple[int, int] = (width, height)
        self.layout: Layout = layout
        self.caps_lock: bool | None = False
        self.hid_online: bool | None = True
        self.ocr_text = ""
        self._frame = render_frame("desktop", (width, height))
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # ---- test/dev controls ----------------------------------------------- #

    def set_screen(self, text: str, bg: tuple[int, int, int] = (24, 28, 36)) -> None:
        self._frame = render_frame(text, (self.dims["width"], self.dims["height"]), bg)

    def set_frame_bytes(self, data: bytes) -> None:
        self._frame = data

    def _record(self, method: str, **kw: Any) -> None:
        self.calls.append((method, kw))

    # ---- ComputerBackend surface ----------------------------------------- #

    async def screenshot(self, region: Region | None = None) -> CapturedFrame:
        return to_captured_frame(self._frame, self.dims["width"], self.dims["height"])

    async def ocr(self, region: Region | None = None, langs: str = "eng") -> str:
        return self.ocr_text

    async def keypress(self, keys: list[str]) -> None:
        self._record("keypress", keys=keys)

    async def press_key(self, code: str) -> None:
        self._record("press_key", code=code)

    async def type_text(self, text: str, *, code: bool = False, secret: bool = False) -> None:
        self._record("type_text", text=text, code=code, secret=secret)

    async def print_text(self, text: str) -> None:
        self._record("print_text", text=text)

    async def click(self, x: int, y: int, button: str = "left") -> None:
        self._record("click", x=x, y=y, button=button)

    async def move_mouse(self, x: int, y: int) -> None:
        self._record("move_mouse", x=x, y=y)

    async def scroll(self, dx: int = 0, dy: int = 0) -> None:
        self._record("scroll", dx=dx, dy=dy)

    # ---- state getters --------------------------------------------------- #

    def get_layout(self) -> Layout:
        return self.layout

    def set_layout(self, layout: Layout, *, source: str = "user") -> None:
        self.layout = layout

    def get_caps_lock(self) -> bool | None:
        return self.caps_lock

    def get_keymap_default(self) -> str | None:
        return "en-gb" if self.layout == "uk" else "en-us"

    def is_hid_online(self) -> bool | None:
        return self.hid_online

    def get_dimensions(self) -> dict[str, int]:
        return dict(self.dims)

    async def connect(self) -> None:  # noqa: D401 - no-op
        return None

    async def aclose(self) -> None:
        return None

    async def health(self) -> bool:
        return True
