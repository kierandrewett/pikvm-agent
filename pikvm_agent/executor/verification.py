"""Text read-back verifier — classify what was typed vs what the screen shows.

Pure logic, no network, no side effects. Ported exactly from the TypeScript
`watched-typing.ts` verdict/classification used by the E1–E6 incidents. The
verifier is the *only* component allowed to declare typed text verified or
failed; everything else produces evidence.

Two comparison modes:
  - lenient (prose): folds OCR confusables, tolerates bounded edit distance.
  - precise (commands/code/paths/urls): symbols + case are load-bearing and the
    verifier fails closed on any difference.
"""

from __future__ import annotations

import math
import re
from typing import Literal, Optional

from pikvm_agent.core.models import (
    VERIFIED_STATUSES,
    VerificationResult,
    VerificationStatus,
)
from pikvm_agent.pikvm.text import flatten_line_breaks

Verdict = Literal["match", "contains", "mismatch", "unverified"]
MismatchKind = Literal["layout", "case", "prepend-autocorrect", "prefix-tail"]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Shell metacharacters that force strict verification + a split Enter.
HIGH_RISK_CHARS: set[str] = set("|&;><$`~*\"'\\!{}[]()")

# Quote folding is family-aware: OCR confuses *within* a family (straight vs
# curly), so fold curly/smart glyphs to their straight equivalent — but DO NOT
# merge single vs double, and leave the backtick alone. `"x"` vs `'x'` and
# `` `cmd` `` vs `'cmd'` are semantic differences in shell/code that strict
# verification must catch (Codex P1.5).
_SINGLE_QUOTES = "'‘’´′"  # ' ‘ ’ ´ ′  -> '
_DOUBLE_QUOTES = '"“”″'  # " “ ” ″  -> "

# Alphanumeric confusables (lenient mode only, applied AFTER lowercasing). The
# pipe `|` is deliberately NOT folded: it must stay a distinguishing symbol so a
# real `| -> l` layout slip is never hidden as verified.
CONFUSABLE: dict[str, str] = {
    "0": "o",
    "1": "l",
    "i": "l",
    "5": "s",
    "8": "b",
    "2": "z",
    "9": "g",
    "q": "g",
    "6": "g",
}

# Leading shell/REPL prompt a read-back crop captured (read-back only, never the
# intended text): `$ `, `# `, `% `, `user@host:~/p$ `, `PS C:\>`, `C:\>`, glyphs.
PROMPT_RE = re.compile(
    r"^\s*(?:PS\s+[^>\n]*>|[A-Za-z]:\\[^>\n]*>|[^\s@]+@[^\s@]+[^$#%>\n]*[$#%>]|[$#%>❯➜λ»])\s+"
)

_LOOKS_LIKE_CODE_SYMBOLS = re.compile(r"[{}();=<>\[\]]")
_INDENTED_LINE = re.compile(r"^[\t ]{2,}")
_SHELL_METACHARS = re.compile(r"[|<>$`~\\]")
_FLAGS_PATHS = re.compile(r"(^|\s)(--?[A-Za-z]|/[\w./-]+|~/|\.{1,2}/)")
_URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_COMMAND_HEAD = re.compile(
    r"(^|\s)(sudo|git|npm|yarn|pnpm|node|cd|ls|cat|grep|find|echo|rm|mkdir|cp|mv"
    r"|chmod|chown|ssh|scp|curl|wget|docker|kubectl|systemctl|tar|sed|awk)(\s|$)"
)
_CODE_OR_COMMAND_HEAD = re.compile(
    r"^\s*(?:"
    r"select|insert|update|delete|create|alter|drop|merge|"
    r"def|class|function|import|from|const|let|var|if|for|while|"
    r"terraform|powershell|pwsh|cmd"
    r")\b",
    re.IGNORECASE,
)
_PROSE_WORD = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")
_PROSE_UNSAFE_SYMBOLS = re.compile(r"[|&><$`~*\\{}\[\]=]")
_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]", re.IGNORECASE)
_NON_ALNUM_LOWER = re.compile(r"[^a-z0-9]")
_CARET_GLYPHS = frozenset({"|", "│", "┃", "I"})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def fold_quotes(s: str) -> str:
    """Fold curly/smart quotes to their straight equivalent, within family.

    Single-family glyphs collapse to `'` and double-family to `"`; the backtick
    is left untouched. This kills OCR's straight-vs-curly noise without erasing
    the `'` / `"` / `` ` `` distinctions that change shell and code semantics.
    """
    out: list[str] = []
    for ch in s:
        if ch in _SINGLE_QUOTES:
            out.append("'")
        elif ch in _DOUBLE_QUOTES:
            out.append('"')
        else:
            out.append(ch)
    return "".join(out)


