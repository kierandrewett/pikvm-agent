"""Speed and efficiency summaries for durable provider-neutral harness runs."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

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


class CriticalPathBreakdown(BaseModel):
    """Observed wall-time buckets from durable run events."""

    model_config = ConfigDict(extra="forbid")

    startup_ms: int = 0
    provider_wait_ms: int = 0
    action_execution_ms: int = 0
    evidence_capture_ms: int = 0
    unclassified_overhead_ms: int = 0
    overlap_ms: int = 0
    provider_wait_share: float = 0.0
    action_execution_share: float = 0.0
    provider_calls: int = 0
    reasoner_calls: int = 0
    controller_calls: int = 0
    verifier_calls: int = 0


class HumanSpeedComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    human_baseline_ms: int
    agent_wall_clock_ms: int
    time_over_human_ms: int
    agent_to_human_ratio: float
    human_competitive: bool


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
    critical_path: CriticalPathBreakdown = Field(
        default_factory=CriticalPathBreakdown
    )
    human_comparison: HumanSpeedComparison | None = None


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
        "action.completed_unverified",
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


def _bounded_interval(
    start: datetime,
    end: datetime,
    *,
    run_start: datetime,
    run_end: datetime,
) -> tuple[datetime, datetime] | None:
    bounded_start = max(start, run_start)
    bounded_end = min(end, run_end)
    if bounded_end <= bounded_start:
        return None
    return bounded_start, bounded_end


def _interval_total_ms(
    intervals: list[tuple[datetime, datetime]],
) -> int:
    if not intervals:
        return 0
    ordered = sorted(intervals)
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return round(sum(_duration_ms(start, end) for start, end in merged))


def _matching_pending_index(
    pending: list[HarnessEvent],
    terminal: HarnessEvent,
    *,
    fields: tuple[str, ...],
) -> int | None:
    for index, started in enumerate(pending):
        if all(
            field not in terminal.data
            or field not in started.data
            or terminal.data.get(field) == started.data.get(field)
            for field in fields
        ):
            return index
    return None


def _provider_wait_intervals(
    run: RunSnapshot,
) -> list[tuple[datetime, datetime]]:
    pending: list[HarnessEvent] = []
    intervals: list[tuple[datetime, datetime]] = []
    terminal_kinds = {
        "model.provider_output_received",
        "model.provider_failed",
    }
    fields = ("role", "provider", "route_index", "attempt", "repair")
    for event in run.events:
        if event.kind == "model.provider_request_sent":
            pending.append(event)
            continue
        if event.kind not in terminal_kinds:
            continue
        index = _matching_pending_index(pending, event, fields=fields)
        if index is None:
            continue
        started = pending.pop(index)
        interval = _bounded_interval(
            started.at,
            event.at,
            run_start=run.created_at,
            run_end=run.updated_at,
        )
        if interval is not None:
            intervals.append(interval)
    for started in pending:
        interval = _bounded_interval(
            started.at,
            run.updated_at,
            run_start=run.created_at,
            run_end=run.updated_at,
        )
        if interval is not None:
            intervals.append(interval)
    return intervals


def _action_execution_intervals(
    run: RunSnapshot,
) -> list[tuple[datetime, datetime]]:
    pending: list[HarnessEvent] = []
    intervals: list[tuple[datetime, datetime]] = []
    terminal_kinds = {
        "action.completed",
        "action.completed_unverified",
        "action.recoverable_failure",
        "action.failed",
        "action.refused_stale",
        "action.transport_uncertain",
    }
    for event in run.events:
        if event.kind == "action.attempted":
            pending.append(event)
            continue
        if event.kind not in terminal_kinds:
            continue
        index = _matching_pending_index(
            pending,
            event,
            fields=("index", "attempt"),
        )
        if index is None:
            continue
        started = pending.pop(index)
        interval = _bounded_interval(
            started.at,
            event.at,
            run_start=run.created_at,
            run_end=run.updated_at,
        )
        if interval is not None:
            intervals.append(interval)
    for started in pending:
        interval = _bounded_interval(
            started.at,
            run.updated_at,
            run_start=run.created_at,
            run_end=run.updated_at,
        )
        if interval is not None:
            intervals.append(interval)
    return intervals


def _evidence_capture_intervals(
    run: RunSnapshot,
) -> list[tuple[datetime, datetime]]:
    pending_terminal: datetime | None = None
    intervals: list[tuple[datetime, datetime]] = []
    action_terminal_kinds = {
        "action.completed",
        "action.completed_unverified",
        "action.recoverable_failure",
        "action.failed",
        "action.refused_stale",
        "action.transport_uncertain",
    }
    for event in run.events:
        if event.kind in action_terminal_kinds:
            pending_terminal = event.at
            continue
        if (
            event.kind != "verification.evidence_captured"
            or pending_terminal is None
        ):
            continue
        interval = _bounded_interval(
            pending_terminal,
            event.at,
            run_start=run.created_at,
            run_end=run.updated_at,
        )
        pending_terminal = None
        if interval is not None:
            intervals.append(interval)
    return intervals


def _critical_path(run: RunSnapshot) -> CriticalPathBreakdown:
    wall_ms = round(_duration_ms(run.created_at, run.updated_at))
    first_work = next(
        (
            event.at
            for event in run.events
            if event.kind
            in {"model.provider_request_sent", "action.attempted"}
        ),
        run.created_at,
    )
    startup_interval = _bounded_interval(
        run.created_at,
        first_work,
        run_start=run.created_at,
        run_end=run.updated_at,
    )
    startup_intervals = [startup_interval] if startup_interval else []
    provider_intervals = _provider_wait_intervals(run)
    action_intervals = _action_execution_intervals(run)
    evidence_intervals = _evidence_capture_intervals(run)
    measured_intervals = [
        *startup_intervals,
        *provider_intervals,
        *action_intervals,
        *evidence_intervals,
    ]
    startup_ms = _interval_total_ms(startup_intervals)
    provider_wait_ms = _interval_total_ms(provider_intervals)
    action_execution_ms = _interval_total_ms(action_intervals)
    evidence_capture_ms = _interval_total_ms(evidence_intervals)
    measured_union_ms = _interval_total_ms(measured_intervals)
    category_total_ms = (
        startup_ms
        + provider_wait_ms
        + action_execution_ms
        + evidence_capture_ms
    )
    provider_events = [
        event
        for event in run.events
        if event.kind == "model.provider_request_sent"
    ]
    role_calls = lambda role: sum(
        str(event.data.get("role") or "") == role
        for event in provider_events
    )
    return CriticalPathBreakdown(
        startup_ms=startup_ms,
        provider_wait_ms=provider_wait_ms,
        action_execution_ms=action_execution_ms,
        evidence_capture_ms=evidence_capture_ms,
        unclassified_overhead_ms=max(0, wall_ms - measured_union_ms),
        overlap_ms=max(0, category_total_ms - measured_union_ms),
        provider_wait_share=provider_wait_ms / max(1, wall_ms),
        action_execution_share=action_execution_ms / max(1, wall_ms),
        provider_calls=len(provider_events),
        reasoner_calls=role_calls("reasoner"),
        controller_calls=role_calls("controller"),
        verifier_calls=role_calls("verifier"),
    )


def summarize_run_performance(
    run: RunSnapshot,
    *,
    human_baseline_ms: int | None = None,
) -> RunPerformanceReport:
    """Summarize provider and HID-loop speed without counting hidden idle as work."""

    if human_baseline_ms is not None and human_baseline_ms <= 0:
        raise ValueError("human_baseline_ms must be greater than zero")
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
    wall_clock_ms = round(_duration_ms(run.created_at, run.updated_at))
    return RunPerformanceReport(
        run_id=run.run_id,
        status=run.status.value,
        wall_clock_ms=wall_clock_ms,
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
        critical_path=_critical_path(run),
        human_comparison=(
            HumanSpeedComparison(
                human_baseline_ms=human_baseline_ms,
                agent_wall_clock_ms=wall_clock_ms,
                time_over_human_ms=max(0, wall_clock_ms - human_baseline_ms),
                agent_to_human_ratio=wall_clock_ms / human_baseline_ms,
                human_competitive=(
                    run.status.value == "completed"
                    and wall_clock_ms <= human_baseline_ms
                ),
            )
            if human_baseline_ms is not None
            else None
        ),
    )
