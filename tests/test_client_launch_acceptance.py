"""Clean-environment acceptance tests for generated MCP client launchers."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from typer.testing import CliRunner

from pikvm_agent.cli import app
import pikvm_agent.harness.client_setup as client_setup
import pikvm_agent.harness_mcp_server as harness_mcp_server
from pikvm_agent.harness.client_setup import (
    parse_client_launch_config,
    render_client_config,
)
from pikvm_agent.harness.client_acceptance import (
    run_managed_client_acceptance,
)
from pikvm_agent.harness.config import HarnessSettings
from pikvm_agent.harness.mcp_driver import unpack_tool_result


AGENT_TOKEN = "clean-launch-agent-token-0123456789abcdef"
OPERATOR_TOKEN = "clean-launch-operator-token-0123456789abcd"
DAEMON_ACTION_TOKEN = "daemon-action-token-0123456789abcdef"
DAEMON_HARNESS_TOKEN = "daemon-harness-token-0123456789abcdef"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _free_loopback_port() -> int:
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])
    except PermissionError:
        pytest.skip("runner forbids loopback sockets")


def _settings(port: int) -> HarnessSettings:
    return HarnessSettings.model_validate(
        {
            "listen": f"127.0.0.1:{port}",
            "daemon_url_env": "ABSENT_DAEMON_URL",
            "access_token_env": "ABSENT_OPERATOR_TOKEN",
            "agent_token_env": "CLEAN_AGENT_TOKEN",
            "observer_token_env": "ABSENT_OBSERVER_TOKEN",
            "providers": {
                "fake": {
                    "kind": "subprocess_json",
                    "model": "test",
                    "argv": ["unused-provider"],
                }
            },
            "routes": {
                "reasoner": ["fake"],
                "controller": ["fake"],
                "verifier": ["fake"],
            },
        }
    )


def _write_settings(path: Path, port: int) -> None:
    path.write_text(
        "\n".join(
            [
                f'listen: "127.0.0.1:{port}"',
                'daemon_url_env: "ABSENT_DAEMON_URL"',
                'access_token_env: "ABSENT_OPERATOR_TOKEN"',
                'agent_token_env: "CLEAN_AGENT_TOKEN"',
                'observer_token_env: "ABSENT_OBSERVER_TOKEN"',
                "providers:",
                "  fake:",
                '    kind: "subprocess_json"',
                '    model: "test"',
                '    argv: ["unused-provider"]',
                "routes:",
                '  reasoner: ["fake"]',
                '  controller: ["fake"]',
                '  verifier: ["fake"]',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _start_managed_harness_server(
    port: int,
    state_path: Path,
) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment.update(
        {
            "PIKVM_ACCEPTANCE_AGENT_TOKEN": AGENT_TOKEN,
            "PIKVM_ACCEPTANCE_OPERATOR_TOKEN": OPERATOR_TOKEN,
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONUNBUFFERED": "1",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pikvm_agent.harness.client_acceptance",
            "--serve",
            "--port",
            str(port),
            "--state",
            str(state_path),
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = (process.stderr.read() if process.stderr else b"").decode()
            raise RuntimeError(f"managed harness exited: {stderr}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return process
        except OSError:
            time.sleep(0.02)
    process.terminate()
    raise RuntimeError("managed harness did not start")


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _launch_spec(client: str, rendered: str) -> tuple[str, list[str]]:
    launch = parse_client_launch_config(
        rendered,
        client=client,  # type: ignore[arg-type]
    )
    return launch.command, list(launch.args)


@pytest.mark.asyncio
async def test_high_level_mcp_module_speaks_stdio_without_harness_connection(
    tmp_path: Path,
) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pikvm_agent.harness_mcp_server"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(REPO_ROOT),
            "PIKVM_HARNESS_URL": "http://127.0.0.1:1",
            "PIKVM_HARNESS_AGENT_TOKEN": AGENT_TOKEN,
            "PIKVM_MCP_CALLER_LABEL": "acceptance-client",
        },
        cwd=tmp_path,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await asyncio.wait_for(session.initialize(), timeout=3)
            tools = await asyncio.wait_for(session.list_tools(), timeout=3)

    assert len(tools.tools) == 5


@pytest.mark.asyncio
async def test_high_level_mcp_stdio_survives_a_harness_outage(
    tmp_path: Path,
) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pikvm_agent.harness_mcp_server"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(REPO_ROOT),
            "PIKVM_HARNESS_URL": "http://127.0.0.1:1",
            "PIKVM_HARNESS_AGENT_TOKEN": AGENT_TOKEN,
            "PIKVM_MCP_CALLER_LABEL": "acceptance-client",
        },
        cwd=tmp_path,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await asyncio.wait_for(session.initialize(), timeout=3)
            unavailable = unpack_tool_result(
                await asyncio.wait_for(
                    session.call_tool(
                        "computer_status",
                        arguments={"run_id": "offline-run"},
                    ),
                    timeout=3,
                )
            )
            tools = await asyncio.wait_for(session.list_tools(), timeout=3)

    rendered = json.dumps(unavailable)
    assert unavailable["is_error"] is True
    assert "managed harness unavailable" in rendered
    assert AGENT_TOKEN not in rendered
    assert "127.0.0.1" not in rendered
    assert len(tools.tools) == 5


@pytest.mark.asyncio
async def test_private_raw_mcp_child_initializes_without_a_stdio_worker(
    tmp_path: Path,
) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pikvm_agent.mcp_server"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(REPO_ROOT),
            "PIKVM_AGENT_DAEMON": "http://127.0.0.1:1",
            "PIKVM_AGENT_DAEMON_TOKEN": DAEMON_ACTION_TOKEN,
            "PIKVM_AGENT_HARNESS_TOKEN": DAEMON_HARNESS_TOKEN,
        },
        cwd=tmp_path,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await asyncio.wait_for(session.initialize(), timeout=3)
            tools = await asyncio.wait_for(session.list_tools(), timeout=3)

    names = {tool.name for tool in tools.tools}
    assert {
        "pikvm_open",
        "pikvm_run_burst",
        "pikvm_screenshot",
        "pikvm_resolve_approval",
    } <= names
    assert "debug_pikvm_raw" not in names


def test_legacy_trusted_flag_cannot_create_a_private_raw_mcp_child(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pikvm_agent.mcp_server"],
        input="",
        text=True,
        capture_output=True,
        timeout=3,
        cwd=tmp_path,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(REPO_ROOT),
            "PIKVM_AGENT_DAEMON": "http://127.0.0.1:1",
            "PIKVM_AGENT_TRUSTED_APPROVAL_CLIENT": "1",
        },
        check=False,
    )

    assert result.returncode != 0
    assert "PIKVM_AGENT_DAEMON_TOKEN is required" in result.stderr


def test_shipped_mcp_config_defaults_to_managed_harness_not_raw_hid() -> None:
    repo = Path(__file__).resolve().parents[1]
    body = json.loads((repo / ".mcp.json").read_text())
    server = body["mcpServers"]["pikvm"]

    assert server["args"] == [
        "-m",
        "pikvm_agent.cli",
        "harness",
        "active-managed-mcp",
        "--caller-label",
        "claude-cli",
    ]
    assert str(repo / "config.harness.example.yaml") not in json.dumps(server)
    assert "pikvm_agent.mcp_server" not in json.dumps(server)
    assert "direct-mcp" not in json.dumps(server)
    assert "PIKVM_AGENT_DAEMON" not in json.dumps(server)


def test_top_level_help_does_not_advertise_raw_mcp_compatibility_command() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "\n│ mcp " not in result.stdout


def test_legacy_plan_no_longer_contains_copyable_raw_pikvm_client_config() -> None:
    plan = (REPO_ROOT / "docs" / "PLAN.md").read_text()
    combined = plan.split("# Combined MCP config", 1)[1].split(
        "# Use these upstream patterns",
        1,
    )[0]

    assert "pikvm_agent.mcp_server" not in combined
    assert "managed-mcp" in combined


@pytest.mark.parametrize(
    "client", ["codex", "claude", "gemini", "opencode"]
)
def test_generated_config_invokes_managed_cli_with_only_scoped_agent_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: str,
) -> None:
    port = 48124
    settings_path = tmp_path / "harness.yaml"
    _write_settings(settings_path, port)
    rendered = render_client_config(
        _settings(port),
        client=client,  # type: ignore[arg-type]
        executable=sys.executable,
        harness_config=settings_path,
    )
    _command, args = _launch_spec(client, rendered)
    assert args[:2] == ["-m", "pikvm_agent.cli"]

    for name in (
        "ABSENT_DAEMON_URL",
        "ABSENT_OPERATOR_TOKEN",
        "ABSENT_OBSERVER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLEAN_AGENT_TOKEN", AGENT_TOKEN)
    monkeypatch.setenv("PIKVM_HARNESS_URL", "will-be-replaced")
    monkeypatch.setenv("PIKVM_HARNESS_AGENT_TOKEN", "will-be-replaced")
    monkeypatch.setattr(
        client_setup,
        "verify_managed_harness_ready",
        lambda _settings: (_ for _ in ()).throw(
            AssertionError("default managed launch must not require startup order")
        ),
    )
    launched: list[dict[str, str]] = []

    def fake_mcp_main() -> None:
        launched.append(
            {
                name: os.environ.get(name, "")
                for name in (
                    "PIKVM_HARNESS_URL",
                    "PIKVM_HARNESS_AGENT_TOKEN",
                    "PIKVM_MCP_CALLER_LABEL",
                    "PIKVM_AGENT_DAEMON",
                    "PIKVM_HARNESS_OBSERVER_TOKEN",
                    "PIKVM_HARNESS_TOKEN",
                )
            }
        )

    monkeypatch.setattr(harness_mcp_server, "main", fake_mcp_main)

    result = CliRunner().invoke(app, args[2:])

    assert result.exit_code == 0, result.output
    assert launched == [
        {
            "PIKVM_HARNESS_URL": "http://127.0.0.1:48124",
            "PIKVM_HARNESS_AGENT_TOKEN": AGENT_TOKEN,
            "PIKVM_MCP_CALLER_LABEL": f"{client}-cli",
            "PIKVM_AGENT_DAEMON": "",
            "PIKVM_HARNESS_OBSERVER_TOKEN": "",
            "PIKVM_HARNESS_TOKEN": "",
        }
    ]


def test_managed_cli_can_explicitly_require_live_harness_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "harness.yaml"
    _write_settings(settings_path, 48124)
    monkeypatch.setenv("CLEAN_AGENT_TOKEN", AGENT_TOKEN)
    monkeypatch.setattr(
        client_setup,
        "verify_managed_harness_ready",
        lambda _settings: (_ for _ in ()).throw(
            RuntimeError("harness offline")
        ),
    )
    launched = False

    def fake_mcp_main() -> None:
        nonlocal launched
        launched = True

    monkeypatch.setattr(harness_mcp_server, "main", fake_mcp_main)

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "managed-mcp",
            "--config",
            str(settings_path),
            "--require-ready",
        ],
    )

    assert result.exit_code == 2
    assert "managed MCP startup refused: RuntimeError" in result.output
    assert launched is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client", ["codex", "claude", "gemini", "opencode"]
)
async def test_generated_managed_config_launches_scoped_mcp_from_clean_env(
    tmp_path: Path,
    client: str,
) -> None:
    port = 48124
    settings_path = tmp_path / "harness.yaml"
    _write_settings(settings_path, port)
    rendered = render_client_config(
        _settings(port),
        client=client,  # type: ignore[arg-type]
        executable=sys.executable,
        harness_config=settings_path,
    )
    command, args = _launch_spec(client, rendered)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "CLEAN_AGENT_TOKEN": AGENT_TOKEN,
        "PYTHONUNBUFFERED": "1",
    }
    parameters = StdioServerParameters(
        command=command,
        args=args,
        env=environment,
        cwd=tmp_path,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()

    assert sorted(tool.name for tool in tools.tools) == [
        "computer_abort",
        "computer_continue",
        "computer_pause",
        "computer_start_task",
        "computer_status",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client", ["codex", "claude", "gemini", "opencode"]
)
async def test_runtime_backed_config_launches_without_shell_credential(
    tmp_path: Path,
    client: str,
) -> None:
    port = 48124
    settings_path = tmp_path / "harness.yaml"
    runtime_path = tmp_path / "managed-client-runtime.json"
    _write_settings(settings_path, port)
    runtime_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "harness_config": str(settings_path),
                "agent_token_env": "CLEAN_AGENT_TOKEN",
                "agent_token": AGENT_TOKEN,
            }
        ),
        encoding="utf-8",
    )
    runtime_path.chmod(0o600)
    rendered = render_client_config(
        _settings(port),
        client=client,  # type: ignore[arg-type]
        executable=sys.executable,
        harness_config=settings_path,
        managed_runtime=runtime_path,
    )
    command, args = _launch_spec(client, rendered)
    parameters = StdioServerParameters(
        command=command,
        args=args,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONUNBUFFERED": "1",
        },
        cwd=tmp_path,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()

    assert sorted(tool.name for tool in tools.tools) == [
        "computer_abort",
        "computer_continue",
        "computer_pause",
        "computer_start_task",
        "computer_status",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client", ["codex", "claude", "gemini", "opencode"]
)
async def test_active_runtime_config_launches_without_path_or_shell_credential(
    tmp_path: Path,
    client: str,
) -> None:
    port = 48124
    settings_path = tmp_path / "harness.yaml"
    runtime_path = tmp_path / "managed-client-runtime.json"
    _write_settings(settings_path, port)
    runtime_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "harness_config": str(settings_path),
                "agent_token_env": "CLEAN_AGENT_TOKEN",
                "agent_token": AGENT_TOKEN,
            }
        ),
        encoding="utf-8",
    )
    runtime_path.chmod(0o600)
    rendered = render_client_config(
        _settings(port),
        client=client,  # type: ignore[arg-type]
        executable=sys.executable,
        harness_config=settings_path,
        active_runtime=True,
    )
    assert str(runtime_path) not in rendered
    command, args = _launch_spec(client, rendered)
    parameters = StdioServerParameters(
        command=command,
        args=args,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONUNBUFFERED": "1",
            "PIKVM_MANAGED_CLIENT_RUNTIME": str(runtime_path),
        },
        cwd=tmp_path,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()

    assert sorted(tool.name for tool in tools.tools) == [
        "computer_abort",
        "computer_continue",
        "computer_pause",
        "computer_start_task",
        "computer_status",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client", ["codex", "claude", "gemini", "opencode"]
)
async def test_generated_config_creates_visible_durable_managed_run(
    tmp_path: Path,
    client: str,
) -> None:
    port = _free_loopback_port()
    settings_path = tmp_path / "harness.yaml"
    state_path = tmp_path / f"{client}-managed-runs.sqlite3"
    _write_settings(settings_path, port)
    rendered = render_client_config(
        _settings(port),
        client=client,  # type: ignore[arg-type]
        executable=sys.executable,
        harness_config=settings_path,
    )
    command, args = _launch_spec(client, rendered)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO_ROOT),
        "CLEAN_AGENT_TOKEN": AGENT_TOKEN,
        "PYTHONUNBUFFERED": "1",
    }
    server: subprocess.Popen[bytes] | None = _start_managed_harness_server(
        port,
        state_path,
    )
    run_id = ""
    try:
        parameters = StdioServerParameters(
            command=command,
            args=args,
            env=environment,
            cwd=tmp_path,
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert sorted(tool.name for tool in tools.tools) == [
                    "computer_abort",
                    "computer_continue",
                    "computer_pause",
                    "computer_start_task",
                    "computer_status",
                ]
                started = unpack_tool_result(
                    await session.call_tool(
                        "computer_start_task",
                        arguments={
                            "task": "Type hello world and verify the result"
                        },
                    )
                )
                assert started["is_error"] is False
                assert started["state"] is not None
                run_id = started["state"]["run_id"]
                assert started["state"]["operator_ui"] == (
                    f"http://127.0.0.1:{port}/app/"
                )
                assert AGENT_TOKEN not in json.dumps(started)

                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = unpack_tool_result(
                        await session.call_tool(
                            "computer_status",
                            arguments={"run_id": run_id},
                        )
                    )
                    if status["state"]["status"] == "completed":
                        break
                    await asyncio.sleep(0.02)
                assert status["state"]["status"] == "completed"

                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}",
                    headers={
                        "Authorization": f"Bearer {OPERATOR_TOKEN}"
                    },
                ) as operator:
                    page = await operator.get("/app/")
                    runs = await operator.get("/api/runs")
                    visible = await operator.get(f"/api/runs/{run_id}")
                assert page.status_code == 200
                assert any(
                    run["run_id"] == run_id for run in runs.json()
                )
                assert visible.json()["caller"] == {
                    "interface": "managed_mcp",
                    "label": f"{client}-cli",
                }
                assert {
                    event["kind"] for event in visible.json()["events"]
                }.issuperset(
                    {
                        "run.created",
                        "computer.opened",
                        "model.provider_started",
                        "action.attempted",
                        "action.completed",
                        "run.completed",
                    }
                )

                _stop_process(server)
                server = None
                unavailable = unpack_tool_result(
                    await session.call_tool(
                        "computer_status",
                        arguments={"run_id": run_id},
                    )
                )
                assert unavailable["is_error"] is True

                server = _start_managed_harness_server(port, state_path)
                recovered = unpack_tool_result(
                    await session.call_tool(
                        "computer_status",
                        arguments={"run_id": run_id},
                    )
                )
                assert recovered["is_error"] is False
                assert recovered["state"]["status"] == "completed"
                assert recovered["state"]["event_count"] >= 6
    finally:
        if server is not None:
            _stop_process(server)


@pytest.mark.asyncio
async def test_product_acceptance_runner_measures_generated_codex_path() -> None:
    _free_loopback_port()

    report = await run_managed_client_acceptance(
        clients=["codex"],
        timeout_s=5,
    )

    assert report.clients_requested == 1
    assert report.clients_passed == 1
    assert report.clients_failed == 0
    case = report.cases[0]
    assert case.tool_inventory_exact is True
    assert case.operator_run_visible is True
    assert case.outage_error_safe is True
    assert case.durable_run_recovered is True
    assert case.startup_latency_ms is not None
    assert case.task_latency_ms is not None
    assert case.recovery_latency_ms is not None
