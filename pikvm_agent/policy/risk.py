"""Text-level command risk classification.

Ported from the battle-tested TypeScript reviewer in
``~/dev/pikvm-desktop-agentic/src/reviewer.ts``. This is *evidence the policy
engine consumes*: it reads a shell-style command line and reports how risky the
text is, with no knowledge of the screen, the app, or the policy profile. The
safety engine layers policy on top.

Precedence when a line has several clauses: ``side_effect > dangerous > medium >
safe``. A line is split on ``;``, ``&&``, ``||``, ``|`` and ``&`` so the riskiest
clause wins (``ls && rm -rf foo`` is dangerous; ``git status | grep foo`` is
safe).
"""

from __future__ import annotations

import re
from typing import Literal

CommandRisk = Literal["safe", "medium", "dangerous", "side_effect"]

# --------------------------------------------------------------------------- #
# Dangerous command shapes — irreversible or destructive (port of the TS list)
# --------------------------------------------------------------------------- #

DANGEROUS_COMMAND_RE: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+(-\w*\s+)*-?\w*[rf]\w*", re.IGNORECASE),  # rm with -r/-f in any combo
    re.compile(r"\brm\s+-[a-z]*[rf]", re.IGNORECASE),
    re.compile(r"\bdd\s+if=", re.IGNORECASE),  # raw disk write
    re.compile(r"\bmkfs(\.\w+)?\b", re.IGNORECASE),  # filesystem format
    re.compile(r"\b(shutdown|reboot|poweroff|halt)\b", re.IGNORECASE),
    re.compile(r"\binit\s+0\b", re.IGNORECASE),
    re.compile(r"\bchmod\s+-R\b", re.IGNORECASE),
    re.compile(r"\bchown\s+-R\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\b.*--force|\bgit\s+push\s+-f\b", re.IGNORECASE),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
    re.compile(r"\bgit\s+clean\s+-[a-z]*f", re.IGNORECASE),
    re.compile(r"\b(format)\s+[a-z]:", re.IGNORECASE),  # format <drive>:
    re.compile(r"\bdrop\s+(table|database)\b", re.IGNORECASE),  # destructive SQL
    re.compile(r":\(\)\s*\{.*\};:"),  # fork bomb
    re.compile(r"\b(curl|wget)\b.+\|\s*(sudo\s+)?(ba|z|c)?sh\b", re.IGNORECASE),  # pipe-to-shell
    re.compile(r"(^|\s|\|)\s*sudo\b", re.IGNORECASE),  # anything under sudo
    re.compile(r">\s*/dev/sd[a-z]", re.IGNORECASE),  # writing to a block device
    re.compile(
        r"\b(rm|mv|cp|chmod|chown|tee|truncate)\b[^|;&]*\s/etc(/|\b)", re.IGNORECASE
    ),  # mutating /etc
    re.compile(r">>?\s*/etc(/|\b)", re.IGNORECASE),  # redirecting output into /etc
]
"""Compiled regexes matching irreversible/destructive shell shapes."""

# Outward, human-visible side effects: someone else sees/receives something, or
# money/commitment moves. These dominate (others see it), tested per clause so a
# benign use (`git checkout`) can be exempted.
SIDE_EFFECT_RE: re.Pattern[str] = re.compile(
    r"\b(send|sendmail|post|publish|tweet|toot|comment|reply|submit|pay|purchase|"
    r"checkout|deploy|release|email|mailx?|message|dm|broadcast|invite|rsvp|share|join)\b",
    re.IGNORECASE,
)

# Read-only / inspection commands — no side effects.
_SAFE_COMMANDS: frozenset[str] = frozenset(
    {
        "ls", "ll", "la", "dir", "pwd", "cd", "cat", "bat", "less", "more", "head", "tail",
        "grep", "egrep", "fgrep", "rg", "ag", "find", "fd", "locate", "which", "whereis",
        "type", "echo", "printf", "stat", "file", "wc", "du", "df", "tree", "realpath",
        "basename", "dirname", "whoami", "id", "hostname", "uname", "date", "uptime", "env",
        "printenv", "history", "man", "ps", "top", "htop", "free", "lscpu", "lsblk", "lsusb",
        "lspci", "ip", "ifconfig", "ping", "diff", "cmp", "sort", "uniq", "cut", "jq",
        "column", "tac", "nl",
    }
)

# Read-only git subcommands; anything else under git falls through to medium/danger.
_SAFE_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "status", "branch", "log", "diff", "show", "describe", "blame", "remote",
        "rev-parse", "ls-files", "ls-remote", "shortlog", "reflog", "whatchanged",
    }
)

# Shell metacharacters that make a typed command line need strict verification —
# any of these changes meaning, so the typed text must be checked exactly.
HIGH_RISK_CHARS: set[str] = set("|&;><$`~*\"'\\!{}[]()")

_CLAUSE_SPLIT_RE: re.Pattern[str] = re.compile(r"\s*(?:\|\||&&|;|\||&)\s*")


def _is_benign_git_verb(clause: str) -> bool:
    """`git checkout` reads as a side-effect verb but is benign; exempt it."""
    words = clause.strip().split()
    return len(words) >= 2 and words[0].lower() == "git" and words[1].lower() == "checkout"


def _is_safe_clause(clause: str) -> bool:
    """Is a single command clause read-only / inspection?"""
    words = clause.strip().split()
    if not words:
        return False
    head = words[0].lower()
    if head == "git":
        sub = words[1].lower() if len(words) >= 2 else ""
        return sub in _SAFE_GIT_SUBCOMMANDS
    return head in _SAFE_COMMANDS


def _classify_clause(clause: str) -> CommandRisk:
    """Classify a single clause. Whole-line danger is checked by the caller."""
    if not _is_benign_git_verb(clause) and SIDE_EFFECT_RE.search(clause):
        return "side_effect"
    if any(pat.search(clause) for pat in DANGEROUS_COMMAND_RE):
        return "dangerous"
    return "safe" if _is_safe_clause(clause) else "medium"


_PRECEDENCE: dict[CommandRisk, int] = {
    "safe": 0,
    "medium": 1,
    "dangerous": 2,
    "side_effect": 3,
}


def classify_command(text: str) -> CommandRisk:
    """Classify a shell-style command line by its text alone.

    Splits on ``;``, ``&&``, ``||``, ``|`` and ``&`` and returns the riskiest
    clause (precedence ``side_effect > dangerous > medium > safe``). Some
    dangerous shapes span a pipe (``curl ... | sh``), so the whole line is also
    tested against the dangerous patterns.
    """
    trimmed = (text or "").strip()
    if not trimmed:
        return "medium"

    clauses = [c for c in _CLAUSE_SPLIT_RE.split(trimmed) if c]
    if not clauses:
        return "medium"

    worst: CommandRisk = "safe"
    for clause in clauses:
        risk = _classify_clause(clause)
        if _PRECEDENCE[risk] > _PRECEDENCE[worst]:
            worst = risk

    # Pipe-spanning dangerous shapes (curl | sh, write to /dev/sd*): test the
    # full line so they aren't lost when split into clauses.
    if _PRECEDENCE[worst] < _PRECEDENCE["dangerous"] and any(
        pat.search(trimmed) for pat in DANGEROUS_COMMAND_RE
    ):
        worst = "dangerous"

    return worst


def requires_strict_verification(text: str) -> bool:
    """True when typed text contains shell metacharacters that change meaning.

    Such text must be verified exactly after typing (no normalisation slack):
    a single dropped or transposed metacharacter alters what the command does.
    """
    return any(ch in HIGH_RISK_CHARS for ch in (text or ""))
