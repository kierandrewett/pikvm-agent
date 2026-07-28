"""Fail-closed target selection for MCP startup and the out-of-band brake."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

import pikvm_agent.mcp_server as mcp_server
from pikvm_agent.cli import app
from pikvm_agent.config import require_daemon_url


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


def test_daemon_target_is_required_and_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PIKVM_AGENT_DAEMON", raising=False)
    with pytest.raises(ValueError, match="there is no implicit target"):
        require_daemon_url()
    with pytest.raises(ValueError, match="without embedded credentials"):
        require_daemon_url("http://user:secret@127.0.0.1:48765")
    with pytest.raises(ValueError, match=r"HTTP\(S\) daemon URL"):
        require_daemon_url("vnc://127.0.0.1:48765")
    assert (
        require_daemon_url("http://127.0.0.1:48765/")
        == "http://127.0.0.1:48765"
    )


def test_raw_mcp_client_refuses_to_guess_a_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PIKVM_AGENT_DAEMON", raising=False)
    with pytest.raises(ValueError, match="there is no implicit target"):
        mcp_server._daemon_client(1.0)


def test_mcp_cli_refuses_missing_target_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PIKVM_AGENT_DAEMON", raising=False)

    result = CliRunner().invoke(app, ["mcp"])

    assert result.exit_code == 2
    assert "MCP startup refused" in result.output
    assert "there is no implicit target" in result.output
    assert "Traceback" not in result.output


def test_mcp_cli_refuses_selected_target_without_visibility_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIKVM_AGENT_DAEMON", "http://127.0.0.1:48765")
    monkeypatch.setenv(
        "PIKVM_AGENT_DAEMON_TOKEN",
        "daemon-action-token-0123456789abcdef",
    )
    monkeypatch.delenv("PIKVM_HARNESS_OBSERVER_URL", raising=False)
    monkeypatch.delenv("PIKVM_HARNESS_OBSERVER_TOKEN", raising=False)
    monkeypatch.delenv("PIKVM_AGENT_TRUSTED_APPROVAL_CLIENT", raising=False)

    result = CliRunner().invoke(app, ["mcp"])

    assert result.exit_code == 2
    assert "MCP startup refused" in result.output
    assert "operator visibility is not configured" in result.output
    assert "Traceback" not in result.output


def test_panic_stop_without_target_makes_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PIKVM_AGENT_DAEMON", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(httpx, "post", lambda url, **_: calls.append(url))

    result = CliRunner().invoke(app, ["panic-stop"])

    assert result.exit_code == 2
    assert "panic-stop refused" in result.output
    assert "there is no implicit target" in result.output
    assert calls == []


def test_panic_stop_confirms_selected_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    def post(url: str, *, timeout: float) -> _Response:
        calls.append((url, timeout))
        return _Response(
            {
                "ok": True,
                "quiesced": True,
                "in_flight_actions": 0,
                "stopped": ["s_one"],
                "machine": {
                    "alias": "Isolated test machine",
                    "fingerprint": "target:0123456789abcdef",
                },
            }
        )

    monkeypatch.setattr(httpx, "post", post)
    result = CliRunner().invoke(
        app,
        ["panic-stop", "--daemon", "http://127.0.0.1:48765"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("http://127.0.0.1:48765/panic-stop", 10.0)]
    assert "PANIC STOP confirmed" in result.output
    assert "halted 1 session" in result.output
    assert "Isolated test machine · target:0123456789abcdef" in result.output


def test_panic_stop_fails_if_hid_does_not_quiesce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *_args, **_kwargs: _Response(
            {
                "ok": False,
                "quiesced": False,
                "in_flight_actions": 1,
                "stopped": ["s_one"],
                "machine": {
                    "alias": "Isolated test machine",
                    "fingerprint": "target:0123456789abcdef",
                },
            }
        ),
    )

    result = CliRunner().invoke(
        app,
        ["panic-stop", "--daemon", "http://127.0.0.1:48765"],
    )

    assert result.exit_code == 1
    assert "did not confirm HID quiescence" in result.output
    assert "PANIC STOP confirmed" not in result.output
