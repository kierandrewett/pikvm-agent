from __future__ import annotations

import io
import json

from PIL import Image, ImageDraw

from pikvm_agent.config import AppConfig, PikvmConfig, PolicyConfig
from pikvm_agent.core.models import OCRLine, OCRResult, Region
from pikvm_agent.runtime import (
    Runtime,
    RuntimeCapabilities,
    _local_pointer_freshness_enabled,
    nearest_ocr_target_text,
)


def _hid_calls(runtime: Runtime) -> list[tuple]:
    return [
        call
        for call in runtime.backend.calls
        if call[0] in {"click", "keypress", "type_text", "print_text"}
    ]


def _animated_surface_frame(
    *,
    video_color: tuple[int, int, int],
    menu_color: tuple[int, int, int] = (60, 70, 85),
) -> bytes:
    image = Image.new("RGB", (1280, 720), (24, 28, 36))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1279, 70), fill=menu_color)
    draw.rectangle((120, 100, 1150, 690), fill=video_color)
    draw.ellipse((1165, 18, 1195, 48), fill=(220, 226, 235))
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def test_localized_freshness_requires_explicit_isolated_benchmark_profile() -> None:
    default = PolicyConfig(allow_local_pointer_freshness=True)
    lab = PolicyConfig(
        default_profile="isolated_benchmark",
        allow_local_pointer_freshness=True,
    )
    capability = RuntimeCapabilities(
        isolated_benchmark_pointer_freshness=True
    )

    assert _local_pointer_freshness_enabled(default, capability) is False
    assert (
        _local_pointer_freshness_enabled(lab, RuntimeCapabilities())
        is False
    )
    assert _local_pointer_freshness_enabled(lab, capability) is True


async def test_machine_identity_is_visible_stable_and_does_not_expose_endpoint(
    runtime: Runtime,
) -> None:
    started = await runtime.start_session("direct")
    observed = await runtime.get_session_summary(
        started["session_id"], capture=True
    )

    assert started["machine"] == observed["machine"]
    assert observed["machine"]["alias"] == "Unlabelled target"
    assert observed["machine"]["fingerprint"].startswith("target:")
    assert observed["machine"]["identity_source"] == "configured_endpoint"
    assert "pikvm.local" not in json.dumps(observed["machine"])


