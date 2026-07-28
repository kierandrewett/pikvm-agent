"""The generated lab is isolated and contains no baked-in VNC target."""

from __future__ import annotations

import argparse
import json
import os
import stat

import pytest
import yaml

from pikvm_agent.harness.lab import (
    LabChildStartupError,
    PRODUCTION_DAEMON_PORT,
    LabPorts,
    RunningLab,
    allocate_lab_ports,
    isolated_benchmark_policy,
    write_lab_assets,
)
from pikvm_agent.harness.service import _nonempty_endpoint
from pikvm_agent.policy.direct import classify_direct_burst


def test_lab_assets_point_only_at_isolated_adapter_and_daemon(tmp_path) -> None:
    assets = write_lab_assets(
        tmp_path,
        ports=LabPorts(adapter=48140, daemon=48141, harness=48142),
        executable="/opt/pikvm-agent/bin/pikvm-agent",
    )

    config = yaml.safe_load(assets.config.read_text())
    assert config["pikvm"]["base_url"] == "http://127.0.0.1:48140"
    assert config["daemon"]["listen"] == "127.0.0.1:48141"
    assert config["daemon"]["session_dir"].startswith(str(tmp_path))
    assert config["omniparser"]["enabled"] is False
    assert assets.log_dir == tmp_path / "logs"
    assert assets.log_dir.is_dir()
    assert "vnc" not in assets.config.read_text().lower()

    harness = yaml.safe_load(assets.harness_config.read_text())
    assert harness["listen"] == "127.0.0.1:48142"
    assert harness["daemon_url_env"] == "PIKVM_LAB_DAEMON_URL"
    assert harness["state_path"].startswith(str(tmp_path))
    assert harness["artifact_dir"].startswith(str(tmp_path))
    assert harness["managed_mcp_name"] == "PiKVM lab"
    assert harness["computer_name"] == "Disposable lab computer"
    assert harness["routes"] == {
        "reasoner": [
            "claude-account",
            "claude-fast",
            "codex-account",
        ],
        "controller": [
            "claude-account",
            "claude-fast",
            "codex-account",
        ],
        "verifier": [
            "claude-fast",
            "claude-account",
            "codex-account",
        ],
    }
    assert harness["providers"]["claude-fast"]["kind"] == "claude_cli"
    assert harness["providers"]["claude-fast"]["model"] == "haiku"
    assert harness["model_budget"]["max_provider_attempts_per_run"] == 500
    assert "vnc" not in assets.harness_config.read_text().lower()

    mcp = json.loads(assets.mcp_config.read_text())
    server = mcp["mcpServers"]["pikvm-lab"]
    assert server["command"] == "/opt/pikvm-agent/bin/pikvm-agent"
    assert server["args"] == [
        "-m",
        "pikvm_agent.cli",
        "harness",
        "managed-mcp",
        "--config",
        str(assets.harness_config),
        "--caller-label",
        "claude-cli",
    ]
    assert server["env"] == {
        "PIKVM_HARNESS_AGENT_TOKEN": "${PIKVM_HARNESS_AGENT_TOKEN}"
    }
    assert "PIKVM_AGENT_DAEMON" not in server["env"]
    assert "vnc" not in assets.mcp_config.read_text().lower()

    codex = assets.codex_mcp_config.read_text()
    assert "[mcp_servers.pikvm-lab]" in codex
    assert '"PIKVM_HARNESS_AGENT_TOKEN"' in codex
    assert "managed-mcp" in codex
    assert "PIKVM_AGENT_DAEMON" not in codex

    opencode = json.loads(assets.opencode_mcp_config.read_text())
    opencode_server = opencode["mcp"]["pikvm-lab"]
    assert opencode_server["type"] == "local"
    assert "managed-mcp" in opencode_server["command"]
    assert opencode_server["environment"] == {
        "PIKVM_HARNESS_AGENT_TOKEN": "{env:PIKVM_HARNESS_AGENT_TOKEN}"
    }
    assert "vnc" not in assets.opencode_mcp_config.read_text().lower()


def test_dynamic_lab_ports_are_distinct_and_never_production(monkeypatch) -> None:
    allocated = iter([49120, 49121, 49122])

    class FakeSocket:
        def bind(self, _address) -> None:
            self.port = next(allocated)

        def getsockname(self):
            return ("127.0.0.1", self.port)

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "pikvm_agent.harness.lab.socket.socket",
        lambda *_args, **_kwargs: FakeSocket(),
    )
    ports = allocate_lab_ports()

    assert len({ports.adapter, ports.daemon, ports.harness}) == 3
    assert PRODUCTION_DAEMON_PORT not in (
        ports.adapter,
        ports.daemon,
        ports.harness,
    )


