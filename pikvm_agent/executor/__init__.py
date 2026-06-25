"""Action execution + outcome verification.

The text read-back *verifier* lives here: it is the only thing in the runtime
allowed to declare typed text verified or failed. It is pure logic — no network,
no side effects — ported exactly from the battle-tested TypeScript
implementation (`watched-typing.ts`) behind the E1–E6 regression incidents.
"""

from __future__ import annotations

from pikvm_agent.executor.verification import (
    compute_verdict,
    is_exact_text,
    verify_text,
)

__all__ = ["compute_verdict", "is_exact_text", "verify_text"]