async def test_explicit_machine_id_controls_safe_fingerprint(
    app_config: AppConfig,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PIKVM_MACHINE_ID", "lab-windows-a")
    app_config.pikvm = PikvmConfig(
        base_url="https://private-endpoint.example:9443",
        machine_alias="Windows test VM",
        desktop_layer="VNC console",
    )
    runtime = await Runtime.from_config(app_config)
    try:
        machine = runtime.machine_identity()
    finally:
        await runtime.aclose()

    assert machine["alias"] == "Windows test VM"
    assert machine["desktop_layer"] == "VNC console"
    assert machine["identity_source"] == "explicit_machine_id"
    assert "lab-windows-a" not in json.dumps(machine)
    assert "private-endpoint" not in json.dumps(machine)


def test_click_grounding_uses_nearest_line_not_dangerous_neighbor_text() -> None:
    observed = OCRResult(
        lines=[
            OCRLine(text="File History & Trash", bbox=[20, 3, 180, 20]),
            OCRLine(text="Screen", bbox=[135, 36, 190, 54]),
            OCRLine(text="Diagnostics", bbox=[20, 68, 120, 86]),
        ]
    )

    target = nearest_ocr_target_text(
        observed,
        click_x=390,
        click_y=261,
        region=Region(x=210, y=216, width=360, height=90),
    )

    assert target == "Screen"


def test_click_grounding_refuses_text_from_adjacent_rows() -> None:
    observed = OCRResult(
        lines=[
            OCRLine(text="File History & Trash", bbox=[20, 3, 180, 20]),
            OCRLine(text="Diagnostics", bbox=[20, 68, 120, 86]),
        ]
    )

    target = nearest_ocr_target_text(
        observed,
        click_x=352,
        click_y=261,
        region=Region(x=172, y=216, width=360, height=90),
    )

    assert target == ""


async def test_idempotency_replays_result_without_repeating_hid(runtime: Runtime) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    kwargs = {
        "based_on_world_version": shot["world_version"],
        "based_on_control_epoch": shot["control_epoch"],
        "idempotency_key": "open-search-1",
    }
    first = await runtime.run_burst(
        sid, [{"type": "key", "keys": ["CTRL", "F"]}], **kwargs
    )
    calls_after_first = list(_hid_calls(runtime))
    second = await runtime.run_burst(
        sid, [{"type": "key", "keys": ["CTRL", "F"]}], **kwargs
    )

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert second["idempotent_replay"] is True
    assert _hid_calls(runtime) == calls_after_first


async def test_stable_click_target_can_act_while_unrelated_video_region_changes(
    runtime: Runtime,
) -> None:
    runtime.config.policy.allow_local_pointer_freshness = True
    runtime.config.policy.default_profile = "isolated_benchmark"
    runtime.capabilities = RuntimeCapabilities(
        isolated_benchmark_pointer_freshness=True
    )
    sid = (await runtime.start_session("direct"))["session_id"]
    runtime.backend.set_frame_bytes(
        _animated_surface_frame(video_color=(10, 10, 10))
    )
    planned = await runtime.get_session_summary(sid, capture=True)
    runtime.backend.set_frame_bytes(
        _animated_surface_frame(video_color=(240, 240, 240))
    )

    result = await runtime.run_burst(
        sid,
        [
            {
                "type": "click",
                "x": 1180,
                "y": 33,
                "observed_target_text": "Application menu",
            }
        ],
        based_on_world_version=planned["world_version"],
        based_on_control_epoch=planned["control_epoch"],
        idempotency_key="stable-menu-over-video",
    )

    assert result["status"] == "completed"
    assert result["localized_freshness"] is True
    assert ("click", {"x": 1180, "y": 33, "button": "left"}) in _hid_calls(
        runtime
    )


async def test_localized_freshness_refuses_changed_click_target(
    runtime: Runtime,
) -> None:
    runtime.config.policy.allow_local_pointer_freshness = True
    runtime.config.policy.default_profile = "isolated_benchmark"
    runtime.capabilities = RuntimeCapabilities(
        isolated_benchmark_pointer_freshness=True
    )
    sid = (await runtime.start_session("direct"))["session_id"]
    runtime.backend.set_frame_bytes(
        _animated_surface_frame(video_color=(10, 10, 10))
    )
    planned = await runtime.get_session_summary(sid, capture=True)
    runtime.backend.set_frame_bytes(
        _animated_surface_frame(
            video_color=(240, 240, 240),
            menu_color=(220, 40, 40),
        )
    )

    result = await runtime.run_burst(
        sid,
        [
            {
                "type": "click",
                "x": 1180,
                "y": 33,
                "observed_target_text": "Application menu",
            }
        ],
        based_on_world_version=planned["world_version"],
        based_on_control_epoch=planned["control_epoch"],
        idempotency_key="changed-menu-over-video",
    )

    assert result["status"] == "stale_world"
    assert not _hid_calls(runtime)


async def test_localized_freshness_is_disabled_by_default(
    runtime: Runtime,
) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    runtime.backend.set_frame_bytes(
        _animated_surface_frame(video_color=(10, 10, 10))
    )
    planned = await runtime.get_session_summary(sid, capture=True)
    runtime.backend.set_frame_bytes(
        _animated_surface_frame(video_color=(240, 240, 240))
    )

    result = await runtime.run_burst(
        sid,
        [
            {
                "type": "click",
                "x": 1180,
                "y": 33,
                "observed_target_text": "Application menu",
            }
        ],
        based_on_world_version=planned["world_version"],
        based_on_control_epoch=planned["control_epoch"],
        idempotency_key="production-default-over-video",
    )

    assert result["status"] == "stale_world"
    assert result["localized_freshness"] is False
    assert not _hid_calls(runtime)


async def test_localized_freshness_flag_is_ignored_outside_isolated_benchmark(
    runtime: Runtime,
) -> None:
    runtime.config.policy.allow_local_pointer_freshness = True
    runtime.capabilities = RuntimeCapabilities(
        isolated_benchmark_pointer_freshness=True
    )
    assert runtime.config.policy.default_profile != "isolated_benchmark"
    sid = (await runtime.start_session("direct"))["session_id"]
    runtime.backend.set_frame_bytes(
        _animated_surface_frame(video_color=(10, 10, 10))
    )
    planned = await runtime.get_session_summary(sid, capture=True)
    runtime.backend.set_frame_bytes(
        _animated_surface_frame(video_color=(240, 240, 240))
    )

    result = await runtime.run_burst(
        sid,
        [
            {
                "type": "click",
                "x": 1180,
                "y": 33,
                "observed_target_text": "Application menu",
            }
        ],
        based_on_world_version=planned["world_version"],
        based_on_control_epoch=planned["control_epoch"],
        idempotency_key="non-lab-local-freshness-refused",
    )

    assert result["status"] == "stale_world"
    assert result["localized_freshness"] is False
    assert not _hid_calls(runtime)


async def test_localized_freshness_never_exempts_keyboard_bursts(
    runtime: Runtime,
) -> None:
    runtime.config.policy.allow_local_pointer_freshness = True
    runtime.config.policy.default_profile = "isolated_benchmark"
    runtime.capabilities = RuntimeCapabilities(
        isolated_benchmark_pointer_freshness=True
    )
    sid = (await runtime.start_session("direct"))["session_id"]
    runtime.backend.set_frame_bytes(
        _animated_surface_frame(video_color=(10, 10, 10))
    )
    planned = await runtime.get_session_summary(sid, capture=True)
    runtime.backend.set_frame_bytes(
        _animated_surface_frame(video_color=(240, 240, 240))
    )

    result = await runtime.run_burst(
        sid,
        [{"type": "key", "keys": ["CTRL", "L"]}],
        based_on_world_version=planned["world_version"],
        based_on_control_epoch=planned["control_epoch"],
        idempotency_key="keyboard-over-video",
    )

    assert result["status"] == "stale_world"
    assert result["localized_freshness"] is False
    assert not _hid_calls(runtime)


async def test_runtime_reports_auto_typing_budget(runtime: Runtime) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)

    result = await runtime.run_burst(
        sid,
        [{"type": "type_text", "text": "dim screen when inactive", "method": "print"}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="typing-aware-budget",
    )

    assert result["runtime_budget_ms"] >= 15_000
    assert result["runtime_budget_source"] == "auto"


async def test_runtime_refuses_burst_without_complete_freshness(
    runtime: Runtime,
) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]

    result = await runtime.run_burst(
        sid,
        [{"type": "key", "keys": ["KeyA"]}],
        idempotency_key="missing-freshness",
    )

    assert result["status"] == "freshness_required"
    assert "required before HID" in result["error"]
    assert not _hid_calls(runtime)