def norm(s: str, precise: bool = False) -> str:
    """Quote-fold, collapse whitespace, strip; lowercase unless `precise`."""
    compact = _WHITESPACE.sub(" ", fold_quotes(s)).strip()
    return compact if precise else compact.lower()


def has_whitespace_only_difference(intended: str, observed: str) -> bool:
    """Whether OCR found the same glyph sequence with different spacing.

    Visual line wrapping is canonicalized to one non-submitting space first,
    while repeated spaces inside a displayed line stay load-bearing.
    """

    left = flatten_line_breaks(fold_quotes(intended)).strip()
    right = flatten_line_breaks(
        fold_quotes(strip_prompt(observed))
    ).strip()
    if left == right:
        return False
    return (
        re.sub(r"\s+", "", left)
        == re.sub(r"\s+", "", right)
        and re.findall(r"\s+", left) != re.findall(r"\s+", right)
    )


def fold_confusables(s: str) -> str:
    """Fold alphanumeric confusables char-by-char (lenient mode key)."""
    out = ""
    for ch in s:
        out += CONFUSABLE.get(ch, ch)
    return out


def strip_prompt(s: str) -> str:
    """Remove a leading shell/REPL prompt. READ-BACK ONLY, never intended."""
    return PROMPT_RE.sub("", s, count=1)


def alnum(s: str, precise: bool = False) -> str:
    """Letters + digits only (what a layout slip leaves unchanged)."""
    return _NON_ALNUM.sub("", s if precise else s.lower())


def alnum_fold_case(s: str) -> str:
    """Lowercased letters + digits only."""
    return _NON_ALNUM_LOWER.sub("", s.lower())


def conf_key(s: str, precise: bool) -> str:
    """Canonical 'are these the same text?' key. Precise: no confusable fold."""
    return norm(s, precise) if precise else fold_confusables(norm(s, precise))


def is_prefix_read(intended_norm: str, read_norm: str) -> bool:
    """Read is a non-empty proper prefix of the intended (viewport truncation)."""
    return (
        len(read_norm) > 0
        and len(intended_norm) > len(read_norm)
        and intended_norm.startswith(read_norm)
    )


def read_caught_extra(ni: str, nr: str) -> bool:
    """Read is much longer than the intent and doesn't contain it — the crop
    likely caught surrounding UI, not a typing error."""
    return len(ni) >= 3 and len(nr) > len(ni) * 1.6 and ni not in nr


def looks_like_trailing_caret(intended_norm: str, read_norm: str) -> bool:
    """Recognise a focused edit control's caret as ambiguous OCR evidence."""
    return (
        len(read_norm) == len(intended_norm) + 1
        and read_norm[:-1] == intended_norm
        and read_norm[-1] in _CARET_GLYPHS
    )


def overlap_ratio(a: str, b: str) -> float:
    """Fraction of `a`'s chars present in `b` as a multiset (empty a -> 1.0)."""
    if not a:
        return 1.0
    pool: dict[str, int] = {}
    for c in b:
        pool[c] = pool.get(c, 0) + 1
    hit = 0
    for c in a:
        n = pool.get(c, 0)
        if n > 0:
            hit += 1
            pool[c] = n - 1
    return hit / len(a)


