"""Speed and efficiency summaries for durable provider-neutral harness runs."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from pikvm_agent.harness.agent_models import HarnessEvent, RunSnapshot


class Distribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    samples: int
    min_ms: float
    median_ms: float
    p95_ms: float
    max_ms: float
    mean_ms: float


class ModelLanePerformance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    provider: str
    model: str
    latency: Distribution


class RunPerformanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    wall_clock_ms: int
    model_active_ms: int
    model_lanes: list[ModelLanePerformance]
    model_failures: int
    actions_checkpointed: int
    actions_attempted: int
    actions_completed: int
    progress_actions_completed: int
    observation_only_actions_completed: int
    action_recoverable_failures: int
    action_stale_retries: int
    pointer_noops_rejected: int
    repeated_actions_stopped: int
    non_idempotent_retries_stopped: int
    autonomous_resumes: int = 0
    autonomy_stops: int = 0
    provider_attempts: int
    provider_failures: int
    provider_fallbacks: int
    provider_schema_repairs: int
    provider_safety_downgrades: int
    action_latency: Distribution | None
    completion_efficiency: float
    progress_action_ratio: float


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _distribution(values: list[float]) -> Distribution:
    if not values:
        raise ValueError("distribution requires at least one value")
    return Distribution(
        samples=len(values),
        min_ms=min(values),
        median_ms=statistics.median(values),
        p95_ms=_percentile(values, 0.95),
        max_ms=max(values),
        mean_ms=statistics.fmean(values),
    )


def _duration_ms(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() * 1_000)


def _action_latencies(events: list[HarnessEvent]) -> list[float]:
    attempts: dict[int, datetime] = {}
    latencies: list[float] = []
    terminal_kinds = {
        "action.completed",
        "action.recoverable_failure",
        "action.failed",
        "action.refused_stale",
        "action.transport_uncertain",
    }
    for event in events:
        index_value = event.data.get("index")
        if not isinstance(index_value, int):
            continue
        if event.kind == "action.attempted":
            attempts[index_value] = event.at
        elif event.kind in terminal_kinds and index_value in attempts:
            latencies.append(_duration_ms(attempts.pop(index_value), event.at))
    return latencies


def summarize_run_performance(run: RunSnapshot) -> RunPerformanceReport:
    """Summarize provider and HID-loop speed without counting hidden idle as work."""

    grouped: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    model_active_ms = 0
    for event in run.events:
        if event.kind != "model.completed":
            continue
        latency = event.data.get("latency_ms")
        if not isinstance(latency, (int, float)):
            continue
        role = str(event.data.get("role") or "unknown")
        provider = str(event.data.get("provider") or "unknown")
        model = str(event.data.get("model") or "unknown")
        grouped[(role, provider, model)].append(float(latency))
        model_active_ms += round(float(latency))

    event_count = lambda kind: sum(event.kind == kind for event in run.events)
    checkpointed = event_count("action.checkpointed")
    completed = event_count("action.completed")
    completed_indices = {
        event.data.get("index")
        for event in run.events
        if event.kind == "action.completed"
        and isinstance(event.data.get("index"), int)
    }
    progress_types = {
        "key",
        "type_text",
        "spreadsheet_grid",
        "click",
        "double_click",
        "scroll",
    }
    progress_indices = {
        event.data.get("index")
        for event in run.events
        if event.kind == "action.checkpointed"
        and isinstance(event.data.get("index"), int)
        and any(
            isinstance(action, dict)
            and str(action.get("type") or "") in progress_types
            for action in event.data.get("actions", [])
        )
    }
    classified_completed = {
        event.data.get("index")
        for event in run.events
        if event.kind == "action.checkpointed"
        and isinstance(event.data.get("index"), int)
    } & completed_indices
    progress_completed = len(completed_indices & progress_indices)
    observation_only_completed = len(classified_completed - progress_indices)
    action_latencies = _action_latencies(run.events)
    return RunPerformanceReport(
        run_id=run.run_id,
        status=run.status.value,
        wall_clock_ms=round(_duration_ms(run.created_at, run.updated_at)),
        model_active_ms=model_active_ms,
        model_lanes=[
            ModelLanePerformance(
                role=role,
                provider=provider,
                model=model,
                latency=_distribution(latencies),
            )
            for (role, provider, model), latencies in sorted(grouped.items())
        ],
        model_failures=event_count("model.failed"),
        actions_checkpointed=checkpointed,
        actions_attempted=event_count("action.attempted"),
        actions_completed=completed,
        progress_actions_completed=progress_completed,
        observation_only_actions_completed=observation_only_completed,
        action_recoverable_failures=(
            event_count("action.recoverable_failure") + event_count("action.failed")
        ),
        action_stale_retries=event_count(
            "action.stale_world_retry_checkpointed"
        ),
        pointer_noops_rejected=event_count(
            "controller.pointer_noop_rejected"
        ),
        repeated_actions_stopped=event_count("controller.repeated_actions"),
        non_idempotent_retries_stopped=event_count(
            "controller.non_idempotent_retry_stopped"
        ),
        autonomous_resumes=event_count("run.autonomous_resume"),
        autonomy_stops=event_count("run.autonomy_stopped"),
        provider_attempts=event_count("model.provider_started"),
        provider_failures=event_count("model.provider_failed"),
        provider_fallbacks=sum(
            event.kind == "model.provider_started"
            and event.data.get("route_index") not in {0, None}
            and event.data.get("attempt") == 1
            for event in run.events
        ),
        provider_schema_repairs=event_count("model.provider_schema_repair"),
        provider_safety_downgrades=event_count(
            "model.provider_schema_safety_downgrade"
        ),
        action_latency=(
            _distribution(action_latencies) if action_latencies else None
        ),
        completion_efficiency=completed / max(1, checkpointed),
        progress_action_ratio=progress_completed / max(1, completed),
    )