async def test_runtime_refuses_blank_idempotency_key_before_hid(
    runtime: Runtime,
) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)

    result = await runtime.run_burst(
        sid,
        [{"type": "key", "keys": ["KeyA"]}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="   ",
    )

    assert result["status"] == "failed"
    assert "non-blank idempotency_key is required" in result["error"]
    assert not _hid_calls(runtime)


async def test_post_action_capture_failure_returns_structured_partial_result(
    runtime: Runtime,
) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    session = runtime._get(sid)
    original_capture = session.frames.capture
    calls = 0

    async def fail_final_capture(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await original_capture(*args, **kwargs)
        raise RuntimeError("camera unavailable after HID")

    session.frames.capture = fail_final_capture
    result = await runtime.run_burst(
        sid,
        [{"type": "key", "keys": ["KeyA"]}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="capture-failed-after-key",
    )

    assert result["status"] == "failed"
    assert result["action_status"] == "completed"
    assert result["reason"] == "post_action_evidence_failed"
    assert result["completed_actions"] == 1
    assert "camera unavailable after HID" in result["error"]
    assert len(_hid_calls(runtime)) == 1


async def test_reusing_idempotency_key_for_different_burst_is_refused(
    runtime: Runtime,
) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    await runtime.run_burst(
        sid,
        [{"type": "key", "keys": ["KeyA"]}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="same-key",
    )
    conflict = await runtime.run_burst(
        sid,
        [{"type": "key", "keys": ["KeyB"]}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="same-key",
    )
    assert conflict["status"] == "idempotency_conflict"


async def test_dangerous_click_pauses_for_approval_then_executes_once(
    runtime: Runtime,
) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    paused = await runtime.run_burst(
        sid,
        [{"type": "click", "x": 50, "y": 60, "target_text": "Send message"}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="send-message-1",
    )
    assert paused["status"] == "needs_approval"
    assert paused["approval_request"]["risk"] == "communication_send"
    assert paused["approval_request"]["machine"] == runtime.machine_identity()
    assert not _hid_calls(runtime)

    repeated = await runtime.run_burst(
        sid,
        [{"type": "click", "x": 50, "y": 60, "target_text": "Send message"}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="send-message-1",
    )
    assert repeated["approval_request"]["approval_id"] == paused["approval_request"]["approval_id"]
    assert not _hid_calls(runtime)

    result = await runtime.submit_approval(
        sid, paused["approval_request"]["approval_id"], {"type": "approve"}
    )
    completed_summary = await runtime.get_session_summary(sid, capture=False)
    assert result["status"] == "completed"
    assert completed_summary["status"] == "paused"
    assert len([call for call in _hid_calls(runtime) if call[0] == "click"]) == 1


async def test_rejecting_direct_burst_never_executes_hid(runtime: Runtime) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    paused = await runtime.run_burst(
        sid,
        [{"type": "type_text", "text": "rm -rf build", "context": "terminal"}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="delete-build-1",
    )
    pending_summary = await runtime.get_session_summary(sid, capture=False)
    assert pending_summary["status"] == "needs_approval"
    result = await runtime.submit_approval(
        sid, paused["approval_request"]["approval_id"], {"type": "reject", "reason": "no"}
    )
    rejected_summary = await runtime.get_session_summary(sid, capture=False)
    assert result["status"] == "blocked"
    assert rejected_summary["status"] == "paused"
    assert not _hid_calls(runtime)


async def test_security_disable_click_is_blocked_before_hid(runtime: Runtime) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)

    blocked = await runtime.run_burst(
        sid,
        [
            {
                "type": "click",
                "x": 50,
                "y": 60,
                "target_text": "Disable security",
            }
        ],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="disable-security-1",
    )

    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "commit target requires human review"
    assert not _hid_calls(runtime)


async def test_ocr_corrupted_purchase_click_pauses_before_hid(
    runtime: Runtime,
) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)

    paused = await runtime.run_burst(
        sid,
        [
            {
                "type": "click",
                "x": 50,
                "y": 60,
                "observed_target_text": "Purcnase now",
            }
        ],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="ocr-purchase-1",
    )

    assert paused["status"] == "needs_approval"
    assert paused["approval_request"]["risk"] == "financial_or_purchase"
    assert not _hid_calls(runtime)


async def test_bare_enter_waits_for_approval_before_any_keypress(
    runtime: Runtime,
) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)

    paused = await runtime.run_burst(
        sid,
        [{"type": "key", "keys": ["ENTER"]}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="focused-surface-commit-1",
    )

    assert paused["status"] == "needs_approval"
    assert paused["approval_request"]["risk"] == "unknown"
    assert not _hid_calls(runtime)


async def test_direct_approval_is_not_force_execute_after_world_changes(
    runtime: Runtime,
) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    paused = await runtime.run_burst(
        sid,
        [{"type": "click", "x": 50, "y": 60, "target_text": "Delete record"}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="delete-record-1",
    )
    runtime.backend.set_screen("a popup appeared", bg=(220, 30, 30))
    result = await runtime.submit_approval(
        sid, paused["approval_request"]["approval_id"], {"type": "approve"}
    )
    assert result["status"] == "stale_world"
    assert not _hid_calls(runtime)


async def test_concurrent_machine_client_blocks_hid_before_execution(
    runtime: Runtime,
) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    runtime.backend.other_client_count = 1

    result = await runtime.run_burst(
        sid,
        [{"type": "key", "keys": ["KeyA"]}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="blocked-by-other-client",
    )

    assert result["status"] == "control_changed"
    assert result["reason"] == "another machine client is connected"
    assert result["human_concurrency"]["other_clients"] == 1
    assert result["control_epoch"] == shot["control_epoch"] + 1
    assert not _hid_calls(runtime)


async def test_machine_client_appearing_mid_burst_revokes_epoch_and_stops_next_action(
    runtime: Runtime,
) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    original_keypress = runtime.backend.keypress

    async def connect_other_client_after_first_key(keys) -> None:
        await original_keypress(keys)
        runtime.backend.other_client_count = 1

    runtime.backend.keypress = connect_other_client_after_first_key
    result = await runtime.run_burst(
        sid,
        [
            {"type": "key", "keys": ["KeyA"]},
            {"type": "key", "keys": ["KeyB"]},
        ],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="other-client-mid-burst",
    )

    assert result["status"] == "interrupted"
    assert result["reason"] == "control_changed"
    assert result["completed_actions"] == 1
    assert result["control_epoch"] == shot["control_epoch"] + 1
    assert result["human_concurrency"]["other_clients"] == 1
    assert [
        call for call in _hid_calls(runtime) if call[0] == "keypress"
    ] == [("keypress", {"keys": ["KeyA"]})]
