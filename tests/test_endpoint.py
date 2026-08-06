"""Local services reached over a unix socket rather than a loopback port.

The daemon and the operator harness are local, spawned by whoever runs them and
answering only to a bearer token.  A fixed loopback port bought them nothing and
cost the usual thing: an orphan of a previous run holding 47615 or 47616 while a
new run cannot bind.  ``daemon --uds`` and ``harness serve --uds`` take them off
ports; these tests cover the other half, which is every client that has to reach
them there.

Measured against a real uvicorn on a real socket in ``/tmp`` (see
``test_a_unix_endpoint_reaches_a_real_socket``): the client built from
``httpx_client_kwargs`` gets 200 and the body the app returned, with no host or
port anywhere in the call.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.config import require_daemon_url
from pikvm_agent.endpoint import (
    UNIX_BASE_URL,
    endpoint_socket_path,
    httpx_client_kwargs,
    is_unix_endpoint,
    unix_endpoint,
)
from pikvm_agent.harness.client_setup import (
    HARNESS_ENDPOINT_ENV,
    harness_base_url,
)
from pikvm_agent.harness.config import HarnessSettings


def test_an_endpoint_is_read_by_prefix_not_by_url_parsing() -> None:
    # The desktop app keeps its sockets beside its settings, and that directory
    # has a space in it on every platform it ships to. URL parsing would encode
    # the space and hand a filesystem call a path that does not exist.
    spaced = "unix:/home/you/.config/PiKVM Desktop/run/daemon.sock"

    assert is_unix_endpoint(spaced)
    assert endpoint_socket_path(spaced) == (
        "/home/you/.config/PiKVM Desktop/run/daemon.sock"
    )
    assert unix_endpoint("/run/user/1000/d.sock") == "unix:/run/user/1000/d.sock"
    assert not is_unix_endpoint("http://127.0.0.1:47615")
    assert endpoint_socket_path("http://127.0.0.1:47615") is None
    with pytest.raises(ValueError, match="names no socket path"):
        endpoint_socket_path("unix:")


def test_a_tcp_endpoint_builds_exactly_the_call_it_always_did() -> None:
    # No transport key at all, rather than transport=None. A test that stands in
    # for httpx.AsyncClient and injects its own transport would otherwise get
    # two values for one argument, which is how this was first found.
    assert httpx_client_kwargs("http://127.0.0.1:47615/") == {
        "base_url": "http://127.0.0.1:47615"
    }


def test_a_unix_endpoint_becomes_a_uds_transport() -> None:
    kwargs = httpx_client_kwargs("unix:/tmp/pikvm-daemon.sock")

    assert kwargs["base_url"] == UNIX_BASE_URL
    assert isinstance(kwargs["transport"], httpx.AsyncHTTPTransport)
    assert isinstance(
        httpx_client_kwargs("unix:/tmp/x.sock", sync=True)["transport"],
        httpx.HTTPTransport,
    )


def test_an_explicit_transport_is_never_replaced() -> None:
    # That argument is how the tests point an adapter at an in-process ASGI app.
    # A socket endpoint must not quietly swap it for a real connection to a real
    # file, or every one of those tests would start touching the filesystem.
    injected = httpx.AsyncHTTPTransport()

    assert httpx_client_kwargs("unix:/tmp/x.sock", injected) == {
        "base_url": UNIX_BASE_URL,
        "transport": injected,
    }


def test_the_daemon_selection_accepts_a_socket_and_still_refuses_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PIKVM_AGENT_DAEMON", raising=False)

    assert (
        require_daemon_url("unix:/home/you/.config/PiKVM Desktop/run/d.sock")
        == "unix:/home/you/.config/PiKVM Desktop/run/d.sock"
    )
    # A relative path resolves against whatever the client's working directory
    # happens to be, which is not a selection of anything.
    with pytest.raises(ValueError, match="absolute socket path"):
        require_daemon_url("unix:run/daemon.sock")
    with pytest.raises(ValueError, match="names no socket path"):
        require_daemon_url("unix:")
    with pytest.raises(ValueError, match="there is no implicit target"):
        require_daemon_url()


def _harness_settings(listen: str = "127.0.0.1:47616") -> HarnessSettings:
    return HarnessSettings.model_validate(
        {
            "listen": listen,
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


def test_the_harness_endpoint_comes_from_whoever_started_the_harness() -> None:
    # The config's listen is only where the harness would bind by default. The
    # desktop moves it - to a free port, and now onto a socket - without
    # rewriting a file that carries the operator's provider settings, so a child
    # deriving the address from the YAML would call an address nothing answers.
    settings = _harness_settings()

    assert harness_base_url(settings, environ={}) == "http://127.0.0.1:47616"
    assert (
        harness_base_url(
            settings,
            environ={HARNESS_ENDPOINT_ENV: "unix:/tmp/pikvm/harness.sock"},
        )
        == "unix:/tmp/pikvm/harness.sock"
    )
    assert (
        harness_base_url(
            settings,
            environ={HARNESS_ENDPOINT_ENV: "http://127.0.0.1:47618/"},
        )
        == "http://127.0.0.1:47618"
    )
    with pytest.raises(ValueError, match="absolute socket path"):
        harness_base_url(settings, environ={HARNESS_ENDPOINT_ENV: "unix:rel.sock"})
    with pytest.raises(ValueError, match="HTTP.S. origin"):
        harness_base_url(settings, environ={HARNESS_ENDPOINT_ENV: "127.0.0.1:1"})


def test_harness_serve_takes_a_socket_and_refuses_two_places_to_listen(
    tmp_path: Path,
) -> None:
    config = tmp_path / "harness.yaml"
    config.write_text("providers: {}\n")

    help_output = CliRunner().invoke(app, ["harness", "serve", "--help"]).output
    assert "--uds" in help_output

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "serve",
            "--config",
            str(config),
            "--uds",
            str(tmp_path / "harness.sock"),
            "--listen",
            "127.0.0.1:47616",
        ],
    )

    assert result.exit_code == 2
    assert "two different" in result.output


def test_a_stale_socket_file_does_not_block_the_next_start(
    tmp_path: Path,
) -> None:
    # A socket file outlives the process that made it and uvicorn will not bind
    # over an existing path, so a harness killed rather than closed would leave
    # one behind that stops every later start - the socket version of exactly
    # the stale-listener problem this move is meant to end. The config is
    # deliberately unloadable, so the run stops right after the unlink.
    stale = tmp_path / "harness.sock"
    stale.write_text("")
    config = tmp_path / "harness.yaml"
    # A loadable config with a bind that ensure_safe_bind refuses, so the run
    # stops after the unlink and before uvicorn is asked for anything.
    config.write_text(
        yaml.safe_dump(
            _harness_settings(listen="203.0.113.5:47616").model_dump(
                mode="json"
            )
        )
    )

    CliRunner().invoke(
        app,
        ["harness", "serve", "--config", str(config), "--uds", str(stale)],
    )

    assert not stale.exists()


def test_a_unix_endpoint_reaches_a_real_socket(tmp_path: Path) -> None:
    """The whole point, end to end: no port, and a 200 from the app."""

    import uvicorn

    async def endpoint(scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    # /tmp, not tmp_path: sun_path holds 108 bytes and pytest's temporary
    # directory names are long enough to overflow it on some runners.
    socket_path = f"/tmp/pikvm-endpoint-test-{os.getpid()}.sock"
    server = uvicorn.Server(
        uvicorn.Config(endpoint, uds=socket_path, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            threading.Event().wait(0.05)
        assert server.started, "uvicorn never bound the socket"
        assert socket.AF_UNIX  # the transport this whole file is about

        async def fetch() -> httpx.Response:
            async with httpx.AsyncClient(
                **httpx_client_kwargs(unix_endpoint(socket_path))
            ) as client:
                return await client.get("/healthz")

        response = asyncio.run(fetch())
        assert response.status_code == 200
        assert response.text == "ok"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if os.path.exists(socket_path):
            os.unlink(socket_path)
