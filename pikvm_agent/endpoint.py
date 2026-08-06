"""How a local pikvm-agent service is reached: a TCP origin, or a unix socket.

The daemon and the operator harness are local services. Their callers - the raw
MCP child, the managed MCP facade, the harness's own media and live-frame
adapters - have always been handed an ``http://host:port`` and built an httpx
client straight from it. That works, and it costs the usual things: a fixed
loopback port can be held by an orphan of a previous run, and anything on the
box can connect to it.

A unix socket has neither problem, so both services can now be asked to listen
on one (``daemon --uds`` and ``harness serve --uds``). One string carries either
form, so it stays readable in a config file, in a log line and in the
environment variables that carry it between processes::

    http://127.0.0.1:47615                       a TCP origin, still supported
    unix:/home/you/.config/PiKVM Desktop/d.sock  a socket path, spaces and all

Parsed by prefix rather than through ``urlsplit``, deliberately: the desktop
app's settings directory has a space in it, and URL parsing percent-encodes that
into a path no filesystem call can open.

The socket is not a boundary on its own. uvicorn chmods it to 0o666 after
binding, so what gates access is the directory the caller chose, and the bearer
token is still the real check. See ``cli.py``'s ``--uds`` notes.
"""

from __future__ import annotations

from typing import Any

UNIX_SCHEME = "unix:"

# httpx needs *a* URL to resolve relative paths against, and a socket has no
# host. uvicorn does not care what the Host header says, but Starlette builds
# absolute URLs from it, so it has to be something rather than nothing.
UNIX_BASE_URL = "http://localhost"


def is_unix_endpoint(value: str) -> bool:
    """True for anything this module reads as a socket rather than a host."""

    return value.strip().startswith(UNIX_SCHEME)


def unix_endpoint(socket_path: str) -> str:
    """Build the endpoint string for a socket path, so nobody hand-splices it."""

    return f"{UNIX_SCHEME}{socket_path}"


def endpoint_socket_path(value: str) -> str | None:
    """The socket path of a unix endpoint, or ``None`` for a TCP origin.

    Raises for a socket endpoint with no path: an empty path otherwise fails at
    connect time with a message that names neither the setting nor the value
    that produced it.
    """

    raw = value.strip()
    if not is_unix_endpoint(raw):
        return None
    socket_path = raw[len(UNIX_SCHEME):]
    if not socket_path:
        raise ValueError(f'endpoint "{value}" names no socket path')
    return socket_path


def httpx_client_kwargs(
    endpoint: str,
    transport: Any | None = None,
    *,
    sync: bool = False,
) -> dict[str, Any]:
    """The ``base_url`` (and ``transport``) for one httpx client on ``endpoint``.

    Returned as kwargs rather than a pair so that a TCP endpoint with no
    transport produces exactly the call the caller made before this existed.
    Passing ``transport=None`` explicitly is not the same thing: a test that
    stands in for ``httpx.AsyncClient`` and injects its own transport then gets
    two values for one argument.

    An explicit ``transport`` always wins - that argument is how the tests point
    an adapter at an in-process ASGI app, and a socket endpoint must not quietly
    replace it with a real connection to a real file.
    """

    socket_path = endpoint_socket_path(endpoint)
    if socket_path is None:
        kwargs: dict[str, Any] = {"base_url": endpoint.strip().rstrip("/")}
    elif transport is None:
        import httpx

        factory = httpx.HTTPTransport if sync else httpx.AsyncHTTPTransport
        return {"base_url": UNIX_BASE_URL, "transport": factory(uds=socket_path)}
    else:
        kwargs = {"base_url": UNIX_BASE_URL}
    if transport is not None:
        kwargs["transport"] = transport
    return kwargs
