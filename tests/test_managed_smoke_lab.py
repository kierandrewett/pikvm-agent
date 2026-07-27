from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.agent_store import InMemoryRunStore, SqliteRunStore
from pikvm_agent.harness.smoke_lab import build_managed_smoke_app


OPERATOR_TOKEN = "smoke-operator-token-0123456789abcdef"
AGENT_TOKEN = "smoke-agent-token-0123456789abcdef00"


@pytest.mark.asyncio
async def test_managed_smoke_lab_exercises_the_real_visible_task_interface(
    tmp_path: Path,
) -> None:
    app = build_managed_smoke_app(
        root=tmp_path,
        access_token=OPERATOR_TOKEN,
        agent_token=AGENT_TOKEN,
        allowed_origin="http://smoke.test",
        store=InMemoryRunStore(),
    )
    agent_headers = {"authorization": f"Bearer {AGENT_TOKEN}"}
    operator_headers = {"authorization": f"Bearer {OPERATOR_TOKEN}"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://smoke.test",
    ) as client:
        created = await client.post(
            "/api/runs",
            headers=agent_headers,
            json={
                "task": "Complete the managed smoke canvas.",
                "auto_start": False,
                "source_client": "codex-cli",
            },
        )
        assert created.status_code == 200
        run_id = created.json()["run_id"]

        continued = await client.post(
            f"/api/runs/{run_id}/continue",
            headers=agent_headers,
        )
        frame = await client.get(
            f"/api/runs/{run_id}/frame",
            headers=operator_headers,
        )
        providers = await client.get(
            "/api/providers",
            headers=operator_headers,
        )

    payload = continued.json()
    kinds = [event["kind"] for event in payload["events"]]
    assert payload["status"] == "completed"
    assert payload["origin"] == "managed"
    assert payload["caller"] == {
        "interface": "managed_mcp",
        "label": "codex-cli",
    }
    assert payload["observation"]["machine"] == {
        "alias": "Managed smoke canvas",
        "fingerprint": "target:0000000000000000",
        "desktop_layer": "No-machine managed smoke lab",
    }
    assert "model.started" in kinds
    assert "model.completed" in kinds
    assert "action.checkpointed" in kinds
    assert "action.completed" in kinds
    assert payload["verification_image_available"] is True
    assert frame.status_code == 200
    assert frame.headers["x-pikvm-frame-mode"] == "checkpoint"
    assert frame.headers["content-type"] == "image/png"
    provider = providers.json()["managed-smoke"]
    assert provider["configured_model"] == (
        "deterministic-smoke-v1"
    )
    assert provider["calls"] == 3
    assert provider["successes"] == 3
    assert provider["last_model"] == "deterministic-smoke-v1"
    assert app.state.synthetic_smoke_lab is True


def test_smoke_lab_cli_starts_only_the_loopback_managed_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "smoke.yaml"
    config.write_text(
        "\n".join(
            [
                'listen: "127.0.0.1:47777"',
                'state_path: "state.sqlite3"',
                'artifact_dir: "artifacts"',
                "providers:",
                "  unused:",
                '    kind: "codex_cli"',
                '    model: "account-default"',
                "routes:",
                '  reasoner: ["unused"]',
                '  controller: ["unused"]',
                '  verifier: ["unused"]',
            ]
        )
    )
    monkeypatch.setenv("PIKVM_HARNESS_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("PIKVM_HARNESS_AGENT_TOKEN", AGENT_TOKEN)
    launched: dict[str, object] = {}

    def record_run(
        target: object,
        *,
        host: str,
        port: int,
        **kwargs: object,
    ) -> None:
        launched.update(
            {
                "target": target,
                "host": host,
                "port": port,
                "kwargs": kwargs,
            }
        )

    monkeypatch.setattr("uvicorn.run", record_run)
    result = CliRunner().invoke(
        app,
        [
            "harness",
            "smoke-lab",
            "--config",
            str(config),
            "--root",
            str(tmp_path / "runtime"),
        ],
    )

    assert result.exit_code == 0
    assert "Target-free managed smoke lab" in result.stdout
    assert "http://127.0.0.1:47777/app/" in result.stdout
    assert launched["host"] == "127.0.0.1"
    assert launched["port"] == 47777
    assert getattr(launched["target"], "state").synthetic_smoke_lab is True
    assert isinstance(
        getattr(launched["target"], "state").harness_store,
        SqliteRunStore,
    )


def test_smoke_lab_refuses_live_providers_without_explicit_call_consent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "smoke.yaml"
    config.write_text(
        "\n".join(
            [
                'listen: "127.0.0.1:47777"',
                'state_path: "state.sqlite3"',
                'artifact_dir: "artifacts"',
                "providers:",
                "  account:",
                '    kind: "codex_cli"',
                '    model: "account-default"',
                "routes:",
                '  reasoner: ["account"]',
                '  controller: ["account"]',
                '  verifier: ["account"]',
            ]
        )
    )
    monkeypatch.setenv("PIKVM_HARNESS_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("PIKVM_HARNESS_AGENT_TOKEN", AGENT_TOKEN)

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "smoke-lab",
            "--config",
            str(config),
            "--root",
            str(tmp_path / "runtime"),
            "--live-providers",
        ],
    )

    assert result.exit_code == 2
    assert (
        "Live provider calls require --allow-provider-calls"
        in result.stdout
    )


def test_smoke_lab_uses_configured_routes_when_live_calls_are_authorized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "smoke.yaml"
    config.write_text(
        "\n".join(
            [
                'listen: "127.0.0.1:47777"',
                'state_path: "state.sqlite3"',
                'artifact_dir: "artifacts"',
                "providers:",
                "  account:",
                '    kind: "codex_cli"',
                '    model: "account-default"',
                "routes:",
                '  reasoner: ["account"]',
                '  controller: ["account"]',
                '  verifier: ["account"]',
            ]
        )
    )
    monkeypatch.setenv("PIKVM_HARNESS_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("PIKVM_HARNESS_AGENT_TOKEN", AGENT_TOKEN)
    launched: dict[str, object] = {}

    def record_run(target: object, **kwargs: object) -> None:
        launched.update({"target": target, "kwargs": kwargs})

    monkeypatch.setattr("uvicorn.run", record_run)
    result = CliRunner().invoke(
        app,
        [
            "harness",
            "smoke-lab",
            "--config",
            str(config),
            "--root",
            str(tmp_path / "runtime"),
            "--live-providers",
            "--allow-provider-calls",
        ],
    )

    assert result.exit_code == 0
    assert (
        "synthetic computer with configured live model routes"
        in result.stdout
    )
    with TestClient(launched["target"]) as client:
        response = client.get(
            "/api/providers",
            headers={
                "authorization": f"Bearer {OPERATOR_TOKEN}",
                "origin": "http://127.0.0.1:47777",
            },
        )
    assert response.status_code == 200
    assert set(response.json()) == {"account"}
    assert response.json()["account"]["kind"] == "codex_cli"
