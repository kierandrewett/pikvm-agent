from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.config import HarnessSettings
from pikvm_agent.harness.support_bundle import (
    build_support_bundle,
    write_support_bundle,
)


def _settings(tmp_path: Path) -> HarnessSettings:
    return HarnessSettings(
        state_path=tmp_path / "private-state" / "state.sqlite3",
        artifact_dir=tmp_path / "private-artifacts",
        providers={
            "internal-claude-account": {
                "kind": "claude_cli",
                "model": "private-model-alias",
                "executable": "missing-private-claude",
            },
            "internal-gateway": {
                "kind": "openai_compatible",
                "model": "private-deployment-name",
                "base_url": "https://private-gateway.invalid/v1",
                "api_key_env": "PRIVATE_GATEWAY_KEY",
            },
        },
        routes={
            "reasoner": ["internal-claude-account"],
            "controller": ["internal-gateway", "internal-claude-account"],
            "verifier": ["internal-gateway"],
        },
    )


def test_support_bundle_is_offline_and_excludes_secrets_endpoints_and_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    settings.artifact_dir.mkdir()
    (settings.artifact_dir / "private-task-name.png").write_bytes(b"image")
    settings.state_path.parent.mkdir()
    settings.state_path.write_bytes(b"sqlite")
    secrets = {
        "PIKVM_HARNESS_TOKEN": "operator-secret-" + "a" * 32,
        "PIKVM_HARNESS_AGENT_TOKEN": "agent-secret-" + "b" * 32,
        "PIKVM_HARNESS_OBSERVER_TOKEN": "observer-secret-" + "c" * 32,
        "PIKVM_AGENT_DAEMON": "http://private-machine.invalid:47892",
        "PRIVATE_GATEWAY_KEY": "gateway-secret-" + "d" * 32,
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    config_bytes = (
        b"private config https://private-gateway.invalid/v1 "
        b"private-model-alias"
    )

    bundle = build_support_bundle(
        settings,
        config_bytes=config_bytes,
        generated_at=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
    )
    rendered = json.dumps(bundle, sort_keys=True)

    for value in secrets.values():
        assert value not in rendered
    for private_value in (
        "private-machine.invalid",
        "private-gateway.invalid",
        "private-model-alias",
        "private-deployment-name",
        "internal-claude-account",
        "internal-gateway",
        "private-task-name",
        str(tmp_path),
    ):
        assert private_value not in rendered
    assert bundle["payload"]["privacy"]["network_requests"] == 0
    assert bundle["payload"]["target"] == {
        "selected": True,
        "valid_url": True,
        "endpoint_included": False,
    }
    assert bundle["payload"]["routes"]["controller"] == [
        "provider-2",
        "provider-1",
    ]
    assert bundle["payload"]["storage"]["artifacts"]["files"] == 1
    assert bundle["payload"]["storage"]["state"]["bytes"] == 6
    assert bundle["payload"]["configuration"]["model_budget"] == {
        "provider_attempt_limit": 500,
        "cost_cap_enabled": False,
        "pricing_version_included": False,
        "price_values_included": False,
    }
    assert bundle["payload"]["configuration"]["autonomous_resume_limit"] == 64
    assert {
        provider["billing_mode"]
        for provider in bundle["payload"]["providers"]
    } == {"unclassified"}
    assert len(bundle["payload_sha256"]) == 64


def test_support_bundle_reports_missing_credentials_without_failing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    for name in (
        "PIKVM_HARNESS_TOKEN",
        "PIKVM_HARNESS_AGENT_TOKEN",
        "PIKVM_HARNESS_OBSERVER_TOKEN",
        "PIKVM_AGENT_DAEMON",
        "PRIVATE_GATEWAY_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    bundle = build_support_bundle(settings, config_bytes=b"config")

    assert bundle["payload"]["credentials"]["present"] == {
        "operator": False,
        "agent": False,
        "observer": False,
    }
    assert bundle["payload"]["credentials"]["all_distinct"] is False
    assert bundle["payload"]["target"]["selected"] is False
    assert [provider["ready"] for provider in bundle["payload"]["providers"]] == [
        False,
        False,
    ]


def test_support_bundle_reports_budget_shape_without_prices_or_version(
    tmp_path: Path,
) -> None:
    settings = HarnessSettings(
        state_path=tmp_path / "state.sqlite3",
        artifact_dir=tmp_path / "artifacts",
        providers={
            "oauth-private": {
                "kind": "codex_cli",
                "model": "private-oauth-model",
                "billing": {"mode": "subscription"},
            },
            "metered-private": {
                "kind": "openai_responses",
                "model": "private-metered-model",
                "api_key_env": "PRIVATE_API_KEY",
                "billing": {
                    "mode": "metered",
                    "reservation_usd": "0.123456",
                    "usage_usd_per_million": {
                        "input_tokens": "7.654321",
                    },
                },
            },
        },
        routes={
            "reasoner": ["oauth-private"],
            "controller": ["metered-private"],
            "verifier": ["metered-private"],
        },
        model_budget={
            "max_provider_attempts_per_run": 73,
            "max_cost_usd_per_run": "9.876543",
            "pricing_version": "private-price-table-2026-07-26",
        },
    )

    rendered = json.dumps(
        build_support_bundle(settings, config_bytes=b"private config"),
        sort_keys=True,
    )

    for private_value in (
        "private-price-table-2026-07-26",
        "0.123456",
        "7.654321",
        "9.876543",
        "private-oauth-model",
        "private-metered-model",
        "oauth-private",
        "metered-private",
    ):
        assert private_value not in rendered
    bundle = json.loads(rendered)
    assert bundle["payload"]["configuration"]["model_budget"] == {
        "provider_attempt_limit": 73,
        "cost_cap_enabled": True,
        "pricing_version_included": False,
        "price_values_included": False,
    }
    assert bundle["payload"]["configuration"]["autonomous_resume_limit"] == 64
    assert {
        provider["billing_mode"]
        for provider in bundle["payload"]["providers"]
    } == {"subscription", "metered"}


def test_support_bundle_writer_uses_private_mode_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "support.json"
    bundle = {"schema_version": 1, "payload_sha256": "a" * 64, "payload": {}}

    write_support_bundle(destination, bundle)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    try:
        write_support_bundle(destination, bundle)
    except ValueError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("support bundle must not overwrite existing output")


def test_support_bundle_cli_writes_redacted_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = tmp_path / "harness.yaml"
    destination = tmp_path / "support.json"
    config.write_text(
        """
providers:
  local:
    kind: subprocess_json
    model: private-local-model
    argv: ["missing-private-bridge"]
routes:
  reasoner: ["local"]
  controller: ["local"]
  verifier: ["local"]
"""
    )
    monkeypatch.setenv("PIKVM_AGENT_DAEMON", "http://private-target.invalid:47892")

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "support-bundle",
            "--config",
            str(config),
            "--out",
            str(destination),
        ],
    )

    assert result.exit_code == 0
    assert destination.is_file()
    rendered = destination.read_text()
    assert "private-target.invalid" not in rendered
    assert "private-local-model" not in rendered
    assert str(config) not in rendered
    assert json.loads(rendered)["payload"]["privacy"]["offline_only"] is True