def test_lab_ports_require_a_distinct_visible_harness_port() -> None:
    ports = LabPorts(adapter=48140, daemon=48141, harness=48141)

    try:
        ports.validate()
    except ValueError as exc:
        assert "ports must differ" in str(exc)
    else:
        raise AssertionError("duplicate harness port should be refused")


def test_lab_assets_can_reuse_custom_provider_routes_without_target_leak(
    tmp_path,
) -> None:
    source = tmp_path / "source-harness.yaml"
    source.write_text(
        yaml.safe_dump(
            {
                "listen": "127.0.0.1:59999",
                "daemon_url_env": "SHOULD_BE_REPLACED",
                "access_token_env": "CUSTOM_OPERATOR_TOKEN",
                "agent_token_env": "CUSTOM_AGENT_TOKEN",
                "observer_token_env": "CUSTOM_OBSERVER_TOKEN",
                "providers": {
                    "controller": {
                        "kind": "codex_cli",
                        "model": "account-default",
                    }
                },
                "routes": {
                    "reasoner": ["controller"],
                    "controller": ["controller"],
                    "verifier": ["controller"],
                },
            }
        )
    )

    assets = write_lab_assets(
        tmp_path / "generated",
        ports=LabPorts(adapter=48160, daemon=48161, harness=48162),
        executable="/opt/pikvm-agent/bin/pikvm-agent",
        harness_config=source,
    )

    harness = yaml.safe_load(assets.harness_config.read_text())
    assert harness["listen"] == "127.0.0.1:48162"
    assert harness["daemon_url_env"] == "PIKVM_LAB_DAEMON_URL"
    assert list(harness["providers"]) == ["controller"]
    assert harness["agent_token_env"] == "CUSTOM_AGENT_TOKEN"

    server = json.loads(assets.mcp_config.read_text())["mcpServers"]["pikvm-lab"]
    assert server["env"] == {"CUSTOM_AGENT_TOKEN": "${CUSTOM_AGENT_TOKEN}"}


def test_visible_lab_generates_scoped_tokens_and_private_handoffs(
    tmp_path,
    monkeypatch,
) -> None:
    token_names = (
        "PIKVM_HARNESS_TOKEN",
        "PIKVM_HARNESS_AGENT_TOKEN",
        "PIKVM_HARNESS_OBSERVER_TOKEN",
    )
    for name in token_names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "pikvm_agent.harness.lab._assert_port_available",
        lambda _port: None,
    )
    monkeypatch.setattr(
        "pikvm_agent.harness.lab._wait_ready",
        lambda _url, _child, _timeout: None,
    )
    monkeypatch.setattr(
        "pikvm_agent.harness.lab.ensure_provider_prerequisites",
        lambda _settings: None,
    )
    child_environments: list[dict[str, str]] = []

    class FakeChild:
        returncode = None

        def __init__(self, _args, **kwargs) -> None:
            child_environments.append(dict(kwargs["env"]))
            self.running = True

        def poll(self):
            return None if self.running else 0

        def terminate(self) -> None:
            self.running = False

        def wait(self, timeout=None):
            self.running = False
            return 0

        def kill(self) -> None:
            self.running = False

    monkeypatch.setattr("pikvm_agent.harness.lab.subprocess.Popen", FakeChild)
    lab = RunningLab(
        endpoint="runtime-only.invalid:5900",
        root=tmp_path,
        ports=LabPorts(adapter=48170, daemon=48171, harness=48172),
        executable="/opt/pikvm-agent/bin/pikvm-agent",
        keymap="en-us",
        start_harness=True,
    )

    lab.start()
    try:
        assert len(child_environments) == 3
        values = [lab.env[name] for name in token_names]
        assert all(len(value) >= 32 for value in values)
        assert len(set(values)) == len(values)
        assert all(name not in os.environ for name in token_names)
        assert all(
            child[name] == lab.env[name]
            for child in child_environments
            for name in token_names
        )
        assert lab.assets is not None
        assert stat.S_IMODE(lab.assets.operator_runtime.stat().st_mode) == 0o600
        assert stat.S_IMODE(lab.assets.client_runtime.stat().st_mode) == 0o600

        operator_runtime = json.loads(
            lab.assets.operator_runtime.read_text(encoding="utf-8")
        )
        assert operator_runtime == {
            "schema_version": 1,
            "harness_url": "http://127.0.0.1:48172/app/",
            "token_env": "PIKVM_HARNESS_TOKEN",
            "token": lab.env["PIKVM_HARNESS_TOKEN"],
        }
        client_runtime = json.loads(
            lab.assets.client_runtime.read_text(encoding="utf-8")
        )
        assert client_runtime == {
            "schema_version": 1,
            "harness_config": str(tmp_path / "harness-config.yaml"),
            "agent_token_env": "PIKVM_HARNESS_AGENT_TOKEN",
            "agent_token": lab.env["PIKVM_HARNESS_AGENT_TOKEN"],
        }
        assert lab.env["PIKVM_HARNESS_TOKEN"] not in (
            lab.assets.client_runtime.read_text(encoding="utf-8")
        )
        assert lab.env["PIKVM_HARNESS_AGENT_TOKEN"] not in (
            lab.assets.operator_runtime.read_text(encoding="utf-8")
        )
    finally:
        lab.close()


