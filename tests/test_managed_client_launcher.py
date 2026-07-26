from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.client_config_audit import ClientConfigDocument
from pikvm_agent.harness.config import HarnessSettings
from pikvm_agent.harness.managed_client_launcher import (
    ClientIsolationError,
    audit_managed_client_launch,
    build_managed_client_launch,
    build_managed_client_task_argv,
    run_managed_client_task,
)


def settings(monkeypatch: pytest.MonkeyPatch) -> HarnessSettings:
    monkeypatch.setenv(
        "TEST_AGENT_TOKEN",
        "runtime-only-agent-token-0123456789abcdef",
    )
    return HarnessSettings.model_validate(
        {
            "listen": "127.0.0.1:48124",
            "agent_token_env": "TEST_AGENT_TOKEN",
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


def test_codex_launch_overrides_only_pikvm_and_preflights_effective_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="codex",
        client_executable="/opt/codex",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )

    assert plan.client == "codex"
    assert plan.isolation_mode == "effective-inventory-override"
    assert plan.argv[0] == "/opt/codex"
    assert plan.argv[-2:] == ("-C", str(tmp_path.resolve()))
    assert plan.inventory_config_overrides
    combined = "\n".join(plan.inventory_config_overrides)
    assert "mcp_servers.pikvm.command" in combined
    assert "managed-mcp" in combined
    assert "direct-mcp" not in combined
    assert "TEST_AGENT_TOKEN" in combined
    assert "runtime-only-agent-token" not in combined
    assert plan.preserve_unrelated_mcp is True
    assert plan.modifies_persisted_config is False


def test_claude_launch_uses_strict_secret_free_mcp_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="claude",
        client_executable="/opt/claude",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )

    assert plan.isolation_mode == "strict-explicit-config"
    assert plan.argv[0] == "/opt/claude"
    assert "--mcp-config" in plan.argv
    assert "--strict-mcp-config" in plan.argv
    rendered = json.loads(plan.rendered_config)
    assert list(rendered["mcpServers"]) == ["pikvm"]
    assert rendered["mcpServers"]["pikvm"]["args"][3] == "managed-mcp"
    assert "runtime-only-agent-token" not in plan.rendered_config
    assert plan.preserve_unrelated_mcp is False
    assert plan.modifies_persisted_config is False


def test_opencode_launch_uses_pure_inline_managed_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="opencode",
        client_executable="/opt/opencode",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )

    assert plan.client == "opencode"
    assert plan.isolation_mode == "pure-inline-config"
    assert plan.argv == ("/opt/opencode", "--pure")
    rendered = json.loads(plan.rendered_config)
    assert list(rendered["mcp"]) == ["pikvm"]
    assert rendered["mcp"]["pikvm"]["command"][4] == "managed-mcp"
    assert rendered["permission"] == {
        "*": "deny",
        "pikvm_*": "allow",
    }
    assert "runtime-only-agent-token" not in plan.rendered_config
    assert plan.preserve_unrelated_mcp is False
    assert plan.modifies_persisted_config is False


def test_gemini_launch_uses_system_mcp_allowlist_and_default_deny_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="gemini",
        client_executable="/opt/gemini",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )

    assert plan.client == "gemini"
    assert plan.isolation_mode == "system-policy-allowlist"
    assert plan.argv == (
        "/opt/gemini",
        "--allowed-mcp-server-names",
        "pikvm",
        "--extensions",
        "none",
    )
    rendered = json.loads(plan.rendered_config)
    assert rendered["mcp"] == {"allowed": ["pikvm"]}
    assert list(rendered["mcpServers"]) == ["pikvm"]
    assert rendered["mcpServers"]["pikvm"]["args"][3] == "managed-mcp"
    assert rendered["security"] == {
        "disableYoloMode": True,
        "disableAlwaysAllow": True,
        "enablePermanentToolApproval": False,
    }
    assert rendered["skills"] == {"enabled": False}
    assert rendered["hooksConfig"] == {"enabled": False}
    assert "runtime-only-agent-token" not in plan.rendered_config
    assert plan.preserve_unrelated_mcp is False
    assert plan.modifies_persisted_config is False


