"""The hard-coded safety policy engine.

This is *owned, hard-coded policy* — never prompt-only. The operator proposes;
this engine independently classifies the proposal's risk, escalates anything in
``policy.require_human_for`` to a human, blocks anything in
``policy.always_block``, and refuses to act on a stale plan. It also re-derives
risk from the *text being typed* so a dangerous shell command can never ride
into a terminal disguised as a low-risk ``type_text``.
"""

from __future__ import annotations

from typing import Any, TypedDict

from pikvm_agent.config import PolicyConfig
from pikvm_agent.core.models import OperatorDecision, PolicyResult, RiskCategory
from pikvm_agent.policy.risk import classify_command

# type_text is only ever allowed in modes where free-text entry is meaningful;
# everywhere else it is blocked outright (E9: a pager swallows keystrokes as
# commands; credential/captcha prompts must be human-driven; unknown is unsafe).
_TYPE_TEXT_BLOCKED_MODES: dict[str, str] = {
    "terminal.pager": "type_text blocked in a terminal pager (keystrokes are pager commands)",
    "credential_prompt": "type_text blocked at a credential prompt (human-only)",
    "captcha_or_human_verification": "type_text blocked during human verification (captcha)",
    "unknown": "type_text blocked in unknown mode (target not identified)",
}


class LocalRisk(TypedDict):
    """The engine's independent read of a decision's risk."""

    level: str
    category: str
    requires_human: bool
    blocked: bool
    reason: str


def _as_decision(decision: OperatorDecision | dict[str, Any]) -> OperatorDecision:
    """Accept either a model or a plain dict from the graph state."""
    if isinstance(decision, OperatorDecision):
        return decision
    return OperatorDecision.model_validate(decision)


class SafetyPolicyEngine:
    """Owns the local risk classification and the policy gate.

    Construct with a :class:`PolicyConfig`; the engine reads
    ``require_human_for`` and ``always_block`` from it. All methods are pure and
    do no network or screen access — they reason only over the decision and the
    supplied freshness counters.
    """

    def __init__(self, policy: PolicyConfig) -> None:
        self.policy = policy
        self._require_human: frozenset[str] = frozenset(policy.require_human_for)
        self._always_block: frozenset[str] = frozenset(policy.always_block)

    def classify_local_risk(
        self,
        decision: OperatorDecision | dict[str, Any],
        state: dict[str, Any] | None = None,
    ) -> LocalRisk:
        """Independently classify a decision's risk.

        Starts from the operator's declared ``risk.category``/``level``, then:
          * escalates ``requires_human`` if the category is in
            ``policy.require_human_for`` (or the operator already asked for it);
          * sets ``blocked`` if the category is in ``policy.always_block``;
          * if any action is a ``type_text`` whose text classifies *dangerous*
            (see :func:`classify_command`), raises the category to
            ``terminal_mutating`` and forces ``requires_human``.
        """
        dec = _as_decision(decision)

        category: str = dec.risk.category
        level: str = dec.risk.level
        reason: str = dec.risk.reason
        requires_human: bool = bool(dec.risk.requires_human)

        # Re-derive risk from the text being typed — never trust the label alone.
        for action in dec.actions:
            if getattr(action, "type", None) == "type_text":
                if classify_command(getattr(action, "text", "")) == "dangerous":
                    category = "terminal_mutating"
                    level = "high"
                    requires_human = True
                    reason = "dangerous shell command in type_text"
                    break

        if category in self._require_human:
            requires_human = True
            if not reason:
                reason = f"category '{category}' always requires human approval"

        blocked = category in self._always_block
        if blocked and not reason:
            reason = f"category '{category}' is blocked by policy"

        return LocalRisk(
            level=level,
            category=category,
            requires_human=requires_human,
            blocked=blocked,
            reason=reason,
        )

    def policy_gate(
        self,
        decision: OperatorDecision | dict[str, Any],
        current_frame_id: int,
        current_world_version: int,
        *,
        approved: bool = False,
    ) -> PolicyResult:
        """Gate a decision: freshness first, then hard-block, then human gate.

        A plan is only valid against the exact ``(frame_id, world_version)`` it
        was built on. A frame mismatch yields ``blocked`` "stale_frame"; a world
        mismatch yields ``blocked`` "stale_world" — even an approved decision
        fails a stale check (approval is not a force-execute button).

        Order then is **hard-block before human gate**: an ``always_block``
        category is blocked and is *not* approvable. Otherwise a
        ``requires_human`` category needs approval — unless ``approved=True``
        (the human already said yes and the frame/world is still fresh), in which
        case it is ``allowed``.
        """
        dec = _as_decision(decision)

        if dec.based_on_frame_id != current_frame_id:
            return PolicyResult(status="blocked", blocked=True, reason="stale_frame")
        if dec.based_on_world_version != current_world_version:
            return PolicyResult(status="blocked", blocked=True, reason="stale_world")

        risk = self.classify_local_risk(dec)
        category: RiskCategory = risk["category"]  # type: ignore[assignment]
        level = risk["level"]

        if risk["blocked"]:
            return PolicyResult(
                status="blocked",
                category=category,
                level=level,  # type: ignore[arg-type]
                requires_human=False,
                blocked=True,
                reason=risk["reason"] or "blocked by policy",
            )
        if risk["requires_human"] and not approved:
            return PolicyResult(
                status="approval_required",
                category=category,
                level=level,  # type: ignore[arg-type]
                requires_human=True,
                blocked=False,
                reason=risk["reason"] or "human approval required",
            )
        return PolicyResult(
            status="allowed",
            category=category,
            level=level,  # type: ignore[arg-type]
            requires_human=False,
            blocked=False,
            reason=risk["reason"],
        )

    def action_allowed_in_mode(self, action_type: str, mode: str) -> tuple[bool, str]:
        """Whether an action type may run in the current detected mode.

        ``type_text`` is blocked in ``terminal.pager``, ``credential_prompt``,
        ``captcha_or_human_verification`` and ``unknown`` (E9). Returns
        ``(False, reason)`` when blocked, ``(True, "")`` otherwise.
        """
        if action_type == "type_text" and mode in _TYPE_TEXT_BLOCKED_MODES:
            return (False, _TYPE_TEXT_BLOCKED_MODES[mode])
        return (True, "")
