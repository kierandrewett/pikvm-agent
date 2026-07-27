"""Text shaping shared by every non-submitting keyboard transport."""

from __future__ import annotations

import re


_LINE_BREAKS = re.compile(
    r"(?:\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029])"
)
_LINE_BREAK_BOUNDARY = re.compile(
    r"[ \t]*(?:(?:\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029])[ \t]*)+"
)
def flatten_line_breaks(text: str) -> str:
    """Turn a line-break boundary into exactly one non-submitting space.

    PiKVM's typing tools must never turn a newline into Enter. Replacing each
    newline independently is subtly wrong, though: ``"word \n next"`` becomes
    three spaces. Collapse only whitespace touching a line-break boundary and
    preserve deliberate repeated spaces inside a line.
    """

    if not _LINE_BREAKS.search(text):
        return text
    return _LINE_BREAK_BOUNDARY.sub(" ", text)
