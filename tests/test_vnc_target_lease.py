"""Cross-process ownership for disposable VNC targets."""

from __future__ import annotations

import asyncio
import io
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from PIL import Image

from pikvm_agent.harness.vnc_pikvm_api import VncDotoolTransport
from pikvm_agent.harness.vnc_target_lease import (
    VncTargetAlreadyLeased,
    VncTargetLease,
)


def test_equivalent_endpoint_spellings_share_one_exclusive_lease(
    tmp_path: Path,
) -> None:
    first = VncTargetLease.acquire("Example.invalid:5900", lock_dir=tmp_path)
    try:
        with pytest.raises(
            VncTargetAlreadyLeased,
            match="already controlled by another local lab",
        ):
            VncTargetLease.acquire("example.invalid::5900", lock_dir=tmp_path)
    finally:
        first.release()

    replacement = VncTargetLease.acquire(
        "example.invalid::5900",
        lock_dir=tmp_path,
    )
    replacement.release()

    entries = list(tmp_path.iterdir())
    assert len(entries) == 1
    assert "example.invalid" not in entries[0].name
    assert "example.invalid" not in entries[0].read_text()


def test_lease_excludes_a_separate_adapter_process(tmp_path: Path) -> None:
    probe = """
import sys
from pathlib import Path
from pikvm_agent.harness.vnc_target_lease import (
    VncTargetAlreadyLeased,
    VncTargetLease,
)
try:
    lease = VncTargetLease.acquire("process.invalid:5900", lock_dir=Path(sys.argv[1]))
except VncTargetAlreadyLeased:
    raise SystemExit(23)
lease.release()
"""
    first = VncTargetLease.acquire("process.invalid::5900", lock_dir=tmp_path)
    try:
        refused = subprocess.run(
            [sys.executable, "-c", probe, str(tmp_path)],
            check=False,
        )
        assert refused.returncode == 23
    finally:
        first.release()

    accepted = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path)],
        check=False,
    )
    assert accepted.returncode == 0


async def test_transport_refuses_second_adapter_before_vnc_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIKVM_LAB_TARGET_LEASE_DIR", str(tmp_path))
    connect_calls: list[str] = []

    class Client:
        def keyUp(self, _key: str) -> None:
            return

        def captureScreen(self, output: io.BytesIO, *, format: str) -> None:
            assert format == "PNG"
            Image.new("RGB", (16, 9), "black").save(output, "PNG")

        def disconnect(self) -> None:
            return

    def connect(
        endpoint: str,
        _password: str | None,
        _factory: type[object],
        *,
        timeout: int,
        username: str | None,
    ) -> Client:
        assert timeout == 30
        assert username is None
        connect_calls.append(endpoint)
        return Client()

    api = SimpleNamespace(connect=connect, shutdown=lambda: None)
    client = SimpleNamespace(VNCDoToolFactory=object)
    package = ModuleType("vncdotool")
    package.api = api  # type: ignore[attr-defined]
    package.client = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "vncdotool", package)

    first = VncDotoolTransport("Example.invalid:5900")
    second = VncDotoolTransport("example.invalid::5900")
    await first.connect()
    try:
        with pytest.raises(VncTargetAlreadyLeased):
            await second.connect()
        assert connect_calls == ["Example.invalid::5900"]
    finally:
        await first.close()

    await second.connect()
    await second.close()
    assert connect_calls == [
        "Example.invalid::5900",
        "example.invalid::5900",
    ]


async def test_failed_vnc_connection_does_not_strand_the_target_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIKVM_LAB_TARGET_LEASE_DIR", str(tmp_path))

    def refuse_connect(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic connection failure")

    api = SimpleNamespace(connect=refuse_connect)
    client = SimpleNamespace(VNCDoToolFactory=object)
    package = ModuleType("vncdotool")
    package.api = api  # type: ignore[attr-defined]
    package.client = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "vncdotool", package)

    transport = VncDotoolTransport("unused.invalid:5900")
    with pytest.raises(OSError, match="synthetic connection failure"):
        await transport.connect()

    replacement = VncTargetLease.acquire(
        "unused.invalid::5900",
        lock_dir=tmp_path,
    )
    replacement.release()


async def test_cancelled_connect_holds_lease_until_worker_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIKVM_LAB_TARGET_LEASE_DIR", str(tmp_path))
    started = threading.Event()
    finish = threading.Event()
    disconnected = threading.Event()

    class Client:
        def disconnect(self) -> None:
            disconnected.set()

    def connect(*_args: object, **_kwargs: object) -> Client:
        started.set()
        assert finish.wait(timeout=2)
        return Client()

    api = SimpleNamespace(connect=connect)
    client = SimpleNamespace(VNCDoToolFactory=object)
    package = ModuleType("vncdotool")
    package.api = api  # type: ignore[attr-defined]
    package.client = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "vncdotool", package)

    transport = VncDotoolTransport("unused.invalid:5900")
    connecting = asyncio.create_task(transport.connect())
    assert await asyncio.to_thread(started.wait, 1)
    connecting.cancel()
    await asyncio.sleep(0)
    assert not connecting.done()

    with pytest.raises(VncTargetAlreadyLeased):
        VncTargetLease.acquire("unused.invalid::5900", lock_dir=tmp_path)

    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    assert disconnected.is_set()

    replacement = VncTargetLease.acquire(
        "unused.invalid::5900",
        lock_dir=tmp_path,
    )
    replacement.release()
