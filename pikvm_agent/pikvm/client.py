"""PiKVMBackend — the concrete :class:`ComputerBackend`.

Composes the HID channel + HTTP snapshot/OCR/print into the single object that
touches the Pi. Pixel coordinates are in the agent's frame space (``self.dims``);
this class converts them to normalized HID units. Typing routes through the
US/UK key map with Caps-Lock compensation; long prose can take the fast
server-side print path. Newlines never auto-submit.
"""

from __future__ import annotations

import asyncio
import math
import random
from urllib.parse import urlsplit, urlunsplit

import httpx

from pikvm_agent.config import PikvmConfig
from pikvm_agent.core.errors import BackendError
from pikvm_agent.debuglog import DEBUG
from pikvm_agent.core.models import CapturedFrame, Region
from pikvm_agent.pikvm import keyboard_state as ks
from pikvm_agent.pikvm import timing
from pikvm_agent.pikvm.hid import HidChannel, clamp_norm, to_norm
from pikvm_agent.pikvm.windmouse import WindMouseOptions, wind_mouse_path
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
        # Single source of truth for the cursor, in FRAME PIXELS. Updated on EVERY mouse
        # operation the daemon performs (moves, clicks). `trusted` is False until we've
        # actually positioned it, and flips back to False when another kvmd client connects
        # (it may have moved the cursor — kvmd doesn't report position, so we can't know).
        # Absolute moves always LAND correctly regardless; `trusted` only governs the
        # WindMouse curve's start point + informs the controller.
        self._cursor: dict[str, Any] = {
            "x": self.dims["width"] / 2.0, "y": self.dims["height"] / 2.0, "trusted": False,
        }
        self._client_count: int | None = None
        self._shift_held = False
        # Per-session typing persona — a consistent personal speed for this session.
        self._type_base_gap = timing.base_gap_ms()

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
        elif event_type == "clients":
            cnt = ks.client_count_of(self.hid.state)
            if cnt is not None and self._client_count is not None and cnt != self._client_count:
                # The set of connected clients changed — another client may have moved the
                # cursor. kvmd doesn't report position, so distrust our stored one until our
                # next move re-establishes it.
                self._cursor["trusted"] = False
            self._client_count = cnt

    # ---- cursor tracking -------------------------------------------------- #

    def _set_cursor(self, px: float, py: float, *, trusted: bool = True) -> None:
        """Record the cursor at frame-pixel (px, py). Called after every mouse op."""
        w, h = self.dims["width"], self.dims["height"]
        self._cursor = {"x": max(0.0, min(float(px), w - 1)),
                        "y": max(0.0, min(float(py), h - 1)), "trusted": trusted}

    def set_cursor_from_norm(self, nx: float, ny: float) -> None:
        """Record the cursor from an EXTERNAL absolute report in HID norm units (±32767) —
        e.g. the desktop live-view telling us where the USER just moved it. We were told the
        position, so trust it (this is how we observe moves kvmd won't report)."""
        w, h = self.dims["width"], self.dims["height"]
        px = (clamp_norm(nx) + 32767) / 65534.0 * w
        py = (clamp_norm(ny) + 32767) / 65534.0 * h
        self._set_cursor(px, py, trusted=True)

    def other_clients(self) -> int:
        """How many OTHER kvmd clients are connected (could move the mouse externally)."""
        return max(0, (self._client_count or 1) - 1)

    def cursor(self) -> dict[str, Any]:
        """Current tracked cursor: pixel x/y, whether we trust it, and other-client count."""
        return {"x": round(self._cursor["x"]), "y": round(self._cursor["y"]),
                "trusted": self._cursor["trusted"], "other_clients": self.other_clients()}

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
        with DEBUG.span("pikvm.screenshot", region=region is not None) as result:
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
            # Decode + LANCZOS downscale + JPEG re-encode is tens of ms of pure CPU — run it
            # off the event loop so it can't stall other sessions / status polls.
            frame = await asyncio.to_thread(self._finalize, data, hw, hh, region)
            result(raw_bytes=len(data), out_bytes=len(frame.data or b""),
                   w=frame.width, h=frame.height)
            return frame

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
        # Send as word-boundary bursts with human pauses between, rather than one long
        # perfectly-uniform stream (the metronomic ~20ms/char of a single slow=1 print is
        # the most detectable signal).
        chunks = timing.word_chunks(body)
        for i, chunk in enumerate(chunks):
            await self._print_chunk(chunk)
            if i < len(chunks) - 1:
                await _sleep(timing.print_chunk_pause_ms(chunk))

    async def _print_chunk(self, chunk: str) -> None:
        params: dict[str, object] = {"limit": 0, "slow": 1}
        km = self.get_keymap_default()
        if km:
            params["keymap"] = km
        resp = await self._http.post(
            f"{self._http_base()}/api/hid/print",
            headers={**self._auth_headers(), "Content-Type": "text/plain"},
            content=chunk.encode("utf-8"),
            params=params,
            timeout=120.0,
        )
        resp.raise_for_status()

    # ---- keyboard --------------------------------------------------------- #

    async def keypress(self, keys: list[str]) -> None:
        """Press a chord: hold each key down in order, then release in reverse.
        Keys are staggered (a human presses modifier→key, not all in one instant)
        and the hold is randomized."""
        for i, c in enumerate(keys):
            if i:
                await _sleep(timing.chord_stagger_ms())
            await self.hid.key(c, True)
        await _sleep(timing.chord_hold_ms())
        for i, c in enumerate(reversed(keys)):
            if i:
                await _sleep(timing.chord_stagger_ms())
            await self.hid.key(c, False)

    async def release_all(self) -> None:
        """Best-effort HID safety: release the modifiers + mouse buttons that could be
        held (during a hotkey or drag). We don't track arbitrary held keys, so this
        targets the dangerous ones; each release is independent so one failure doesn't
        block the rest."""
        for mod in ("ShiftLeft", "ShiftRight", "ControlLeft", "ControlRight",
                    "AltLeft", "AltRight", "MetaLeft", "MetaRight"):
            try:
                await self.hid.key(mod, False)
            except Exception:  # noqa: BLE001
                pass
        for btn in ("left", "right", "middle"):
            try:
                await self.hid.mouse_button(btn, False)
            except Exception:  # noqa: BLE001
                pass

    async def press_key(self, code: str) -> None:
        await self.hid.key(code, True)
        await _sleep(timing.press_dwell_ms())
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
            strokes.append({"code": info.code, "shift": info.shift, "ch": ch})
        ks.compensate_caps_lock(strokes, self.get_caps_lock() is True)
        try:
            prev: str | None = None
            for s in strokes:
                want_shift = bool(s["shift"])
                if want_shift and not self._shift_held:
                    await self.hid.key("ShiftLeft", True)
                    self._shift_held = True
                elif not want_shift and self._shift_held:
                    await self.hid.key("ShiftLeft", False)
                    self._shift_held = False
                ch = str(s.get("ch", ""))
                await self.hid.key(str(s["code"]), True)
                await _sleep(timing.key_hold_ms())            # key-down hold (log-normal)
                await self.hid.key(str(s["code"]), False)
                # Inter-key gap: persona-anchored, right-skewed, with think-pauses.
                await _sleep(timing.inter_key_gap_ms(prev, ch, self._type_base_gap))
                prev = ch
        finally:
            if self._shift_held:
                await self.hid.key("ShiftLeft", False)
                self._shift_held = False

    # ---- mouse ------------------------------------------------------------ #

    async def move_mouse(self, x: int, y: int) -> None:
        """Move the cursor along a WindMouse path: gravity pulls toward the target
        while a random wind walk perturbs it, giving an organic curved trajectory with
        a natural speed profile, tremor and an off-centre landing. Generated in frame-
        pixel space (where the force constants are tuned) and converted to HID units;
        always lands EXACTLY on the target so clicks hit the resolved site. The tracked
        cursor is updated to the landing point (our single source of truth)."""
        w, h = self.dims["width"], self.dims["height"]
        tx, ty = to_norm(x, w), to_norm(y, h)
        start = (self._cursor["x"], self._cursor["y"])  # last known position, in pixels
        end = (float(x), float(y))
        if math.hypot(end[0] - start[0], end[1] - start[1]) < 2:
            await self.hid.mouse_move(tx, ty)
            self._set_cursor(x, y)
            return

        hum = max(0.0, self._cfg.mouse_humanize)
        opts = WindMouseOptions(speed=self._cfg.mouse_speed,
                                tremor=0.5 * hum, end_scatter=2.0 * hum, hes=0.04 * hum)
        samples = wind_mouse_path(start, end, opts)
        # WindMouse emits ~one point per integration step (a long move can be 100+).
        # Decimate to a sane cap (timing preserved) so we don't flood the HID socket.
        cap = 80
        if len(samples) > cap:
            samples = [samples[round(i * (len(samples) - 1) / (cap - 1))] for i in range(cap)]

        prev_t = 0.0
        for i, (sx, sy, t) in enumerate(samples):
            await self.hid.mouse_move(to_norm(sx, w), to_norm(sy, h))
            dt = t - prev_t
            prev_t = t
            if i < len(samples) - 1 and dt > 0.5:
                await _sleep(dt)
        # Land exactly on the requested target (the click site).
        await self.hid.mouse_move(tx, ty)
        self._set_cursor(x, y)

    async def click(self, x: int, y: int, button: str = "left") -> None:
        await self.move_mouse(x, y)
        await _sleep(timing.click_settle_ms())  # hover-settle after the cursor arrives
        await self.hid.mouse_button(button, True)
        await _sleep(timing.click_hold_ms())
        await self.hid.mouse_button(button, False)

    async def double_click(self, x: int, y: int, button: str = "left") -> None:
        """Two clicks at one site with a human inter-click gap (no second move)."""
        await self.click(x, y, button)
        await _sleep(timing.double_click_gap_ms())
        await self.hid.mouse_button(button, True)
        await _sleep(timing.click_hold_ms())
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
