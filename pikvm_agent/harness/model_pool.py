"""Role routing and failover for structured model providers."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from pikvm_agent.harness.agent_models import (
    ControllerDecision,
    ModelRequest,
    ModelResponse,
    ModelRole,
)
from pikvm_agent.harness.model_budget import (
    ModelAttemptBudget,
    ModelBudgetExceeded,
)


class ModelProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class ModelPoolError(RuntimeError):
    pass


@dataclass(frozen=True)
class RoleRoute:
    providers: list[str]

    def __post_init__(self) -> None:
        if not self.providers:
            raise ValueError("a role route needs at least one provider")


OutputT = TypeVar("OutputT", bound=BaseModel)
ModelEventSink = Callable[[str, dict[str, object]], Awaitable[None]]


@dataclass
class ProviderHealth:
    kind: str = "unknown"
    configured_model: str | None = None
    billing_mode: str = "unclassified"
    interface: str = "Unknown interface"
    pixel_input: str = "Unknown pixel input"
    structured_output: str = "Unknown output contract"
    support_tier: str = "unclassified"
    implementation_contract: str = "unknown"
    ready: bool = True
    credential: str = "unknown"
    auth_mode: str = "unknown"
    credential_owner: str = "unknown"
    credential_source: str | None = None
    readiness_error: str | None = None
    routes: list[dict[str, object]] = field(default_factory=list)
    calls: int = 0
    successes: int = 0
    failures: int = 0
    skipped: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None
    last_error_class: str | None = None
    last_latency_ms: int | None = None
    last_model: str | None = None
    last_success_at: str | None = None
    cooldown_until: str | None = None


_SAFE_PROVIDER_FAILURES = (
    "authentication-failed",
    "rate-limited",
    "quota-or-billing",
    "timeout",
    "provider-unavailable",
    "structured-output-error",
    "request-rejected",
    "executable-not-found",
    "invalid-structured-output",
    "provider-error",
)


def _provider_failure_class(exc: Exception) -> str:
    text = str(exc).casefold()
    if "timed out" in text:
        return "timeout"
    for failure_class in _SAFE_PROVIDER_FAILURES:
        if failure_class in text:
            return failure_class
    if isinstance(exc, ValidationError):
        return "invalid-structured-output"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(exc, FileNotFoundError):
        return "executable-not-found"
    return "provider-error"


def _safe_controller_downgrade(
    output_type: type[BaseModel],
    data: dict[str, Any],
    exc: ValidationError,
) -> tuple[BaseModel, dict[str, object]] | None:
    """Keep a safe text draft while dropping its unverified commit suffix.

    This is deliberately narrower than general output repair. It applies only
    when the controller's first action is valid text input and the sole local
    validation error is an active follow-up after that text. The harness never
    invents or reorders an action; it removes the unsafe suffix and records the
    downgrade before any HID checkpoint exists.
    """

    if output_type is not ControllerDecision or not isinstance(data, dict):
        return None
    errors = exc.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )
    if len(errors) != 1 or "active follow-up" not in str(
        errors[0].get("msg") or ""
    ):
        return None
    actions = data.get("actions")
    if (
        data.get("outcome") != "act"
        or not isinstance(actions, list)
        or not actions
        or not isinstance(actions[0], dict)
        or actions[0].get("type") != "type_text"
    ):
        return None
    passive_types = {"wait", "wait_for_stable_screen", "wait_for_change"}
    active_index = next(
        (
            index
            for index, action in enumerate(actions[1:], start=1)
            if not isinstance(action, dict)
            or action.get("type") not in passive_types
        ),
        None,
    )
    if active_index is None:
        return None
    preserved = actions[:active_index]
    dropped = actions[active_index:]
    candidate = dict(data)
    candidate["intent"] = (
        "Prepare the model-requested text as a draft; active follow-up "
        "separated for independent verification."
    )
    candidate["actions"] = preserved
    candidate["expected_evidence"] = [
        "The exact drafted text is visibly present in the focused input "
        "without being submitted."
    ]
    try:
        output = output_type.model_validate(candidate)
    except ValidationError:
        return None
    return output, {
        "reason": "text-active-follow-up-separated",
        "preserved_actions": len(preserved),
        "dropped_actions": len(dropped),
        "dropped_action_types": [
            str(action.get("type") or "unknown")
            if isinstance(action, dict)
            else "invalid"
            for action in dropped
        ],
    }


class ModelPool:
    """Try an ordered provider chain and return only schema-valid output.

    Provider fallback happens before any computer action.  Once the harness has
    checkpointed and submitted an action, transport recovery is handled through
    the computer idempotency key instead of asking another model to improvise.
    """

    def __init__(
        self,
        *,
        providers: dict[str, ModelProvider],
        routes: dict[ModelRole, RoleRoute],
        provider_metadata: dict[str, dict[str, Any]] | None = None,
        provider_conformance_path: Path | None = None,
        failure_cooldowns: dict[str, float] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.providers = providers
        self.routes = routes
        metadata = provider_metadata or {}
        self._health = {
            name: ProviderHealth(
                kind=str(metadata.get(name, {}).get("kind") or "unknown"),
                configured_model=(
                    str(metadata[name]["configured_model"])
                    if metadata.get(name, {}).get("configured_model")
                    else None
                ),
                billing_mode=str(
                    metadata.get(name, {}).get("billing_mode")
                    or "unclassified"
                ),
                interface=str(
                    metadata.get(name, {}).get("interface")
                    or "Unknown interface"
                ),
                pixel_input=str(
                    metadata.get(name, {}).get("pixel_input")
                    or "Unknown pixel input"
                ),
                structured_output=str(
                    metadata.get(name, {}).get("structured_output")
                    or "Unknown output contract"
                ),
                support_tier=str(
                    metadata.get(name, {}).get("support_tier")
                    or "unclassified"
                ),
                implementation_contract=str(
                    metadata.get(name, {}).get("implementation_contract")
                    or "unknown"
                ),
                ready=bool(metadata.get(name, {}).get("ready", True)),
                credential=str(
                    metadata.get(name, {}).get("credential") or "unknown"
                ),
                auth_mode=str(
                    metadata.get(name, {}).get("auth_mode") or "unknown"
                ),
                credential_owner=str(
                    metadata.get(name, {}).get("credential_owner") or "unknown"
                ),
                credential_source=(
                    str(metadata[name]["credential_source"])
                    if metadata.get(name, {}).get("credential_source")
                    else None
                ),
                readiness_error=(
                    str(metadata[name]["error"])
                    if metadata.get(name, {}).get("error")
                    else None
                ),
                routes=[
                    dict(route)
                    for route in metadata.get(name, {}).get("routes", [])
                    if isinstance(route, dict)
                ],
            )
            for name in providers
        }
        self._failure_cooldowns = {
            name: max(0.0, float(seconds))
            for name, seconds in (failure_cooldowns or {}).items()
        }
        self._provider_conformance_path = provider_conformance_path
        self._provider_conformance_loaded = False
        self._provider_conformance_signature: (
            tuple[int, int, int] | None
        ) = None
        self._provider_conformance_health: dict[
            str, dict[str, object]
        ] = {}
        self._cooldown_deadlines: dict[str, float] = {}
        self._monotonic = monotonic

    def route_names(
        self,
        role: ModelRole,
        *,
        preferred_provider: str | None = None,
    ) -> list[str]:
        if preferred_provider is not None:
            return [preferred_provider]
        route = self.routes.get(role)
        return list(route.providers) if route else []

    def health(self) -> dict[str, dict[str, object]]:
        now = self._monotonic()
        for name, deadline in list(self._cooldown_deadlines.items()):
            if deadline <= now:
                self._cooldown_deadlines.pop(name, None)
                if name in self._health:
                    self._health[name].cooldown_until = None
        health = {
            name: asdict(status) for name, status in self._health.items()
        }
        if self._provider_conformance_path is not None:
            from pikvm_agent.harness.provider_conformance import (
                read_provider_conformance_health,
            )

            try:
                report_stat = self._provider_conformance_path.stat()
                signature = (
                    report_stat.st_ino,
                    report_stat.st_size,
                    report_stat.st_mtime_ns,
                )
            except OSError:
                signature = None
            if (
                not self._provider_conformance_loaded
                or signature != self._provider_conformance_signature
            ):
                self._provider_conformance_health = (
                    read_provider_conformance_health(
                        self._provider_conformance_path,
                        provider_names=list(health),
                    )
                )
                self._provider_conformance_signature = signature
                self._provider_conformance_loaded = True
            for name, status in self._provider_conformance_health.items():
                health[name].update(status)
        return health

    async def complete(
        self,
        request: ModelRequest,
        output_type: type[OutputT],
        *,
        on_event: ModelEventSink | None = None,
        bypass_cooldown: bool = False,
        budget: ModelAttemptBudget | None = None,
        preferred_provider: str | None = None,
    ) -> tuple[OutputT, ModelResponse]:
        route = self.routes.get(request.role)
        if route is None:
            raise ModelPoolError(f"no provider route for role {request.role}")
        provider_names = (
            [preferred_provider]
            if preferred_provider is not None
            else route.providers
        )
        errors: list[str] = []
        async def emit(kind: str, **data: object) -> None:
            if on_event is not None:
                await on_event(kind, data)

        for route_index, name in enumerate(provider_names):
            provider = self.providers.get(name)
            if provider is None:
                errors.append(f"{name}=not-configured")
                await emit(
                    "provider_failed",
                    provider=name,
                    route_index=route_index,
                    attempt=0,
                    error_type="NotConfigured",
                    error="provider is not configured",
                )
                continue
            health = self._health.setdefault(name, ProviderHealth())
            if not health.ready:
                health.skipped += 1
                reason = health.readiness_error or "provider-not-ready"
                errors.append(f"{name}={reason}")
                await emit(
                    "provider_skipped",
                    provider=name,
                    route_index=route_index,
                    reason="not-ready",
                    error=reason,
                )
                continue
            cooldown_deadline = self._cooldown_deadlines.get(name)
            if (
                not bypass_cooldown
                and cooldown_deadline is not None
                and cooldown_deadline > self._monotonic()
            ):
                health.skipped += 1
                errors.append(f"{name}=cooldown")
                await emit(
                    "provider_skipped",
                    provider=name,
                    route_index=route_index,
                    reason="cooldown",
                    error=health.last_error_class or "provider-error",
                    cooldown_until=health.cooldown_until or "",
                )
                continue
            if cooldown_deadline is not None:
                self._cooldown_deadlines.pop(name, None)
                health.cooldown_until = None
            attempt = -1
            try:
                active_request = request
                response: ModelResponse | None = None
                output: OutputT | None = None
                for attempt in range(2):
                    lease = None
                    if budget is not None:
                        try:
                            lease = await budget.authorize(
                                provider=name,
                                request=request,
                                attempt=attempt + 1,
                                repair=bool(attempt),
                            )
                        except ModelBudgetExceeded:
                            await emit(
                                "provider_budget_blocked",
                                provider=name,
                                route_index=route_index,
                                attempt=attempt + 1,
                                repair=bool(attempt),
                            )
                            raise
                    await emit(
                        "provider_started",
                        provider=name,
                        route_index=route_index,
                        attempt=attempt + 1,
                        repair=bool(attempt),
                    )
                    health.calls += 1
                    try:
                        response = await provider.complete(active_request)
                    except Exception:
                        if budget is not None and lease is not None:
                            await budget.close(
                                lease,
                                usage=None,
                                succeeded=False,
                            )
                        raise
                    if budget is not None and lease is not None:
                        try:
                            await budget.close(
                                lease,
                                usage=response.usage,
                                succeeded=True,
                            )
                        except ModelBudgetExceeded:
                            await emit(
                                "provider_budget_blocked",
                                provider=name,
                                route_index=route_index,
                                attempt=attempt + 1,
                                repair=bool(attempt),
                                reason="settlement",
                            )
                            raise
                    try:
                        output = output_type.model_validate(response.data)
                        break
                    except ValidationError as exc:
                        downgraded = _safe_controller_downgrade(
                            output_type,
                            response.data,
                            exc,
                        )
                        if downgraded is not None:
                            output, downgrade = downgraded
                            await emit(
                                "provider_schema_safety_downgrade",
                                provider=name,
                                route_index=route_index,
                                attempt=attempt + 1,
                                **downgrade,
                            )
                            break
                        if attempt:
                            raise
                        # Strict JSON Schema cannot express every Pydantic
                        # dependent invariant.  Give the same provider one
                        # bounded repair attempt before failover, still before
                        # any HID action is checkpointed or submitted.
                        safe_errors = exc.errors(
                            include_url=False,
                            include_context=False,
                            include_input=False,
                        )
                        await emit(
                            "provider_schema_repair",
                            provider=name,
                            route_index=route_index,
                            attempt=attempt + 1,
                            validation_errors=len(safe_errors),
                        )
                        active_request = request.model_copy(
                            update={
                                "prompt": (
                                    request.prompt
                                    + "\n\nYOUR PREVIOUS JSON WAS REJECTED BY "
                                    "LOCAL VALIDATION. Return a fresh JSON object "
                                    "that satisfies both the schema and these "
                                    "invariants:\n"
                                    + json.dumps(safe_errors, sort_keys=True)
                                )
                            }
                        )
                if response is None or output is None:  # pragma: no cover - defensive
                    raise RuntimeError("provider returned no validated response")
                health.successes += 1
                health.consecutive_failures = 0
                health.last_error = None
                health.last_error_class = None
                health.last_latency_ms = response.latency_ms
                health.last_model = response.model
                health.last_success_at = datetime.now(UTC).isoformat()
                health.cooldown_until = None
                self._cooldown_deadlines.pop(name, None)
                await emit(
                    "provider_completed",
                    provider=name,
                    route_index=route_index,
                    attempt=attempt + 1,
                    model=response.model,
                    latency_ms=response.latency_ms,
                )
                return output, response
            except ModelBudgetExceeded:
                raise
            except Exception as exc:  # provider boundary: fallback before any HID
                failure_class = _provider_failure_class(exc)
                health.failures += 1
                health.consecutive_failures += 1
                health.last_error = failure_class
                health.last_error_class = failure_class
                cooldown_s = self._failure_cooldowns.get(name, 15.0)
                if cooldown_s > 0:
                    self._cooldown_deadlines[name] = (
                        self._monotonic() + cooldown_s
                    )
                    health.cooldown_until = (
                        datetime.now(UTC) + timedelta(seconds=cooldown_s)
                    ).isoformat()
                errors.append(f"{name}={failure_class}")
                await emit(
                    "provider_failed",
                    provider=name,
                    route_index=route_index,
                    attempt=attempt + 1,
                    error_type=failure_class,
                    error=failure_class,
                    cooldown_until=health.cooldown_until or "",
                )
        raise ModelPoolError(
            f"all providers unavailable for {request.role}: "
            + " | ".join(errors)
        )
