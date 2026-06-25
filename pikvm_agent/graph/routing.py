"""Conditional edge routers.

Pure functions over state — no services, no I/O — so the control flow is easy to
reason about and test. A stale block routes back to a fresh observation (the
natural recovery); a hard policy block ends the run; the step cap is the loop
backstop.
"""

from __future__ import annotations

from typing import Any


def route_after_policy(state: dict[str, Any]) -> str:
    """approval | stale | blocked | allowed | done."""
    if state.get("status") == "done":
        return "done"
    pr = state.get("policy_result") or {}
    status = pr.get("status")
    if status == "done":
        return "done"
    if status == "approval_required":
        return "approval"
    if status == "blocked":
        if pr.get("reason") in ("stale_frame", "stale_world"):
            return "stale"  # re-observe and re-plan against the current screen
        return "blocked"
    return "allowed"


def route_after_interrupt(state: dict[str, Any]) -> str:
    """execute | replan | blocked.

    A reject/abort never reaches execution; an edit/respond re-plans rather than
    executing the (now-superseded) original action; an approve executes.
    """
    if state.get("status") == "blocked":
        return "blocked"
    if state.get("replan"):
        return "replan"
    return "execute"


def route_after_verify(state: dict[str, Any]) -> str:
    """continue | recover | done | failed.

    Max-step exhaustion is turned into a ``failed`` status by verify_result, so
    it is caught by the failed branch — it is never reported as ``done``.
    """
    if state.get("status") == "done":
        return "done"
    if state.get("status") in ("failed", "blocked"):
        return "failed"
    tr = state.get("transaction_result") or {}
    if tr.get("status") == "failed_stale_frame":
        return "recover"
    if tr.get("status") in ("blocked_by_policy", "failed"):
        return "failed"
    return "continue"
