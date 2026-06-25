"""PiKVMBackend — the concrete :class:`ComputerBackend`.

Composes the HID channel + HTTP snapshot/OCR/print into the single object that
touches the Pi. Pixel coordinates are in the agent's frame space (``self.dims``);
this class converts them to normalized HID units. Typing routes through the
US/UK key map with Caps-Lock compensation; long prose can take the fast
server-side print path. Newlines never auto-submit.
"""

from __future__ import annotations

import asyncio
import random
from urllib.parse import urlsplit, urlunsplit

import httpx

from pikvm_agent.config import PikvmConfig
from pikvm_agent.core.errors import BackendError
from pikvm_agent.core.models import CapturedFrame, Region
from pikvm_agent.pikvm import keyboard_state as ks
from pikvm_agent.pikvm.hid import HidChannel, to_norm
from pikvm_agent.pikvm.screenshot import crop, downscale, jpeg_size, to_captured_frame


async def _sleep(ms: float) -> None:
    await asyncio.sleep(ms / 1000.0)


class PiKVMBackend:
    def __init__(self, cfg: PikvmConfig) -> None:
        self._cfg = cfg
        parts = urlsplit(cfg.base_url)
        self._scheme = parts.scheme or "https"
        self._host = parts.netloc
        self._user = cfg.username
        self._pass = cfg.password
        self._token = cfg.token
        self._http = httpx.AsyncClient(verify=cfg.verify_tls, timeout=30.0)
        self.hid = HidChannel(
            self._ws_url(),
            {},  # headers attached lazily at connect time via reconfigure
            verify_tls=cfg.verify_tls,
            on_event=self._on_kvmd_event,
        )
        self.hid._headers = self._auth_headers()
        self.dims = {"width": 1920, "height": 1080}
        self.native_dims: tuple[int, int] | None = None
        self.layout: ks.Layout = "uk" if cfg.layout == "uk" else "us"
        self._layout_from_user = False
        self._mouse = {"x": 0, "y": 0}  # last normalized position
        self._shift_held = False

    # ---- wiring ----------------------------------------------------------- #

    def _http_base(self) -> str:
        return urlunsplit((self._scheme, self._host, "", "", ""))

    def _ws_url(self) -> str:
        proto = "wss" if self._scheme == "https" else "ws"
        return f"{proto}://{self._host}/api/ws"

    def _auth_headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self._user:
            h["X-KVMD-User"] = self._user
            h["X-KVMD-Passwd"] = self._pass or ""
        if self._token:
            h["Cookie"] = self._token if "=" in self._token else f"auth_token={self._token}"
        return h

    async def _on_kvmd_event(self, event_type: str, _event: object) -> None:
        if event_type == "hid_keymaps" and not self._layout_from_user:
            mapped = ks.keymap_to_layout(ks.keymap_default_of(self.hid.state))
            if mapped:
                self.layout = mapped
        elif event_type == "streamer":
            res = ks.native_resolution_of(self.hid.state)
            if res:
                self.native_dims = res

    async def connect(self) -> None:
        await self.hid.connect()

    async def aclose(self) -> None:
        await self.hid.close()
        await self._http.aclose()

    async def health(self) -> bool:
        """Cheap reachability probe for status UIs: did the PiKVM host answer? Any
        HTTP response (even an auth error) means the host is up; only a transport
        error / timeout counts as unreachable. Uses kvmd's lightweight info route."""
        try:
            await self._http.get(
                f"{self._http_base()}/api/info", headers=self._auth_headers(), timeout=4.0
            )
            return True
        except Exception:  # noqa: BLE001 - transport error / timeout ⇒ unreachable
            return False

    # ---- KVMD state getters ---------------------------------------------- #

    def get_caps_lock(self) -> bool | None:
        return ks.caps_lock_of(self.hid.state)

    def get_keymap_default(self) -> str | None:
        return ks.keymap_default_of(self.hid.state)

    def is_hid_online(self) -> bool | None:
        return ks.hid_online_of(self.hid.state)

    def get_layout(self) -> ks.Layout:
        return self.layout

    def set_layout(self, layout: ks.Layout, *, source: str = "user") -> None:
        if source != "auto":
            self._layout_from_user = True
        self.layout = "uk" if layout == "uk" else "us"

    def get_dimensions(self) -> dict[str, int]:
        return dict(self.dims)

    # ---- screen ----------------------------------------------------------- #

    async def screenshot(self, region: Region | None = None) -> CapturedFrame:
        resp = await self._http.get(
            f"{self._http_base()}/api/streamer/snapshot",
            headers=self._auth_headers(),
            params={"allow_offline": 1},
        )
        resp.raise_for_status()
        data = resp.content
        hw = int(resp.headers.get("x-ustreamer-width", 0) or 0)
        hh = int(resp.headers.get("x-ustreamer-height", 0) or 0)
        if not hw or not hh:
            parsed = jpeg_size(data) or (self.dims["width"], self.dims["height"])
            hw, hh = parsed
        return self._finalize(data, hw, hh, region)

    def _finalize(self, raw: bytes, fw: int, fh: int, region: Region | None) -> CapturedFrame:
        if region is not None:
            sx = fw / (self.dims["width"] or fw)
            sy = fh / (self.dims["height"] or fh)
            scaled = Region(
                x=region.x * sx, y=region.y * sy, width=region.width * sx, height=region.height * sy
            )
            data, w, h = crop(raw, fw, fh, scaled)
            return to_captured_frame(data, w, h)
        data, w, h = downscale(raw, fw, fh)
        self.dims = {"width": w, "height": h}
        return to_captured_frame(data, w, h)

    async def ocr(self, region: Region | None = None, langs: str = "eng") -> str:
        nat = self.native_dims or (self.dims["width"], self.dims["height"])
        sx = nat[0] / (self.dims["width"] or nat[0])
        sy = nat[1] / (self.dims["height"] or nat[1])
        if region is not None:
            r = {
                "ocr_left": round(region.x * sx),
                "ocr_top": round(region.y * sy),
                "ocr_right": round((region.x + region.width) * sx),
                "ocr_bottom": round((region.y + region.height) * sy),
            }
        else:
            r = {"ocr_left": 0, "ocr_top": 0, "ocr_right": nat[0], "ocr_bottom": nat[1]}
        resp = await self._http.get(
            f"{self._http_base()}/api/streamer/snapshot",
            headers=self._auth_headers(),
            params={"allow_offline": 1, "ocr": 1, "ocr_langs": langs, **r},
            timeout=25.0,
        )
        resp.raise_for_status()
        return resp.text.strip()

    async def print_text(self, text: str) -> None:
        body = " ".join(text.splitlines())  # never auto-submit
        if not body:
            return
        params: dict[str, object] = {"limit": 0, "slow": 1}
        km = self.get_keymap_default()
        if km:
            params["keymap"] = km
        resp = await self._http.post(
            f"{self._http_base()}/api/hid/print",
            headers={**self._auth_headers(), "Content-Type": "text/plain"},
            content=body.encode("utf-8"),
            params=params,
            timeout=120.0,
        )
        resp.raise_for_status()

    # ---- keyboard --------------------------------------------------------- #

    async def keypress(self, keys: list[str]) -> None:
        """Press a chord: hold each key down in order, then release in reverse."""
        for c in keys:
            await self.hid.key(c, True)
        await _sleep(60)
        for c in reversed(keys):
            await self.hid.key(c, False)

    async def press_key(self, code: str) -> None:
        await self.hid.key(code, True)
        await _sleep(40)
        await self.hid.key(code, False)

    async def type_text(self, text: str, *, code: bool = False, secret: bool = False) -> None:
        """Type per-key with layout + Caps-Lock compensation. Newlines -> spaces
        (never submits). For long plain prose prefer :meth:`print_text`."""
        body = " ".join(text.splitlines())
        strokes: list[dict[str, object]] = []
        for ch in body:
            info = ks.key_for(ch, self.layout)
            if info is None:
                continue
            strokes.append({"code": info.code, "shift": info.shift})
        ks.compensate_caps_lock(strokes, self.get_caps_lock() is True)
        try:
            for s in strokes:
                want_shift = bool(s["shift"])
                if want_shift and not self._shift_held:
                    await self.hid.key("ShiftLeft", True)
                    self._shift_held = True
                elif not want_shift and self._shift_held:
                    await self.hid.key("ShiftLeft", False)
                    self._shift_held = False
                await self.hid.key(str(s["code"]), True)
                await _sleep(12 + random.random() * 28)
                await self.hid.key(str(s["code"]), False)
                await _sleep(20 + random.random() * 40)
        finally:
            if self._shift_held:
                await self.hid.key("ShiftLeft", False)
                self._shift_held = False

    # ---- mouse ------------------------------------------------------------ #

    async def move_mouse(self, x: int, y: int) -> None:
        tx = to_norm(x, self.dims["width"])
        ty = to_norm(y, self.dims["height"])
        await self.hid.mouse_move(tx, ty)
        self._mouse = {"x": tx, "y": ty}

    async def click(self, x: int, y: int, button: str = "left") -> None:
        await self.move_mouse(x, y)
        await _sleep(40)
        await self.hid.mouse_button(button, True)
        await _sleep(40 + random.random() * 60)
        await self.hid.mouse_button(button, False)

    async def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Wheel scroll, broken into ticks. dy>0 = up, dx>0 = right."""
        ticks = max(1, min(12, round(max(abs(dx), abs(dy)))))
        sx, sy = dx / ticks, dy / ticks
        for _ in range(ticks):
            await self.hid.mouse_wheel(
                round(sx) or (1 if dx > 0 else -1 if dx < 0 else 0),
                round(sy) or (1 if dy > 0 else -1 if dy < 0 else 0),
            )
            await _sleep(18 + random.random() * 22)