def test_gemini_preflight_audits_native_effective_settings_and_admin_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = tmp_path / "gemini-profile"
    profile.mkdir()
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="gemini",
        client_executable="/opt/gemini",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )
    observed: dict[str, object] = {}

    def fake_effective(
        _plan: object,
        *,
        child_environment: dict[str, str],
        workspace: Path,
    ) -> dict[str, object]:
        system_settings = json.loads(
            Path(
                child_environment["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]
            ).read_text()
        )
        policy_path = Path(system_settings["adminPolicyPaths"][0])
        observed["workspace"] = workspace
        observed["policy"] = policy_path.read_text()
        observed["environment"] = child_environment
        return {
            "mcp": system_settings["mcp"],
            "mcpServers": system_settings["mcpServers"],
            "security": system_settings["security"],
            "skills": system_settings["skills"],
            "hooksConfig": system_settings["hooksConfig"],
            "context": system_settings["context"],
            "adminPolicyPaths": system_settings["adminPolicyPaths"],
            "errors": [],
        }

    monkeypatch.setattr(
        "pikvm_agent.harness.managed_client_launcher."
        "_read_gemini_effective_settings",
        fake_effective,
    )
    help_text = (
        "--admin-policy --allowed-mcp-server-names --approval-mode "
        "--extensions --output-format --prompt"
    ).encode()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(
            argv,
            0,
            stdout=help_text,
            stderr=b"",
        ),
    )

    report = audit_managed_client_launch(
        plan,
        environ={
            "HOME": "/home/operator",
            "PATH": "/usr/bin",
            "GEMINI_CLI_HOME": str(profile),
            "TEST_AGENT_TOKEN": "runtime-token",
            "UNRELATED_SECRET": "must-not-be-forwarded",
        },
    )

    assert report.safe is True
    assert report.managed_count == 1
    assert Path(observed["workspace"]) != tmp_path
    assert observed["policy"] == (
        '[[rule]]\n'
        'toolName = "*"\n'
        'decision = "deny"\n'
        "priority = 900\n"
        'deny_message = "Only the managed PiKVM MCP surface is available."\n'
        "\n"
        '[[rule]]\n'
        'mcpName = "pikvm"\n'
        'decision = "allow"\n'
        "priority = 999\n"
    )
    child_env = observed["environment"]
    assert isinstance(child_env, dict)
    assert child_env["GEMINI_CLI_HOME"] == str(profile)
    assert child_env["TEST_AGENT_TOKEN"] == "runtime-token"
    assert "HOME" not in child_env
    assert "UNRELATED_SECRET" not in child_env


def test_gemini_preflight_requires_a_dedicated_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="gemini",
        client_executable="/opt/gemini",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )

    with pytest.raises(
        ClientIsolationError,
        match="dedicated GEMINI_CLI_HOME",
    ):
        audit_managed_client_launch(
            plan,
            environ={
                "HOME": "/home/operator",
                "PATH": "/usr/bin",
                "TEST_AGENT_TOKEN": "runtime-token",
            },
        )


def test_codex_preflight_audits_the_exact_inline_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="codex",
        client_executable="/opt/codex",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )
    captured: dict[str, object] = {}

    def fake_inventory(**kwargs) -> ClientConfigDocument:
        captured.update(kwargs)
        return ClientConfigDocument(
            source_label="native-inventory",
            rendered=json.dumps(
                [
                    {
                        "name": "pikvm",
                        "enabled": True,
                        "transport": {
                            "command": "/opt/pikvm/python",
                            "args": [
                                "-m",
                                "pikvm_agent.cli",
                                "harness",
                                "managed-mcp",
                            ],
                        },
                    }
                ]
            ),
        )

    monkeypatch.setattr(
        "pikvm_agent.harness.managed_client_launcher."
        "read_codex_effective_inventory",
        fake_inventory,
    )

    report = audit_managed_client_launch(plan)

    assert report.safe is True
    assert captured["executable"] == "/opt/codex"
    assert captured["project_dir"] == tmp_path.resolve()
    assert captured["config_overrides"] == plan.inventory_config_overrides


def test_codex_preflight_refuses_a_competing_production_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="codex",
        client_executable="/opt/codex",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )

    monkeypatch.setattr(
        "pikvm_agent.harness.managed_client_launcher."
        "read_codex_effective_inventory",
        lambda **_: ClientConfigDocument(
            source_label="native-inventory",
            rendered=json.dumps(
                [
                    {
                        "name": "pikvm",
                        "enabled": True,
                        "transport": {
                            "command": "/opt/pikvm/python",
                            "args": [
                                "-m",
                                "pikvm_agent.cli",
                                "harness",
                                "managed-mcp",
                            ],
                        },
                    },
                    {
                        "name": "production-pikvm",
                        "enabled": True,
                        "transport": {
                            "command": "pikvm-agent",
                            "args": ["mcp"],
                        },
                    },
                ]
            ),
        ),
    )

    with pytest.raises(
        ClientIsolationError,
        match="isolated client preflight found a competing PiKVM surface",
    ):
        audit_managed_client_launch(plan)


