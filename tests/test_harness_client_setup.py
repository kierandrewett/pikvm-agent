from __future__ import annotations

import json
import tomllib
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.daemon_access import DAEMON_TOKEN_ENV
from pikvm_agent.harness.client_setup import (
    direct_mcp_environment,
    managed_mcp_environment,
    parse_client_launch_config,
    render_active_managed_client_config,
    render_client_config,
    verify_direct_harness_ready,
    verify_managed_harness_ready,
)
from pikvm_agent.harness.config import HarnessSettings


def settings(monkeypatch: pytest.MonkeyPatch) -> HarnessSettings:
    monkeypatch.setenv("TEST_DAEMON_URL", "http://127.0.0.1:48123")
    monkeypatch.setenv(
        DAEMON_TOKEN_ENV,
        "runtime-only-daemon-action-token-0123456789abcdef",
    )
    monkeypatch.setenv(
        "TEST_HARNESS_TOKEN",
        "runtime-only-secret-token-0123456789abcdef",
    )
    monkeypatch.setenv(
        "TEST_OBSERVER_TOKEN",
        "runtime-only-observer-token-0123456789abcdef",
    )
    monkeypatch.setenv(
        "TEST_AGENT_TOKEN",
        "runtime-only-agent-token-0123456789abcdef",
    )
    return HarnessSettings.model_validate(
        {
            "listen": "127.0.0.1:48124",
            "daemon_url_env": "TEST_DAEMON_URL",
            "access_token_env": "TEST_HARNESS_TOKEN",
            "agent_token_env": "TEST_AGENT_TOKEN",
            "observer_token_env": "TEST_OBSERVER_TOKEN",
            "providers": {
                "fake": {
                    "kind": "subprocess_json",
                    "model": "test",
                    "argv": ["provider"],
                }
            },
            "routes": {
                "reasoner": ["fake"],
                "controller": ["fake"],
                "verifier": ["fake"],
            },
        }
    )


def test_direct_mcp_runtime_environment_maps_custom_names_without_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings(monkeypatch)
    monkeypatch.delenv("TEST_HARNESS_TOKEN")
    monkeypatch.delenv("TEST_AGENT_TOKEN")

    environment = direct_mcp_environment(
        configured,
        mode="guarded",
        caller_label="codex-cli",
    )

    assert environment == {
        "PIKVM_AGENT_DAEMON": "http://127.0.0.1:48123",
        DAEMON_TOKEN_ENV: (
            "runtime-only-daemon-action-token-0123456789abcdef"
        ),
        "PIKVM_HARNESS_OBSERVER_URL": "http://127.0.0.1:48124",
        "PIKVM_HARNESS_OBSERVER_TOKEN": (
            "runtime-only-observer-token-0123456789abcdef"
        ),
        "PIKVM_HARNESS_OBSERVER_MODE": "guarded",
        "PIKVM_MCP_CALLER_LABEL": "codex-cli",
    }


def test_managed_mcp_runtime_environment_needs_no_machine_or_operator_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings(monkeypatch)
    monkeypatch.delenv("TEST_DAEMON_URL")
    monkeypatch.delenv("TEST_HARNESS_TOKEN")
    monkeypatch.delenv("TEST_OBSERVER_TOKEN")

    environment = managed_mcp_environment(
        configured,
        caller_label="codex-cli",
    )

    assert environment == {
        "PIKVM_HARNESS_URL": "http://127.0.0.1:48124",
        "PIKVM_HARNESS_AGENT_TOKEN": (
            "runtime-only-agent-token-0123456789abcdef"
        ),
        "PIKVM_MCP_CALLER_LABEL": "codex-cli",
    }


