from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.client_config_audit import (
    ClientConfigDocument,
    audit_client_configs,
    read_codex_effective_inventory,
)


def test_claude_audit_accepts_one_managed_registration_without_leaking_config() -> None:
    rendered = json.dumps(
        {
            "mcpServers": {
                "atlas": {
                    "command": "atlas-mcp",
                    "env": {"ATLAS_TOKEN": "unrelated-secret"},
                },
                "pikvm": {
                    "command": "/opt/pikvm/python",
                    "args": [
                        "-m",
                        "pikvm_agent.cli",
                        "harness",
                        "managed-mcp",
                        "--config",
                        "/private/harness.yaml",
                    ],
                    "env": {"PIKVM_HARNESS_AGENT_TOKEN": "secret-value"},
                },
            }
        }
    )

    report = audit_client_configs(
        client="claude",
        documents=[
            ClientConfigDocument(source_label="project", rendered=rendered)
        ],
    )

    assert report.safe is True
    assert report.managed_count == 1
    assert report.failures == ()
    finding_rows = [
        (item.source_label, item.server_name, item.classification)
        for item in report.findings
    ]
    assert finding_rows == [
        ("project", "pikvm", "managed")
    ]
    serialized = report.model_dump_json()
    assert "secret-value" not in serialized
    assert "unrelated-secret" not in serialized
    assert "/private/harness.yaml" not in serialized
    assert "/opt/pikvm/python" not in serialized


def test_claude_audit_rejects_legacy_raw_registration_beside_managed_harness() -> None:
    rendered = json.dumps(
        {
            "mcpServers": {
                "pikvm-managed": {
                    "command": "pikvm-agent",
                    "args": ["harness", "managed-mcp"],
                },
                "pikvm-legacy": {
                    "command": "python",
                    "args": ["-m", "pikvm_agent.mcp_server"],
                },
            }
        }
    )

    report = audit_client_configs(
        client="claude",
        documents=[
            ClientConfigDocument(source_label="global", rendered=rendered)
        ],
    )

    assert report.safe is False
    assert report.managed_count == 1
    assert report.failures == ("competing_raw_or_direct",)
    assert [item.classification for item in report.findings] == [
        "managed",
        "raw",
    ]


def test_codex_audit_parses_managed_toml_registration() -> None:
    rendered = """
[mcp_servers.atlas]
command = "atlas-mcp"

[mcp_servers.pikvm]
command = "/opt/pikvm/python"
args = [
  "-m",
  "pikvm_agent.cli",
  "harness",
  "managed-mcp",
]
env_vars = ["PIKVM_HARNESS_AGENT_TOKEN"]
"""

    report = audit_client_configs(
        client="codex",
        documents=[
            ClientConfigDocument(source_label="project", rendered=rendered)
        ],
    )

    assert report.safe is True
    assert report.managed_count == 1
    assert report.failures == ()


def test_codex_audit_parses_project_mcp_json_registration() -> None:
    rendered = json.dumps(
        {
            "mcpServers": {
                "pikvm": {
                    "command": "/opt/pikvm/python",
                    "args": [
                        "-m",
                        "pikvm_agent.cli",
                        "harness",
                        "managed-mcp",
                    ],
                }
            }
        }
    )

    report = audit_client_configs(
        client="codex",
        documents=[
            ClientConfigDocument(source_label="project", rendered=rendered)
        ],
    )

    assert report.safe is True
    assert report.managed_count == 1
    assert report.failures == ()


def test_codex_audit_parses_native_effective_inventory() -> None:
    rendered = json.dumps(
        [
            {
                "name": "pikvm",
                "enabled": True,
                "transport": {
                    "type": "stdio",
                    "command": "python",
                    "args": ["-m", "pikvm_agent.mcp_server"],
                    "env": {"TOKEN": "do-not-retain"},
                },
            }
        ]
    )

    report = audit_client_configs(
        client="codex",
        documents=[
            ClientConfigDocument(
                source_label="native-inventory",
                rendered=rendered,
            )
        ],
    )

    assert report.safe is False
    assert report.managed_count == 0
    assert report.failures == (
        "missing_managed",
        "competing_raw_or_direct",
    )
    assert report.findings[0].classification == "raw"
    assert "do-not-retain" not in report.model_dump_json()


