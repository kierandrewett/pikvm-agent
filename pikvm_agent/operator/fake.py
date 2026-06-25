"""Deterministic fake operator.

Implements :class:`~pikvm_agent.core.ports.OperatorProvider` without any network.
It proposes a single safe, low-risk action and always cites the exact
``(frame_id, world_version)`` it was shown — so graph (Phase 3) and policy tests
get a predictable, freshness-correct decision with no model in the loop.

Pass ``scripted`` to replay a fixed sequence (one decision popped per call);
when the script is exhausted it falls back to the no-op observe step.
"""

from __future__ import annotations

from pikvm_agent.core.models import (
    KeypressAction,
    OperatorDecision,
    OperatorRequest,
    RiskAssessment,
    WaitAction,
)

__all__ = ["FakeOperator"]


class FakeOperator:
    """A no-op-by-default :class:`OperatorProvider` for tests and dry runs."""

    def __init__(self, scripted: list[OperatorDecision] | None = None) -> None:
        # Copied so the caller's list isn't mutated as we pop replayed steps.
        self._scripted: list[OperatorDecision] = list(scripted or [])

    async def decide(self, request: OperatorRequest) -> OperatorDecision:
        """Return the next scripted decision, or a deterministic safe no-op.

        The fallback always references ``request.frame["id"]`` /
        ``["world_version"]`` so it survives the runtime's freshness check. If
        the frame carries a ``"keys"`` hint, the no-op becomes that keypress;
        otherwise it is a short ``wait``.
        """
        if self._scripted:
            return self._scripted.pop(0)

        frame_id = int(request.frame["id"])
        world_version = int(request.frame["world_version"])

        keys = request.frame.get("keys")
        if isinstance(keys, list) and keys:
            action = KeypressAction(type="keypress", keys=[str(k) for k in keys])
        else:
            action = WaitAction(type="wait", ms=200)

        return OperatorDecision(
            based_on_frame_id=frame_id,
            based_on_world_version=world_version,
            intent="fake: no-op observe step",
            state_assessment={},
            risk=RiskAssessment(
                level="low",
                category="navigation",
                requires_human=False,
                reason="",
            ),
            preconditions={},
            actions=[action],
            postconditions={},
            fallback="reobserve",
        )
