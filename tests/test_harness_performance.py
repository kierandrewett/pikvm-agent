from __future__ import annotations

from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.agent_models import HarnessEvent, RunSnapshot, RunStatus
from pikvm_agent.harness.performance import summarize_run_performance


def _event(
    sequence: int,
    milliseconds: int,
    kind: str,
    **data: object,
) -> HarnessEvent:
    return HarnessEvent(
        sequence=sequence,
        at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(milliseconds=milliseconds),
        kind=kind,
        data=data,
    )


def test_run_performance_separates_model_lanes_and_action_latency() -> None:
    run = RunSnapshot(
        run_id="run_speed",
        task="benchmark",
        status=RunStatus.COMPLETED,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=8),
        events=[
            _event(
                1,
                0,
                "model.completed",
                role="reasoner",
                provider="deep",
                model="large",
                latency_ms=2_000,
            ),
            _event(
                2,
                2_100,
                "model.completed",
                role="controller",
                provider="fast",
                model="small",
                latency_ms=500,
            ),
            _event(
                3,
                2_700,
                "action.checkpointed",
                index=0,
                actions=[{"type": "type_text", "text": "hello"}],
            ),
            _event(4, 2_800, "action.attempted", index=0),
            _event(5, 3_300, "action.completed", index=0),
            _event(
                6,
                3_400,
                "model.completed",
                role="verifier",
                provider="vision",
                model="grounder",
                latency_ms=700,
            ),
            _event(7, 4_200, "controller.repeated_actions"),
            _event(8, 4_300, "controller.non_idempotent_retry_stopped"),
            _event(
                9,
                4_400,
                "model.provider_started",
                provider="first",
                route_index=0,
                attempt=1,
            ),
            _event(
                10,
                4_500,
                "model.provider_failed",
                provider="first",
                route_index=0,
                error_type="TimeoutError",
            ),
            _event(
                11,
                4_600,
                "model.provider_started",
                provider="fallback",
                route_index=1,
                attempt=1,
            ),
            _event(12, 4_700, "model.provider_schema_repair"),
            _event(13, 4_800, "model.provider_schema_safety_downgrade"),
            _event(14, 4_900, "action.stale_world_retry_checkpointed"),
            _event(15, 5_000, "controller.pointer_noop_rejected"),
            _event(16, 5_100, "run.autonomous_resume"),
            _event(17, 5_200, "run.autonomy_stopped"),
        ],
    )

    report = summarize_run_performance(run)

    assert report.wall_clock_ms == 8_000
    assert report.model_active_ms == 3_200
    assert report.actions_completed == 1
    assert report.completion_efficiency == 1.0
    assert report.progress_actions_completed == 1
    assert report.observation_only_actions_completed == 0
    assert report.progress_action_ratio == 1.0
    assert report.action_latency is not None
    assert report.action_latency.median_ms == 500
    assert report.repeated_actions_stopped == 1
    assert report.non_idempotent_retries_stopped == 1
    assert report.provider_attempts == 2
    assert report.provider_failures == 1
    assert report.provider_fallbacks == 1
    assert report.provider_schema_repairs == 1
    assert report.provider_safety_downgrades == 1
    assert report.action_stale_retries == 1
    assert report.pointer_noops_rejected == 1
    assert report.autonomous_resumes == 1
    assert report.autonomy_stops == 1
    assert [(lane.role, lane.provider) for lane in report.model_lanes] == [
        ("controller", "fast"),
        ("reasoner", "deep"),
        ("verifier", "vision"),
    ]


def test_run_performance_does_not_count_pointer_wiggles_as_progress() -> None:
    run = RunSnapshot(
        run_id="run_pointer_wiggle",
        task="benchmark",
        status=RunStatus.ABORTED,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=2),
        events=[
            _event(
                1,
                0,
                "action.checkpointed",
                index=0,
                actions=[{"type": "move", "x": 353, "y": 245}],
            ),
            _event(2, 100, "action.attempted", index=0),
            _event(3, 200, "action.completed", index=0),
            _event(
                4,
                300,
                "action.checkpointed",
                index=1,
                actions=[{"type": "key", "keys": ["ENTER"]}],
            ),
            _event(5, 400, "action.attempted", index=1),
            _event(6, 500, "action.completed", index=1),
        ],
    )

    report = summarize_run_performance(run)

    assert report.actions_completed == 2
    assert report.completion_efficiency == 1.0
    assert report.progress_actions_completed == 1
    assert report.observation_only_actions_completed == 1
    assert report.progress_action_ratio == 0.5


def test_run_performance_counts_spreadsheet_grid_entry_as_progress() -> None:
    run = RunSnapshot(
        run_id="run_spreadsheet_grid",
        task="benchmark",
        status=RunStatus.COMPLETED,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=1),
        events=[
            _event(
                1,
                0,
                "action.checkpointed",
                index=0,
                actions=[
                    {
                        "type": "spreadsheet_grid",
                        "rows": [["Quarter", "Revenue"], ["Q1", "120"]],
                    }
                ],
            ),
            _event(2, 100, "action.attempted", index=0),
            _event(3, 200, "action.completed", index=0),
        ],
    )

    report = summarize_run_performance(run)

    assert report.progress_actions_completed == 1
    assert report.observation_only_actions_completed == 0


def test_cli_exposes_saved_run_speed_report() -> None:
    result = CliRunner().invoke(app, ["harness", "run-metrics", "--help"])

    assert result.exit_code == 0
    assert "--state" in result.stdout
    assert "--run-id" in result.stdout