@pytest.mark.parametrize(
    "client", ["codex", "claude", "gemini", "opencode"]
)
@pytest.mark.parametrize("control_mode", ["managed", "direct"])
def test_client_configs_forward_only_scoped_env_names_and_never_runtime_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: str,
    control_mode: str,
) -> None:
    configured = settings(monkeypatch)
    harness_config = tmp_path / "harness.yaml"
    rendered = render_client_config(
        configured,
        client=client,  # type: ignore[arg-type]
        control_mode=control_mode,  # type: ignore[arg-type]
        executable="/opt/pikvm/python",
        harness_config=harness_config,
    )

    assert "runtime-only-secret-token" not in rendered
    assert "runtime-only-observer-token" not in rendered
    assert "runtime-only-daemon-action-token" not in rendered
    assert "http://127.0.0.1:48123" not in rendered
    assert "TEST_HARNESS_TOKEN" not in rendered
    if control_mode == "managed":
        assert "managed-mcp" in rendered
        assert "TEST_AGENT_TOKEN" in rendered
        assert "TEST_DAEMON_URL" not in rendered
        assert "TEST_OBSERVER_TOKEN" not in rendered
        expected_forwarded = ("TEST_AGENT_TOKEN",)
    else:
        assert "direct-mcp" in rendered
        assert "TEST_OBSERVER_TOKEN" in rendered
        assert "TEST_AGENT_TOKEN" not in rendered
        assert "TEST_DAEMON_URL" in rendered
        expected_forwarded = (
            "TEST_DAEMON_URL",
            DAEMON_TOKEN_ENV,
            "TEST_OBSERVER_TOKEN",
            "PIKVM_MCP_PROVIDER",
            "PIKVM_MCP_MODEL",
        )
    launch = parse_client_launch_config(
        rendered,
        client=client,  # type: ignore[arg-type]
    )
    assert launch.forwarded_env == expected_forwarded
    if client == "codex":
        parsed = tomllib.loads(rendered)
        server = parsed["mcp_servers"]["pikvm"]
        expected = (
            ["TEST_AGENT_TOKEN"]
            if control_mode == "managed"
            else [
                "TEST_DAEMON_URL",
                DAEMON_TOKEN_ENV,
                "TEST_OBSERVER_TOKEN",
            ]
        )
        assert server["env_vars"][: len(expected)] == expected
    elif client == "opencode":
        parsed = json.loads(rendered)
        server = parsed["mcp"]["pikvm"]
        assert parsed["$schema"] == "https://opencode.ai/config.json"
        assert server["type"] == "local"
        assert server["command"][:3] == [
            "/opt/pikvm/python",
            "-m",
            "pikvm_agent.cli",
        ]
        assert server["enabled"] is True
        token_env = (
            "TEST_AGENT_TOKEN"
            if control_mode == "managed"
            else "TEST_OBSERVER_TOKEN"
        )
        assert server["environment"][token_env] == f"{{env:{token_env}}}"
        if control_mode == "direct":
            assert server["environment"][DAEMON_TOKEN_ENV] == (
                f"{{env:{DAEMON_TOKEN_ENV}}}"
            )
    else:
        parsed = json.loads(rendered)
        server = parsed["mcpServers"]["pikvm"]
        token_env = (
            "TEST_AGENT_TOKEN"
            if control_mode == "managed"
            else "TEST_OBSERVER_TOKEN"
        )
        assert server["env"][token_env] == f"${{{token_env}}}"
        if control_mode == "direct":
            assert server["env"][DAEMON_TOKEN_ENV] == (
                f"${{{DAEMON_TOKEN_ENV}}}"
            )


def test_client_config_defaults_to_managed_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured = settings(monkeypatch)

    rendered = render_client_config(
        configured,
        client="codex",
        executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
    )

    assert "managed-mcp" in rendered
    assert "direct-mcp" not in rendered
    assert "TEST_AGENT_TOKEN" in rendered


@pytest.mark.parametrize(
    "client", ["codex", "claude", "gemini", "opencode"]
)
def test_runtime_backed_managed_config_needs_no_shell_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: str,
) -> None:
    configured = settings(monkeypatch)
    runtime = tmp_path / "managed-client-runtime.json"

    rendered = render_client_config(
        configured,
        client=client,  # type: ignore[arg-type]
        executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        managed_runtime=runtime,
    )

    launch = parse_client_launch_config(
        rendered,
        client=client,  # type: ignore[arg-type]
    )
    assert launch.args == (
        "-m",
        "pikvm_agent.cli",
        "harness",
        "managed-runtime-mcp",
        "--runtime",
        str(runtime),
        "--caller-label",
        f"{client}-cli",
    )
    assert launch.forwarded_env == ()
    assert "TEST_AGENT_TOKEN" not in rendered
    assert "PIKVM_AGENT_DAEMON" not in rendered


@pytest.mark.parametrize(
    "client", ["codex", "claude", "gemini", "opencode"]
)
def test_active_runtime_config_is_path_free_and_needs_no_shell_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: str,
) -> None:
    configured = settings(monkeypatch)

    rendered = render_client_config(
        configured,
        client=client,  # type: ignore[arg-type]
        executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        active_runtime=True,
    )

    launch = parse_client_launch_config(
        rendered,
        client=client,  # type: ignore[arg-type]
    )
    assert launch.args == (
        "-m",
        "pikvm_agent.cli",
        "harness",
        "active-managed-mcp",
        "--caller-label",
        f"{client}-cli",
    )
    assert launch.forwarded_env == ()
    assert "TEST_AGENT_TOKEN" not in rendered
    assert str(tmp_path) not in rendered
    assert "PIKVM_AGENT_DAEMON" not in rendered