def test_codex_native_inventory_uses_exact_bounded_secret_free_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    inventory = json.dumps(
        [
            {
                "name": "pikvm",
                "enabled": True,
                "transport": {
                    "type": "stdio",
                    "command": "pikvm-agent",
                    "args": ["harness", "managed-mcp"],
                },
            }
        ]
    ).encode()

    def fake_run(
        argv: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=inventory,
            stderr=b"private diagnostic",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    document = read_codex_effective_inventory(
        executable="/opt/codex",
        project_dir=tmp_path,
        environ={
            "HOME": "/safe/home",
            "PATH": "/safe/bin",
            "CODEX_HOME": "/safe/codex",
            "OPENAI_API_KEY": "do-not-forward",
        },
    )

    assert document.source_label == "native-inventory"
    assert document.rendered == inventory.decode()
    assert calls[0]["argv"] == [
        "/opt/codex",
        "mcp",
        "list",
        "--json",
    ]
    assert calls[0]["cwd"] == tmp_path
    assert calls[0]["stdin"] is subprocess.DEVNULL
    assert calls[0]["timeout"] == 10.0
    assert calls[0]["env"] == {
        "HOME": "/safe/home",
        "PATH": "/safe/bin",
        "CODEX_HOME": "/safe/codex",
    }


def test_codex_native_inventory_does_not_expose_child_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout=b'{"token":"stdout-secret"}',
            stderr=b"stderr-secret",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(
        RuntimeError,
        match="^Codex inventory command failed$",
    ) as failure:
        read_codex_effective_inventory()

    rendered = str(failure.value)
    assert "stdout-secret" not in rendered
    assert "stderr-secret" not in rendered


def test_audit_allows_valid_scope_without_an_mcp_section() -> None:
    managed = """
[mcp_servers.pikvm]
command = "pikvm-agent"
args = ["harness", "managed-mcp"]
"""

    report = audit_client_configs(
        client="codex",
        documents=[
            ClientConfigDocument(
                source_label="user",
                rendered='model = "gpt-example"\n',
            ),
            ClientConfigDocument(
                source_label="project",
                rendered=managed,
            ),
        ],
    )

    assert report.safe is True
    assert report.managed_count == 1
    assert report.failures == ()


def test_opencode_audit_parses_command_array() -> None:
    rendered = json.dumps(
        {
            "mcp": {
                "pikvm": {
                    "type": "local",
                    "command": [
                        "/opt/pikvm/python",
                        "-m",
                        "pikvm_agent.cli",
                        "harness",
                        "managed-mcp",
                    ],
                    "enabled": True,
                }
            }
        }
    )

    report = audit_client_configs(
        client="opencode",
        documents=[
            ClientConfigDocument(source_label="project", rendered=rendered)
        ],
    )

    assert report.safe is True
    assert report.managed_count == 1
    assert report.failures == ()


@pytest.mark.parametrize(
    "client", ["codex", "claude", "gemini", "opencode"]
)
def test_audit_recognizes_official_managed_runtime_launcher(
    client: str,
) -> None:
    server = {
        "command": "pikvm-agent",
        "args": [
            "harness",
            "managed-runtime-mcp",
            "--runtime",
            "/run/user/1000/pikvm/managed-client-runtime.json",
            "--caller-label",
            f"{client}-cli",
        ],
    }
    if client == "codex":
        rendered = """
[mcp_servers.pikvm]
command = "pikvm-agent"
args = [
  "harness",
  "managed-runtime-mcp",
  "--runtime",
  "/run/user/1000/pikvm/managed-client-runtime.json",
  "--caller-label",
  "codex-cli",
]
"""
    elif client == "opencode":
        server["command"] = [
            "pikvm-agent",
            *server.pop("args"),
        ]
        rendered = json.dumps({"mcp": {"pikvm": server}})
    else:
        rendered = json.dumps({"mcpServers": {"pikvm": server}})

    report = audit_client_configs(
        client=client,  # type: ignore[arg-type]
        documents=[
            ClientConfigDocument(source_label="user", rendered=rendered)
        ],
    )

    assert report.safe is True
    assert report.managed_count == 1
    assert report.failures == ()


@pytest.mark.parametrize(
    "client", ["codex", "claude", "gemini", "opencode"]
)
def test_audit_recognizes_path_free_active_managed_launcher(
    client: str,
) -> None:
    server = {
        "command": "pikvm-agent",
        "args": [
            "harness",
            "active-managed-mcp",
            "--caller-label",
            f"{client}-cli",
        ],
    }
    if client == "codex":
        rendered = """
[mcp_servers.pikvm]
command = "pikvm-agent"
args = [
  "harness",
  "active-managed-mcp",
  "--caller-label",
  "codex-cli",
]
"""
    elif client == "opencode":
        server["command"] = [
            "pikvm-agent",
            *server.pop("args"),
        ]
        rendered = json.dumps({"mcp": {"pikvm": server}})
    else:
        rendered = json.dumps({"mcpServers": {"pikvm": server}})

    report = audit_client_configs(
        client=client,  # type: ignore[arg-type]
        documents=[
            ClientConfigDocument(source_label="user", rendered=rendered)
        ],
    )

    assert report.safe is True
    assert report.managed_count == 1
    assert report.failures == ()


@pytest.mark.parametrize(
    "arguments",
    [
        ["harness", "managed-runtime-mcp"],
        [
            "harness",
            "managed-runtime-mcp",
            "--runtime",
            "/run/user/1000/pikvm/runtime.json",
            "--runtime",
            "/tmp/other.json",
        ],
    ],
)
def test_audit_refuses_incomplete_official_launch_shapes(
    arguments: list[str],
) -> None:
    report = audit_client_configs(
        client="claude",
        documents=[
            ClientConfigDocument(
                source_label="user",
                rendered=json.dumps(
                    {
                        "mcpServers": {
                            "pikvm": {
                                "command": "pikvm-agent",
                                "args": arguments,
                            }
                        }
                    }
                ),
            )
        ],
    )

    assert report.safe is False
    assert report.managed_count == 0
    assert report.findings[0].classification == "ambiguous"
    assert report.failures == (
        "missing_managed",
        "ambiguous_pikvm_registration",
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["harness", "active-managed-mcp", "--runtime", "/tmp/runtime.json"],
        ["harness", "active-managed-mcp", "--caller-label"],
        ["harness", "active-managed-mcp", "--unknown"],
    ],
)
def test_audit_refuses_modified_active_launcher_shapes(
    arguments: list[str],
) -> None:
    report = audit_client_configs(
        client="claude",
        documents=[
            ClientConfigDocument(
                source_label="user",
                rendered=json.dumps(
                    {
                        "mcpServers": {
                            "pikvm": {
                                "command": "pikvm-agent",
                                "args": arguments,
                            }
                        }
                    }
                ),
            )
        ],
    )

    assert report.safe is False
    assert report.managed_count == 0
    assert report.findings[0].classification == "ambiguous"


