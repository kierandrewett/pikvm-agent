"""Human-in-the-loop interrupt.

Wraps LangGraph's ``interrupt`` so the graph pauses at an approval point and the
daemon can surface the request to a human, then resume with their decision. A
non-dict resume value is treated as a reject (fail safe). Approval is re-checked
against freshness + policy after resume — it is never a force-execute.
"""

from __future__ import annotations

from langgraph.types import interrupt


def approval_interrupt(payload: dict) -> dict:
    response = interrupt(payload)
    if not isinstance(response, dict):
        return {"type": "reject", "reason": "Invalid approval response"}
    return response
