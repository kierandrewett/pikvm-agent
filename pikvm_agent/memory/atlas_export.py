"""Session trace -> SAFE Atlas memory-update proposal.

``build_memory_update`` summarizes a session's trace (observes, decisions,
approvals, executions, recoveries, policy blocks, verification outcomes, stale
refusals) and emits a curated markdown page plus a compact incident dict.

REDACTION is the point of this module. The plan forbids leaking secrets, raw
screenshot/frame paths, credential text, verbatim typed text bodies, private
message bodies, or API keys into Atlas. Anything we echo from the trace is run
through :func:`_scrub` first; if any sensitive field was present and stripped,
``redacted`` is set True. This is enforcement, not best-effort: nothing from a
``REDACT_KEYS`` field ever reaches the output markdown or incident.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from pikvm_agent.core.models import VERIFIED_STATUSES
from pikvm_agent.memory.templates import INCIDENT_TEMPLATE, PLAYBOOK_TEMPLATE

# --------------------------------------------------------------------------- #
# Redaction policy
# --------------------------------------------------------------------------- #

REDACT_KEYS: frozenset[str] = frozenset(
    {
        # Screenshot / frame file paths (never durable, may embed user paths).
        "screenshot_path",
        "image_path",
        "frame_path",
        "img_path",
        "screenshot",
        "image",
        "frame",
        # Verbatim typed / message / prose bodies.
        "text",
        "typed_text",
        "message",
        "body",
        "instruction",
        "prompt",
        "content",
        # Credentials / secrets / keys.
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "credential",
        "credentials",
        "auth",
        "authorization",
        "cookie",
        "session_token",
        "bearer",
        "private_key",
    }
)
"""Field names whose *values* must NEVER appear in exported output."""

# A typed text field replaced by a length-only placeholder rather than dropped,
# so the playbook can still say "typed N chars" without leaking the content.
_LENGTH_SUMMARIZED_KEYS: frozenset[str] = frozenset(
    {"text", "typed_text", "message", "body", "content"}
)

_MAX_LIST = 12


class MemoryUpdate(BaseModel):
    """A safe, durable Atlas memory-update proposal.

    ``markdown`` is a curated ``memory/<topic>.md`` page; ``incident`` is a
    compact quick-capture dict. Both are post-redaction.
    """

    title: str
    page_path: str = Field(description="e.g. memory/pikvm/<slug>.md")
    markdown: str
    incident: dict[str, Any]
    redacted: bool
    stats: dict[str, Any]


# --------------------------------------------------------------------------- #
# Slug
# --------------------------------------------------------------------------- #


def memory_slug(task: str) -> str:
    """A filesystem-safe kebab slug from a task string."""
    s = (task or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) > 60:
        s = s[:60].rstrip("-")
    return s or "session"


# --------------------------------------------------------------------------- #
# Redaction helpers
# --------------------------------------------------------------------------- #


def _scrub(value: Any, flag: list[bool]) -> Any:
    """Recursively strip REDACT_KEYS values from ``value``.

    Mutates nothing; returns a cleaned copy. ``flag`` is a 1-element list used as
    an out-param so callers learn whether anything was stripped.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if key.lower() in REDACT_KEYS:
                flag[0] = True
                if key.lower() in _LENGTH_SUMMARIZED_KEYS and isinstance(v, str):
                    out[key] = f"<{len(v)} chars redacted>"
                # else: drop entirely (paths/secrets are never summarized).
                continue
            out[key] = _scrub(v, flag)
        return out
    if isinstance(value, list):
        return [_scrub(v, flag) for v in value]
    return value


def _safe_text_blob(value: Any) -> str:
    """Concatenate all *non-sensitive* string values for keyword scanning.

    Skips REDACT_KEYS so we never inspect secret or verbatim-typed content.
    """
    parts: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in REDACT_KEYS:
                continue
            parts.append(_safe_text_blob(v))
    elif isinstance(value, list):
        parts.extend(_safe_text_blob(v) for v in value)
    elif isinstance(value, str):
        parts.append(value)
    return " ".join(p for p in parts if p)


def _typed_chars(events: list[dict[str, Any]]) -> int:
    """Total length of typed text across the trace, by intent (never content)."""
    total = 0
    for e in events:
        if e.get("kind") in ("decision", "executed", "execute_record_only"):
            for a in e.get("actions") or []:
                # actions in the trace are recorded as type names, not bodies;
                # any stray body field is summarized, never read for content.
                if isinstance(a, dict) and isinstance(a.get("text"), str):
                    total += len(a["text"])
    return total


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #


def _count(events: list[dict[str, Any]], kind: str) -> int:
    return sum(1 for e in events if e.get("kind") == kind)


