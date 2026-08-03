from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.client_config_audit import (
    ClientConfigDocument,
    audit_client_configs,
)
from pikvm_agent.harness.managed_client_install import (
    ManagedClientInstallError,
    install_active_managed_registration,
    plan_active_managed_install,
    rollback_active_managed_registration,
)


def _settings(path: Path) -> bytes:
    payload = (
        json.dumps(
            {
                "general": {"previewFeatures": True},
                "ui": {"theme": "system"},
            },
            indent=4,
        )
        + "\n"
    ).encode()
    path.write_bytes(payload)
    path.chmod(0o640)
    return payload


def _plan(path: Path):
    return plan_active_managed_install(
        client="gemini",
        config_path=path,
        executable=sys.executable,
    )


def test_plan_is_additive_secret_free_and_managed_only(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = _settings(path)

    plan = _plan(path)
    candidate = json.loads(plan.candidate)

    assert plan.original == original
    assert candidate["general"] == {"previewFeatures": True}
    assert candidate["ui"] == {"theme": "system"}
    registration = candidate["mcpServers"]["pikvm"]
    assert registration["args"][-2:] == [
        "--caller-label",
        "gemini-cli",
    ]
    assert "active-managed-mcp" in registration["args"]
    assert registration["env"] == {}
    assert "token" not in plan.candidate.decode().lower()
    assert "runtime.json" not in plan.candidate.decode()
    report = audit_client_configs(
        client="gemini",
        documents=[
            ClientConfigDocument(
                source_label="candidate",
                rendered=plan.candidate.decode(),
            )
        ],
    )
    assert report.safe is True
    assert report.managed_count == 1
    assert plan.summary()["registration"] == registration


def test_plan_refuses_conflicting_or_ambiguous_settings(tmp_path: Path) -> None:
    conflict = tmp_path / "conflict.json"
    conflict.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "pikvm": {
                        "command": "pikvm-agent",
                        "args": ["mcp"],
                        "env": {},
                    }
                }
            }
        )
    )
    with pytest.raises(ManagedClientInstallError, match="different registration"):
        _plan(conflict)

    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]")
    with pytest.raises(ManagedClientInstallError, match="JSON object"):
        _plan(invalid)


def test_install_requires_exact_review_and_rolls_back_exact_bytes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    original = _settings(path)
    plan = _plan(path)

    with pytest.raises(ManagedClientInstallError, match="review digest"):
        install_active_managed_registration(
            plan=plan,
            reviewed_sha256="0" * 64,
        )
    assert path.read_bytes() == original

    receipt = install_active_managed_registration(
        plan=plan,
        reviewed_sha256=plan.review_sha256,
    )
    assert path.read_bytes() == plan.candidate
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert receipt.backup_path.read_bytes() == original
    assert stat.S_IMODE(receipt.backup_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt.receipt_path.stat().st_mode) == 0o600

    result = rollback_active_managed_registration(receipt.receipt_path)
    assert result["backup_retained"] is True
    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert receipt.backup_path.exists()
    assert receipt.receipt_path.exists()


def test_install_and_rollback_refuse_concurrent_changes(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _settings(path)
    stale = _plan(path)
    path.write_text('{"ui":{"theme":"dark"}}\n')
    with pytest.raises(ManagedClientInstallError, match="changed after review"):
        install_active_managed_registration(
            plan=stale,
            reviewed_sha256=stale.review_sha256,
        )

    fresh = _plan(path)
    receipt = install_active_managed_registration(
        plan=fresh,
        reviewed_sha256=fresh.review_sha256,
    )
    path.write_text(path.read_text() + " ")
    with pytest.raises(ManagedClientInstallError, match="rollback refused"):
        rollback_active_managed_registration(receipt.receipt_path)


def test_install_refuses_symlink_settings(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    _settings(target)
    link = tmp_path / "settings.json"
    link.symlink_to(target)
    with pytest.raises(ManagedClientInstallError, match="non-symlink"):
        _plan(link)


def test_cli_plan_apply_audit_and_rollback(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = _settings(path)
    runner = CliRunner()

    planned = runner.invoke(
        app,
        [
            "harness",
            "active-client-install",
            "--client",
            "gemini",
            "--config",
            str(path),
        ],
    )
    assert planned.exit_code == 0, planned.output
    plan_payload = json.loads(planned.output)
    assert plan_payload["applied"] is False
    assert path.read_bytes() == original

    applied = runner.invoke(
        app,
        [
            "harness",
            "active-client-install",
            "--client",
            "gemini",
            "--config",
            str(path),
            "--reviewed-sha256",
            plan_payload["review_sha256"],
        ],
    )
    assert applied.exit_code == 0, applied.output
    receipt = json.loads(applied.output)
    assert receipt["applied"] is True

    audit = runner.invoke(
        app,
        [
            "harness",
            "client-audit",
            "--client",
            "gemini",
            "--config",
            str(path),
        ],
    )
    assert audit.exit_code == 0, audit.output
    assert json.loads(audit.output)["safe"] is True

    rolled_back = runner.invoke(
        app,
        [
            "harness",
            "active-client-rollback",
            "--receipt",
            receipt["receipt_path"],
        ],
    )
    assert rolled_back.exit_code == 0, rolled_back.output
    assert path.read_bytes() == original
