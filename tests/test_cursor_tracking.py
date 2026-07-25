"""Cursor position is stored on every mouse op + surfaced; distrusted on client change."""

from __future__ import annotations

from pikvm_agent.config import PikvmConfig
from pikvm_agent.pikvm.client import PiKVMBackend
from pikvm_agent.pikvm.fake import FakeBackend


async def test_fake_tracks_cursor_on_move_and_click() -> None:
    be = FakeBackend()
    assert be.cursor()["trusted"] is False  # not positioned yet
    await be.move_mouse(640, 480)
    assert be.cursor() == {"x": 640, "y": 480, "trusted": True, "other_clients": 0}
    await be.click(100, 200)
    assert be.cursor()["x"] == 100 and be.cursor()["y"] == 200


async def test_real_backend_records_cursor_and_clamps() -> None:
    be = PiKVMBackend(PikvmConfig(base_url="http://127.0.0.1"))
    be.dims = {"width": 1280, "height": 720}
    be._set_cursor(50, 60)
    assert be.cursor() == {"x": 50, "y": 60, "trusted": True, "other_clients": 0}
    # out-of-frame coordinates clamp into the frame.
    be._set_cursor(5000, -10)
    c = be.cursor()
    assert c["x"] == 1279 and c["y"] == 0


async def test_new_client_distrusts_tracked_position() -> None:
    be = PiKVMBackend(PikvmConfig(base_url="http://127.0.0.1"))
    be._set_cursor(300, 300)
    assert be.cursor()["trusted"] is True

    # First clients event establishes the count (1 = just us) — still trusted.
    be.hid.state.clients = {"count": 1}
    await be._on_kvmd_event("clients", None)
    assert be.cursor()["trusted"] is True and be.other_clients() == 0

    # A second client connects — it may have moved the mouse, so distrust our position.
    be.hid.state.clients = {"count": 2}
    await be._on_kvmd_event("clients", None)
    assert be.cursor()["trusted"] is False and be.other_clients() == 1


async def test_external_cursor_report_updates_tracked_position(runtime) -> None:
    # The desktop live-view reports a manual move (norm ±32767); the daemon's tracked
    # cursor follows it and is trusted.
    sid = (await runtime.start_session("direct"))["session_id"]
    # norm 0 ~ centre; +32767 ~ right/bottom edge.
    before = runtime._get(sid).control_epoch
    report = runtime.report_external_cursor(0, 0)
    c = runtime._cursor_state()
    w = runtime.backend.dims["width"]
    assert abs(c["x"] - w // 2) <= 1 and c["trusted"] is True
    assert report["invalidated_sessions"] == [sid]
    assert runtime._get(sid).control_epoch == before + 1
    # a screenshot now carries that cursor
    obs = await runtime.get_session_summary(sid, capture=False)
    assert obs["cursor"]["x"] == c["x"]
    assert obs["human_input_since_observation"] is True
    assert obs["last_human_input_at"] is not None


async def test_human_cursor_input_invalidates_a_planned_burst(runtime) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)

    runtime.report_external_cursor(200, -100)
    result = await runtime.run_burst(
        sid,
        [{"type": "key", "keys": ["KeyA"]}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="human-took-over",
    )

    assert result["status"] == "control_changed"
    assert result["control_epoch"] == shot["control_epoch"] + 1
    assert not any(call[0] == "keypress" for call in runtime.backend.calls)
