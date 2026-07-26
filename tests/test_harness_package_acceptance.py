from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Mapping

import pytest
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.package_acceptance import WheelAcceptanceError
from pikvm_agent.harness.package_acceptance import inspect_operator_wheel


def _wheel(
    tmp_path: Path,
    *,
    extra_files: Mapping[str, bytes] | None = None,
) -> Path:
    wheel = tmp_path / "pikvm_agent-0.1.0-py3-none-any.whl"
    files = {
        "pikvm_agent/__init__.py": b'__version__ = "0.1.0"\n',
        "pikvm_agent/cli.py": b"app = object()\n",
        "pikvm_agent/harness/agent.py": b"class AgentHarness: pass\n",
        "pikvm_agent/harness/client_acceptance.py": (
            b"def run_managed_client_acceptance(): pass\n"
        ),
        "pikvm_agent/harness/client_config_audit.py": (
            b"def audit_client_configs(): pass\n"
        ),
        "pikvm_agent/harness/managed_client_launcher.py": (
            b"def build_managed_client_launch(): pass\n"
        ),
        "pikvm_agent/harness/smoke_lab.py": (
            b"def build_managed_smoke_app(): pass\n"
        ),
        "pikvm_agent/harness/stdio_transport.py": (
            b"def run_fastmcp_stdio(server): pass\n"
        ),
        "pikvm_agent/harness/model_budget.py": b"class ModelBudgetPolicy: pass\n",
        "pikvm_agent/harness/office_acceptance.py": (
            b"def verify_office_artifact(): pass\n"
        ),
        "pikvm_agent/harness/office_runner.py": (
            b"def run_live_office_case(): pass\n"
        ),
        "pikvm_agent/harness/provider_conformance.py": (
            b"def run_provider_conformance(): pass\n"
        ),
        "pikvm_agent/harness/server.py": b"def build_harness_app(): pass\n",
        "pikvm_agent/harness/scorecard.py": b"def render_scorecard(): pass\n",
        "pikvm_agent/harness/support_bundle.py": b"def build_support_bundle(): pass\n",
        "pikvm_agent/harness_mcp_server.py": b"def main(): pass\n",
        "pikvm_agent/harness_ui/index.html": b"<!doctype html><title>Operator</title>",
        "pikvm_agent/harness_ui/app.js": b'console.log("operator");\n',
        "pikvm_agent/harness_ui/styles.css": b"body { color: white; }\n",
        "pikvm_agent-0.1.0.dist-info/METADATA": (
            b"Metadata-Version: 2.3\nName: pikvm-agent\nVersion: 0.1.0\n"
        ),
        "pikvm_agent-0.1.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: acceptance-fixture\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        "pikvm_agent-0.1.0.dist-info/entry_points.txt": (
            b"[console_scripts]\npikvm-agent = pikvm_agent.cli:app\n"
        ),
    }
    files.update(extra_files or {})
    record_path = "pikvm_agent-0.1.0.dist-info/RECORD"
    rows = []
    for name, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        rows.append([name, f"sha256={digest.decode()}", str(len(data))])
    rows.append([record_path, "", ""])
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    files[record_path] = buffer.getvalue().encode()
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return wheel


def _rewrite_wheel(
    wheel: Path,
    *,
    omit: set[str] | None = None,
    replace: Mapping[str, bytes] | None = None,
) -> None:
    with zipfile.ZipFile(wheel) as archive:
        files = {
            info.filename: archive.read(info)
            for info in archive.infolist()
            if info.filename not in (omit or set())
        }
    files.update(replace or {})
    wheel.unlink()
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)


def test_customer_wheel_contains_a_runnable_operator_surface(tmp_path: Path) -> None:
    report = inspect_operator_wheel(_wheel(tmp_path))

    assert report["valid"] is True
    assert report["package"] == {"name": "pikvm-agent", "version": "0.1.0"}
    assert report["console_script"] == "pikvm_agent.cli:app"
    assert report["operator_assets"] == {
        "pikvm_agent/harness_ui/app.js": 25,
        "pikvm_agent/harness_ui/index.html": 38,
        "pikvm_agent/harness_ui/styles.css": 23,
    }
    assert report["record_entries_verified"] == 22


def test_customer_wheel_rejects_runtime_secrets_even_with_a_valid_record(
    tmp_path: Path,
) -> None:
    wheel = _wheel(
        tmp_path,
        extra_files={"pikvm_agent/.env": b"PIKVM_TOKEN=must-not-ship\n"},
    )

    with pytest.raises(WheelAcceptanceError, match="forbidden"):
        inspect_operator_wheel(wheel)


def test_operator_can_inspect_a_built_wheel_from_the_cli(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "harness",
            "inspect-wheel",
            "--wheel",
            str(_wheel(tmp_path)),
        ],
    )

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["valid"] is True
    assert report["console_script"] == "pikvm_agent.cli:app"


def test_customer_wheel_rejects_a_missing_operator_asset(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    _rewrite_wheel(wheel, omit={"pikvm_agent/harness_ui/app.js"})

    with pytest.raises(WheelAcceptanceError, match="harness_ui/app.js"):
        inspect_operator_wheel(wheel)


def test_customer_wheel_requires_managed_client_acceptance_runner(
    tmp_path: Path,
) -> None:
    wheel = _wheel(tmp_path)
    _rewrite_wheel(
        wheel,
        omit={"pikvm_agent/harness/client_acceptance.py"},
    )

    with pytest.raises(
        WheelAcceptanceError,
        match="harness/client_acceptance.py",
    ):
        inspect_operator_wheel(wheel)


def test_customer_wheel_requires_client_isolation_audit(
    tmp_path: Path,
) -> None:
    wheel = _wheel(tmp_path)
    _rewrite_wheel(
        wheel,
        omit={"pikvm_agent/harness/client_config_audit.py"},
    )

    with pytest.raises(
        WheelAcceptanceError,
        match="harness/client_config_audit.py",
    ):
        inspect_operator_wheel(wheel)


def test_customer_wheel_requires_isolated_managed_client_launcher(
    tmp_path: Path,
) -> None:
    wheel = _wheel(tmp_path)
    _rewrite_wheel(
        wheel,
        omit={"pikvm_agent/harness/managed_client_launcher.py"},
    )

    with pytest.raises(
        WheelAcceptanceError,
        match="harness/managed_client_launcher.py",
    ):
        inspect_operator_wheel(wheel)


def test_customer_wheel_requires_target_free_managed_smoke_lab(
    tmp_path: Path,
) -> None:
    wheel = _wheel(tmp_path)
    _rewrite_wheel(
        wheel,
        omit={"pikvm_agent/harness/smoke_lab.py"},
    )

    with pytest.raises(
        WheelAcceptanceError,
        match="harness/smoke_lab.py",
    ):
        inspect_operator_wheel(wheel)


def test_customer_wheel_requires_worker_free_stdio_transport(
    tmp_path: Path,
) -> None:
    wheel = _wheel(tmp_path)
    _rewrite_wheel(
        wheel,
        omit={"pikvm_agent/harness/stdio_transport.py"},
    )

    with pytest.raises(
        WheelAcceptanceError,
        match="harness/stdio_transport.py",
    ):
        inspect_operator_wheel(wheel)


def test_customer_wheel_rejects_record_integrity_mismatch(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    _rewrite_wheel(
        wheel,
        replace={"pikvm_agent/harness_ui/app.js": b"tampered after RECORD"},
    )

    with pytest.raises(WheelAcceptanceError, match="integrity"):
        inspect_operator_wheel(wheel)
