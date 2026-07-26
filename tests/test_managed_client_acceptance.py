from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pikvm_agent.cli import app
import pikvm_agent.harness.client_acceptance as client_acceptance
from pikvm_agent.harness.client_acceptance import (
    ManagedClientAcceptanceCase,
    build_managed_client_acceptance_report,
    write_managed_client_acceptance_report,
)


def acceptance_case(
    *,
    client: str,
    passed: bool,
) -> ManagedClientAcceptanceCase:
    return ManagedClientAcceptanceCase(
        client=client,  # type: ignore[arg-type]
        passed=passed,
        scoped_environment_exact=passed,
        tool_inventory_exact=passed,
        task_completed=passed,
        operator_run_visible=passed,
        durable_run_recovered=passed,
        outage_error_safe=passed,
        mcp_process_survived_outage=passed,
        startup_latency_ms=120,
        task_latency_ms=340,
        recovery_latency_ms=80,
        error_class=None if passed else "stdio-worker-unavailable",
    )


def test_report_keeps_every_client_and_failure_in_denominator() -> None:
    report = build_managed_client_acceptance_report(
        cases=[
            acceptance_case(client="codex", passed=True),
            acceptance_case(client="claude", passed=False),
        ],
        evaluation_wall_ms=900,
    )

    assert report.clients_requested == 2
    assert report.clients_passed == 1
    assert report.clients_failed == 1
    assert report.success_rate == 0.5
    assert report.computer_target_contacted is False
    assert report.computer_execution == "deterministic-synthetic"
    assert report.provider_execution == "deterministic-synthetic"
    assert report.external_provider_calls == 0
    assert report.cases[1].error_class == "stdio-worker-unavailable"


def test_passing_case_requires_every_visibility_and_recovery_gate() -> None:
    with pytest.raises(ValueError, match="all managed-client gates"):
        acceptance_case(client="codex", passed=True).model_copy(
            update={"operator_run_visible": False}
        ).model_validate(
            {
                **acceptance_case(
                    client="codex",
                    passed=True,
                ).model_dump(),
                "operator_run_visible": False,
            }
        )


def test_report_writer_is_private_and_never_overwrites(tmp_path: Path) -> None:
    output = tmp_path / "managed-client-acceptance.json"
    report = build_managed_client_acceptance_report(
        cases=[acceptance_case(client="opencode", passed=False)],
        evaluation_wall_ms=250,
    )

    write_managed_client_acceptance_report(output, report)

    body = json.loads(output.read_text())
    assert body["suite"] == "managed-client-acceptance"
    assert body["clients_failed"] == 1
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(ValueError, match="already exists"):
        write_managed_client_acceptance_report(output, report)


def test_cli_exposes_target_free_managed_client_acceptance() -> None:
    result = CliRunner().invoke(
        app,
        ["harness", "client-acceptance", "--help"],
    )

    assert result.exit_code == 0
    assert "--client" in result.stdout
    assert "--out" in result.stdout
    assert "synthetic" in result.stdout.casefold()


def test_synthetic_server_is_explicitly_target_free(tmp_path: Path) -> None:
    server = client_acceptance.build_managed_client_acceptance_app(
        state_path=tmp_path / "state.sqlite3",
        port=48124,
        operator_token="o" * 32,
        agent_token="a" * 32,
    )
    settings = client_acceptance._acceptance_settings(
        root=tmp_path,
        port=48124,
    )

    assert server.state.synthetic_managed_client_acceptance is True
    assert settings.listen == "127.0.0.1:48124"
    assert settings.daemon_url_env == (
        "PIKVM_ACCEPTANCE_ABSENT_DAEMON_URL"
    )
    assert "vnc" not in json.dumps(
        settings.model_dump(mode="json")
    ).casefold()


def test_synthetic_server_environment_strips_targets_and_provider_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIKVM_AGENT_DAEMON", "https://selected-machine.invalid")
    monkeypatch.setenv("PIKVM_LAB_VNC", "selected-vnc.invalid:5900")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-self-test")
    monkeypatch.setenv("PATH", "/safe/bin")

    environment = client_acceptance._acceptance_server_environment(
        operator_token="o" * 32,
        agent_token="a" * 32,
    )

    assert environment["PIKVM_ACCEPTANCE_OPERATOR_TOKEN"] == "o" * 32
    assert environment["PIKVM_ACCEPTANCE_AGENT_TOKEN"] == "a" * 32
    assert environment["PATH"] == "/safe/bin"
    assert "PIKVM_AGENT_DAEMON" not in environment
    assert "PIKVM_LAB_VNC" not in environment
    assert "OPENAI_API_KEY" not in environment


@pytest.mark.asyncio
async def test_runner_rejects_invalid_or_duplicate_clients_before_launch() -> None:
    with pytest.raises(ValueError, match="unsupported managed clients"):
        await client_acceptance.run_managed_client_acceptance(
            clients=["unknown"],
        )
    with pytest.raises(ValueError, match="must be unique"):
        await client_acceptance.run_managed_client_acceptance(
            clients=["codex", "codex"],
        )


@pytest.mark.asyncio
async def test_runner_reports_infrastructure_failure_for_every_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> int:
        raise PermissionError("restricted runner detail must not be retained")

    monkeypatch.setattr(
        client_acceptance,
        "_allocate_loopback_port",
        unavailable,
    )

    report = await client_acceptance.run_managed_client_acceptance(
        clients=["codex", "claude"],
    )

    assert report.clients_requested == 2
    assert report.clients_passed == 0
    assert report.clients_failed == 2
    assert {case.error_class for case in report.cases} == {
        "permissionerror"
    }
    assert "restricted runner detail" not in report.model_dump_json()


def test_cli_writes_failure_inclusive_acceptance_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report = build_managed_client_acceptance_report(
        cases=[acceptance_case(client="codex", passed=False)],
        evaluation_wall_ms=500,
    )

    async def fake_run(**_kwargs: object):
        return report

    monkeypatch.setattr(
        client_acceptance,
        "run_managed_client_acceptance",
        fake_run,
    )
    output = tmp_path / "client-acceptance.json"
    result = CliRunner().invoke(
        app,
        [
            "harness",
            "client-acceptance",
            "--client",
            "codex",
            "--out",
            str(output),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "0/1 clients passed" in result.stdout
    assert json.loads(output.read_text())["clients_failed"] == 1

    repeated = CliRunner().invoke(
        app,
        [
            "harness",
            "client-acceptance",
            "--client",
            "codex",
            "--out",
            str(output),
        ],
    )
    assert repeated.exit_code == 2
    assert "--out already exists" in repeated.stderr
