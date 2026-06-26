"""Phase 1 — "own the shell" acceptance.

    * pikvm_start_task creates a session
    * pikvm_observe returns frame_id / world_version / screenshot_path
    * no OmniParser / OpenRouter required
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport

import pikvm_agent.mcp_server as mcp_server
from pikvm_agent.config import AppConfig
from pikvm_agent.daemon import create_app
from pikvm_agent.runtime import Runtime


async def test_runtime_start_observe_abort(runtime: Runtime) -> None:
    started = await runtime.start_session("open the README")
    sid = started["session_id"]
    assert sid.startswith("s_") and started["status"] == "running"

    obs = await runtime.get_session_summary(sid)
    assert obs["frame_id"] == 1
    assert obs["world_version"] == 1
    assert os.path.exists(obs["screenshot_path"])
    assert obs["keyboard_state"]["layout"] == "us"

    # observing again advances the frame id, not the world version
    obs2 = await runtime.get_session_summary(sid)
    assert obs2["frame_id"] == 2
    assert obs2["world_version"] == 1

    aborted = await runtime.abort_session(sid, "stopped")
    assert aborted["status"] == "failed"


async def test_world_version_bumps_on_screen_change(runtime: Runtime) -> None:
    started = await runtime.start_session("t")
    sid = started["session_id"]
    o1 = await runtime.get_session_summary(sid)
    runtime.backend.set_screen("a modal appeared", bg=(210, 30, 30))
    o2 = await runtime.get_session_summary(sid)
    assert o2["world_version"] == o1["world_version"] + 1


def test_daemon_http_endpoints(app_config: AppConfig) -> None:
    app = create_app(app_config)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True}
        sid = client.post("/sessions", json={"task": "t"}).json()["session_id"]
        obs = client.get(f"/sessions/{sid}").json()
        assert obs["frame_id"] == 1 and obs["world_version"] == 1
        assert client.get("/sessions/does-not-exist").status_code == 404


async def test_mcp_facade_forwards_to_daemon(app_config: AppConfig,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(app_config)
    rt = await Runtime.from_config(app_config)
    app.state.runtime = rt  # set state directly; ASGITransport doesn't run lifespan
    transport = ASGITransport(app=app)
    monkeypatch.setattr(
        mcp_server,
        "_daemon_client",
        lambda timeout: httpx.AsyncClient(transport=transport, base_url="http://daemon", timeout=timeout),
    )
    try:
        names = sorted(t.name for t in await mcp_server.mcp.list_tools())
        assert names == [
            "pikvm_abort",
            "pikvm_approve",
            "pikvm_continue",
            "pikvm_export_memory_update",
            "pikvm_observe",
            "pikvm_start_task",
        ]
        started = await mcp_server.pikvm_start_task("open the README")
        obs = await mcp_server.pikvm_observe(session_id=started["session_id"])
        assert obs["frame_id"] == 1 and os.path.exists(obs["screenshot_path"])
    finally:
        await rt.aclose()


async def test_cancel_continue_aborts_session(monkeypatch) -> None:
    # Cancelling a blocking call (e.g. Esc in Claude) must abort the daemon session,
    # so interrupting the agent actually stops the machine instead of leaving the
    # daemon driving on its own.
    calls: list[str] = []

    async def fake_post(path, json=None, timeout=60.0):
        calls.append(path)
        if path.endswith("/continue"):
            await asyncio.sleep(5)  # hang so we can cancel mid-run
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_post", fake_post)
    task = asyncio.ensure_future(mcp_server.pikvm_continue("s_abc"))
    await asyncio.sleep(0.05)  # let it reach the hanging continue
    assert calls == ["/sessions/s_abc/continue"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)  # let the abort land

    assert "/sessions/s_abc/abort" in calls  # cancellation fired the abort
