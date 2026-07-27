from __future__ import annotations

from pathlib import Path

from PIL import Image
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.ui_fixture import (
    FixtureHarness,
    FixtureLiveFrames,
    FixtureModels,
    advance_fixture_run,
    build_approval_fixture_run,
    build_direct_fixture_run,
    build_fixture_app,
    build_fixture_run,
)
from pikvm_agent.harness.agent_store import InMemoryRunStore


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


def test_ui_fixture_alternates_visible_model_and_exact_tool_activity(
    tmp_path: Path,
) -> None:
    run = build_fixture_run(64)
    evidence_path = tmp_path / "before-after.png"
    verifier = next(
        event
        for event in reversed(run.events)
        if event.kind == "model.completed"
        and event.data.get("role") == "verifier"
    )
    assert verifier.data["model"] == "opus"

    advance_fixture_run(run, 1, evidence_path)
    assert run.active_activity is not None
    assert run.active_activity.kind == "tool"
    assert run.active_activity.arguments["actions"][0]["type"] == "click"

    advance_fixture_run(run, 2, evidence_path)
    assert run.active_activity is not None
    assert run.active_activity.kind == "model"
    assert run.event_cursor > 64


def test_ui_fixture_includes_production_shaped_typing_readback() -> None:
    run = build_fixture_run(96)

    completion = next(
        event
        for event in run.events
        if event.kind == "action.completed"
        and event.data.get("input_receipts")
    )
    receipt = completion.data["input_receipts"][0]

    assert receipt["type"] == "type_text"
    assert receipt["observed_text"] == "Quarterly review draft"
    assert receipt["focus_evidence"] == "read_back_verified"
    assert receipt["edit_distance"] == 0


def test_ui_fixture_includes_an_honestly_labelled_direct_client_trace(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "direct-before.png"
    Image.new("RGB", (1280, 720), "#10141d").save(evidence_path)
    run = build_direct_fixture_run(evidence_path)

    assert run.origin == "direct_mcp"
    assert run.caller == {
        "interface": "direct_mcp",
        "label": "claude-cli",
        "provider": "anthropic-oauth",
        "model": "opus",
    }
    assert run.status.value == "paused"
    assert run.plan is None
    assert run.last_verification is None
    assert run.verification_images[0].kind == "pre_action"
    assert run.verification_images[0].path == str(evidence_path)
    assert [event.kind for event in run.events] == [
        "run.created",
        "action.pre_action_evidence_captured",
        "action.attempted",
        "action.completed_unverified",
        "run.paused",
    ]
    attempted = run.events[2]
    completed = run.events[3]
    assert attempted.data["caller"] == run.caller
    assert completed.data["caller"] == run.caller
    assert completed.data["effect_state"] == "unverified"


async def test_ui_fixture_exposes_and_resolves_a_synthetic_send_approval() -> None:
    store = InMemoryRunStore()
    harness = FixtureHarness(store)
    run = build_approval_fixture_run()
    await store.save(run)

    assert run.status.value == "needs_approval"
    assert run.pending_approval is not None
    assert run.pending_approval["risk"] == "external_side_effect"
    attempted = next(
        event for event in reversed(run.events)
        if event.kind == "action.attempted"
    )
    assert attempted.data["arguments"]["actions"][1]["keys"] == ["ENTER"]

    completed = await harness.resolve_approval(
        run.run_id,
        "fixture-send-approval",
        {"type": "approve", "reason": "browser fixture test"},
    )

    assert completed.status.value == "completed"
    assert completed.pending_approval is None
    assert completed.events[-1].kind == "run.completed"


async def test_ui_fixture_frame_is_explicitly_synthetic_and_changes() -> None:
    frames = FixtureLiveFrames()

    first = await frames.get("synthetic-session")
    second = await frames.get("synthetic-session")

    assert first.media_type == "image/svg+xml"
    assert b"No VNC or PiKVM target is connected" in first.data
    assert first.data != second.data
    assert (first.width, first.height) == (1280, 720)


async def test_ui_fixture_accepts_a_chat_task_and_selected_model() -> None:
    store = InMemoryRunStore()
    harness = FixtureHarness(store)

    created = await harness.create(
        "Draft a quarterly earnings spreadsheet",
        caller={"interface": "managed_mcp", "label": "browser"},
        model_provider="claude-account",
    )
    completed = await harness.continue_run(created.run_id)

    assert created.task == "Draft a quarterly earnings spreadsheet"
    assert created.model_provider == "claude-account"
    assert created.caller["label"] == "browser"
    selected = next(
        event
        for event in reversed(created.events)
        if event.kind == "model.provider_started"
    )
    assert selected.data["provider"] == "claude-account"
    assert completed.status.value == "completed"
    assert completed.events[-1].kind == "run.completed"


def test_ui_fixture_app_exposes_no_machine_marker_and_provider_matrix() -> None:
    app = build_fixture_app(
        access_token="fixture-workspace-token-0123456789abcdef",
        origin="http://127.0.0.1:47619",
        prefill_events=64,
        event_interval_ms=250,
    )
    providers = FixtureModels().health()

    assert app.state.synthetic_fixture is True
    assert app.state.synthetic_approval_run.status.value == "needs_approval"
    assert app.state.synthetic_direct_run.origin == "direct_mcp"
    assert app.state.synthetic_run.verification_images[0].revision == 1
    assert any(
        event.kind == "verification.evidence_captured"
        for event in app.state.synthetic_run.events
    )
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
