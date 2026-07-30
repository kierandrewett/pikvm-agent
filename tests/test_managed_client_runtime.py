from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.managed_client_runtime import (
    ACTIVE_MANAGED_RUNTIME_ENV,
    active_managed_client_runtime_path,
    load_managed_client_runtime,
    publish_active_managed_client_runtime,
)

AGENT_TOKEN = "runtime-agent-capability-0123456789abcdef"


def _write_harness_config(path: Path, *, token_env: str = "LAB_AGENT") -> None:
    path.write_text(
        "\n".join(
            [
                'listen: "127.0.0.1:48124"',
                'daemon_url_env: "ABSENT_DAEMON_URL"',
                'access_token_env: "ABSENT_OPERATOR_TOKEN"',
                f'agent_token_env: "{token_env}"',
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


def _write_runtime(
    path: Path,
    config: Path,
    *,
    token_env: str = "LAB_AGENT",
    token: str = AGENT_TOKEN,
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "harness_config": str(config),
                "agent_token_env": token_env,
                "agent_token": token,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_runtime_loads_only_agent_scope_into_a_copied_environment(
    tmp_path: Path,
) -> None:
    config = tmp_path / "harness.yaml"
    runtime = tmp_path / "managed-runtime.json"
    _write_harness_config(config)
    _write_runtime(runtime, config)
    parent = {"PATH": "/safe/bin", "UNRELATED": "preserved"}

    loaded = load_managed_client_runtime(
        runtime,
        expected_harness_config=config,
        environ=parent,
    )

    assert parent == {"PATH": "/safe/bin", "UNRELATED": "preserved"}
    assert loaded.harness_config == config
    assert loaded.settings.agent_token_env == "LAB_AGENT"
    assert loaded.environment == {
        "PATH": "/safe/bin",
        "UNRELATED": "preserved",
        "LAB_AGENT": AGENT_TOKEN,
    }


def test_active_runtime_path_is_stable_and_overrideable(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "selected" / "runtime.json"

    assert active_managed_client_runtime_path(
        environ={ACTIVE_MANAGED_RUNTIME_ENV: str(selected)}
    ) == selected
    assert active_managed_client_runtime_path(
        environ={"XDG_RUNTIME_DIR": str(tmp_path)}
    ) == (
        tmp_path
        / "pikvm-agent"
        / "managed"
        / "managed-client-runtime.json"
    )
    with pytest.raises(ValueError, match="must be an absolute path"):
        active_managed_client_runtime_path(
            environ={ACTIVE_MANAGED_RUNTIME_ENV: "relative/runtime.json"}
        )


def test_publish_active_runtime_reduces_and_atomically_replaces_agent_scope(
    tmp_path: Path,
) -> None:
    config = tmp_path / "harness.yaml"
    source = tmp_path / "source-runtime.json"
    destination = tmp_path / "active" / "managed-client-runtime.json"
    _write_harness_config(config)
    _write_runtime(source, config)

    published = publish_active_managed_client_runtime(
        source,
        destination=destination,
        environ={"UNRELATED": "must-not-be-persisted"},
    )

    assert published == destination
    assert destination.stat().st_mode & 0o077 == 0
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "harness_config": str(config),
        "agent_token_env": "LAB_AGENT",
        "agent_token": AGENT_TOKEN,
    }
    assert "UNRELATED" not in destination.read_text(encoding="utf-8")
    assert list(destination.parent.glob(f".{destination.name}.*")) == []
    loaded = load_managed_client_runtime(destination, environ={})
    assert loaded.environment == {"LAB_AGENT": AGENT_TOKEN}

    replacement = "replacement-agent-capability-0123456789abcdef"
    _write_runtime(source, config, token=replacement)
    publish_active_managed_client_runtime(
        source,
        destination=destination,
        environ={},
    )
    assert load_managed_client_runtime(
        destination,
        environ={},
    ).environment == {"LAB_AGENT": replacement}


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o666])
def test_runtime_refuses_group_or_world_access(
    tmp_path: Path,
    mode: int,
) -> None:
    config = tmp_path / "harness.yaml"
    runtime = tmp_path / "managed-runtime.json"
    _write_harness_config(config)
    _write_runtime(runtime, config)
    runtime.chmod(mode)

    with pytest.raises(ValueError, match="owner-only"):
        load_managed_client_runtime(runtime, environ={})


def test_runtime_refuses_a_symlink_even_when_target_is_private(
    tmp_path: Path,
) -> None:
    config = tmp_path / "harness.yaml"
    target = tmp_path / "managed-runtime-target.json"
    runtime = tmp_path / "managed-runtime.json"
    _write_harness_config(config)
    _write_runtime(target, config)
    runtime.symlink_to(target)

    with pytest.raises(ValueError, match="must not be a symlink"):
        load_managed_client_runtime(runtime, environ={})


def test_runtime_refuses_another_harness_config(
    tmp_path: Path,
) -> None:
    config = tmp_path / "harness.yaml"
    other = tmp_path / "other-harness.yaml"
    runtime = tmp_path / "managed-runtime.json"
    _write_harness_config(config)
    _write_harness_config(other)
    _write_runtime(runtime, config)

    with pytest.raises(ValueError, match="another harness config"):
        load_managed_client_runtime(
            runtime,
            expected_harness_config=other,
            environ={},
        )


def test_runtime_refuses_a_mismatched_agent_scope(
    tmp_path: Path,
) -> None:
    config = tmp_path / "harness.yaml"
    runtime = tmp_path / "managed-runtime.json"
    _write_harness_config(config)
    _write_runtime(runtime, config, token_env="OTHER_AGENT")

    with pytest.raises(ValueError, match="agent scope does not match"):
        load_managed_client_runtime(runtime, environ={})


def test_runtime_refuses_non_utf8_and_oversized_payloads(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid-runtime.json"
    invalid.write_bytes(b"\xff")
    invalid.chmod(0o600)
    oversized = tmp_path / "oversized-runtime.json"
    oversized.write_bytes(b"{" + b" " * (16 * 1024))
    oversized.chmod(0o600)

    with pytest.raises(ValueError, match="not valid UTF-8"):
        load_managed_client_runtime(invalid, environ={})
    with pytest.raises(ValueError, match="exceeds 16 KiB"):
        load_managed_client_runtime(oversized, environ={})


def test_cli_refusal_never_echoes_runtime_capability(
    tmp_path: Path,
) -> None:
    config = tmp_path / "harness.yaml"
    runtime = tmp_path / "managed-runtime.json"
    _write_harness_config(config)
    _write_runtime(runtime, config)
    runtime.chmod(0o644)

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "managed-runtime-mcp",
            "--runtime",
            str(runtime),
        ],
    )

    assert result.exit_code == 2
    assert "startup refused: ValueError" in result.stderr
    assert AGENT_TOKEN not in result.stdout
    assert AGENT_TOKEN not in result.stderr
