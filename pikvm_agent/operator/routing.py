"""Deterministic model-role routing for the operator control loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pikvm_agent.config import OperatorRoutingConfig


@dataclass(frozen=True)
class ModelRoute:
    role: str
    lane: str
    reason: str


class OperatorModelRouter:
    """Choose model role from checkpointed state, never from model preference."""

    def __init__(self, config: OperatorRoutingConfig) -> None:
        self.config = config

    def select(self, state: dict[str, Any]) -> ModelRoute:
        if not self.config.enabled:
            return ModelRoute(
                role="single",
                lane=self.config.fallback_lane,
                reason="routing_disabled",
            )
        step = int(state.get("step", 0))
        if state.get("model_replan"):
            return ModelRoute(
                role="reasoner",
                lane=self.config.reasoner_lane,
                reason="explicit_replan",
            )
        verification = state.get("verification_result") or {}
        if verification and (
            verification.get("safe_to_continue") is False
            or verification.get("status") in {"failed", "uncertain", "mismatch"}
        ):
            return ModelRoute(
                role="reasoner",
                lane=self.config.reasoner_lane,
                reason="verification_failure",
            )
        if step == 0 or not state.get("reasoning_plan"):
            return ModelRoute(
                role="reasoner",
                lane=self.config.reasoner_lane,
                reason="initial_plan",
            )
        if step and step % self.config.refresh_every_steps == 0:
            return ModelRoute(
                role="reasoner",
                lane=self.config.reasoner_lane,
                reason="periodic_refresh",
            )
        return ModelRoute(
            role="controller",
            lane=self.config.controller_lane,
            reason="routine_step",
        )