def _compute_stats(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll the trace up into structured counts. Pure; no content echoed."""
    executed = [e for e in events if e.get("kind") == "executed"]
    verif_failures = 0
    verif_mismatch = 0
    layout_correction = False
    for e in executed:
        status = ((e.get("verification") or {}) or {}).get("status")
        # verification may instead ride inside the transaction result if present.
        if not status:
            status = (((e.get("result") or {}).get("verification")) or {}).get("status")
        if isinstance(status, str) and status not in VERIFIED_STATUSES:
            if status.startswith("failed_") or status.startswith("unverified_"):
                verif_failures += 1
            if "mismatch" in status:
                verif_mismatch += 1
            if status == "failed_keyboard_layout" or "wrong_region" in status:
                layout_correction = True

    # Layout corrections also surface as explicit recover/decision signals; scan
    # the whole (already-redacted-safe) event recursively, not just top level.
    for e in events:
        blob = _safe_text_blob(e).lower()
        if "uk keyboard" in blob or "keyboard layout" in blob or "layout correction" in blob:
            layout_correction = True

    stale_refusals = sum(
        1
        for e in events
        if e.get("kind") == "execute_refused" and e.get("reason") == "stale_world"
    )
    stale_refusals += _count(events, "failed_stale_frame")

    return {
        "observes": _count(events, "observe"),
        "decisions": _count(events, "decision"),
        "approvals_required": _count(events, "approval_required"),
        "approvals_approved": _count(events, "approved"),
        "approvals_denied": _count(events, "approval_denied"),
        "executions": len(executed),
        "recoveries": _count(events, "recover"),
        "policy_blocks": _count(events, "policy_block"),
        "execute_refused": _count(events, "execute_refused"),
        "verification_failures": verif_failures,
        "verification_mismatches": verif_mismatch,
        "stale_refusals": stale_refusals,
        "operator_errors": _count(events, "operator_error"),
        "typed_chars": _typed_chars(events),
        "keyboard_layout_correction": layout_correction,
        "total_events": len(events),
    }


# --------------------------------------------------------------------------- #
# Narrative builders (all content is derived from stats/kinds, never bodies)
# --------------------------------------------------------------------------- #


def _final_status(events: list[dict[str, Any]], status: str) -> tuple[str, list[str]]:
    """Final status + de-duplicated failure reasons (reasons are safe strings)."""
    final = status
    for e in reversed(events):
        if e.get("kind") == "finalise" and isinstance(e.get("status"), str):
            final = e["status"]
            break
    reasons: list[str] = []
    for e in events:
        if e.get("kind") in ("policy_block", "execute_refused", "operator_error"):
            r = e.get("reason") or e.get("error")
            if r and r not in reasons:
                reasons.append(str(r))
        if e.get("kind") in ("max_steps_exhausted",):
            if "max_steps_exhausted" not in reasons:
                reasons.append("max_steps_exhausted")
    return final, reasons[:_MAX_LIST]


def _bullets(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return "- (none recorded)"
    return "\n".join(f"- {i}" for i in items[:_MAX_LIST])


def _summary_line(task: str, final: str, stats: dict[str, Any]) -> str:
    return (
        f'Session for task "{task}" ended **{final}** after {stats["decisions"]} '
        f'decision(s) and {stats["executions"]} execution(s); '
        f'{stats["approvals_required"]} approval(s) requested '
        f'({stats["approvals_approved"]} approved, {stats["approvals_denied"]} denied), '
        f'{stats["recoveries"]} recovery action(s), '
        f'{stats["policy_blocks"]} policy block(s).'
    )


def _worked(stats: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if stats["executions"]:
        out.append(f'{stats["executions"]} transaction(s) executed under the guard.')
    if stats["approvals_approved"]:
        out.append(
            f'{stats["approvals_approved"]} consequential action(s) ran after explicit approval.'
        )
    if stats["typed_chars"]:
        out.append(f'Typed roughly {stats["typed_chars"]} chars (content not stored).')
    if stats["recoveries"]:
        out.append(f'Recovery handled {stats["recoveries"]} interruption(s).')
    return out


def _blocked(stats: dict[str, Any], reasons: list[str]) -> list[str]:
    out: list[str] = []
    if stats["approvals_required"]:
        out.append(
            f'{stats["approvals_required"]} action(s) required human approval before running.'
        )
    if stats["policy_blocks"]:
        out.append(f'{stats["policy_blocks"]} action(s) blocked by policy.')
    if stats["stale_refusals"]:
        out.append(
            f'{stats["stale_refusals"]} execution(s) refused on a stale world (re-observed).'
        )
    if stats["verification_failures"]:
        out.append(f'{stats["verification_failures"]} verification failure(s).')
    out.extend(reasons)
    return out


def _lessons(events: list[dict[str, Any]], stats: dict[str, Any]) -> list[str]:
    """Durable, generalizable lessons. Pure heuristics over kinds/categories."""
    out: list[str] = []
    if stats["keyboard_layout_correction"]:
        out.append(
            "This target uses the UK keyboard layout — correct symbol layout before typing."
        )
    # Approval categories are safe to name (RiskCategory strings, not bodies).
    for e in events:
        if e.get("kind") == "approval_required":
            cat = e.get("risk")
            if cat:
                lesson = f"{cat} always required approval here."
                if lesson not in out:
                    out.append(lesson)
    if stats["stale_refusals"]:
        out.append(
            "Re-observe before executing: the world changed between plan and execution."
        )
    if stats["verification_mismatches"]:
        out.append(
            "Typed-text verification mismatched — prefer paste/print for long or symbol-heavy text."
        )
    if not out:
        out.append("No durable lessons surfaced from this run.")
    return out


# --------------------------------------------------------------------------- #
# Free-text redaction (the task + error strings aren't structured trace fields)
# --------------------------------------------------------------------------- #

_SECRET_KW = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|credential|cookie|auth|bearer)\b\s*[:=]\s*\S+"
)
# Pydantic validation errors echo the offending value as `input_value=...`.
_PYDANTIC_INPUT = re.compile(r"input_value=.*?(?=,\s*input_type=|$)", re.DOTALL)
# Quoted text/body/message/content values inside an error or task string.
_BODY_KV = re.compile(
    r"(?i)(['\"]?(?:text|body|message|content|prompt|instruction)['\"]?\s*[:=]\s*)(['\"]).*?\2"
)


def _redact_text(s: str) -> tuple[str, bool]:
    """Mask sensitive substrings in a FREE-TEXT string (the task, an error
    message) — these aren't structured trace fields so ``_scrub`` never sees
    them. Masks Pydantic ``input_value=`` echoes, quoted body/message values,
    and ``key: value`` secret pairs. Returns ``(clean, changed)``."""
    original = s or ""
    out = _PYDANTIC_INPUT.sub("input_value=<redacted>", original)
    out = _BODY_KV.sub(r"\1<redacted>", out)
    out = _SECRET_KW.sub(r"\1=<redacted>", out)
    return out, out != original


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def build_memory_update(
    session_id: str,
    task: str,
    trace_events: list[dict[str, Any]],
    *,
    status: str = "done",
) -> MemoryUpdate:
    """Build a SAFE Atlas memory-update proposal from a session trace.

    No network. Output markdown + incident are fully redacted: REDACT_KEYS
    values (screenshot/frame paths, secrets, credentials, tokens, verbatim typed
    text, private message bodies) never appear. ``redacted`` is True iff any such
    field was present in the trace and stripped.
    """
    events = list(trace_events or [])

    # Detect presence of any sensitive field anywhere in the raw trace.
    redaction_flag = [False]
    _scrub(events, redaction_flag)

    stats = _compute_stats(events)
    final, reasons = _final_status(events, status)

    # Redact free-text that can carry secrets/bodies: the task itself and any
    # error/reason strings (e.g. a Pydantic error echoing a typed value). These
    # are not structured trace fields, so _scrub never reaches them.
    safe_task, task_red = _redact_text((task or "PiKVM session").strip())
    safe_reasons: list[str] = []
    reason_red = False
    for r in reasons:
        rr, changed = _redact_text(r)
        safe_reasons.append(rr)
        reason_red = reason_red or changed
    reasons = safe_reasons
    if task_red or reason_red:
        redaction_flag[0] = True

    summary = _summary_line(safe_task, final, stats)
    worked = _worked(stats)
    blocked = _blocked(stats, reasons)
    lessons = _lessons(events, stats)

    is_incident = final in ("failed", "blocked") or bool(reasons) or (
        stats["verification_failures"] > 0
    )
    kind = "incident" if is_incident else "playbook"

    title_task = safe_task
    title = f'{"Incident" if is_incident else "Playbook"}: {title_task}'

    template = INCIDENT_TEMPLATE if is_incident else PLAYBOOK_TEMPLATE
    if is_incident:
        markdown = template.format(
            title=title,
            task=safe_task,
            summary=summary,
            steps=_bullets(blocked),
            lessons=_bullets(lessons),
        )
    else:
        markdown = template.format(
            title=title,
            task=safe_task,
            summary=summary,
            steps=_bullets(worked),
            blocked=_bullets(blocked),
            lessons=_bullets(lessons),
        )

    # Compact quick-capture dict — built from safe, derived fields only.
    incident: dict[str, Any] = {
        "kind": kind,
        "session_id": session_id,
        "task": safe_task,
        "final_status": final,
        "summary": summary,
        "signals": {
            "decisions": stats["decisions"],
            "executions": stats["executions"],
            "approvals_required": stats["approvals_required"],
            "policy_blocks": stats["policy_blocks"],
            "stale_refusals": stats["stale_refusals"],
            "verification_failures": stats["verification_failures"],
            "recoveries": stats["recoveries"],
            "keyboard_layout_correction": stats["keyboard_layout_correction"],
        },
        "failure_reasons": reasons,
        "lessons": lessons,
    }

    slug = memory_slug(safe_task)
    page_path = f"memory/pikvm/{slug}.md"

    return MemoryUpdate(
        title=title,
        page_path=page_path,
        markdown=markdown,
        incident=incident,
        redacted=redaction_flag[0],
        stats=stats,
    )
