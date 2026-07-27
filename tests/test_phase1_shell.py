"""Phase 1 — "own the shell" acceptance.

    * pikvm_start_task creates a session
    * pikvm_observe returns frame_id / world_version / screenshot_path
    * no OmniParser / OpenRouter required
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport

import pikvm_agent.mcp_server as mcp_server
from pikvm_agent.config import AppConfig
from pikvm_agent.daemon import BurstRequest, create_app
from pikvm_agent.executor.burst import MAX_TYPE_TEXT_CHARS
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


async def test_model_facing_raw_mcp_excludes_approval_tools_and_declares_risk() -> None:
    tools = {
        tool.name: tool for tool in await mcp_server.mcp.list_tools()
    }

    assert "pikvm_resolve_approval" not in tools
    assert "pikvm_autonomous_approve" not in tools
    assert all("media" not in name and "upload" not in name for name in tools)
    assert tools["pikvm_screenshot"].annotations.readOnlyHint is True
    assert tools["pikvm_parse_screen"].annotations.readOnlyHint is True
    assert tools["pikvm_run_burst"].annotations.readOnlyHint is False
    assert tools["pikvm_abort"].annotations.destructiveHint is True
    assert tools["pikvm_panic_stop"].annotations.destructiveHint is True
    freshness_fields = {
        "based_on_world_version",
        "based_on_control_epoch",
        "idempotency_key",
    }
    for name in (
        "pikvm_run_burst",
        "pikvm_run_playbook",
        "pikvm_key",
        "pikvm_type_text",
        "pikvm_click",
        "pikvm_scroll",
    ):
        assert freshness_fields <= set(tools[name].inputSchema["required"])
        key_schema = tools[name].inputSchema["properties"]["idempotency_key"]
        assert key_schema["minLength"] == 1
        assert key_schema["maxLength"] == 160


def test_daemon_burst_request_rejects_blank_idempotency_key() -> None:
    with pytest.raises(ValueError):
        BurstRequest.model_validate(
            {
                "actions": [{"type": "key", "keys": ["KeyA"]}],
                "based_on_world_version": 1,
                "based_on_control_epoch": 1,
                "idempotency_key": " ",
            }
        )


def test_daemon_http_endpoints(app_config: AppConfig) -> None:
    app = create_app(app_config)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True}
        sid = client.post("/sessions", json={"task": "t"}).json()["session_id"]
        # Plain GET is read-only — no capture yet, so no frame (polling must not capture).
        poll = client.get(f"/sessions/{sid}").json()
        assert poll["frame_id"] is None and poll["status"] == "running"
        bad_burst = client.post(
            f"/sessions/{sid}/burst",
            json={
                "actions": [
                    {"type": "type_text", "text": "x" * (MAX_TYPE_TEXT_CHARS + 1)}
                ],
                "return_screenshot": False,
                "idempotency_key": "invalid-payload-preflight",
            },
        ).json()
        assert bad_burst["status"] == "failed"
        assert "bad burst: type_text action 0" in bad_burst["error"]
        assert client.get(f"/sessions/{sid}").json()["frame_id"] is None
        # capture=true takes a fresh screenshot (the pikvm_observe path).
        obs = client.get(f"/sessions/{sid}?capture=true").json()
        assert obs["frame_id"] == 1 and obs["world_version"] == 1
        missing_freshness = client.post(
            f"/sessions/{sid}/burst",
            json={
                "actions": [{"type": "key", "keys": ["KeyA"]}],
                "idempotency_key": "http-missing-freshness",
            },
        ).json()
        assert missing_freshness["status"] == "freshness_required"
        assert missing_freshness["control_epoch"] == obs["control_epoch"]
        # A subsequent read-only poll returns that last frame WITHOUT advancing it.
        assert client.get(f"/sessions/{sid}").json()["frame_id"] == 1
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
            "pikvm_autonomous_continue",
            "pikvm_autonomous_start",
            "pikvm_click",
            "pikvm_export_memory_update",
            "pikvm_find_text",
            "pikvm_key",
            "pikvm_ocr_region",
        "pikvm_open",
        "pikvm_panic_stop",
        "pikvm_parse_screen",
        "pikvm_run_burst",
        "pikvm_run_playbook",
        "pikvm_screenshot",
            "pikvm_scroll",
            "pikvm_type_text",
        ]
        annotations = {
            tool.name: tool.annotations
            for tool in await mcp_server.mcp.list_tools()
        }
        assert annotations["pikvm_screenshot"].readOnlyHint is True
        assert annotations["pikvm_parse_screen"].readOnlyHint is True
        assert annotations["pikvm_run_burst"].readOnlyHint is False
        assert annotations["pikvm_abort"].destructiveHint is True
        assert annotations["pikvm_panic_stop"].destructiveHint is True
        from mcp.server.fastmcp.utilities.types import Image

        def _state(result):
            # Screen-producing tools return [Image, json-state]; the image is INLINE so the
            # controller never reads a file, and screenshot_path is intentionally absent.
            assert isinstance(result, list) and isinstance(result[0], Image)
            state = json.loads(result[-1])
            assert "screenshot_path" not in state
            return state

        started = await mcp_server.pikvm_autonomous_start("open the README")
        obs = _state(await mcp_server.pikvm_screenshot(session_id=started["session_id"]))
        assert obs["frame_id"] == 1

        # Fast path: a burst runs locally and returns the screen INLINE + control_epoch.
        opened = _state(await mcp_server.pikvm_open("direct"))
        sid = opened["session_id"]
        assert "control_epoch" in opened
        res = _state(await mcp_server.pikvm_run_burst(sid, [
            {"type": "key", "keys": ["CTRL", "P"]},
            {"type": "type_text", "text": "readme.md", "method": "print"},
            {"type": "key", "keys": ["ENTER"]},
        ],
            based_on_world_version=opened["world_version"],
            based_on_control_epoch=opened["control_epoch"],
            idempotency_key="phase1-fast-path",
        ))
        assert res["status"] == "needs_approval"
        assert res["approval_request"]["risk"] == "unknown"
        approved = _state(
            await mcp_server.pikvm_resolve_approval(
                sid,
                res["approval_request"]["approval_id"],
                {"type": "approve"},
            )
        )
        assert approved["status"] == "completed"
        assert approved["completed_actions"] == 3
    finally:
        await rt.aclose()


async def test_trusted_harness_child_gets_approval_tools_with_danger_metadata() -> None:
    trusted = mcp_server.VisibleFastMCP("trusted-approval-test")

    mcp_server.register_trusted_approval_tools(trusted)

    tools = {tool.name: tool for tool in await trusted.list_tools()}
    assert set(tools) == {
        "pikvm_autonomous_approve",
        "pikvm_resolve_approval",
    }
    assert tools["pikvm_resolve_approval"].annotations.destructiveHint is True
    assert tools["pikvm_autonomous_approve"].annotations.destructiveHint is True


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
    task = asyncio.ensure_future(mcp_server.pikvm_autonomous_continue("s_abc"))
    await asyncio.sleep(0.05)  # let it reach the hanging continue
    assert calls == ["/sessions/s_abc/continue"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)  # let the abort land

    assert "/sessions/s_abc/abort" in calls  # cancellation fired the abort


async def test_mcp_burst_preflight_rejects_oversized_type_text(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_post(path, json=None, timeout=60.0):
        calls.append(path)
        raise AssertionError("oversized type_text should not reach the daemon")

    monkeypatch.setattr(mcp_server, "_post", fake_post)

    result = await mcp_server.pikvm_run_burst(
        "s_abc",
        [{"type": "type_text", "text": "x" * (MAX_TYPE_TEXT_CHARS + 1), "method": "print"}],
        based_on_world_version=1,
        based_on_control_epoch=1,
        idempotency_key="oversized-preflight",
    )
    state = json.loads(result[-1])

    assert calls == []
    assert state["status"] == "failed"
    assert "bad burst: type_text action 0" in state["error"]


async def test_mcp_burst_preflight_rejects_encoded_transfer_hack(
    monkeypatch,
) -> None:
    calls: list[str] = []

    async def fake_post(path, json=None, timeout=60.0):
        calls.append(path)
        raise AssertionError("encoded transfer payload should not reach the daemon")

    monkeypatch.setattr(mcp_server, "_post", fake_post)
    encoded_chunk = (
        "ZXhhY3QtcGF5bG9hZC10aGF0LW11c3Qtbm90LWJlLXR5cGVkLXRo"
        "cm91Z2gtcmF3LUhJRC1hcy1hLXRyYW5zZmVyLWhhY2s="
    )

    result = await mcp_server.pikvm_run_burst(
        "s_abc",
        [
            {
                "type": "type_text",
                "text": f"printf '%s' '{encoded_chunk}' >> /tmp/payload.b64",
                "context": "terminal",
            }
        ],
        based_on_world_version=1,
        based_on_control_epoch=1,
        idempotency_key="encoded-preflight",
    )
    state = json.loads(result[-1])

    assert calls == []
    assert state["status"] == "failed"
    assert "encoded file-transfer payload" in state["error"]
