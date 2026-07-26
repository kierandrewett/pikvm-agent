"""Durable per-run authorization for model-provider attempts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, Literal, Mapping, Protocol

from pikvm_agent.harness.agent_models import ModelRequest, RunSnapshot


class ModelBudgetExceeded(RuntimeError):
    """A model attempt was refused by a harness-owned run budget."""


class BudgetStore(Protocol):
    async def save(self, run: RunSnapshot) -> None: ...


class ModelAttemptBudget(Protocol):
    async def authorize(
        self,
        *,
        provider: str,
        request: ModelRequest,
        attempt: int,
        repair: bool,
    ) -> ModelAttemptLease: ...

    async def close(
        self,
        lease: ModelAttemptLease,
        *,
        usage: dict[str, Any] | None,
        succeeded: bool,
    ) -> None: ...


@dataclass(frozen=True)
class ModelAttemptLease:
    lease_id: str
    provider: str
    reservation_microusd: int


@dataclass(frozen=True)
class ProviderCostTerms:
    mode: Literal["subscription", "metered"]
    reservation_microusd: int = 0
    usage_usd_per_million: Mapping[str, Decimal] = field(default_factory=dict)

    @classmethod
    def subscription(cls) -> ProviderCostTerms:
        return cls(mode="subscription")

    @classmethod
    def metered(
        cls,
        *,
        reservation_microusd: int,
        usage_usd_per_million: Mapping[str, Decimal | str | int | float],
    ) -> ProviderCostTerms:
        return cls(
            mode="metered",
            reservation_microusd=reservation_microusd,
            usage_usd_per_million={
                path: Decimal(str(price))
                for path, price in usage_usd_per_million.items()
            },
        )

    def __post_init__(self) -> None:
        if self.mode == "subscription":
            if self.reservation_microusd or self.usage_usd_per_million:
                raise ValueError("subscription billing cannot define metered prices")
            return
        if self.reservation_microusd <= 0:
            raise ValueError("metered billing requires a positive cost reservation")
        if not self.usage_usd_per_million:
            raise ValueError("metered billing requires explicit usage prices")
        if any(price < 0 for price in self.usage_usd_per_million.values()):
            raise ValueError("usage prices cannot be negative")


@dataclass(frozen=True)
class ModelBudgetPolicy:
    max_provider_attempts: int
    max_cost_microusd: int | None = None
    pricing_version: str | None = None
    provider_costs: Mapping[str, ProviderCostTerms] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_provider_attempts < 1:
            raise ValueError("model provider attempt budget must be positive")
        if self.max_cost_microusd is not None and self.max_cost_microusd < 1:
            raise ValueError("model cost budget must be positive")


class DurableRunModelBudget:
    """Checkpoint each provider attempt before the provider is invoked."""

    def __init__(
        self,
        *,
        run: RunSnapshot,
        store: BudgetStore,
        policy: ModelBudgetPolicy,
    ) -> None:
        self._run = run
        self._store = store
        self._policy = policy

    async def authorize(
        self,
        *,
        provider: str,
        request: ModelRequest,
        attempt: int,
        repair: bool,
    ) -> ModelAttemptLease:
        state = self._run.model_budget
        state.provider_attempt_limit = self._policy.max_provider_attempts
        state.max_cost_microusd = self._policy.max_cost_microusd
        state.pricing_version = self._policy.pricing_version
        if state.provider_attempts >= self._policy.max_provider_attempts:
            raise ModelBudgetExceeded("model provider attempt budget exhausted")
        terms = self._policy.provider_costs.get(provider)
        reservation = 0
        if self._policy.max_cost_microusd is not None:
            if terms is None:
                raise ModelBudgetExceeded(
                    "model provider has no billing classification"
                )
            reservation = terms.reservation_microusd
            projected = (
                state.committed_cost_microusd
                + state.outstanding_cost_microusd
                + reservation
            )
            if projected > self._policy.max_cost_microusd:
                raise ModelBudgetExceeded("model cost budget exhausted")
        lease = ModelAttemptLease(
            lease_id=str(uuid.uuid4()),
            provider=provider,
            reservation_microusd=reservation,
        )
        state.provider_attempts += 1
        if reservation:
            state.reservations_microusd[lease.lease_id] = reservation
        self._run.record(
            "model.budget_reserved",
            role=request.role,
            provider=provider,
            attempt=attempt,
            repair=repair,
            provider_attempts=state.provider_attempts,
            provider_attempt_limit=self._policy.max_provider_attempts,
            reservation_microusd=reservation,
            committed_cost_microusd=state.committed_cost_microusd,
            outstanding_cost_microusd=state.outstanding_cost_microusd,
            max_cost_microusd=self._policy.max_cost_microusd,
        )
        await self._store.save(self._run)
        return lease

    async def close(
        self,
        lease: ModelAttemptLease,
        *,
        usage: dict[str, Any] | None,
        succeeded: bool,
    ) -> None:
        terms = self._policy.provider_costs.get(lease.provider)
        if self._policy.max_cost_microusd is None or terms is None:
            return
        state = self._run.model_budget
        reservation = state.reservations_microusd.pop(lease.lease_id, None)
        if reservation is None and lease.reservation_microusd:
            raise ModelBudgetExceeded(
                "model cost reservation state is inconsistent"
            )
        reservation = int(reservation or 0)
        if terms.mode == "subscription":
            actual = 0
        elif not succeeded:
            actual = reservation
        else:
            try:
                actual = self._reported_cost(usage or {}, terms)
            except ValueError:
                state.committed_cost_microusd += reservation
                state.provider_cost_microusd[lease.provider] = (
                    state.provider_cost_microusd.get(lease.provider, 0)
                    + reservation
                )
                self._run.record(
                    "model.budget_settlement_failed",
                    provider=lease.provider,
                    reservation_microusd=reservation,
                    reason="usage-report-missing-or-invalid",
                )
                await self._store.save(self._run)
                raise ModelBudgetExceeded(
                    "model usage report missing for metered provider"
                )
        state.committed_cost_microusd += actual
        state.provider_cost_microusd[lease.provider] = (
            state.provider_cost_microusd.get(lease.provider, 0) + actual
        )
        self._run.record(
            "model.budget_settled",
            provider=lease.provider,
            succeeded=succeeded,
            reservation_microusd=reservation,
            actual_cost_microusd=actual,
            committed_cost_microusd=state.committed_cost_microusd,
            outstanding_cost_microusd=state.outstanding_cost_microusd,
            max_cost_microusd=self._policy.max_cost_microusd,
        )
        await self._store.save(self._run)
        if (
            self._policy.max_cost_microusd is not None
            and state.committed_cost_microusd
            + state.outstanding_cost_microusd
            > self._policy.max_cost_microusd
        ):
            raise ModelBudgetExceeded(
                "model cost budget exhausted after provider settlement"
            )

    @classmethod
    def _reported_cost(
        cls,
        usage: dict[str, Any],
        terms: ProviderCostTerms,
    ) -> int:
        total = Decimal(0)
        for path, price in terms.usage_usd_per_million.items():
            value = cls._usage_value(usage, path)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("usage field is not numeric")
            try:
                tokens = Decimal(str(value))
            except InvalidOperation as exc:
                raise ValueError("usage field is not numeric") from exc
            if tokens < 0:
                raise ValueError("usage field is negative")
            # One USD per million tokens is one micro-USD per token.
            total += tokens * price
        return int(total.to_integral_value(rounding=ROUND_CEILING))

    @staticmethod
    def _usage_value(usage: dict[str, Any], path: str) -> Any:
        value: Any = usage
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                raise ValueError("usage report is missing a priced field")
            value = value[part]
        return value
