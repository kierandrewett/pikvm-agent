"""Raw HID over the PiKVM WebSocket (`wss://<host>/api/ws`).

Low-level transport only: it sends key/mouse events (coordinates already
normalized by the caller) and, in the receive direction, folds the KVMD state
stream (LEDs, keymap, resolution, mouse mode) into a cached ``KvmdState``.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any, Awaitable, Callable

import websockets

from pikvm_agent.core.errors import BackendError
from pikvm_agent.pikvm.keyboard_state import KvmdState, merge_kvmd_event

# Normalized absolute-mouse coordinate bounds (PiKVM HID units).
NORM_MIN = -32768
NORM_MAX = 32767


def to_norm(px: float, span: int) -> int:
    """Pixel -> normalized HID coordinate for absolute mouse positioning."""
    ratio = px / (span - 1) if span > 1 else 0.0
    return max(NORM_MIN, min(NORM_MAX, round(ratio * 65534) - 32767))


def clamp_norm(v: float) -> int:
    return max(NORM_MIN, min(NORM_MAX, round(v)))


class HidChannel:
    def __init__(
        self,
        ws_url: str,
        headers: dict[str, str],
        *,
        verify_tls: bool = False,
        on_event: Callable[[str, Any], Awaitable[None] | None] | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._headers = headers
        self._verify_tls = verify_tls
        self._on_event = on_event
        self._ws: Any | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self.state = KvmdState()

    def _ssl_ctx(self) -> ssl.SSLContext | None:
        if not self._ws_url.startswith("wss"):
            return None
        ctx = ssl.create_default_context()
        if not self._verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def connect(self) -> None:
        if self._ws is not None:
            return
        async with self._connect_lock:
            if self._ws is not None:
                return
            try:
                self._ws = await websockets.connect(
                    self._ws_url,
                    additional_headers=self._headers,
                    ssl=self._ssl_ctx(),
                    open_timeout=10,
                    max_size=None,
                )
            except Exception as exc:  # noqa: BLE001
                raise BackendError(f"HID WebSocket connect failed: {exc}") from exc
            self._recv_task = asyncio.create_task(self._recv_loop())

    async def _recv_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue  # binary ping frames etc.
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                et = msg.get("event_type")
                if not isinstance(et, str):
                    continue
                merge_kvmd_event(self.state, et, msg.get("event"))
                if self._on_event is not None:
                    res = self._on_event(et, msg.get("event"))
                    if asyncio.iscoroutine(res):
                        await res
        except Exception:  # noqa: BLE001 - a dropped stream must not crash the daemon
            pass
        finally:
            self._ws = None

    async def send(self, event_type: str, event: Any) -> None:
        await self.connect()
        ws = self._ws
        if ws is None:
            raise BackendError("HID WebSocket not connected")
        await ws.send(json.dumps({"event_type": event_type, "event": event}))

    # ---- keyboard --------------------------------------------------------- #

    async def key(self, code: str, state: bool) -> None:
        await self.send("key", {"key": code, "state": state})

    # ---- mouse (caller passes normalized coords for absolute moves) -------- #

    async def mouse_move(self, x_norm: int, y_norm: int) -> None:
        await self.send("mouse_move", {"to": {"x": clamp_norm(x_norm), "y": clamp_norm(y_norm)}})

    async def mouse_relative(self, dx: int, dy: int) -> None:
        await self.send("mouse_relative", {"delta": {"x": round(dx), "y": round(dy)}, "squash": True})

    async def mouse_button(self, button: str, state: bool) -> None:
        await self.send("mouse_button", {"button": button, "state": state})

    async def mouse_wheel(self, dx: int, dy: int) -> None:
        await self.send("mouse_wheel", {"delta": {"x": round(dx), "y": round(dy)}})

    async def close(self) -> None:
        task, ws = self._recv_task, self._ws
        self._recv_task, self._ws = None, None
        if task is not None:
            task.cancel()
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
