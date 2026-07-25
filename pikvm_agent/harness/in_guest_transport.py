"""PiKVM lab transport for OSWorld-style in-guest evaluation servers.

The endpoint is supplied at runtime. The transport exposes only screenshots and
fixed HID-equivalent pyautogui templates; task setup, filesystem inspection and
evaluation stay on the upstream coordinator side and are never exposed to the
agent or MCP server.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from pikvm_agent.harness.vnc_pikvm_api import code_to_vnc_key


_PYAUTOGUI_KEYS = {
    "bsp": "backspace",
    "del": "delete",
    "esc": "esc",
    "ins": "insert",
    "pgup": "pgup",
    "pgdn": "pgdn",
    "super": "win",
}


def _pyautogui_key(code: str) -> str:
    key = code_to_vnc_key(code)
    return _PYAUTOGUI_KEYS.get(key, key)


class InGuestComputerTransport:
    """Translate the lab's PiKVM-shaped contract to a benchmark guest server."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient | None = None,
        ocr_lang: str = "eng",
    ) -> None:
        value = endpoint.strip().rstrip("/")
        parsed = httpx.URL(value)
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ValueError("in-guest endpoint must be an absolute http(s) URL")
        if parsed.userinfo:
            raise ValueError("put credentials in transport configuration, not the URL")
        self.endpoint = value
        self.ocr_lang = ocr_lang
        self.width = 1
        self.height = 1
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=value,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self._lock = asyncio.Lock()
        self._pointer = (0, 0)
        self._pointer_dirty = False

    async def connect(self) -> None:
        await self.screenshot()

    async def close(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def screenshot(self) -> bytes:
        await self._flush_pointer()
        image: Image.Image | None = None
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self._client.get("/screenshot")
                response.raise_for_status()
                with Image.open(io.BytesIO(response.content)) as source:
                    source.load()
                    image = source.convert("RGB")
                break
            except (httpx.HTTPError, OSError, SyntaxError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.1 * (attempt + 1))
        if image is None:
            raise RuntimeError(
                "in-guest screenshot remained corrupt or unavailable after 3 attempts"
            ) from last_error
        self.width, self.height = image.size
        if self._pointer == (0, 0):
            self._pointer = (self.width // 2, self.height // 2)
        output = io.BytesIO()
        image.save(output, "JPEG", quality=90)
        return output.getvalue()

    async def _post_script(self, expression: str) -> None:
        script = (
            "import pyautogui\n"
            "pyautogui.FAILSAFE = False\n"
            f"{expression}\n"
        )
        response = await self._client.post("/run_python", json={"code": script})
        response.raise_for_status()
        payload: Any = response.json()
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise RuntimeError("in-guest HID template failed")

    async def _run(self, expression: str) -> None:
        async with self._lock:
            await self._post_script(expression)

    async def _flush_pointer(self, extra: str | None = None) -> None:
        async with self._lock:
            expressions: list[str] = []
            if self._pointer_dirty:
                x, y = self._pointer
                expressions.append(f"pyautogui.moveTo({x}, {y}, duration=0)")
                self._pointer_dirty = False
            if extra:
                expressions.append(extra)
            if expressions:
                await self._post_script("; ".join(expressions))

    async def key(self, code: str, down: bool) -> None:
        key = _pyautogui_key(code)
        method = "keyDown" if down else "keyUp"
        await self._run(f"pyautogui.{method}({key!r})")

    async def mouse_move(self, x: int, y: int) -> None:
        x = max(0, min(self.width - 1, int(x)))
        y = max(0, min(self.height - 1, int(y)))
        # PiKVM's WindMouse can emit ~80 intermediate positions. Starting a
        # guest Python process for each point is both slow and less faithful
        # than one HID motion. Coalesce the path and flush its exact landing
        # point before a button event or screenshot.
        self._pointer = (x, y)
        self._pointer_dirty = True

    async def mouse_relative(self, dx: int, dy: int) -> None:
        x, y = self._pointer
        await self.mouse_move(x + int(dx), y + int(dy))

    async def mouse_button(self, button: str, down: bool) -> None:
        safe_button = button if button in {"left", "middle", "right"} else "left"
        method = "mouseDown" if down else "mouseUp"
        await self._flush_pointer(
            f"pyautogui.{method}(button={safe_button!r})"
        )

    async def mouse_wheel(self, dx: int, dy: int) -> None:
        expressions: list[str] = []
        if dy:
            expressions.append(f"pyautogui.scroll({int(dy)})")
        if dx:
            expressions.append(f"pyautogui.hscroll({int(dx)})")
        if expressions:
            await self._run("; ".join(expressions))

    async def print_text(self, text: str) -> None:
        # Newlines are flattened exactly as in the real PiKVM print endpoint:
        # typing prose cannot accidentally submit a terminal command or message.
        body = " ".join(text.splitlines())
        if body:
            await self._run(f"pyautogui.write({body!r}, interval=0.02)")

    async def ocr(
        self,
        *,
        left: int,
        top: int,
        right: int,
        bottom: int,
    ) -> str:
        if shutil.which("tesseract") is None:
            return ""
        data = await self.screenshot()
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
        try:
            process = await asyncio.create_subprocess_exec(
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
            stdout, _ = await process.communicate()
            return stdout.decode("utf-8", errors="replace").strip()
        finally:
            path.unlink(missing_ok=True)
