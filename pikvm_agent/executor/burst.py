"""PiKVM HID burst engine — the fast path for a model-in-the-loop controller.

A *burst* is a short, controller-authored sequence of raw HID actions (keys, typed
text, clicks, scrolls, waits) that the daemon executes LOCALLY in one shot — so one
model decision covers "Ctrl+P → type path → Enter → wait for the screen to settle"
instead of five round-trips. No OmniParser, no full-frame OCR, no operator LLM in
this path: the controller (Claude/Codex) is the brain and already knows what to do.

The engine only DISPATCHES HID — freshness / control-epoch / panic gating and the
screenshot bookkeeping live in the runtime. Between every action it polls a
``should_continue`` gate (abort/panic/steer/lease) and the per-call deadline, so a
burst stops mid-sequence the instant control changes. It reuses the same humanized
backend (WindMouse, humanized typing) as everything else.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pikvm_agent.vision.frame_diff import FP_MEANINGFUL, grid

# --- key-name normalisation ------------------------------------------------ #
# Accept friendly tokens ("CTRL", "P", "ENTER") AND already-valid PiKVM codes
# ("ControlLeft", "KeyP", "Enter"), so a controller can use either.

_MODS = {
    "CTRL": "ControlLeft", "CONTROL": "ControlLeft", "LCTRL": "ControlLeft", "RCTRL": "ControlRight",
    "SHIFT": "ShiftLeft", "LSHIFT": "ShiftLeft", "RSHIFT": "ShiftRight",
    "ALT": "AltLeft", "OPTION": "AltLeft", "LALT": "AltLeft", "RALT": "AltRight", "ALTGR": "AltRight",
    "META": "MetaLeft", "WIN": "MetaLeft", "WINDOWS": "MetaLeft", "CMD": "MetaLeft", "SUPER": "MetaLeft",
}
_NAMED = {
    "ENTER": "Enter", "RETURN": "Enter", "TAB": "Tab", "ESC": "Escape", "ESCAPE": "Escape",
    "SPACE": "Space", "SPACEBAR": "Space", "BACKSPACE": "Backspace", "BKSP": "Backspace",
    "DELETE": "Delete", "DEL": "Delete", "HOME": "Home", "END": "End",
    "PAGEUP": "PageUp", "PGUP": "PageUp", "PAGEDOWN": "PageDown", "PGDN": "PageDown",
    "UP": "ArrowUp", "DOWN": "ArrowDown", "LEFT": "ArrowLeft", "RIGHT": "ArrowRight",
    "INSERT": "Insert", "INS": "Insert", "CAPSLOCK": "CapsLock", "PRINTSCREEN": "PrintScreen",
    "MINUS": "Minus", "EQUAL": "Equal", "PLUS": "Equal", "PERIOD": "Period", "DOT": "Period",
    "COMMA": "Comma", "SLASH": "Slash", "BACKSLASH": "Backslash", "SEMICOLON": "Semicolon",
}


def normalize_key(token: str) -> str | None:
    t = (token or "").strip()
    if not t:
        return None
    up = t.upper()
    if up in _MODS:
        return _MODS[up]
    if up in _NAMED:
        return _NAMED[up]
    if len(t) == 1 and t.isalpha():
        return "Key" + t.upper()
    if len(t) == 1 and t.isdigit():
        return "Digit" + t
    if up[0] == "F" and up[1:].isdigit() and 1 <= int(up[1:]) <= 24:
        return up  # F1..F24
    return t  # assume it's already a valid PiKVM code (KeyA, ControlLeft, …)


def normalize_keys(keys: list[str]) -> list[str]:
    return [k for k in (normalize_key(x) for x in keys) if k]


# --- outcome --------------------------------------------------------------- #

@dataclass
class BurstOutcome:
    status: str                 # "completed" | "interrupted" | "failed"
    completed: int              # actions fully executed
    total: int
    reason: str = ""            # why it stopped early (control_changed / deadline / error / …)
    error: str = ""
    executed: list[str] = field(default_factory=list)  # action types that ran

    @property
    def remaining(self) -> int:
        return self.total - self.completed


class BurstError(Exception):
    """A malformed/unsupported burst action — surfaced to the controller, never executed."""


class TypingNotVerified(Exception):
    """Typed text was read back and is CONFIRMED wrong (or the field wasn't focused). The
    burst stops here so the next action (e.g. Enter) can't act on the wrong text."""


# --- the engine ------------------------------------------------------------ #

ShouldContinue = Callable[[], bool]


async def run_burst(
    actions: list[dict[str, Any]],
    *,
    backend: Any,
    should_continue: ShouldContinue | None = None,
    deadline_ms: float | None = None,
    typer: Any = None,
) -> BurstOutcome:
    """Execute ``actions`` as one local HID burst. Polls ``should_continue`` (control /
    panic / lease) and ``deadline_ms`` between every action and stops mid-burst if either
    trips — returning how far it got so the controller can re-plan from a fresh screen."""
    total = len(actions)
    executed: list[str] = []

    def _stop() -> tuple[str, str] | None:
        if should_continue is not None and not should_continue():
            return ("interrupted", "control_changed")
        if deadline_ms is not None and time.monotonic() * 1000 >= deadline_ms:
            return ("interrupted", "deadline")
        return None

    for i, raw in enumerate(actions):
        stop = _stop()
        if stop is not None:
            return BurstOutcome(stop[0], i, total, reason=stop[1], executed=executed)
        a = raw if isinstance(raw, dict) else dict(raw)
        kind = a.get("type")
        try:
            await _dispatch(a, kind, backend=backend, typer=typer, should_continue=should_continue)
        except BurstError:
            raise
        except TypingNotVerified as exc:
            # Confirmed wrong typed text — stop BEFORE the next action (don't Enter on it).
            return BurstOutcome("failed", i, total, reason="type_unverified",
                                error=str(exc), executed=executed)
        except Exception as exc:  # noqa: BLE001 - a backend failure ends the burst, not the daemon
            return BurstOutcome("failed", i, total, reason="action_error",
                                error=f"{kind}: {exc}", executed=executed)
        executed.append(str(kind))

    return BurstOutcome("completed", total, total, executed=executed)


async def _dispatch(a: dict[str, Any], kind: str | None, *, backend: Any, typer: Any,
                    should_continue: ShouldContinue | None) -> None:
    if kind == "key":
        keys = normalize_keys(a.get("keys") or ([a["key"]] if a.get("key") else []))
        if not keys:
            raise BurstError("key action needs 'keys' (or 'key')")
        await backend.keypress(keys)
    elif kind == "type_text":
        text = a.get("text", "")
        method = str(a.get("method", "")).lower()
        code, secret = bool(a.get("code")), bool(a.get("secret"))
        fast = method in ("print", "hid_print", "pikvm_hid_print")
        if fast and hasattr(backend, "print_text"):
            # Explicit FAST path: server-side HID printer, no read-back. Use only when you
            # don't care to confirm what landed (and it's plain, non-secret text).
            await backend.print_text(text)
        elif typer is not None and not a.get("no_verify") and not secret:
            # DEFAULT: watched typer — humanized, reads the field back, self-corrects once.
            # A CONFIRMED wrong result (not merely "couldn't read it") stops the burst so the
            # next action can't run on bad text — exactly the Ctrl+F mistake-blindness risk.
            res = await typer.type_text(text, code=code, secret=secret,
                                        should_continue=should_continue)
            status = str(getattr(res, "status", "") or "")
            if status.startswith("failed_"):
                raise TypingNotVerified(
                    f"typed {text!r} but read-back disagrees ({status}): "
                    f"{getattr(res, 'summary', '')}")
        else:
            await backend.type_text(text, code=code, secret=secret)
    elif kind in ("click", "double_click"):
        x, y = int(a["x"]), int(a["y"])
        button = a.get("button", "left")
        if kind == "double_click" and hasattr(backend, "double_click"):
            await backend.double_click(x, y, button)
        else:
            await backend.click(x, y, button)
    elif kind == "move":
        await backend.move_mouse(int(a["x"]), int(a["y"]))
    elif kind == "scroll":
        ux, uy = _SCROLL.get(a.get("direction", "down"), (0, -1))
        amount = max(1, int(a.get("amount", 3)))
        await backend.scroll(ux * amount, uy * amount)
    elif kind == "wait":
        await asyncio.sleep(max(0, int(a.get("ms", 0))) / 1000.0)
    elif kind == "wait_for_stable_screen":
        await wait_for_stable_screen(backend, stable_ms=int(a.get("stable_ms", 300)),
                                     timeout_ms=int(a.get("timeout_ms", 1500)),
                                     should_continue=should_continue)
    elif kind == "wait_for_change":
        await wait_for_screen_change(backend, timeout_ms=int(a.get("timeout_ms", 8000)),
                                     should_continue=should_continue)
    else:
        raise BurstError(f"unsupported burst action: {kind!r}")


_SCROLL = {"up": (0, 1), "down": (0, -1), "right": (1, 0), "left": (-1, 0)}


async def wait_for_screen_change(backend: Any, *, timeout_ms: int = 8000, poll_ms: int = 150,
                                 should_continue: ShouldContinue | None = None) -> bool:
    """Block until the screen CHANGES from how it looks right now (an app launching, a remote
    desktop connecting, a page loading), or ``timeout_ms`` elapses — so a burst can say 'wait
    for it to appear' instead of guessing a blind 20s wait. Returns True if it changed."""
    import numpy as np

    deadline = time.monotonic() * 1000 + max(0, timeout_ms)
    base = None
    while True:
        if should_continue is not None and not should_continue():
            return False
        try:
            frame = await backend.screenshot()
            g = await asyncio.to_thread(grid, frame.data) if frame and frame.data else None
        except Exception:  # noqa: BLE001
            g = None
        if g is not None:
            if base is None:
                base = g
            else:
                delta = float(np.abs(g.astype(np.int32) - base.astype(np.int32)).sum()) / max(1, g.size) / 255.0
                if delta > FP_MEANINGFUL:
                    return True
        if time.monotonic() * 1000 >= deadline:
            return False
        await asyncio.sleep(poll_ms / 1000.0)


async def wait_for_stable_screen(backend: Any, *, stable_ms: int = 300, timeout_ms: int = 1500,
                                 poll_ms: int = 120, should_continue: ShouldContinue | None = None) -> bool:
    """Block until the screen stops changing for ``stable_ms`` (cheap grid frame-diff), or
    ``timeout_ms`` elapses. Lets a burst say 'wait for the editor to finish loading'
    without a model round-trip. Returns True if it settled, False on timeout."""
    deadline = time.monotonic() * 1000 + max(0, timeout_ms)
    last = None
    stable_since: float | None = None
    while True:
        if should_continue is not None and not should_continue():
            return False
        now = time.monotonic() * 1000
        try:
            frame = await backend.screenshot()
            g = await asyncio.to_thread(grid, frame.data) if frame and frame.data else None
        except Exception:  # noqa: BLE001
            g = None
        if g is not None and last is not None:
            import numpy as np

            delta = float(np.abs(g.astype(np.int32) - last.astype(np.int32)).sum()) / max(1, g.size) / 255.0
            if delta <= FP_MEANINGFUL:
                if stable_since is None:
                    stable_since = now
                elif now - stable_since >= stable_ms:
                    return True
            else:
                stable_since = None
        if g is not None:
            last = g
        if now >= deadline:
            return False
        await asyncio.sleep(poll_ms / 1000.0)
