"""Human-timing distributions for HID I/O.

One home for the cadence of keystrokes, clicks, chords and reaction time, so the
daemon's I/O to the PiKVM looks human instead of metronomic. Everything returns
milliseconds (``*_ms``) except reaction (``*_s``). The numbers are tuned to look
human, not to a spec; each takes an injectable ``random.Random`` for tests.

Keystroke timing is right-skewed (log-normal — humans have a long tail of slow
keys), anchored to a per-session WPM persona, with think-pauses at word/sentence
boundaries and a slow-down on repeated keys — the techniques the old TypeScript
``human-typing.ts`` used, distilled.
"""

from __future__ import annotations

import math
import random

_DEFAULT = random.Random()


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


# ---- keystrokes ----------------------------------------------------------- #

def base_gap_ms(rng: random.Random = _DEFAULT) -> float:
    """A session's baseline inter-key gap, from a per-persona WPM (~65-130, mean 95).
    Drawn ONCE per backend so a session types at a consistent personal speed."""
    wpm = _clamp(rng.gauss(95, 12), 65, 130)
    return 60000.0 / (wpm * 5.0)  # 5 chars/word


def key_hold_ms(rng: random.Random = _DEFAULT) -> float:
    """How long a key is held down (key-down → key-up). Log-normal, ~48ms median."""
    return _clamp(rng.lognormvariate(math.log(48), 0.30), 22, 130)


def inter_key_gap_ms(prev: str | None, ch: str, base_gap: float,
                     rng: random.Random = _DEFAULT) -> float:
    """Gap AFTER typing ``ch`` (whose predecessor was ``prev``). Log-normal jitter
    around the persona base, a slow-down on repeated keys, and occasional
    think-pauses after spaces / sentence / clause punctuation."""
    g = base_gap * math.exp(rng.gauss(0, 0.38))
    if prev is not None and prev == ch and ch.isalpha():
        g *= 1.4  # same key twice (ll, ss) is slower
    g = _clamp(g, 16, 600)
    if ch == " " and rng.random() < 0.12:
        g += _clamp(rng.gauss(420, 200), 180, 1200)          # mid-sentence think pause
    elif ch in ".!?" and rng.random() < 0.5:
        g += _clamp(rng.gauss(560, 260), 220, 1500)          # end-of-sentence pause
    elif ch in ",;:" and rng.random() < 0.25:
        g += _clamp(rng.gauss(260, 120), 120, 720)           # clause pause
    return g


# ---- clicks / keys -------------------------------------------------------- #

def click_settle_ms(rng: random.Random = _DEFAULT) -> float:
    """Hover-settle after the cursor arrives, before the button goes down."""
    return _clamp(rng.gauss(120, 45), 55, 260)


def click_hold_ms(rng: random.Random = _DEFAULT) -> float:
    return _clamp(rng.gauss(70, 22), 35, 140)


def double_click_gap_ms(rng: random.Random = _DEFAULT) -> float:
    return _clamp(rng.gauss(95, 30), 55, 185)


def press_dwell_ms(rng: random.Random = _DEFAULT) -> float:
    """Dwell of a single key tap (Enter/Tab/Delete…)."""
    return _clamp(rng.gauss(70, 22), 35, 140)


def chord_stagger_ms(rng: random.Random = _DEFAULT) -> float:
    """Gap between successive keys of a chord going down (or up)."""
    return _clamp(rng.gauss(45, 18), 16, 110)


def chord_hold_ms(rng: random.Random = _DEFAULT) -> float:
    return _clamp(rng.gauss(75, 25), 40, 150)


# ---- reaction / print ----------------------------------------------------- #

def reaction_s(rng: random.Random = _DEFAULT) -> float:
    """"Saw the screen, now reaching for the mouse/keys" delay before an action.
    Human simple-reaction time, ~150-400ms; returned in SECONDS for asyncio.sleep."""
    return _clamp(rng.gauss(0.24, 0.09), 0.10, 0.55)


def print_chunk_pause_ms(tail: str, rng: random.Random = _DEFAULT) -> float:
    """Pause between fast-print bursts; longer when the burst ended a sentence."""
    g = _clamp(rng.gauss(110, 50), 30, 320)
    if tail[-1:] in ".!?" and rng.random() < 0.5:
        g += _clamp(rng.gauss(450, 220), 180, 1200)
    return g


def word_chunks(text: str, target: int = 42, rng: random.Random = _DEFAULT) -> list[str]:
    """Split text into word-boundary bursts of ~``target`` chars (jittered ±25%) so
    fast-print goes out as human bursts rather than one uniform stream.
    Invariant: ``"".join(word_chunks(s)) == s``."""
    if len(text) <= target:
        return [text] if text else []
    out: list[str] = []
    buf = ""
    cap = int(target * (0.75 + rng.random() * 0.5))
    for word in _split_keep_spaces(text):
        if buf and len(buf) + len(word) > cap and buf.strip():
            out.append(buf)
            buf = ""
            cap = int(target * (0.75 + rng.random() * 0.5))
        buf += word
    if buf:
        out.append(buf)
    return out


def _split_keep_spaces(s: str) -> list[str]:
    import re
    return [w for w in re.split(r"(\s+)", s) if w]