def test_opencode_audit_parses_v2_jsonc_server_shape() -> None:
    rendered = """
{
  // OpenCode v2 nests named servers under mcp.servers.
  "mcp": {
    "servers": {
      "pikvm": {
        "type": "local",
        "command": [
          "pikvm-agent",
          "harness",
          "managed-mcp",
        ],
      },
    },
  },
}
"""

    report = audit_client_configs(
        client="opencode",
        documents=[
            ClientConfigDocument(source_label="project", rendered=rendered)
        ],
    )

    assert report.safe is True
    assert report.managed_count == 1
    assert report.failures == ()


def test_jsonc_comment_syntax_inside_strings_is_not_rewritten() -> None:
    rendered = r'''
{
  "note": "literal // and /* text */,}",
  "mcp": {
    "servers": {
      "pikvm": {
        "type": "local",
        "command": [
          "pikvm-agent",
          "harness",
          "managed-mcp"
        ],
        "cwd": "https://example.invalid/a//b",
      },
    },
  },
}
'''

    report = audit_client_configs(
        client="opencode",
        documents=[
            ClientConfigDocument(source_label="project", rendered=rendered)
        ],
    )

    assert report.safe is True
    assert report.managed_count == 1
    assert report.failures == ()


def test_audit_ignores_explicitly_disabled_registration() -> None:
    rendered = """
[mcp_servers.pikvm-old]
command = "python"
args = ["-m", "pikvm_agent.mcp_server"]
enabled = false

[mcp_servers.pikvm]
command = "pikvm-agent"
args = ["harness", "managed-mcp"]
enabled = true
"""

    report = audit_client_configs(
        client="codex",
        documents=[
            ClientConfigDocument(source_label="user", rendered=rendered)
        ],
    )

    assert report.safe is True
    assert report.managed_count == 1
    assert [item.server_name for item in report.findings] == ["pikvm"]
    assert report.failures == ()


def test_audit_rejects_duplicate_managed_registrations_across_scopes() -> None:
    managed_user = json.dumps(
        {
            "mcpServers": {
                "pikvm-user": {
                    "command": "pikvm-agent",
                    "args": ["harness", "managed-mcp"],
                }
            }
        }
    )
    managed_project = json.dumps(
        {
            "mcpServers": {
                "pikvm-project": {
                    "command": "pikvm-agent",
                    "args": ["harness", "managed-mcp"],
                }
            }
        }
    )

    report = audit_client_configs(
        client="gemini",
        documents=[
            ClientConfigDocument(
                source_label="user",
                rendered=managed_user,
            ),
            ClientConfigDocument(
                source_label="project",
                rendered=managed_project,
            ),
        ],
    )

    assert report.safe is False
    assert report.managed_count == 2
    assert report.failures == ("duplicate_managed",)
    assert [item.source_label for item in report.findings] == [
        "user",
        "project",
    ]


