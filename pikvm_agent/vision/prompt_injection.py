"""Visible prompt-injection scanner.

Scans OCR'd on-screen text for instruction phrases that try to override the
agent's directives ("ignore previous instructions", "you are now…"). Produces
*evidence* only: a list of matched phrases the daemon can raise as a
``possible_prompt_injection`` event. It never decides or executes anything.
"""

from __future__ import annotations

import re

_I = re.IGNORECASE

# Each entry: (compiled case-insensitive pattern, canonical label). The label is
# the phrase reported back, not the raw matched text, so callers get a stable set.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ignore\s+(?:all\s+)?(?:the\s+)?previous\s+instructions", _I),
     "ignore previous instructions"),
    (re.compile(r"ignore\s+all\s+(?:the\s+)?prior\b", _I), "ignore all prior"),
    (re.compile(r"ignore\s+(?:everything|all)\s+(?:that\s+came\s+)?above", _I),
     "ignore everything above"),
    (re.compile(r"disregard\s+(?:the\s+|all\s+)?above", _I), "disregard the above"),
    (re.compile(r"disregard\s+(?:all\s+)?(?:the\s+)?previous\b", _I),
     "disregard previous instructions"),
    (re.compile(r"forget\s+(?:all\s+)?(?:your\s+|the\s+)?(?:previous\s+)?instructions", _I),
     "forget your instructions"),
    (re.compile(r"you\s+are\s+now\b", _I), "you are now"),
    (re.compile(r"system\s+prompt\b", _I), "system prompt"),
    (re.compile(r"developer\s+mode\b", _I), "developer mode"),
    # "do anything now" is case-insensitive like every other phrase; the bare
    # DAN acronym stays case-sensitive so it doesn't fire on the name "Dan".
    (re.compile(r"do\s+anything\s+now\b", _I), "do anything now"),
    (re.compile(r"\bDAN\b"), "DAN"),
    (re.compile(r"new\s+instructions?\s*:", _I), "new instructions:"),
    (re.compile(r"override\s+(?:your\s+|the\s+)?(?:safety|system|previous)\b", _I),
     "override your instructions"),
    (re.compile(r"reveal\s+(?:your\s+|the\s+)?(?:system\s+)?prompt", _I),
     "reveal your prompt"),
]


def scan(text: str) -> list[str]:
    """Return the canonical labels of suspicious instruction phrases found.

    Case-insensitive; duplicates collapsed; empty list if nothing matches.
    """
    if not text:
        return []

    found: list[str] = []
    seen: set[str] = set()
    for pattern, label in _PATTERNS:
        if pattern.search(text) and label not in seen:
            seen.add(label)
            found.append(label)
    return found


def has_injection(text: str) -> bool:
    """True if any suspicious instruction phrase is present."""
    return bool(scan(text))


__all__ = ["scan", "has_injection"]
