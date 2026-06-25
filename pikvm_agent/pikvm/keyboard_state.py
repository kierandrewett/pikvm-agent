"""Target keyboard state + physical key mapping.

Ported from the TypeScript client (``src/pikvm-control.ts`` /
``src/human-typing.ts``). We send physical JS ``KeyboardEvent.code`` values over
HID; the target's keymap decides the glyph. This module owns:

  * char -> (code, shift) mapping for US and UK ISO layouts,
  * Caps-Lock compensation (invert Shift for letters when the LED is on),
  * the cached KVMD state stream (LEDs, keymap, native resolution, mouse mode).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Layout = Literal["us", "uk"]


@dataclass(frozen=True)
class KeyInfo:
    code: str
    shift: bool


# Physical rows as they sit on a US board (unshifted), aligned three ways.
_ROWS = [
    list("`1234567890-="),
    list("qwertyuiop[]\\"),
    list("asdfghjkl;'"),
    list("zxcvbnm,./"),
]
_SHIFT_ROWS = [
    list("~!@#$%^&*()_+"),
    list("QWERTYUIOP{}|"),
    list('ASDFGHJKL:"'),
    list("ZXCVBNM<>?"),
]
_CODE_ROWS = [
    "Backquote Digit1 Digit2 Digit3 Digit4 Digit5 Digit6 Digit7 Digit8 Digit9 Digit0 Minus Equal".split(),
    "KeyQ KeyW KeyE KeyR KeyT KeyY KeyU KeyI KeyO KeyP BracketLeft BracketRight Backslash".split(),
    "KeyA KeyS KeyD KeyF KeyG KeyH KeyJ KeyK KeyL Semicolon Quote".split(),
    "KeyZ KeyX KeyC KeyV KeyB KeyN KeyM Comma Period Slash".split(),
]

CHAR_TO_KEY: dict[str, KeyInfo] = {}
for _r, _row in enumerate(_ROWS):
    for _c, _ch in enumerate(_row):
        _code = _CODE_ROWS[_r][_c]
        CHAR_TO_KEY[_ch] = KeyInfo(_code, False)
        _sh = _SHIFT_ROWS[_r][_c]
        if _sh:
            CHAR_TO_KEY[_sh] = KeyInfo(_code, True)
CHAR_TO_KEY[" "] = KeyInfo("Space", False)
CHAR_TO_KEY["\t"] = KeyInfo("Tab", False)

# UK ISO overrides: printable chars whose PHYSICAL key differs from US. Without
# these, `cd ~/...` types `cd ¬/...` and `"` types `@` on a UK target.
UK_OVERRIDES: dict[str, KeyInfo] = {
    '"': KeyInfo("Digit2", True),
    "@": KeyInfo("Quote", True),
    "#": KeyInfo("Backslash", False),
    "~": KeyInfo("Backslash", True),
    "\\": KeyInfo("IntlBackslash", False),
    "|": KeyInfo("IntlBackslash", True),
    "£": KeyInfo("Digit3", True),
    "¬": KeyInfo("Backquote", True),
}


def key_for(ch: str, layout: Layout = "us") -> KeyInfo | None:
    """Resolve a character to a physical key + shift state for the given layout."""
    if layout == "uk" and ch in UK_OVERRIDES:
        return UK_OVERRIDES[ch]
    return CHAR_TO_KEY.get(ch)


def compensate_caps_lock(strokes: list[dict[str, Any]], caps_on: bool) -> list[dict[str, Any]]:
    """Invert Shift for letter keys when the target Caps-Lock LED is ON, so the
    OUTPUT case is correct without toggling the target's Caps Lock. Letters only;
    digits/symbols are unaffected. Mutates and returns ``strokes`` (each a dict
    with ``code`` and ``shift``)."""
    if not caps_on:
        return strokes
    for s in strokes:
        code = s.get("code", "")
        if len(code) == 4 and code.startswith("Key") and code[3].isalpha():
            s["shift"] = not s["shift"]
    return strokes


def keymap_to_layout(name: str | None) -> Layout | None:
    """Map a KVMD keymap name (e.g. "en-gb") to our send-side layout, or None for
    layouts we can't represent (so the current layout is kept, not mis-forced)."""
    if not name:
        return None
    n = name.lower()
    if n == "en-gb" or n.startswith("en-gb"):
        return "uk"
    if n == "en-us" or n.startswith("en-us"):
        return "us"
    return None


# --------------------------------------------------------------------------- #
# KVMD state stream cache (server -> client events on /api/ws)
# --------------------------------------------------------------------------- #


@dataclass
class KvmdState:
    hid: dict[str, Any] = field(default_factory=dict)
    keymaps: dict[str, Any] = field(default_factory=dict)
    streamer: dict[str, Any] = field(default_factory=dict)
    ocr: dict[str, Any] = field(default_factory=dict)
    ready: bool = False


def _deep_merge(base: dict[str, Any], patch: Any) -> dict[str, Any]:
    if not isinstance(patch, dict):
        return base
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def merge_kvmd_event(prev: KvmdState, event_type: str, event: Any) -> KvmdState:
    """Merge one /api/ws event into the cached state. Total + pure — unknown
    events pass through unchanged so it can never throw on an unmodelled shape."""
    if event_type == "hid":
        prev.hid = _deep_merge(prev.hid, event)
    elif event_type == "hid_keymaps":
        keymaps = event.get("keymaps") if isinstance(event, dict) else None
        prev.keymaps = _deep_merge(prev.keymaps, keymaps)
    elif event_type == "streamer":
        prev.streamer = _deep_merge(prev.streamer, event)
    elif event_type == "ocr":
        prev.ocr = _deep_merge(prev.ocr, event)
    elif event_type == "loop":
        prev.ready = True
    return prev


def caps_lock_of(state: KvmdState) -> bool | None:
    leds = (state.hid.get("keyboard") or {}).get("leds") or {}
    return leds.get("caps")


def keymap_default_of(state: KvmdState) -> str | None:
    return state.keymaps.get("default")


def native_resolution_of(state: KvmdState) -> tuple[int, int] | None:
    res = ((state.streamer.get("source") or {}).get("resolution")) or {}
    w, h = res.get("width"), res.get("height")
    if w and h:
        return int(w), int(h)
    return None


def hid_online_of(state: KvmdState) -> bool | None:
    """Tri-state: True attached, False detached (block input), None unknown."""
    h = state.hid
    if not h:
        return None
    if h.get("connected") is False:
        return False
    if h.get("online") is False or (h.get("keyboard") or {}).get("online") is False:
        return False
    if h.get("online") is True:
        return True
    return None