def test_lab_refuses_empty_runtime_target_before_starting_child(
    tmp_path,
    monkeypatch,
) -> None:
    contacted = False

    def refuse_popen(*_args, **_kwargs):
        nonlocal contacted
        contacted = True
        raise AssertionError("empty target must fail before child startup")

    monkeypatch.setattr("pikvm_agent.harness.lab.subprocess.Popen", refuse_popen)
    lab = RunningLab(
        endpoint=" \t",
        root=tmp_path,
        ports=LabPorts(adapter=48175, daemon=48176, harness=48177),
        executable="/opt/pikvm-agent/bin/pikvm-agent",
        keymap="en-us",
    )

    with pytest.raises(ValueError, match="target endpoint must not be empty"):
        lab.start()
    assert contacted is False


def test_adapter_cli_rejects_empty_runtime_target() -> None:
    with pytest.raises(
        argparse.ArgumentTypeError,
        match="target endpoint must not be empty",
    ):
        _nonempty_endpoint(" \t")


def test_visible_lab_supervises_adapter_daemon_and_managed_harness(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PIKVM_HARNESS_TOKEN", "o" * 32)
    monkeypatch.setenv("PIKVM_HARNESS_AGENT_TOKEN", "a" * 32)
    monkeypatch.setenv("PIKVM_HARNESS_OBSERVER_TOKEN", "v" * 32)
    monkeypatch.setattr(
        "pikvm_agent.harness.lab._assert_port_available",
        lambda _port: None,
    )
    monkeypatch.setattr(
        "pikvm_agent.harness.lab._wait_ready",
        lambda _url, _child, _timeout: None,
    )
    monkeypatch.setattr(
        "pikvm_agent.harness.lab.ensure_provider_prerequisites",
        lambda _settings: None,
    )
    commands: list[list[str]] = []

    class FakeChild:
        returncode = None

        def __init__(self, args, **_kwargs) -> None:
            commands.append(list(args))
            self.running = True

        def poll(self):
            return None if self.running else 0

        def terminate(self) -> None:
            self.running = False

        def wait(self, timeout=None):
            self.running = False
            return 0

        def kill(self) -> None:
            self.running = False

    monkeypatch.setattr("pikvm_agent.harness.lab.subprocess.Popen", FakeChild)
    lab = RunningLab(
        endpoint="runtime-only.invalid:5900",
        root=tmp_path,
        ports=LabPorts(adapter=48180, daemon=48181, harness=48182),
        executable="/opt/pikvm-agent/bin/pikvm-agent",
        keymap="en-us",
        start_harness=True,
    )

    lab.start()
    try:
        assert len(commands) == 3
        assert lab.env["PIKVM_HARNESS_TOKEN"] != "o" * 32
        assert lab.env["PIKVM_HARNESS_AGENT_TOKEN"] != "a" * 32
        assert lab.env["PIKVM_HARNESS_OBSERVER_TOKEN"] != "v" * 32
        assert "pikvm_agent.daemon:lab_app" in commands[1]
        assert commands[2] == [
            "/opt/pikvm-agent/bin/pikvm-agent",
            "-m",
            "pikvm_agent.cli",
            "harness",
            "serve",
            "--config",
            str(tmp_path / "harness-config.yaml"),
        ]
        assert lab.harness_url == "http://127.0.0.1:48182"
    finally:
        lab.close()


def test_quiet_lab_surfaces_redacted_child_startup_failure(
    tmp_path,
    monkeypatch,
) -> None:
    endpoint = "runtime-only.invalid:5900"
    password = "vnc-password-do-not-report"
    username = "vnc-user-do-not-report"
    lab = RunningLab(
        endpoint=endpoint,
        root=tmp_path,
        ports=LabPorts(adapter=48190, daemon=48191, harness=48192),
        executable="/opt/pikvm-agent/bin/pikvm-agent",
        keymap="en-us",
        password=password,
        username=username,
        quiet=True,
    )
    lab.assets = write_lab_assets(
        tmp_path,
        ports=lab.ports,
        executable=lab.executable,
    )
    stderr = lab.assets.log_dir / "adapter.stderr.log"
    stderr.write_text(
        "target runtime-only.invalid:5900 refused for "
        "vnc-user-do-not-report:vnc-password-do-not-report\n"
        "VNC target is already controlled by another local lab\n"
    )
    monkeypatch.setattr(
        "pikvm_agent.harness.lab._wait_ready",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError("lab child exited early with code 1")
        ),
    )

    with pytest.raises(RuntimeError) as caught:
        lab._wait_for_child(
            "adapter",
            "http://127.0.0.1:48190/api/info",
            object(),
            20,
        )

    message = str(caught.value)
    assert isinstance(caught.value, LabChildStartupError)
    assert "VNC target is already controlled by another local lab" in message
    assert endpoint not in message
    assert password not in message
    assert username not in message
    assert "Traceback" not in message
    assert caught.value.debug_detail == (
        "VNC target is already controlled by another local lab"
    )


