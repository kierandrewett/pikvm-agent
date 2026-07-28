"""PiKVM-shaped HTTP/WebSocket API backed by an arbitrary RFB/VNC server.

This is a lab adapter, not a second execution runtime. The normal
``PiKVMBackend`` still connects to ``/api/info``, ``/api/ws``,
``/api/streamer/snapshot`` and ``/api/hid/print``. This service translates only
that bounded wire contract to VNC, allowing a completely isolated daemon/MCP
instance to exercise the production client/runtime against a disposable VM.

The VNC endpoint is supplied at runtime. No target, password, port, or operating
system is compiled into this module.
"""

from __future__ import annotations

import asyncio
import io
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from PIL import Image

from pikvm_agent.harness.vnc_target_lease import (
    VncTargetLease,
    normalize_vnc_endpoint,
)
from pikvm_agent.pikvm.hid import NORM_MAX, NORM_MIN
from pikvm_agent.pikvm import keyboard_state as ks
from pikvm_agent.pikvm.text import flatten_line_breaks


@runtime_checkable
class VncTransport(Protocol):
    width: int
    height: int

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def screenshot(self) -> bytes: ...
    async def key(self, code: str, down: bool) -> None: ...
    async def mouse_move(self, x: int, y: int) -> None: ...
    async def mouse_button(self, button: str, down: bool) -> None: ...
    async def mouse_wheel(self, dx: int, dy: int) -> None: ...
    async def print_text(self, text: str) -> None: ...
    async def ocr(self, *, left: int, top: int, right: int, bottom: int) -> str: ...


def _norm_to_pixel(value: int | float, span: int) -> int:
    value = max(NORM_MIN, min(NORM_MAX, int(value)))
    ratio = (value - NORM_MIN) / (NORM_MAX - NORM_MIN)
    return max(0, min(span - 1, round(ratio * (span - 1))))


def initial_state_messages(width: int, height: int, keymap: str) -> list[dict[str, Any]]:
    return [
        {
            "event_type": "hid",
            "event": {
                "online": True,
                "connected": True,
                "keyboard": {"online": True, "leds": {"caps": False}},
            },
        },
        {
            "event_type": "hid_keymaps",
            "event": {"keymaps": {"default": keymap}},
        },
        {
            "event_type": "streamer",
            "event": {
                "online": True,
                "source": {"resolution": {"width": width, "height": height}},
            },
        },
        {
            "event_type": "ocr",
            "event": {"enabled": True, "langs": {"default": ["eng"], "available": ["eng"]}},
        },
        {"event_type": "clients", "event": {"count": 1}},
        {"event_type": "loop", "event": None},
    ]


async def dispatch_hid_event(vnc: VncTransport, message: dict[str, Any]) -> None:
    event_type = message.get("event_type")
    event = message.get("event") or {}
    if event_type == "key":
        await vnc.key(str(event.get("key", "")), bool(event.get("state")))
    elif event_type == "mouse_move":
        target = event.get("to") or {}
        await vnc.mouse_move(
            _norm_to_pixel(target.get("x", 0), vnc.width),
            _norm_to_pixel(target.get("y", 0), vnc.height),
        )
    elif event_type == "mouse_relative":
        delta = event.get("delta") or {}
        # RFB uses absolute pointer coordinates. A relative event cannot be
        # translated without tracked state, so the real transport owns it.
        move_relative = getattr(vnc, "mouse_relative", None)
        if callable(move_relative):
            await move_relative(int(delta.get("x", 0)), int(delta.get("y", 0)))
    elif event_type == "mouse_button":
        await vnc.mouse_button(
            str(event.get("button", "left")), bool(event.get("state"))
        )
    elif event_type == "mouse_wheel":
        delta = event.get("delta") or {}
        await vnc.mouse_wheel(int(delta.get("x", 0)), int(delta.get("y", 0)))


