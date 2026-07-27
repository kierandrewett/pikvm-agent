from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.browser_matrix import (
    _clean_failure,
    _is_fixture_request,
    parse_browser_names,
)


def test_parse_browser_names_preserves_requested_order() -> None:
    assert parse_browser_names("firefox, chromium") == (
        "firefox",
        "chromium",
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "at least one browser"),
        ("chromium,chromium", "must be unique"),
        ("edge", "unsupported browser"),
    ],
)
def test_parse_browser_names_rejects_ambiguous_matrices(
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_browser_names(value)


def test_fixture_request_allowlist_is_exact() -> None:
    assert _is_fixture_request("http://127.0.0.1:43123/app/", 43123)
    assert _is_fixture_request("ws://127.0.0.1:43123/events", 43123)
    assert _is_fixture_request("blob:http://127.0.0.1:43123/frame", 43123)
    assert _is_fixture_request("data:image/svg+xml;base64,AA==", 43123)
    assert not _is_fixture_request("http://localhost:43123/app/", 43123)
    assert not _is_fixture_request("http://127.0.0.1:43124/app/", 43123)
    assert not _is_fixture_request("https://example.com/app/", 43123)


def test_public_failure_does_not_retain_local_paths() -> None:
    failure = _clean_failure(
        RuntimeError("failed at /home/kieran/private/token.txt\ntrace")
    )

    assert failure == "failed at <local-path>"


def test_public_failure_retains_sanitized_missing_library_diagnostic() -> None:
    failure = _clean_failure(
        RuntimeError(
            "BrowserType.launch failed\n"
            "/home/kieran/browser: error while loading shared libraries: "
            "libexample.so: cannot open shared object file"
        )
    )

    assert failure == (
        "BrowserType.launch failed: <local-path> error while loading shared "
        "libraries: libexample.so: cannot open shared object file"
    )


def test_browser_audit_cli_writes_passing_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    report = {
        "summary": {
            "requested": 2,
            "passed": 2,
            "failed": 0,
            "release_gate_passed": True,
        },
        "browsers": {
            "chromium": {"status": "passed"},
            "firefox": {"status": "passed"},
        },
    }
    monkeypatch.setattr(
        "pikvm_agent.harness.browser_matrix.run_browser_matrix_audit",
        lambda *args, **kwargs: report,
    )
    output = tmp_path / "matrix.json"

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "browser-audit",
            "--browsers",
            "chromium,firefox",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert "2/2 passed" in result.stdout
    assert json.loads(output.read_text()) == report


def test_browser_audit_cli_fails_closed_on_engine_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = {
        "summary": {
            "requested": 1,
            "passed": 0,
            "failed": 1,
            "release_gate_passed": False,
        },
        "browsers": {
            "webkit": {
                "status": "failed",
                "failure": "browser executable is missing",
            }
        },
    }
    monkeypatch.setattr(
        "pikvm_agent.harness.browser_matrix.run_browser_matrix_audit",
        lambda *args, **kwargs: report,
    )

    result = CliRunner().invoke(
        app,
        ["harness", "browser-audit", "--browsers", "webkit"],
    )

    assert result.exit_code == 1
    assert "webkit: browser executable is missing" in result.stderr