def test_quiet_lab_redacts_runtime_inputs_from_unknown_child_failure(
    tmp_path,
    monkeypatch,
) -> None:
    endpoint = "runtime-only.invalid:5900"
    password = "vnc-password-do-not-report"
    username = "vnc-user-do-not-report"
    lab = RunningLab(
        endpoint=endpoint,
        root=tmp_path,
        ports=LabPorts(adapter=48193, daemon=48194, harness=48195),
        executable="/opt/pikvm-agent/bin/pikvm-agent",
        keymap="en-us",
        password=password,
        username=username,
        quiet=True,
    )
    lab.assets = write_lab_assets(
        tmp_path,
        ports=lab.ports,
        executable=lab.executable,
    )
    (lab.assets.log_dir / "adapter.stderr.log").write_text(
        f"could not connect to {endpoint} as {username} with {password}\n"
    )
    monkeypatch.setattr(
        "pikvm_agent.harness.lab._wait_ready",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError("lab child exited early with code 1")
        ),
    )

    with pytest.raises(RuntimeError) as caught:
        lab._wait_for_child(
            "adapter",
            "http://127.0.0.1:48193/api/info",
            object(),
            20,
        )

    message = str(caught.value)
    assert isinstance(caught.value, LabChildStartupError)
    assert endpoint not in message
    assert password not in message
    assert username not in message
    assert "diagnostics retained in the private report" in message
    assert caught.value.debug_detail.count("[redacted]") == 3


def test_isolated_benchmark_policy_allows_routine_navigation_but_not_danger() -> None:
    policy = isolated_benchmark_policy()

    assert policy.default_profile == "isolated_benchmark"
    assert policy.allow_local_pointer_freshness is True
    assert classify_direct_burst(
        [{"type": "click", "x": 1246, "y": 9}],
        policy,
    ).status == "allowed"
    assert classify_direct_burst(
        [{"type": "click", "target_text": "Send message"}],
        policy,
    ).status == "approval_required"
    assert classify_direct_burst(
        [{"type": "click", "target_text": "Delete record"}],
        policy,
    ).status == "approval_required"


def test_lab_assets_persist_explicit_isolated_benchmark_policy(tmp_path) -> None:
    assets = write_lab_assets(
        tmp_path,
        ports=LabPorts(adapter=48150, daemon=48151, harness=48152),
        executable="/opt/pikvm-agent/bin/pikvm-agent",
        policy=isolated_benchmark_policy(),
    )

    policy = yaml.safe_load(assets.config.read_text())["policy"]
    assert policy["default_profile"] == "isolated_benchmark"
    assert "unknown" not in policy["require_human_for"]
    assert "communication_send" in policy["require_human_for"]
    assert "delete" in policy["require_human_for"]