def levenshtein(a: str, b: str, max_d: int) -> int:
    """Bounded edit distance; early-out once it exceeds `max_d`."""
    if abs(len(a) - len(b)) > max_d:
        return max_d + 1
    prev = list(range(len(b) + 1))
    cur = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur[0] = i
        row_min = cur[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > max_d:
            return max_d + 1
        prev, cur = cur, prev
    return prev[len(b)]


def word_set(s: str, precise: bool = False) -> str:
    """Sorted unique-order word tokens (for prepend/reorder detection)."""
    return " ".join(sorted(w for w in norm(s, precise).split(" ") if w))


def looks_like_code(s: str) -> bool:
    """Symbol density > 0.04 OR >= 2 lines indented >= 2 spaces."""
    symbols = len(_LOOKS_LIKE_CODE_SYMBOLS.findall(s))
    lines = s.split("\n")
    indented = sum(1 for line in lines if _INDENTED_LINE.match(line))
    return symbols / max(len(s), 1) > 0.04 or indented >= 2


def is_exact_text(s: str) -> bool:
    """Compare in precise mode? ANY high-risk character, shell metachars,
    flags/paths, urls, code, or a common command head all make case + symbols
    load-bearing. A single dropped/transposed metacharacter changes meaning, so
    such text must be verified exactly (per the plan's high-risk-char rule)."""
    if any(ch in HIGH_RISK_CHARS for ch in s):
        return True
    if looks_like_code(s):
        return True
    if _SHELL_METACHARS.search(s):
        return True
    if _FLAGS_PATHS.search(s):
        return True
    if _URL_SCHEME.search(s):
        return True
    if _COMMAND_HEAD.search(s):
        return True
    return False


def is_editor_prose(s: str) -> bool:
    """Recognise long natural-language editor text without relaxing code.

    This is an eligibility check for lenient OCR and guarded printer delivery,
    not a general safety classification. Shell metacharacters, paths, URLs,
    command/code heads, indentation, and code-like symbol density all keep the
    strict verifier even when a controller labels the target as an editor.
    """

    if len(s) < 80 or len(_PROSE_WORD.findall(s)) < 12:
        return False
    if _PROSE_UNSAFE_SYMBOLS.search(s):
        return False
    if _FLAGS_PATHS.search(s) or _URL_SCHEME.search(s):
        return False
    if _COMMAND_HEAD.search(s) or _CODE_OR_COMMAND_HEAD.search(s):
        return False
    if looks_like_code(s):
        return False
    return True


# --------------------------------------------------------------------------- #
# Verdict + mismatch classification
# --------------------------------------------------------------------------- #


def compute_verdict(intended: str, read_back: str, precise: bool = False) -> Verdict:
    """Classify a read-back as match / contains / mismatch / unverified."""
    if not read_back:
        return "unverified"
    ni = norm(intended, precise)
    if has_whitespace_only_difference(intended, read_back):
        return "unverified"
    raw_nr = norm(read_back, precise)
    # A literal Markdown heading can begin with the same ``# `` shape as a
    # root-shell prompt. Prefer a complete whole-field match before applying
    # the read-back-only prompt heuristic; actual captured prompts still fall
    # through to the stripped comparison below.
    if ni == raw_nr:
        return "match"
    nr = norm(strip_prompt(read_back), precise)
    if ni == nr:
        return "match"
    if precise and looks_like_trailing_caret(ni, nr):
        return "unverified"
    if ni in nr:
        return "contains"
    if is_prefix_read(ni, nr):
        return "unverified"
    # Letters/digits identical, only symbols/case differ ⇒ a confident
    # layout/caps slip, not noise — never report this as a clean match.
    if alnum_fold_case(ni) and alnum_fold_case(ni) == alnum_fold_case(nr) and ni != nr:
        return "mismatch"
    if read_caught_extra(ni, nr):
        return "unverified"
    if precise:
        # OCR on small monospace code often substitutes a handful of letters
        # even when the pixels are correct. That is not sufficient evidence to
        # clear/retype exact text. Symbol-only differences were already caught
        # above as a strong layout/case mismatch; mixed small edit distance is
        # ambiguity and must stop before commit without destructive correction.
        tol = max(1, math.ceil(len(ni) * 0.08))
        if levenshtein(ni, nr, tol) <= tol:
            return "unverified"
        return "unverified" if overlap_ratio(ni, nr) < 0.5 else "mismatch"
    ck = conf_key(intended, precise)
    cr = conf_key(read_back, precise)
    if ck == cr:
        return "match"
    if ck in cr:
        return "contains"
    if is_prefix_read(ck, cr):
        return "unverified"
    tol = max(1, math.ceil(len(ni) * 0.08))
    if levenshtein(ni, nr, tol) <= tol:
        return "match"
    if overlap_ratio(ni, nr) < 0.5:
        return "unverified"
    return "mismatch"


def classify_mismatch(
    intended: str, read_back: str, precise: bool
) -> Optional[MismatchKind]:
    """Classify a structural mismatch, or None if it is a match / OCR tolerance."""
    ni = norm(intended, precise)
    nr = norm(strip_prompt(read_back), precise)
    if not nr:
        return None
    if ni == nr:
        return None
    if precise and looks_like_trailing_caret(ni, nr):
        return None
    if ni in nr:
        return None
    if is_prefix_read(ni, nr):
        return None
    # Preserve case as its own signal before throwing punctuation away.  This
    # commonly means the guest's real Caps Lock state differs from the state a
    # VNC adapter can observe; changing the configured keyboard layout cannot
    # correct it.
    if precise and ni.casefold() == nr.casefold():
        return "case"
    if alnum_fold_case(ni) and alnum_fold_case(ni) == alnum_fold_case(nr):
        return "layout"
    if read_caught_extra(ni, nr):
        return None
    if precise:
        tol = max(1, math.ceil(len(ni) * 0.08))
        if levenshtein(ni, nr, tol) <= tol:
            return None
        if overlap_ratio(ni, nr) < 0.5:
            return None
        return "prefix-tail"
    ck = conf_key(intended, precise)
    cr = conf_key(read_back, precise)
    if ck == cr or ck in cr:
        return None
    if is_prefix_read(ck, cr):
        return None
    tol = max(1, math.ceil(len(ni) * 0.08))
    if levenshtein(ni, nr, tol) <= tol:
        return None
    if nr.endswith(ni) or word_set(ni, precise) == word_set(nr, precise):
        return "prepend-autocorrect"
    if overlap_ratio(ni, nr) < 0.5:
        return None
    return "prefix-tail"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def verify_text(
    intended: str,
    observed: str,
    mode: str | None = None,
    *,
    code: bool = False,
) -> VerificationResult:
    """Verify typed `intended` text against the `observed` read-back.

    `mode` is the detected interaction mode (accepted for caller convenience /
    future routing); the verdict itself is content-driven. `code=True` forces
    precise comparison.
    """
    precise = code or is_exact_text(intended)
    verdict = compute_verdict(intended, observed, precise)

    ni = norm(intended, precise)
    nr = norm(strip_prompt(observed), precise)

    status: VerificationStatus
    detail: str
    if verdict == "match":
        status = "verified_exact" if precise else "verified_safe_normalized"
        detail = "precise exact match" if precise else "safe normalized match"
    elif verdict == "contains":
        if precise:
            status = "unverified_wrong_region"
            detail = "exact read-back contains surrounding or extra text"
        else:
            status = "verified_with_warnings"
            detail = "read-back contains the intended text plus extra"
    elif verdict == "unverified":
        if has_whitespace_only_difference(intended, observed):
            status = "unverified_whitespace"
            detail = "visible spacing differs from the delivery payload"
        elif is_prefix_read(ni, nr):
            status = "unverified_truncated"
            detail = "read-back is a prefix of the intended text (viewport truncation)"
        elif read_caught_extra(ni, nr):
            status = "unverified_wrong_region"
            detail = "read-back caught surrounding UI, not the typed field"
        else:
            status = "unverified_ambiguous"
            detail = "read-back too dissimilar to confirm or refute"
    else:  # mismatch
        kind = classify_mismatch(intended, observed, precise)
        if kind == "layout":
            status = "failed_keyboard_layout"
            detail = "alphanumerics match but symbols differ — keyboard layout slip"
        elif alnum_fold_case(ni) == alnum_fold_case(nr) and ni != nr:
            status = "failed_case_mismatch"
            detail = "difference is purely letter case"
        else:
            status = "failed_symbol_mismatch"
            detail = "symbols or characters differ from the intended text"

    safe = status in VERIFIED_STATUSES
    return VerificationResult(
        status=status,
        safe_to_continue=safe,
        intended=intended,
        observed=observed,
        detail=detail,
    )