def test_claude_preflight_requires_native_strict_mcp_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="claude",
        client_executable="/opt/claude",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(
            argv,
            0,
            stdout=b"Usage: claude --mcp-config <config>\n",
            stderr=b"",
        ),
    )

    with pytest.raises(
        ClientIsolationError,
        match="does not expose strict MCP isolation",
    ):
        audit_managed_client_launch(plan)


def test_claude_preflight_accepts_current_strict_mcp_interface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="claude",
        client_executable="/opt/claude",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                b"Usage: claude --mcp-config <config> "
                b"--strict-mcp-config\n"
            ),
            stderr=b"",
        ),
    )

    report = audit_managed_client_launch(plan)

    assert report.safe is True
    assert report.managed_count == 1


def test_opencode_preflight_audits_resolved_pure_config_in_ephemeral_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="opencode",
        client_executable="/opt/opencode",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )
    captured: dict[str, object] = {}

    def fake_run(
        argv: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=plan.rendered_config.encode(),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = audit_managed_client_launch(
        plan,
        environ={
            "HOME": "/home/operator",
            "PATH": "/usr/bin",
            "TEST_AGENT_TOKEN": "runtime-token",
            "UNRELATED_SECRET": "must-not-be-forwarded",
        },
    )

    assert report.safe is True
    assert report.managed_count == 1
    assert captured["argv"] == [
        "/opt/opencode",
        "debug",
        "config",
        "--pure",
    ]
    assert captured["cwd"] == tmp_path.resolve()
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert child_env["OPENCODE_CONFIG_CONTENT"] == plan.rendered_config
    assert child_env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "1"
    assert child_env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
    assert child_env["HOME"] != "/home/operator"
    assert child_env["XDG_DATA_HOME"] != "/home/operator/.local/share"
    assert child_env["TEST_AGENT_TOKEN"] == "runtime-token"
    assert "UNRELATED_SECRET" not in child_env


def test_opencode_preflight_refuses_competing_resolved_pikvm_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="opencode",
        client_executable="/opt/opencode",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )
    resolved = json.loads(plan.rendered_config)
    resolved["mcp"]["production-pikvm"] = {
        "type": "local",
        "command": ["pikvm-agent", "mcp"],
        "enabled": True,
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(resolved).encode(),
            stderr=b"",
        ),
    )

    with pytest.raises(
        ClientIsolationError,
        match="isolated client preflight found a competing PiKVM surface",
    ):
        audit_managed_client_launch(
            plan,
            environ={
                "HOME": "/home/operator",
                "PATH": "/usr/bin",
                "TEST_AGENT_TOKEN": "runtime-token",
            },
        )


def test_opencode_preflight_refuses_weakened_resolved_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="opencode",
        client_executable="/opt/opencode",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )
    resolved = json.loads(plan.rendered_config)
    resolved["permission"]["bash"] = "allow"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(resolved).encode(),
            stderr=b"",
        ),
    )

    with pytest.raises(
        ClientIsolationError,
        match="weakened default-deny permissions",
    ):
        audit_managed_client_launch(
            plan,
            environ={
                "HOME": "/home/operator",
                "PATH": "/usr/bin",
                "TEST_AGENT_TOKEN": "runtime-token",
            },
        )