@pytest.mark.parametrize(
    "client", ["codex", "claude", "gemini", "opencode"]
)
def test_active_managed_config_needs_no_harness_settings(
    client: str,
) -> None:
    rendered = render_active_managed_client_config(
        client=client,  # type: ignore[arg-type]
        executable="/opt/pikvm/python",
    )

    launch = parse_client_launch_config(
        rendered,
        client=client,  # type: ignore[arg-type]
    )
    assert launch.args == (
        "-m",
        "pikvm_agent.cli",
        "harness",
        "active-managed-mcp",
        "--caller-label",
        f"{client}-cli",
    )
    assert launch.forwarded_env == ()
    assert "harness.yaml" not in rendered
    assert "--config" not in rendered
    assert "TOKEN" not in rendered


def test_runtime_backed_config_refuses_direct_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured = settings(monkeypatch)

    with pytest.raises(
        ValueError,
        match="managed runtime is unavailable in direct mode",
    ):
        render_client_config(
            configured,
            client="claude",
            executable="/opt/pikvm/python",
            harness_config=tmp_path / "harness.yaml",
            control_mode="direct",
            managed_runtime=tmp_path / "managed-client-runtime.json",
        )


def test_active_runtime_config_refuses_explicit_runtime_or_direct_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured = settings(monkeypatch)

    with pytest.raises(ValueError, match="mutually exclusive"):
        render_client_config(
            configured,
            client="claude",
            executable="/opt/pikvm/python",
            harness_config=tmp_path / "harness.yaml",
            managed_runtime=tmp_path / "managed-client-runtime.json",
            active_runtime=True,
        )
    with pytest.raises(ValueError, match="unavailable in direct mode"):
        render_client_config(
            configured,
            client="claude",
            executable="/opt/pikvm/python",
            harness_config=tmp_path / "harness.yaml",
            control_mode="direct",
            active_runtime=True,
        )


@pytest.mark.parametrize(
    ("client", "rendered"),
    [
        (
            "codex",
            (
                "[mcp_servers.pikvm]\n"
                'command = "pikvm-agent"\n'
                'args = ["harness", "managed-runtime-mcp"]\n'
                "env_vars = []\n"
            ),
        ),
        (
            "claude",
            json.dumps(
                {
                    "mcpServers": {
                        "pikvm": {
                            "command": "pikvm-agent",
                            "args": ["harness", "managed-runtime-mcp"],
                            "env": {},
                        }
                    }
                }
            ),
        ),
        (
            "gemini",
            json.dumps(
                {
                    "mcpServers": {
                        "pikvm": {
                            "command": "pikvm-agent",
                            "args": ["harness", "managed-runtime-mcp"],
                            "env": {},
                        }
                    }
                }
            ),
        ),
        (
            "opencode",
            json.dumps(
                {
                    "mcp": {
                        "pikvm": {
                            "command": [
                                "pikvm-agent",
                                "harness",
                                "managed-runtime-mcp",
                            ],
                            "environment": {},
                        }
                    }
                }
            ),
        ),
    ],
)
def test_empty_environment_requires_a_real_runtime_handoff(
    client: str,
    rendered: str,
) -> None:
    with pytest.raises(ValueError, match="no usable environment"):
        parse_client_launch_config(
            rendered,
            client=client,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "arguments",
    [
        ["harness", "active-managed-mcp", "--caller-label"],
        [
            "harness",
            "active-managed-mcp",
            "--caller-label",
            "invalid label",
        ],
        ["harness", "active-managed-mcp", "--unknown"],
    ],
)
def test_active_runtime_parser_refuses_modified_launch_shapes(
    arguments: list[str],
) -> None:
    rendered = json.dumps(
        {
            "mcpServers": {
                "pikvm": {
                    "command": "pikvm-agent",
                    "args": arguments,
                    "env": {},
                }
            }
        }
    )

    with pytest.raises(ValueError, match="no usable environment"):
        parse_client_launch_config(rendered, client="claude")


def test_guarded_launcher_checks_capability_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings(monkeypatch)
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            (
                request.url.path,
                request.headers.get("authorization", ""),
            )
        )
        if request.url.path == "/api/direct/health":
            return httpx.Response(
                200,
                json={"status": "ok", "scope": "direct-call-ingest"},
            )
        return httpx.Response(404, json={})

    verify_direct_harness_ready(
        configured,
        transport=httpx.MockTransport(handler),
    )

    assert requests == [
        (
            "/api/direct/health",
            "Bearer runtime-only-observer-token-0123456789abcdef",
        ),
    ]


def test_managed_launcher_checks_agent_scope_without_operator_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings(monkeypatch)
    monkeypatch.delenv("TEST_HARNESS_TOKEN")
    monkeypatch.delenv("TEST_OBSERVER_TOKEN")
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            (
                request.url.path,
                request.headers.get("authorization", ""),
            )
        )
        return httpx.Response(
            200,
            json={"status": "ok", "scope": "managed-harness-control"},
        )

    verify_managed_harness_ready(
        configured,
        transport=httpx.MockTransport(handler),
    )

    assert requests == [
        (
            "/api/agent/health",
            "Bearer runtime-only-agent-token-0123456789abcdef",
        )
    ]