def test_higher_precedence_scope_replaces_same_named_registration() -> None:
    raw_user = json.dumps(
        {
            "mcpServers": {
                "pikvm": {
                    "command": "python",
                    "args": ["-m", "pikvm_agent.mcp_server"],
                }
            }
        }
    )
    managed_project = json.dumps(
        {
            "mcpServers": {
                "pikvm": {
                    "command": "pikvm-agent",
                    "args": ["harness", "managed-mcp"],
                }
            }
        }
    )

    report = audit_client_configs(
        client="claude",
        documents=[
            ClientConfigDocument(
                source_label="user",
                rendered=raw_user,
            ),
            ClientConfigDocument(
                source_label="project",
                rendered=managed_project,
            ),
        ],
    )

    assert report.safe is True
    assert report.managed_count == 1
    assert report.failures == ()
    assert [
        (item.source_label, item.server_name, item.classification)
        for item in report.findings
    ] == [("project", "pikvm", "managed")]


def test_audit_fails_closed_for_pikvm_named_unknown_wrapper() -> None:
    rendered = json.dumps(
        {
            "mcpServers": {
                "pikvm": {
                    "command": "unknown-wrapper",
                    "args": ["harness", "managed-mcp"],
                }
            }
        }
    )

    report = audit_client_configs(
        client="claude",
        documents=[
            ClientConfigDocument(source_label="user", rendered=rendered)
        ],
    )

    assert report.safe is False
    assert report.managed_count == 0
    assert report.failures == (
        "missing_managed",
        "ambiguous_pikvm_registration",
    )
    assert report.findings[0].classification == "ambiguous"


def test_audit_document_refuses_path_or_secret_bearing_source_label() -> None:
    with pytest.raises(
        ValueError,
        match="source_label must be a short non-path label",
    ):
        ClientConfigDocument(
            source_label="/home/user/private-config.json",
            rendered="{}",
        )


def test_client_audit_cli_emits_safe_json_for_ordered_config_scopes(
    tmp_path: Path,
) -> None:
    config = tmp_path / "private-name.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "pikvm": {
                        "command": "pikvm-agent",
                        "args": ["harness", "managed-mcp"],
                        "env": {"TOKEN": "never-print-this"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "client-audit",
            "--client",
            "claude",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["safe"] is True
    assert report["findings"][0]["source_label"] == "config-1"
    assert "private-name" not in result.stdout
    assert "never-print-this" not in result.stdout


def test_client_audit_cli_can_use_codex_native_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    inventory = json.dumps(
        [
            {
                "name": "pikvm",
                "enabled": True,
                "transport": {
                    "type": "stdio",
                    "command": "pikvm-agent",
                    "args": ["harness", "managed-mcp"],
                },
            }
        ]
    ).encode()

    def fake_run(
        argv: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[bytes]:
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
            "client-audit",
            "--client",
            "codex",
            "--native-inventory",
            "--project",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["safe"] is True
    assert report["findings"] == [
        {
            "source_label": "native-inventory",
            "server_name": "pikvm",
            "classification": "managed",
        }
    ]


def test_client_audit_cli_returns_one_for_bypassable_config(
    tmp_path: Path,
) -> None:
    config = tmp_path / "claude.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "pikvm": {
                        "command": "pikvm-agent",
                        "args": ["harness", "direct-mcp"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "client-audit",
            "--client",
            "claude",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["safe"] is False
    assert report["failures"] == [
        "missing_managed",
        "competing_raw_or_direct",
    ]
    assert report["findings"][0]["classification"] == "direct"


def test_invalid_config_fails_without_retaining_parser_input() -> None:
    report = audit_client_configs(
        client="codex",
        documents=[
            ClientConfigDocument(
                source_label="user",
                rendered='[mcp_servers.pikvm\nsecret = "do-not-retain"',
            )
        ],
    )

    assert report.safe is False
    assert report.failures == ("invalid_config", "missing_managed")
    serialized = report.model_dump_json()
    assert "do-not-retain" not in serialized
    assert "TOML" not in serialized


def test_client_audit_cli_writes_mode_0600_without_overwrite(
    tmp_path: Path,
) -> None:
    config = tmp_path / "client.json"
    output = tmp_path / "audit.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "pikvm": {
                        "command": "pikvm-agent",
                        "args": ["harness", "managed-mcp"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    args = [
        "harness",
        "client-audit",
        "--client",
        "claude",
        "--config",
        str(config),
        "--out",
        str(output),
    ]

    first = CliRunner().invoke(app, args)
    repeated = CliRunner().invoke(app, args)

    assert first.exit_code == 0, first.output
    assert first.stdout == "Client audit passed; report written.\n"
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text(encoding="utf-8"))["safe"] is True
    assert repeated.exit_code == 2
    assert "already exists" in repeated.stderr
    assert str(output) not in repeated.output
