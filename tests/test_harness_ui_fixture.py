from __future__ import annotations

from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.ui_fixture import (
    FixtureLiveFrames,
    FixtureModels,
    advance_fixture_run,
    build_fixture_app,
    build_fixture_run,
)


def test_ui_fixture_is_a_large_visible_run_without_a_machine_target() -> None:
    run = build_fixture_run(1_200)

    assert run.event_cursor == 1_200
    assert len(run.events) == 1_200
    assert run.active_activity is not None
    assert run.active_activity.kind == "model"
    assert run.observation is not None
    assert run.observation.machine["alias"] == "Synthetic audit target"
    assert run.observation.machine["desktop_layer"] == "No-machine browser fixture"
    assert "VNC" in run.plan.constraints[0]
    assert run.model_budget.provider_attempts == 37
    assert run.model_budget.provider_attempt_limit == 500
    assert run.model_budget.max_cost_microusd == 2_000_000


def test_ui_fixture_alternates_visible_model_and_exact_tool_activity() -> None:
    run = build_fixture_run(64)

    advance_fixture_run(run, 1)
    assert run.active_activity is not None
    assert run.active_activity.kind == "tool"
    assert run.active_activity.arguments["actions"][0]["type"] == "click"

    advance_fixture_run(run, 2)
    assert run.active_activity is not None
    assert run.active_activity.kind == "model"
    assert run.event_cursor > 64


async def test_ui_fixture_frame_is_explicitly_synthetic_and_changes() -> None:
    frames = FixtureLiveFrames()

    first = await frames.get("synthetic-session")
    second = await frames.get("synthetic-session")

    assert first.media_type == "image/svg+xml"
    assert b"No VNC or PiKVM target is connected" in first.data
    assert first.data != second.data
    assert (first.width, first.height) == (1280, 720)


def test_ui_fixture_app_exposes_no_machine_marker_and_provider_matrix() -> None:
    app = build_fixture_app(
        access_token="fixture-operator-token-0123456789abcdef",
        origin="http://127.0.0.1:47619",
        prefill_events=64,
        event_interval_ms=250,
    )
    providers = FixtureModels().health()

    assert app.state.synthetic_fixture is True
    assert set(providers) == {"claude-account", "fast-controller"}
    assert providers["claude-account"]["credential"] == "CLI-owned OAuth"
    assert providers["claude-account"]["auth_mode"] == "saved_cli_login"
    assert providers["claude-account"]["support_tier"] == "stable"
    assert providers["claude-account"]["credential_owner"] == "provider_cli"
    assert providers["claude-account"]["credential_source"] == "claude"
    assert providers["claude-account"]["configured_model"] == "opus"
    assert providers["claude-account"]["conformance_status"] == "passed"
    assert providers["fast-controller"]["kind"] == "openai_responses"
    assert (
        providers["fast-controller"]["implementation_contract"]
        == "first_party"
    )
    assert providers["fast-controller"]["auth_mode"] == "api_key_env"
    assert providers["fast-controller"]["conformance_exact"] == 4


def test_ui_fixture_cli_refuses_remote_and_production_daemon_binds() -> None:
    runner = CliRunner()

    remote = runner.invoke(
        app,
        ["harness", "ui-fixture", "--listen", "0.0.0.0:47619"],
    )
    production = runner.invoke(
        app,
        ["harness", "ui-fixture", "--listen", "127.0.0.1:47615"],
    )

    assert remote.exit_code == 2
    assert "loopback" in remote.stderr
    assert production.exit_code == 2
    assert "production daemon port 47615 is reserved" in production.stderr