def test_opencode_isolation_links_client_owned_oauth_without_copying_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "operator-home"
    source_auth = home / ".local" / "share" / "opencode" / "auth.json"
    source_auth.parent.mkdir(parents=True)
    source_auth.write_text('{"oauth":"client-owned"}\n', encoding="utf-8")
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="opencode",
        client_executable="/opt/opencode",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )
    observed: dict[str, object] = {}

    def fake_run(
        argv: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        child_env = kwargs["env"]
        assert isinstance(child_env, dict)
        isolated_auth = (
            Path(child_env["XDG_DATA_HOME"]) / "opencode" / "auth.json"
        )
        observed["is_symlink"] = isolated_auth.is_symlink()
        observed["target"] = isolated_auth.resolve()
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=plan.rendered_config.encode(),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = audit_managed_client_launch(
        plan,
        environ={
            "HOME": str(home),
            "PATH": "/usr/bin",
            "TEST_AGENT_TOKEN": "runtime-token",
        },
    )

    assert report.safe is True
    assert observed == {
        "is_symlink": True,
        "target": source_auth.resolve(),
    }
    assert source_auth.read_text(encoding="utf-8") == (
        '{"oauth":"client-owned"}\n'
    )


def test_client_launch_dry_run_is_audited_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness_config = tmp_path / "harness.yaml"
    harness_config.write_text(
        """
listen: "127.0.0.1:48124"
agent_token_env: "TEST_AGENT_TOKEN"
providers:
  fake:
    kind: "subprocess_json"
    model: "test"
    argv: ["provider"]
routes:
  reasoner: ["fake"]
  controller: ["fake"]
  verifier: ["fake"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "TEST_AGENT_TOKEN",
        "runtime-only-agent-token-0123456789abcdef",
    )

    def fake_run(
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[bytes]:
        inventory = json.dumps(
            [
                {
                    "name": "pikvm",
                    "enabled": True,
                    "transport": {
                        "command": "/opt/pikvm/python",
                        "args": [
                            "-m",
                            "pikvm_agent.cli",
                            "harness",
                            "managed-mcp",
                        ],
                    },
                }
            ]
        ).encode()
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=inventory,
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "client-launch",
            "--client",
            "codex",
            "--config",
            str(harness_config),
            "--project",
            str(tmp_path),
            "--client-executable",
            "/opt/codex",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    assert summary["safe"] is True
    assert summary["client"] == "codex"
    assert summary["managed_count"] == 1
    assert summary["modifies_persisted_config"] is False
    assert summary["would_launch"] is False
    assert "runtime-only-agent-token" not in result.stdout
    assert "managed-mcp" not in result.stdout


@pytest.mark.parametrize(
    ("client", "expected_suffix"),
    (
        (
            "codex",
            (
                "exec",
                "--json",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "-",
            ),
        ),
        (
            "claude",
            (
                "--print",
                "--output-format",
                "stream-json",
                "--no-session-persistence",
                "--permission-mode",
                "dontAsk",
            ),
        ),
        (
            "opencode",
            (
                "--pure",
                "run",
                "--format",
                "json",
            ),
        ),
        (
            "gemini",
            (
                "--prompt",
                "",
                "--output-format",
                "stream-json",
                "--approval-mode",
                "default",
            ),
        ),
    ),
)
def test_managed_client_task_reads_private_task_from_stdin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: str,
    expected_suffix: tuple[str, ...],
) -> None:
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client=client,
        client_executable=f"/opt/{client}",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )

    argv = build_managed_client_task_argv(plan)

    assert argv[-len(expected_suffix) :] == expected_suffix
    assert "Complete the private managed task" not in "\n".join(argv)
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert "--dangerously-skip-permissions" not in argv


def test_managed_client_task_reports_only_safe_execution_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="codex",
        client_executable="/opt/codex",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_managed_client_task(
        plan,
        task="Complete the private managed task.",
        timeout_s=30,
        environ={"TEST_AGENT_TOKEN": "runtime-token"},
    )
    summary = result.summary()

    assert captured["input"] == b"Complete the private managed task."
    assert "Complete the private managed task." not in "\n".join(
        captured["argv"]  # type: ignore[arg-type]
    )
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["timeout"] == 30
    assert summary["client"] == "codex"
    assert summary["exit_code"] == 0
    assert summary["task_bytes"] == 34
    assert len(str(summary["task_sha256"])) == 64
    assert "private managed task" not in json.dumps(summary)


def test_opencode_task_reuses_ephemeral_default_deny_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="opencode",
        client_executable="/opt/opencode",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_managed_client_task(
        plan,
        task="Complete the private managed task.",
        timeout_s=30,
        environ={
            "HOME": "/home/operator",
            "PATH": "/usr/bin",
            "TEST_AGENT_TOKEN": "runtime-token",
            "UNRELATED_SECRET": "must-not-be-forwarded",
        },
    )

    assert captured["argv"] == [
        "/opt/opencode",
        "--pure",
        "run",
        "--format",
        "json",
    ]
    assert captured["input"] == b"Complete the private managed task."
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert child_env["OPENCODE_CONFIG_CONTENT"] == plan.rendered_config
    assert child_env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "1"
    assert child_env["HOME"] != "/home/operator"
    assert child_env["XDG_DATA_HOME"] != "/home/operator/.local/share"
    assert child_env["TEST_AGENT_TOKEN"] == "runtime-token"
    assert "UNRELATED_SECRET" not in child_env
    assert result.exit_code == 0


def test_gemini_task_reuses_native_policy_runtime_and_clean_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = tmp_path / "gemini-profile"
    profile.mkdir()
    plan = build_managed_client_launch(
        settings(monkeypatch),
        client="gemini",
        client_executable="/opt/gemini",
        mcp_executable="/opt/pikvm/python",
        harness_config=tmp_path / "harness.yaml",
        project_dir=tmp_path,
    )
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        captured["argv"] = argv
        captured.update(kwargs)
        child_env = kwargs["env"]
        assert isinstance(child_env, dict)
        system_settings = json.loads(
            Path(
                child_env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]
            ).read_text()
        )
        captured["system_settings"] = system_settings
        captured["policy"] = Path(
            system_settings["adminPolicyPaths"][0]
        ).read_text()
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_managed_client_task(
        plan,
        task="Complete the private managed task.",
        timeout_s=30,
        environ={
            "HOME": "/home/operator",
            "PATH": "/usr/bin",
            "GEMINI_CLI_HOME": str(profile),
            "TEST_AGENT_TOKEN": "runtime-token",
            "UNRELATED_SECRET": "must-not-be-forwarded",
        },
    )

    assert captured["argv"] == [
        "/opt/gemini",
        "--allowed-mcp-server-names",
        "pikvm",
        "--extensions",
        "none",
        "--prompt",
        "",
        "--output-format",
        "stream-json",
        "--approval-mode",
        "default",
    ]
    assert captured["input"] == b"Complete the private managed task."
    assert captured["cwd"] != tmp_path.resolve()
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert child_env["GEMINI_CLI_HOME"] == str(profile)
    assert child_env["TEST_AGENT_TOKEN"] == "runtime-token"
    assert "HOME" not in child_env
    assert "UNRELATED_SECRET" not in child_env
    system_settings = captured["system_settings"]
    assert isinstance(system_settings, dict)
    assert system_settings["mcp"] == {"allowed": ["pikvm"]}
    assert list(system_settings["mcpServers"]) == ["pikvm"]
    assert captured["policy"] == (
        '[[rule]]\n'
        'toolName = "*"\n'
        'decision = "deny"\n'
        "priority = 900\n"
        'deny_message = "Only the managed PiKVM MCP surface is available."\n'
        "\n"
        '[[rule]]\n'
        'mcpName = "pikvm"\n'
        'decision = "allow"\n'
        "priority = 999\n"
    )
    assert result.exit_code == 0


def test_client_task_cli_audits_then_runs_one_stdin_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness_config = tmp_path / "harness.yaml"
    harness_config.write_text(
        """
listen: "127.0.0.1:48124"
agent_token_env: "TEST_AGENT_TOKEN"
providers:
  fake:
    kind: "subprocess_json"
    model: "test"
    argv: ["provider"]
routes:
  reasoner: ["fake"]
  controller: ["fake"]
  verifier: ["fake"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "TEST_AGENT_TOKEN",
        "runtime-only-agent-token-0123456789abcdef",
    )
    task_call: dict[str, object] = {}

    def fake_run(
        argv: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess:
        if "mcp" in argv and "list" in argv:
            inventory = json.dumps(
                [
                    {
                        "name": "pikvm",
                        "enabled": True,
                        "transport": {
                            "command": "/opt/pikvm/python",
                            "args": [
                                "-m",
                                "pikvm_agent.cli",
                                "harness",
                                "managed-mcp",
                            ],
                        },
                    }
                ]
            ).encode()
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=inventory,
                stderr=b"",
            )
        task_call["argv"] = argv
        task_call.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "pikvm_agent.harness.client_setup.verify_managed_harness_ready",
        lambda _settings: None,
    )
    result = CliRunner().invoke(
        app,
        [
            "harness",
            "client-task",
            "--client",
            "codex",
            "--config",
            str(harness_config),
            "--project",
            str(tmp_path),
            "--client-executable",
            "/opt/codex",
            "--max-runtime-s",
            "30",
        ],
        input="Complete the smoke task.\n",
    )

    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    assert summary["safe"] is True
    assert summary["managed_count"] == 1
    assert summary["task"]["exit_code"] == 0
    assert task_call["input"] == b"Complete the smoke task.\n"
    assert "Complete the smoke task" not in "\n".join(
        task_call["argv"]  # type: ignore[arg-type]
    )
    assert "runtime-only-agent-token" not in result.stdout
