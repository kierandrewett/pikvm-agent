from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pikvm_agent.harness.daemon_media import DaemonMediaTransport
from pikvm_agent.harness.media_transaction import (
    MediaMutationAmbiguousError,
    MediaMutationDefiniteError,
)


@pytest.mark.asyncio
async def test_daemon_media_transport_uses_session_bound_exact_byte_routes(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, dict[str, object], bytes]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        calls.append(
            (
                request.method,
                request.url.path,
                dict(request.url.params),
                body,
            )
        )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "adapter": "pikvm",
                    "supported": True,
                    "machine_fingerprint": "machine-7",
                    "control_epoch": 4,
                    "drive_online": True,
                    "drive_busy": False,
                    "connected": False,
                    "selected_image": None,
                    "images": [],
                    "storage_free_bytes": 4096,
                },
            )
        return httpx.Response(200, json={"outcome": "confirmed"})

    image = tmp_path / "media.iso"
    image.write_bytes(b"\x00exact\xffmedia")
    transport = DaemonMediaTransport(
        "http://daemon.invalid",
        transport=httpx.MockTransport(handler),
    )

    state = await transport.inspect("session-lab")
    await transport.upload("session-lab", "pikvm-abc.iso", image)
    await transport.select("session-lab", "pikvm-abc.iso")
    await transport.connect("session-lab")
    await transport.disconnect("session-lab")
    await transport.select("session-lab", None)
    await transport.remove("session-lab", "pikvm-abc.iso")
    await transport.aclose()

    assert state.machine_fingerprint == "machine-7"
    assert calls == [
        ("GET", "/sessions/session-lab/media", {}, b""),
        (
            "POST",
            "/sessions/session-lab/media/upload",
            {"image": "pikvm-abc.iso"},
            b"\x00exact\xffmedia",
        ),
        (
            "POST",
            "/sessions/session-lab/media/select",
            {},
            b'{"image":"pikvm-abc.iso","cdrom":true,"read_only":true}',
        ),
        (
            "POST",
            "/sessions/session-lab/media/connected",
            {},
            b'{"connected":true}',
        ),
        (
            "POST",
            "/sessions/session-lab/media/connected",
            {},
            b'{"connected":false}',
        ),
        (
            "POST",
            "/sessions/session-lab/media/select",
            {},
            b'{"image":null,"cdrom":true,"read_only":true}',
        ),
        (
            "POST",
            "/sessions/session-lab/media/remove",
            {},
            b'{"image":"pikvm-abc.iso"}',
        ),
    ]
    assert all("write_remote" not in path for _, path, _, _ in calls)


@pytest.mark.asyncio
async def test_daemon_media_transport_classifies_mutation_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/select"):
            return httpx.Response(409, json={"detail": "drive busy"})
        return httpx.Response(503, json={"detail": "response lost"})

    transport = DaemonMediaTransport(
        "http://daemon.invalid",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(MediaMutationDefiniteError, match="drive busy"):
        await transport.select("session-lab", "pikvm-abc.iso")
    with pytest.raises(MediaMutationAmbiguousError, match="response lost"):
        await transport.connect("session-lab")
    await transport.aclose()