def create_vnc_pikvm_app(
    vnc: VncTransport,
    *,
    keymap: str = "en-us",
) -> FastAPI:
    """Create a PiKVM-compatible lab API around an injected VNC transport."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await vnc.connect()
        try:
            yield
        finally:
            await vnc.close()

    app = FastAPI(title="VNC PiKVM Lab Adapter", lifespan=lifespan)

    @app.get("/api/info")
    async def info() -> dict[str, Any]:
        return {
            "ok": True,
            "result": {
                "system": {"hostname": "vnc-lab-adapter"},
                "extras": {"vnc_lab": {"enabled": True}},
            },
        }

    @app.get("/api/streamer/snapshot")
    async def snapshot(request: Request) -> Response:
        query = request.query_params
        if query.get("ocr") in ("1", "true", "yes"):
            text = await vnc.ocr(
                left=int(query.get("ocr_left", 0)),
                top=int(query.get("ocr_top", 0)),
                right=int(query.get("ocr_right", vnc.width)),
                bottom=int(query.get("ocr_bottom", vnc.height)),
            )
            return PlainTextResponse(text)
        data = await vnc.screenshot()
        return Response(
            data,
            media_type="image/jpeg",
            headers={
                "x-ustreamer-width": str(vnc.width),
                "x-ustreamer-height": str(vnc.height),
                "cache-control": "no-store",
            },
        )

    @app.post("/api/hid/print")
    async def hid_print(request: Request) -> JSONResponse:
        body = (await request.body()).decode("utf-8")
        # Match PiKVM's print endpoint contract: the endpoint types characters
        # but never treats newlines as a submit gesture.
        await vnc.print_text(flatten_line_breaks(body))
        return JSONResponse({"ok": True, "result": {}})

    @app.websocket("/api/ws")
    async def hid_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        for message in initial_state_messages(vnc.width, vnc.height, keymap):
            await websocket.send_json(message)
        sequence = 0
        try:
            while True:
                message = await websocket.receive_json()
                await dispatch_hid_event(vnc, message)
                sequence += 1
                # Unknown events are ignored by the real PiKVM client. The ack
                # makes the emulator deterministic and directly testable.
                await websocket.send_json(
                    {"event_type": "lab_ack", "event": {"sequence": sequence}}
                )
        except WebSocketDisconnect:
            return

    return app


_CODE_TO_VNC: dict[str, str] = {
    "ShiftLeft": "shift",
    "ShiftRight": "shift",
    "ControlLeft": "ctrl",
    "ControlRight": "ctrl",
    "AltLeft": "alt",
    "AltRight": "alt",
    "MetaLeft": "super",
    "MetaRight": "super",
    "Enter": "enter",
    "NumpadEnter": "kpenter",
    "Escape": "esc",
    "Backspace": "bsp",
    "Delete": "del",
    "Insert": "ins",
    "Home": "home",
    "End": "end",
    "PageUp": "pgup",
    "PageDown": "pgdn",
    "CapsLock": "caplk",
    "NumLock": "numlk",
    "ScrollLock": "scrlk",
    "Pause": "pause",
    "PrintScreen": "sysrq",
    "Tab": "tab",
    "Space": "space",
    "ArrowUp": "up",
    "ArrowDown": "down",
    "ArrowLeft": "left",
    "ArrowRight": "right",
    "Backquote": "`",
    "Minus": "-",
    "Equal": "=",
    "BracketLeft": "[",
    "BracketRight": "]",
    "Backslash": "\\",
    "IntlBackslash": "\\",
    "Semicolon": ";",
    "Quote": "'",
    "Comma": ",",
    "Period": ".",
    "Slash": "/",
}


def code_to_vnc_key(code: str) -> str:
    if code in _CODE_TO_VNC:
        return _CODE_TO_VNC[code]
    if re.fullmatch(r"Key[A-Z]", code):
        return code[-1].lower()
    if re.fullmatch(r"Digit[0-9]", code):
        return code[-1]
    if re.fullmatch(r"F(?:[1-9]|1[0-2])", code):
        return code.lower()
    raise ValueError(f"unsupported VNC key code: {code!r}")


def shifted_code_to_character(code: str, keymap: str = "en-us") -> str | None:
    """Return the semantic character for Shift+code on supported layouts."""
    return code_to_character(code, shifted=True, keymap=keymap)


def code_to_character(
    code: str,
    *,
    shifted: bool,
    keymap: str = "en-us",
) -> str | None:
    """Resolve a physical key code to its target-layout printable character."""
    normalized = keymap.lower()
    if normalized in {"en-us", "us"}:
        layout = "us"
    elif normalized in {"en-gb", "uk"}:
        layout = "uk"
    else:
        return code[-1].upper() if shifted and re.fullmatch(r"Key[A-Z]", code) else (
            code[-1].lower() if not shifted and re.fullmatch(r"Key[A-Z]", code) else None
        )
    mapping = {
        (info.code, info.shift): character
        for character, info in ks.CHAR_TO_KEY.items()
    }
    if layout == "uk":
        for character, info in ks.UK_OVERRIDES.items():
            mapping[(info.code, info.shift)] = character
    return mapping.get((code, shifted))


class VncDotoolTransport:
    """Real RFB transport, loaded only by the lab CLI.

    ``vncdotool`` runs Twisted in its own thread. Each public method is async and
    serialised so FastAPI never issues overlapping RFB operations.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        password: str | None = None,
        username: str | None = None,
        ocr_lang: str = "eng",
        keymap: str = "en-us",
        keyboard_profile: str = "generic",
    ) -> None:
        self.endpoint = normalize_vnc_endpoint(endpoint)
        self.password = password
        self.username = username
        self.ocr_lang = ocr_lang
        self.keymap = keymap
        self.keyboard_profile = keyboard_profile
        self.width = 1
        self.height = 1
        self._client: Any | None = None
        self._target_lease: VncTargetLease | None = None
        self._lock = asyncio.Lock()
        self._mouse_x = 0
        self._mouse_y = 0
        self._shift_pending = False
        self._shift_sent = False
        self._semantic_shift_keys: dict[str, str] = {}
        self._synthetic_keyups: set[str] = set()
        self._active_chord_modifiers: set[str] = set()

    async def connect(self) -> None:
        if self._client is not None:
            return
        lease = VncTargetLease.acquire(self.endpoint)
        client: Any | None = None
        try:
            try:
                from vncdotool import api, client as vnc_client
            except ImportError as exc:  # pragma: no cover - environment-dependent
                raise RuntimeError(
                    "VNC lab support is not installed; install pikvm-agent[harness]"
                ) from exc

            class LabVncFactory(vnc_client.VNCDoToolFactory):
                # The adapter owns modifier state; vncdotool must not synthesize a
                # US-layout Shift chord from a semantic character.
                force_caps = False

            connect_task = asyncio.create_task(
                asyncio.to_thread(
                    api.connect,
                    self.endpoint,
                    self.password,
                    LabVncFactory,
                    timeout=30,
                    username=self.username,
                )
            )
            try:
                client = await asyncio.shield(connect_task)
            except asyncio.CancelledError:
                # Cancelling ``to_thread`` only abandons the await; the VNC
                # connection continues in its worker. Keep the lease until the
                # bounded attempt finishes, then close any client it created.
                try:
                    client = await asyncio.shield(connect_task)
                except Exception:
                    client = None
                if client is not None:
                    await asyncio.to_thread(client.disconnect)
                    client = None
                raise
            self._client = client
            await asyncio.to_thread(self._release_client_modifiers)
            await self.screenshot()
        except BaseException:
            self._client = None
            if client is not None:
                await asyncio.to_thread(client.disconnect)
            lease.release()
            raise
        self._target_lease = lease

    def _release_client_modifiers(self) -> None:
        client = self._require()
        for modifier in ("shift", "ctrl", "alt", "super"):
            client.keyUp(modifier)
        self._shift_pending = False
        self._shift_sent = False
        self._semantic_shift_keys.clear()
        self._synthetic_keyups.clear()

    @staticmethod
    def _type_windows_alt_code(client: Any, character: str) -> None:
        client.keyDown("alt")
        try:
            for digit in str(ord(character)):
                client.keyPress(f"kp{digit}")
        finally:
            client.keyUp("alt")

    async def close(self) -> None:
        client, self._client = self._client, None
        lease, self._target_lease = self._target_lease, None
        try:
            if client is not None:
                await asyncio.to_thread(client.disconnect)
                # The adapter is the only VNC consumer in this process. Stopping
                # vncdotool's reactor lets uvicorn terminate cleanly on Ctrl+C.
                from vncdotool import api

                await asyncio.to_thread(api.shutdown)
        finally:
            if lease is not None:
                lease.release()

    def _require(self) -> Any:
        if self._client is None:
            raise RuntimeError("VNC transport is not connected")
        return self._client

    async def screenshot(self) -> bytes:
        async with self._lock:
            client = self._require()

            def capture() -> tuple[bytes, int, int]:
                output = io.BytesIO()
                client.captureScreen(output, format="PNG")
                output.seek(0)
                image = Image.open(output).convert("RGB")
                encoded = io.BytesIO()
                image.save(encoded, "JPEG", quality=90)
                return encoded.getvalue(), image.width, image.height

            data, self.width, self.height = await asyncio.to_thread(capture)
            return data

    async def key(self, code: str, down: bool) -> None:
        async with self._lock:
            client = self._require()
            key = code_to_vnc_key(code)
            if code in {"ShiftLeft", "ShiftRight"}:
                if down:
                    # Delay the physical modifier until the following key is
                    # known. Windows VNC servers often mishandle Shift plus a
                    # lowercase letter keysym, but accept an uppercase keysym.
                    self._shift_pending = True
                else:
                    if self._shift_sent:
                        await asyncio.to_thread(client.keyUp, "shift")
                    self._shift_pending = False
                    self._shift_sent = False
                return

            semantic = self._semantic_shift_keys.get(code)
            if not down and semantic is not None:
                self._semantic_shift_keys.pop(code, None)
                await asyncio.to_thread(client.keyUp, semantic)
                return
            if not down and code in self._synthetic_keyups:
                self._synthetic_keyups.discard(code)
                return
            if (
                down
                and self.keyboard_profile == "windows"
                and (
                    code == "IntlBackslash"
                    or (
                        code == "Backslash"
                        and self._shift_pending
                        and ks.keymap_to_layout(self.keymap) == "uk"
                    )
                )
            ):
                character = (
                    "~"
                    if code == "Backslash"
                    else "|" if self._shift_pending else "\\"
                )
                self._synthetic_keyups.add(code)
                await asyncio.to_thread(
                    self._type_windows_alt_code,
                    client,
                    character,
                )
                return
            semantic_shift = (
                code[-1].upper()
                if re.fullmatch(r"Key[A-Z]", code)
                else None
            )
            if (
                down
                and self._shift_pending
                and not self._shift_sent
                and semantic_shift is not None
            ):
                semantic = semantic_shift
                self._semantic_shift_keys[code] = semantic
                await asyncio.to_thread(client.keyDown, semantic)
                return
            if down and self._shift_pending and not self._shift_sent:
                await asyncio.to_thread(client.keyDown, "shift")
                self._shift_sent = True

            modifier = (
                "ctrl"
                if code in {"ControlLeft", "ControlRight"}
                else "alt"
                if code in {"AltLeft", "AltRight"}
                else "super"
                if code in {"MetaLeft", "MetaRight"}
                else None
            )
            if modifier is not None:
                if down:
                    self._active_chord_modifiers.add(modifier)
                else:
                    self._active_chord_modifiers.discard(modifier)
                method = client.keyDown if down else client.keyUp
                await asyncio.to_thread(method, modifier)
                return

            method = client.keyDown if down else client.keyUp
            await asyncio.to_thread(method, key)

    async def mouse_move(self, x: int, y: int) -> None:
        async with self._lock:
            self._mouse_x, self._mouse_y = x, y
            await asyncio.to_thread(self._require().mouseMove, x, y)

    async def mouse_relative(self, dx: int, dy: int) -> None:
        await self.mouse_move(
            max(0, min(self.width - 1, self._mouse_x + dx)),
            max(0, min(self.height - 1, self._mouse_y + dy)),
        )

    async def mouse_button(self, button: str, down: bool) -> None:
        number = {"left": 1, "middle": 2, "right": 3}.get(button, 1)
        async with self._lock:
            method = self._require().mouseDown if down else self._require().mouseUp
            await asyncio.to_thread(method, number)

    async def mouse_wheel(self, dx: int, dy: int) -> None:
        async with self._lock:
            client = self._require()

            def wheel() -> None:
                vertical = 4 if dy > 0 else 5
                horizontal = 7 if dx > 0 else 6
                for _ in range(abs(dy)):
                    client.mousePress(vertical)
                for _ in range(abs(dx)):
                    client.mousePress(horizontal)

            await asyncio.to_thread(wheel)

    async def print_text(self, text: str) -> None:
        async with self._lock:
            client = self._require()
            layout = ks.keymap_to_layout(self.keymap) or "us"

            def type_all() -> None:
                for char in text:
                    key_info = ks.key_for(char, layout)
                    if key_info is None:
                        client.keyPress(char)
                    else:
                        key = code_to_vnc_key(key_info.code)
                        if (
                            self.keyboard_profile == "windows"
                            and (
                                key_info.code == "IntlBackslash"
                                or (
                                    key_info.code == "Backslash"
                                    and char == "~"
                                    and layout == "uk"
                                )
                            )
                        ):
                            self._type_windows_alt_code(client, char)
                            import time

                            time.sleep(0.020)
                            continue
                        semantic_shift = (
                            key_info.shift
                            and (
                                re.fullmatch(r"Key[A-Z]", key_info.code)
                                is not None
                                or (
                                    key_info.code == "IntlBackslash"
                                    and layout == "uk"
                                )
                            )
                        )
                        if semantic_shift:
                            key = char
                        elif key_info.shift:
                            client.keyDown("shift")
                        try:
                            client.keyDown(key)
                            client.keyUp(key)
                        finally:
                            if key_info.shift and not semantic_shift:
                                client.keyUp("shift")
                    # Some RFB servers silently coalesce/drop back-to-back key
                    # events. PiKVM's slow printer is about 20 ms/character, so
                    # preserve that timing contract in the emulator.
                    import time

                    time.sleep(0.020)

            await asyncio.to_thread(type_all)

    async def ocr(self, *, left: int, top: int, right: int, bottom: int) -> str:
        if shutil.which("tesseract") is None:
            return ""
        data = await self.screenshot()

        def prepare() -> Path:
            image = Image.open(io.BytesIO(data))
            crop = image.crop(
                (
                    max(0, left),
                    max(0, top),
                    min(image.width, right),
                    min(image.height, bottom),
                )
            )
            handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            handle.close()
            path = Path(handle.name)
            crop.save(path)
            return path

        path = await asyncio.to_thread(prepare)
        try:
            proc = await asyncio.create_subprocess_exec(
                "tesseract",
                str(path),
                "stdout",
                "-l",
                self.ocr_lang,
                "--psm",
                "6",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            return stdout.decode("utf-8", errors="replace").strip()
        finally:
            path.unlink(missing_ok=True)
