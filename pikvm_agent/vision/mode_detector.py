"""Conservative app/interaction-mode detection from OCR text (+ optional grounding).

Produces *evidence* only: a single `Mode` literal the daemon feeds to
`detect_state`. Pure logic, no network. Heuristics are deliberately cautious —
when nothing matches with confidence we return ``"unknown"`` rather than guess.

Order matters: blocking states (pager, credential, captcha) are checked before a
generic shell-prompt match, because a pager footer or a password prompt can sit
on a line that also superficially resembles a readline prompt.
"""

from __future__ import annotations

import re

from pikvm_agent.core.models import ElementMap, Mode

# --------------------------------------------------------------------------- #
# Prompt regex (reused shape from executor.verification.PROMPT_RE)
# --------------------------------------------------------------------------- #

# A shell/REPL prompt sitting at the end of a line: `$`, `#`, `%`, `>`, `❯`, or a
# `user@host…$` form. Anchored to a line end so prose ending in a symbol does not
# match. Kept here (not imported) so this module stays free of executor deps.
_PROMPT_RE = re.compile(
    r"(?m)^(?:.*?(?:[A-Za-z0-9._-]+@[A-Za-z0-9._-]+).*?[$#%>❯➜]"
    r"|.*?(?:PS\s+[A-Za-z]:\\[^\n]*>|[A-Za-z]:\\[^\n]*>)"
    r"|[$#%>❯➜λ»])\s*$"
)

# --------------------------------------------------------------------------- #
# Rule table — first matching (regex, Mode) wins. All patterns are case-
# insensitive; order encodes priority (blocking states before readline).
# --------------------------------------------------------------------------- #

_IM = re.IGNORECASE | re.MULTILINE

_RULES: list[tuple[re.Pattern[str], Mode]] = [
    # --- terminal pager (E9) -------------------------------------------------
    (re.compile(r"\(END\)", _IM), "terminal.pager"),
    (re.compile(r"--\s*more\s*--", _IM), "terminal.pager"),
    (re.compile(r"press\s+(?:the\s+)?(?:RETURN|ENTER)\b", _IM), "terminal.pager"),
    (re.compile(r"\bpress\s+q\b|\bq\s+to\s+quit\b", _IM), "terminal.pager"),
    (re.compile(r"\blines?\s+\d+-\d+\b", _IM), "terminal.pager"),
    (re.compile(r"^\s*manual page\b|\bmanual page .* line \d+", _IM), "terminal.pager"),
    # a bare `:` prompt alone on the final line (less/more pager waiting state)
    (re.compile(r"^\s*:\s*$", _IM), "terminal.pager"),
    # --- captcha / human verification ---------------------------------------
    (re.compile(r"i['’]?m\s+not\s+a\s+robot", _IM), "captcha_or_human_verification"),
    (re.compile(r"\bcaptcha\b|\brecaptcha\b|\bhcaptcha\b", _IM), "captcha_or_human_verification"),
    (re.compile(r"verify\s+(?:that\s+)?you(?:\s+are|['’]?re)\s+(?:a\s+)?human", _IM),
     "captcha_or_human_verification"),
    (re.compile(r"are\s+you\s+(?:a\s+)?human\b", _IM), "captcha_or_human_verification"),
    # --- credential prompt ---------------------------------------------------
    (re.compile(r"\bpassword\s*:", _IM), "credential_prompt"),
    (re.compile(r"enter\s+your\s+password\b", _IM), "credential_prompt"),
    (re.compile(r"authentication\s+required\b", _IM), "credential_prompt"),
    (re.compile(r"\bsign\s*in\b", _IM), "credential_prompt"),
    (re.compile(r"^\s*login\s*:|^\s*log\s*in\b", _IM), "credential_prompt"),
    # --- Windows update modal ------------------------------------------------
    (re.compile(r"windows\s+update\b", _IM), "windows.update_modal"),
    (re.compile(r"updates?\s+are\s+available\b", _IM), "windows.update_modal"),
    (re.compile(r"\brestart\s+now\b", _IM), "windows.update_modal"),
    # --- VS Code quick open --------------------------------------------------
    (re.compile(r"\bgo\s+to\s+file\b", _IM), "vscode.quick_open"),
    (re.compile(r"search\s+files\s+by\s+name\b", _IM), "vscode.quick_open"),
    (re.compile(r"type\s+the\s+name\s+of\s+a\s+file\s+to\s+open", _IM), "vscode.quick_open"),
    # --- browser address bar -------------------------------------------------
    (re.compile(r"search\s+(?:google\s+)?or\s+(?:type|enter)\s+(?:a\s+)?(?:url|address|web address)",
                _IM), "browser.address_bar"),
    (re.compile(r"\baddress\s+bar\b", _IM), "browser.address_bar"),
]

# A bare URL on its own line — only treated as the address bar, not as prose that
# happens to contain a link, so this is a separate, narrower check.
_BARE_URL_RE = re.compile(r"^\s*(?:https?://)[^\s]+\s*$", _IM)


def _matches_any(text: str) -> Mode | None:
    """Return the Mode of the first rule that matches, else None."""
    for pattern, mode in _RULES:
        if pattern.search(text):
            return mode
    return None


def detect_mode(
    ocr_text: str,
    element_map: ElementMap | None = None,
    app_hint: str | None = None,
) -> Mode:
    """Classify the screen into a `Mode` from OCR text (+ optional grounding).

    Conservative: defaults to ``"unknown"``. Blocking states (pager, credential,
    captcha) are matched before a generic shell prompt. ``element_map`` and
    ``app_hint`` only narrow ambiguous cases; they never override a confident
    blocking-state match.
    """
    text = ocr_text or ""

    matched = _matches_any(text)
    if matched is not None:
        return matched

    if _BARE_URL_RE.search(text):
        return "browser.address_bar"

    # Generic shell prompt — only after pager/credential/captcha ruled out.
    if _PROMPT_RE.search(ocr_text or ""):
        return "terminal.readline"

    hint = (app_hint or "").casefold()
    if hint:
        if "vscode" in hint or "code" in hint:
            return "vscode.editor"
        if "terminal" in hint or "konsole" in hint or "xterm" in hint:
            return "terminal.readline"

    return "unknown"


__all__ = ["detect_mode"]