def test_guarded_launcher_rejects_console_without_direct_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings(monkeypatch)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"detail": "direct-call visibility is not configured"},
        )

    with pytest.raises(RuntimeError, match="without direct-call visibility"):
        verify_direct_harness_ready(
            configured,
            transport=httpx.MockTransport(handler),
        )


def test_guarded_client_commands_are_visible_cli_surfaces() -> None:
    runner = CliRunner()
    direct = runner.invoke(app, ["harness", "direct-mcp", "--help"])
    managed = runner.invoke(app, ["harness", "managed-mcp", "--help"])
    managed_runtime = runner.invoke(
        app,
        ["harness", "managed-runtime-mcp", "--help"],
    )
    active_managed = runner.invoke(
        app,
        ["harness", "active-managed-mcp", "--help"],
    )
    activate_runtime = runner.invoke(
        app,
        ["harness", "activate-managed-runtime", "--help"],
    )
    client_config = runner.invoke(
        app,
        ["harness", "client-config", "--help"],
        terminal_width=180,
    )
    active_client_config = runner.invoke(
        app,
        ["harness", "active-client-config", "--help"],
        terminal_width=180,
    )

    assert direct.exit_code == 0
    assert "--mode" in direct.stdout
    assert managed.exit_code == 0
    assert "--config" in managed.stdout
    assert "--require-ready" in managed.stdout
    assert managed_runtime.exit_code == 0
    assert "--runtime" in managed_runtime.stdout
    assert active_managed.exit_code == 0
    assert "--caller-label" in active_managed.stdout
    assert activate_runtime.exit_code == 0
    assert "--runtime" in activate_runtime.stdout
    assert client_config.exit_code == 0
    assert "--client" in client_config.stdout
    assert "--control-mode" in client_config.stdout
    assert "--managed-runtime" in client_config.stdout
    assert "managed" in client_config.stdout
    assert "codex" in client_config.stdout
    assert "gemini" in client_config.stdout
    assert "opencode" in client_config.stdout
    assert active_client_config.exit_code == 0
    assert "--client" in active_client_config.stdout
    assert "--server-name" in active_client_config.stdout


def test_client_config_cli_defaults_to_path_free_active_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured = settings(monkeypatch)
    harness_config = tmp_path / "harness.yaml"
    harness_config.write_text(
        yaml.safe_dump(configured.model_dump(mode="json")),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "client-config",
            "--config",
            str(harness_config),
            "--client",
            "codex",
        ],
    )

    assert result.exit_code == 0
    assert "active-managed-mcp" in result.stdout
    assert "managed-runtime-mcp" not in result.stdout
    assert str(tmp_path) not in result.stdout
    assert "TEST_AGENT_TOKEN" not in result.stdout


@pytest.mark.parametrize(
    "client", ["codex", "claude", "gemini", "opencode"]
)
def test_active_client_config_cli_needs_no_runtime_or_harness_path(
    client: str,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "harness",
            "active-client-config",
            "--client",
            client,
        ],
    )

    assert result.exit_code == 0
    assert "active-managed-mcp" in result.stdout
    assert "--runtime" not in result.stdout
    assert "--config" not in result.stdout
    assert "TOKEN" not in result.stdout


@pytest.mark.parametrize(
    "client", ["codex", "claude", "gemini", "opencode"]
)
def test_client_launch_parser_refuses_a_missing_server(client: str) -> None:
    with pytest.raises(ValueError, match="no usable launch command"):
        parse_client_launch_config(
            "{}",
            client=client,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("client", "rendered"),
    [
        (
            "codex",
            (
                "[mcp_servers.pikvm]\n"
                'command = "python"\n'
                "args = []\n"
                "env_vars = []\n"
            ),
        ),
        (
            "claude",
            json.dumps(
                {
                    "mcpServers": {
                        "pikvm": {
                            "command": "python",
                            "args": [],
                            "env": {},
                        }
                    }
                }
            ),
        ),
        (
            "gemini",
            json.dumps(
                {
                    "mcpServers": {
                        "pikvm": {
                            "command": "python",
                            "args": [],
                            "env": {},
                        }
                    }
                }
            ),
        ),
        (
            "opencode",
            json.dumps(
                {
                    "mcp": {
                        "pikvm": {
                            "command": ["python"],
                            "environment": {},
                        }
                    }
                }
            ),
        ),
    ],
)
def test_client_launch_parser_refuses_an_empty_environment(
    client: str,
    rendered: str,
) -> None:
    with pytest.raises(ValueError, match="no usable environment"):
        parse_client_launch_config(
            rendered,
            client=client,  # type: ignore[arg-type]
        )
