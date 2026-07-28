from __future__ import annotations

import sys

import httpx
import pytest

from pikvm_agent.harness.config import HarnessSettings
from pikvm_agent.harness.server import build_harness_app


TEST_ACCESS_TOKEN = "test-harness-token-0123456789abcdef"
TEST_AGENT_TOKEN = "test-agent-token-000123456789abcdef"
TEST_OBSERVER_TOKEN = "test-observer-token-0123456789abc"
TEST_DAEMON_ACTION_TOKEN = "test-daemon-action-token-0123456789abcdef"
TEST_DAEMON_HARNESS_TOKEN = "test-daemon-harness-token-0123456789abcde"


def target_free_settings(tmp_path) -> HarnessSettings:
    return HarnessSettings(
        daemon_url_env="TEST_OPTIONAL_DAEMON",
        access_token_env="TEST_OPTIONAL_ACCESS_TOKEN",
        agent_token_env="TEST_OPTIONAL_AGENT_TOKEN",
        observer_token_env="TEST_OPTIONAL_OBSERVER_TOKEN",
        state_path=tmp_path / "state.sqlite3",
        artifact_dir=tmp_path / "artifacts",
        provider_conformance_path=tmp_path / "provider-conformance.json",
        providers={
            "local-fixture": {
                "kind": "subprocess_json",
                "model": "fixture-model",
                "argv": [sys.executable, "-c", "raise SystemExit(0)"],
            }
        },
        routes={
            "reasoner": ["local-fixture"],
            "controller": ["local-fixture"],
            "verifier": ["local-fixture"],
        },
    )


@pytest.mark.asyncio
async def test_chat_server_starts_without_a_selected_computer(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_OPTIONAL_ACCESS_TOKEN", TEST_ACCESS_TOKEN)
    monkeypatch.delenv("TEST_OPTIONAL_DAEMON", raising=False)
    monkeypatch.delenv("TEST_OPTIONAL_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("TEST_OPTIONAL_OBSERVER_TOKEN", raising=False)

    app = build_harness_app(target_free_settings(tmp_path))

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            health = await client.get("/api/health")
            providers = await client.get(
                "/api/providers",
                headers={"Authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
            )

    assert health.status_code == 200
    assert health.json()["computer_control"] == "disabled"
    assert health.json()["direct_call_visibility"] == "disabled"
    assert providers.status_code == 200
    assert providers.json()["local-fixture"]["ready"] is True


@pytest.mark.asyncio
async def test_target_free_server_fails_computer_handoff_without_contact(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_OPTIONAL_ACCESS_TOKEN", TEST_ACCESS_TOKEN)
    monkeypatch.delenv("TEST_OPTIONAL_DAEMON", raising=False)

    app = build_harness_app(target_free_settings(tmp_path))
    run = await app.state.harness.create(
        "Open the calculator on the connected computer"
    )

    assert run.status.value == "failed"
    assert run.session_id is None
    assert run.error == (
        "computer open failed: computer control is not configured; "
        "select a PiKVM agent daemon and restart the harness"
    )
    assert run.events[-1].kind == "computer.open_failed"


def test_selected_computer_keeps_managed_and_direct_control_enabled(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_OPTIONAL_ACCESS_TOKEN", TEST_ACCESS_TOKEN)
    monkeypatch.setenv("TEST_OPTIONAL_AGENT_TOKEN", TEST_AGENT_TOKEN)
    monkeypatch.setenv("TEST_OPTIONAL_OBSERVER_TOKEN", TEST_OBSERVER_TOKEN)
    monkeypatch.setenv(
        "TEST_OPTIONAL_DAEMON",
        "http://127.0.0.1:48123",
    )
    monkeypatch.setenv(
        "PIKVM_AGENT_DAEMON_TOKEN",
        TEST_DAEMON_ACTION_TOKEN,
    )
    monkeypatch.setenv(
        "PIKVM_AGENT_HARNESS_TOKEN",
        TEST_DAEMON_HARNESS_TOKEN,
    )

    app = build_harness_app(target_free_settings(tmp_path))

    assert app.state.computer_control_enabled is True
    assert app.state.direct_calls is not None
