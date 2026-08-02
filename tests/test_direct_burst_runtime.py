from __future__ import annotations

import asyncio
import io
import json

from PIL import Image, ImageDraw

from pikvm_agent.config import AppConfig, PikvmConfig, PolicyConfig
from pikvm_agent.core.models import OCRLine, OCRResult, Region
from pikvm_agent.executor import burst as burst_module
from pikvm_agent.runtime import (
    Runtime,
    RuntimeCapabilities,
    _local_pointer_freshness_enabled,
    _safe_error_dialog_region,
    nearest_ocr_target_text,
)


def _hid_calls(runtime: Runtime) -> list[tuple]:
    return [
        call
        for call in runtime.backend.calls
        if call[0] in {"click", "keypress", "type_text", "print_text"}
    ]


_ORIGINAL_BURST_RUNNER = burst_module.run_burst


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


async def test_ocr_region_uses_the_captured_frame_and_provider_crop(
    runtime: Runtime,
    monkeypatch,
) -> None:
    screenshot_calls = 0
    original_screenshot = runtime.backend.screenshot

    async def counted_screenshot(region=None):
        nonlocal screenshot_calls
        screenshot_calls += 1
        return await original_screenshot(region)

    monkeypatch.setattr(runtime.backend, "screenshot", counted_screenshot)

    class RecordingOCR:
        def __init__(self) -> None:
            self.image_path = None
            self.region = None

        async def ocr(self, image_path, region=None):
            self.image_path = image_path
            self.region = region
            return OCRResult(
                lines=[
                    OCRLine(
                        text="field",
                        confidence=0.99,
                        bbox=[2, 3, 20, 14],
                    )
                ]
            )

    provider = RecordingOCR()
    runtime._screen_parser.ocr = provider
    session_id = (await runtime.start_session("direct"))["session_id"]

    result = await runtime.ocr_region(session_id, 10, 20, 30, 40)

    assert screenshot_calls == 1
    assert provider.image_path is not None
    assert provider.image_path.exists()
    assert provider.region == Region(x=10, y=20, width=30, height=40)
    assert result["text"] == "field"
    assert result["frame_id"] == 1


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
            OCRLine(
                text="Screen",
                confidence=0.42,
                bbox=[135, 36, 190, 54],
            ),
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
    low_confidence_noise = OCRResult(
        lines=[
            OCRLine(
                text="oT",
                confidence=0.00018204,
                bbox=[0, 38, 360, 54],
            ),
        ]
    )
    assert nearest_ocr_target_text(
        low_confidence_noise,
        click_x=326,
        click_y=389,
        region=Region(x=146, y=344, width=360, height=90),
    ) == ""


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


def test_click_grounding_does_not_let_an_adjacent_row_mask_the_target_row() -> None:
    observed = OCRResult(
        lines=[
            OCRLine(text="Type here to search", bbox=[1, 30, 55, 58]),
            OCRLine(text="Quick searches", bbox=[140, 62, 206, 90]),
        ]
    )

    target = nearest_ocr_target_text(
        observed,
        click_x=250,
        click_y=339,
        region=Region(x=70, y=294, width=360, height=90),
    )

    assert target == "Type here to search"


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


async def _run_overlapping_idempotent_requests(
    runtime: Runtime,
    monkeypatch,
    *,
    cancel_initial_request: bool,
) -> tuple[dict | None, dict, int]:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    kwargs = {
        "based_on_world_version": shot["world_version"],
        "based_on_control_epoch": shot["control_epoch"],
        "idempotency_key": (
            "cancelled-slow-input"
            if cancel_initial_request
            else "slow-exact-input"
        ),
    }
    started = asyncio.Event()
    release = asyncio.Event()
    execution_count = 0

    async def slow_run_burst(*args, **call_kwargs):
        nonlocal execution_count
        execution_count += 1
        started.set()
        await release.wait()
        return await _ORIGINAL_BURST_RUNNER(*args, **call_kwargs)

    monkeypatch.setattr(
        "pikvm_agent.runtime._burst.run_burst",
        slow_run_burst,
    )
    first_task = asyncio.create_task(
        runtime.run_burst(
            sid,
            [{"type": "key", "keys": ["CTRL", "F"]}],
            **kwargs,
        )
    )
    await started.wait()
    first: dict | None = None
    if cancel_initial_request:
        first_task.cancel()
        try:
            await first_task
        except asyncio.CancelledError:
            pass
    retry_task = asyncio.create_task(
        runtime.run_burst(
            sid,
            [{"type": "key", "keys": ["CTRL", "F"]}],
            **kwargs,
        )
    )
    await asyncio.sleep(0.05)
    release.set()
    if cancel_initial_request:
        retry = await retry_task
    else:
        first, retry = await asyncio.gather(first_task, retry_task)
    return first, retry, execution_count


async def test_concurrent_and_cancelled_idempotent_retries_join_in_flight_hid(
    runtime: Runtime,
    monkeypatch,
) -> None:
    """Timeout and cancellation must never let a retry execute HID twice."""

    for cancel_initial_request in (False, True):
        calls_before = len(_hid_calls(runtime))
        first, retry, execution_count = (
            await _run_overlapping_idempotent_requests(
                runtime,
                monkeypatch,
                cancel_initial_request=cancel_initial_request,
            )
        )

        assert (first is None) is cancel_initial_request
        if first is not None:
            assert first["status"] == "completed"
        assert retry["status"] == "completed"
        assert retry["idempotent_inflight_replay"] is True
        assert execution_count == 1
        assert len(_hid_calls(runtime)) == calls_before + 1

    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    started = asyncio.Event()
    release = asyncio.Event()
    execution_count = 0

    async def slow_conflict_runner(*args, **call_kwargs):
        nonlocal execution_count
        execution_count += 1
        started.set()
        await release.wait()
        return await _ORIGINAL_BURST_RUNNER(*args, **call_kwargs)

    monkeypatch.setattr(
        "pikvm_agent.runtime._burst.run_burst",
        slow_conflict_runner,
    )
    first_task = asyncio.create_task(
        runtime.run_burst(
            sid,
            [{"type": "key", "keys": ["CTRL", "F"]}],
            based_on_world_version=shot["world_version"],
            based_on_control_epoch=shot["control_epoch"],
            idempotency_key="in-flight-conflict",
        )
    )
    await started.wait()
    conflict = await runtime.run_burst(
        sid,
        [{"type": "key", "keys": ["CTRL", "G"]}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="in-flight-conflict",
    )
    release.set()
    completed = await first_task

    assert conflict["status"] == "idempotency_conflict"
    assert completed["status"] == "completed"
    assert execution_count == 1


async def test_idempotent_retry_rechecks_after_slow_preflight(
    runtime: Runtime,
    monkeypatch,
) -> None:
    """A late retry must replay a result completed while grounding blocked."""

    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    kwargs = {
        "based_on_world_version": shot["world_version"],
        "based_on_control_epoch": shot["control_epoch"],
        "idempotency_key": "late-grounding-retry",
    }
    execution_started = asyncio.Event()
    release_execution = asyncio.Event()
    retry_grounding_started = asyncio.Event()
    release_retry_grounding = asyncio.Event()
    execution_count = 0
    grounding_count = 0
    original_ground = runtime._ground_click_targets

    async def slow_run_burst(*args, **call_kwargs):
        nonlocal execution_count
        execution_count += 1
        execution_started.set()
        await release_execution.wait()
        return await _ORIGINAL_BURST_RUNNER(*args, **call_kwargs)

    async def delayed_second_grounding(actions, frame):
        nonlocal grounding_count
        grounding_count += 1
        if grounding_count == 2:
            retry_grounding_started.set()
            await release_retry_grounding.wait()
        return await original_ground(actions, frame)

    monkeypatch.setattr(
        "pikvm_agent.runtime._burst.run_burst",
        slow_run_burst,
    )
    monkeypatch.setattr(
        runtime,
        "_ground_click_targets",
        delayed_second_grounding,
    )

    first_task = asyncio.create_task(
        runtime.run_burst(
            sid,
            [{"type": "key", "keys": ["CTRL", "F"]}],
            **kwargs,
        )
    )
    await execution_started.wait()
    retry_task = asyncio.create_task(
        runtime.run_burst(
            sid,
            [{"type": "key", "keys": ["CTRL", "F"]}],
            **kwargs,
        )
    )
    await retry_grounding_started.wait()

    release_execution.set()
    first = await first_task
    release_retry_grounding.set()
    retry = await retry_task

    assert first["status"] == "completed"
    assert retry["status"] == "completed"
    assert retry["idempotent_replay"] is True
    assert execution_count == 1
    assert len(_hid_calls(runtime)) == 1


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


async def test_grounded_calculator_expression_does_not_need_send_approval(
    runtime: Runtime,
) -> None:
    class CalculatorOCR:
        async def ocr(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(text="Standard", bbox=[20, 20, 100, 50]),
                    OCRLine(text="History", bbox=[500, 20, 570, 50]),
                    OCRLine(text="Memory", bbox=[580, 20, 650, 50]),
                ]
            )

    runtime._screen_parser.ocr = CalculatorOCR()
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    actions = [
        {"type": "key", "keys": ["Digit3"]},
        {"type": "key", "keys": ["Digit7"]},
        {"type": "key", "keys": ["NumpadMultiply"]},
        {"type": "key", "keys": ["Digit1"]},
        {"type": "key", "keys": ["Digit9"]},
        {"type": "key", "keys": ["Enter"]},
    ]

    result = await runtime.run_burst(
        sid,
        actions,
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="grounded-calculator-expression",
    )

    assert result["status"] == "completed"
    assert [call[1]["keys"] for call in _hid_calls(runtime)] == [
        ["Digit3"],
        ["Digit7"],
        ["NumpadMultiply"],
        ["Digit1"],
        ["Digit9"],
        ["Enter"],
    ]


async def test_safe_error_click_reads_only_the_dialog_before_dismissal(
    runtime: Runtime,
) -> None:
    """A small dark Notepad error must not be diluted by the desktop OCR."""

    click_x = 554
    click_y = 299
    expected_region = _safe_error_dialog_region(
        1280,
        720,
        {"x": click_x, "y": click_y},
    )

    class MissingFileOCR:
        def __init__(self) -> None:
            self.precise_regions: list[Region] = []

        async def ocr(self, image_path, region=None):
            del image_path
            if region is not None:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="OK",
                            confidence=0.99,
                            bbox=[165, 30, 195, 60],
                        )
                    ]
                )
            return OCRResult(lines=[OCRLine(text="Notepad")])

        async def ocr_precise(self, image_path, region=None):
            del image_path
            assert region is not None
            self.precise_regions.append(region)
            return OCRResult(
                lines=[
                    OCRLine(text="Notepad"),
                    OCRLine(
                        text=(
                            "Cannot find the C:\\PiKVM-Harness\\workspace\\"
                            "codex-50\\code-04.sql file."
                        )
                    ),
                    OCRLine(text="OK"),
                ]
            )

    ocr = MissingFileOCR()
    runtime._screen_parser.ocr = ocr
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)

    result = await runtime.run_burst(
        sid,
        [
            {"type": "click", "x": click_x, "y": click_y},
            {"type": "wait_for_change", "timeout_ms": 2_000},
            {
                "type": "wait_for_stable_screen",
                "stable_ms": 400,
                "timeout_ms": 3_000,
            },
        ],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="dismiss-grounded-notepad-error",
    )

    assert result["status"] == "completed"
    assert ocr.precise_regions == [expected_region]
    assert expected_region == Region(x=294, y=179, width=400, height=175)
    assert [call[0] for call in _hid_calls(runtime)] == ["click"]


async def test_exact_explorer_receipt_records_post_action_screen_fingerprint(
    runtime: Runtime,
) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    sr = runtime._get(sid)
    final_frame = sr.frames.latest()
    assert final_frame is not None

    runtime._update_verified_local_navigation_draft(
        sr,
        [
            {
                "type": "type_text",
                "text": "This PC",
                "context": "field",
                "verification": "exact",
            }
        ],
        [
            {
                "index": 0,
                "status": "verified_exact",
                "verdict": "match",
                "focus_evidence": "read_back_verified",
                "exact_readback_sha256_match": True,
                "emitted_exactly_once": True,
                "observed_text": "This PC",
                "readback_frame_sha256": "f" * 64,
            }
        ],
        final_frame,
    )

    assert sr.verified_local_navigation_draft == {
        "text": "This PC",
        "readback_frame_sha256": "f" * 64,
        "post_action_image_sha256": shot["image_sha256"],
        "frame_screen_hash": shot["screen_hash"],
        "world_version": shot["world_version"],
        "control_epoch": shot["control_epoch"],
    }


async def test_exact_save_as_path_receipt_records_local_navigation_draft(
    runtime: Runtime,
) -> None:
    path = r"C:\PiKVM-Harness\workspace\codex-50"
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    sr = runtime._get(sid)
    final_frame = sr.frames.latest()
    assert final_frame is not None

    runtime._update_verified_local_navigation_draft(
        sr,
        [
            {
                "type": "type_text",
                "text": path,
                "context": "field",
                "verification": "exact",
            }
        ],
        [
            {
                "index": 0,
                "status": "verified_exact",
                "verdict": "match",
                "focus_evidence": "read_back_verified",
                "exact_readback_sha256_match": True,
                "emitted_exactly_once": True,
                "observed_text": path,
                "readback_frame_sha256": shot["image_sha256"],
            }
        ],
        final_frame,
    )

    assert sr.verified_local_navigation_draft == {
        "text": path,
        "readback_frame_sha256": shot["image_sha256"],
        "post_action_image_sha256": shot["image_sha256"],
        "frame_screen_hash": shot["screen_hash"],
        "world_version": shot["world_version"],
        "control_epoch": shot["control_epoch"],
    }


async def test_exact_replaced_save_as_filename_records_local_commit_draft(
    runtime: Runtime,
) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    sr = runtime._get(sid)
    final_frame = sr.frames.latest()
    assert final_frame is not None

    runtime._update_verified_local_navigation_draft(
        sr,
        [
            {"type": "key", "keys": ["CTRL", "A"]},
            {
                "type": "type_text",
                "text": "code-04.sql",
                "context": "field",
                "verification": "exact",
            },
        ],
        [
            {
                "index": 1,
                "status": "verified_exact",
                "verdict": "match",
                "focus_evidence": "read_back_verified",
                "exact_readback_sha256_match": True,
                "emitted_exactly_once": True,
                "observed_text": "code-04.sql",
                "readback_frame_sha256": "e" * 64,
            }
        ],
        final_frame,
    )

    assert sr.verified_local_navigation_draft == {
        "text": "code-04.sql",
        "readback_frame_sha256": "e" * 64,
        "post_action_image_sha256": shot["image_sha256"],
        "frame_screen_hash": shot["screen_hash"],
        "world_version": shot["world_version"],
        "control_epoch": shot["control_epoch"],
    }


async def test_exact_windows_run_receipt_records_local_navigation_draft(
    runtime: Runtime,
) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    sr = runtime._get(sid)
    final_frame = sr.frames.latest()
    assert final_frame is not None

    runtime._update_verified_local_navigation_draft(
        sr,
        [
            {
                "type": "type_text",
                "text": "notepad",
                "context": "field",
                "verification": "exact",
            }
        ],
        [
            {
                "index": 0,
                "status": "verified_exact",
                "verdict": "match",
                "focus_evidence": "read_back_verified",
                "exact_readback_sha256_match": True,
                "emitted_exactly_once": True,
                "observed_text": "notepad",
                "readback_frame_sha256": shot["image_sha256"],
            }
        ],
        final_frame,
    )

    assert sr.verified_local_navigation_draft == {
        "text": "notepad",
        "readback_frame_sha256": shot["image_sha256"],
        "post_action_image_sha256": shot["image_sha256"],
        "frame_screen_hash": shot["screen_hash"],
        "world_version": shot["world_version"],
        "control_epoch": shot["control_epoch"],
    }


async def test_matching_exact_windows_run_draft_grounds_one_local_enter(
    runtime: Runtime,
) -> None:
    class WindowsRunOCR:
        async def ocr(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(text="Run"),
                    OCRLine(
                        text=(
                            "Type the name of a program, folder, document "
                            "or Internet resource"
                        )
                    ),
                    OCRLine(text="Open: notepad"),
                    OCRLine(text="OK  Cancel  Browse..."),
                ]
            )

        async def ocr_precise(self, image_path, region=None):
            return await self.ocr(image_path, region)

    runtime._screen_parser.ocr = WindowsRunOCR()
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    runtime._get(sid).verified_local_navigation_draft = {
        "text": "notepad",
        "readback_frame_sha256": shot["image_sha256"],
        "post_action_image_sha256": shot["image_sha256"],
        "frame_screen_hash": shot["screen_hash"],
        "world_version": shot["world_version"],
        "control_epoch": shot["control_epoch"],
    }

    result = await runtime.run_burst(
        sid,
        [
            {"type": "key", "keys": ["ENTER"]},
            {"type": "wait_for_change", "timeout_ms": 3000},
        ],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="grounded-windows-run-launch",
    )

    assert result["status"] == "completed"
    assert [call[1]["keys"] for call in _hid_calls(runtime)] == [["Enter"]]
    assert runtime._get(sid).verified_local_navigation_draft is None


async def test_exact_windows_run_draft_does_not_survive_pointer_input(
    runtime: Runtime,
) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    sr = runtime._get(sid)
    final_frame = sr.frames.latest()
    assert final_frame is not None
    sr.verified_local_navigation_draft = {
        "text": "notepad",
        "readback_frame_sha256": shot["image_sha256"],
        "post_action_image_sha256": shot["image_sha256"],
        "frame_screen_hash": shot["screen_hash"],
        "world_version": shot["world_version"],
        "control_epoch": shot["control_epoch"],
    }

    runtime._update_verified_local_navigation_draft(
        sr,
        [{"type": "click", "x": 10, "y": 10}],
        [],
        final_frame,
    )

    assert sr.verified_local_navigation_draft is None


async def test_matching_exact_explorer_draft_grounds_one_local_enter(
    runtime: Runtime,
) -> None:
    class ExplorerOCR:
        async def ocr(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(text="Home"),
                    OCRLine(text="This PC"),
                    OCRLine(text="Quick access"),
                    OCRLine(text="Downloads"),
                ]
            )

    runtime._screen_parser.ocr = ExplorerOCR()
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    runtime._get(sid).verified_local_navigation_draft = {
        "text": "This PC",
        "readback_frame_sha256": shot["image_sha256"],
        "post_action_image_sha256": shot["image_sha256"],
        "frame_screen_hash": shot["screen_hash"],
        "world_version": shot["world_version"],
        "control_epoch": shot["control_epoch"],
    }

    result = await runtime.run_burst(
        sid,
        [{"type": "key", "keys": ["ENTER"]}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="grounded-explorer-navigation",
    )

    assert result["status"] == "completed"
    assert [call[1]["keys"] for call in _hid_calls(runtime)] == [["Enter"]]
    assert runtime._get(sid).verified_local_navigation_draft is None


async def test_matching_exact_save_as_path_grounds_one_local_enter(
    runtime: Runtime,
) -> None:
    path = r"C:\PiKVM-Harness\workspace\codex-50"

    class SaveAsOCR:
        async def ocr(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(text="Save as"),
                    OCRLine(text="New folder"),
                    OCRLine(text="File name: text-01.txt"),
                    OCRLine(text="Save as type: Text documents"),
                ]
            )

        async def ocr_precise(self, image_path, region=None):
            del image_path
            assert region is not None
            return OCRResult(lines=[OCRLine(text=path)])

    runtime._screen_parser.ocr = SaveAsOCR()
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    runtime._get(sid).verified_local_navigation_draft = {
        "text": path,
        "readback_frame_sha256": shot["image_sha256"],
        "post_action_image_sha256": shot["image_sha256"],
        "frame_screen_hash": shot["screen_hash"],
        "world_version": shot["world_version"],
        "control_epoch": shot["control_epoch"],
    }

    result = await runtime.run_burst(
        sid,
        [
            {"type": "key", "keys": ["ENTER"]},
            {"type": "wait_for_change", "timeout_ms": 5000},
        ],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="grounded-save-as-navigation",
    )

    assert result["status"] == "completed"
    assert [call[1]["keys"] for call in _hid_calls(runtime)] == [["Enter"]]
    assert runtime._get(sid).verified_local_navigation_draft is None


async def test_matching_exact_save_as_filename_grounds_one_local_enter(
    runtime: Runtime,
) -> None:
    class SaveAsOCR:
        async def ocr(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(text="Save"),
                    OCRLine(text="This PC  New folder"),
                    OCRLine(text="File name: code-04.sql"),
                ]
            )

        async def ocr_precise(self, image_path, region=None):
            del image_path
            assert region is not None
            return OCRResult(
                lines=[
                    OCRLine(text="Save as"),
                    OCRLine(text="This PC  New folder"),
                    OCRLine(text="Save as type: Text documents"),
                ]
            )

    runtime._screen_parser.ocr = SaveAsOCR()
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    runtime._get(sid).verified_local_navigation_draft = {
        "text": "code-04.sql",
        "readback_frame_sha256": shot["image_sha256"],
        "post_action_image_sha256": shot["image_sha256"],
        "frame_screen_hash": shot["screen_hash"],
        "world_version": shot["world_version"],
        "control_epoch": shot["control_epoch"],
    }

    result = await runtime.run_burst(
        sid,
        [
            {"type": "key", "keys": ["ENTER"]},
            {"type": "wait_for_change", "timeout_ms": 3000},
        ],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="grounded-save-as-filename",
    )

    assert result["status"] == "needs_approval"
    assert result["approval_request"]["risk"] == "local_file_edit"
    assert not _hid_calls(runtime)


async def test_matching_exact_open_filename_grounds_one_local_enter(
    runtime: Runtime,
) -> None:
    class OpenOCR:
        async def ocr(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(text="Open"),
                    OCRLine(text="This PC  New folder"),
                    OCRLine(text="Name  Date modified  Type  Size"),
                ]
            )

    runtime._screen_parser.ocr = OpenOCR()
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    runtime._get(sid).verified_local_navigation_draft = {
        "text": "code-04.sql",
        "readback_frame_sha256": "f" * 64,
        "post_action_image_sha256": shot["image_sha256"],
        "frame_screen_hash": shot["screen_hash"],
        "world_version": shot["world_version"],
        "control_epoch": shot["control_epoch"],
    }

    result = await runtime.run_burst(
        sid,
        [
            {"type": "key", "keys": ["ENTER"]},
            {"type": "wait_for_change", "timeout_ms": 3000},
        ],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="grounded-open-filename",
    )

    assert result["status"] == "completed"
    assert [call[1]["keys"] for call in _hid_calls(runtime)] == [["Enter"]]
    assert runtime._get(sid).verified_local_navigation_draft is None


async def test_exact_same_frame_grounds_save_as_enter_despite_real_ocr_noise(
    runtime: Runtime,
) -> None:
    path = r"C:\PiKVM-Harness\workspace\codex-50"

    class NoisySaveAsOCR:
        async def ocr(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(text="Reosie BD Swveas"),
                    OCRLine(text="Organise New folder"),
                    OCRLine(
                        text="Reluble sutomation starts with batt"
                    ),
                ]
            )

        async def ocr_precise(self, image_path, region=None):
            del image_path
            assert region is not None
            return OCRResult(
                lines=[
                    OCRLine(
                        text=(
                            r"BD Seve as > Y BB C:\PIKVM-Hamess"
                            r"\workspace\codex-50 Organise New folder"
                        )
                    )
                ]
            )

    runtime._screen_parser.ocr = NoisySaveAsOCR()
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    runtime._get(sid).verified_local_navigation_draft = {
        "text": path,
        "readback_frame_sha256": shot["image_sha256"],
        "post_action_image_sha256": shot["image_sha256"],
        "frame_screen_hash": shot["screen_hash"],
        "world_version": shot["world_version"],
        "control_epoch": shot["control_epoch"],
    }

    result = await runtime.run_burst(
        sid,
        [
            {"type": "key", "keys": ["ENTER"]},
            {"type": "wait_for_change", "timeout_ms": 5000},
        ],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="noisy-grounded-save-as-navigation",
    )

    assert result["status"] == "completed"
    assert [call[1]["keys"] for call in _hid_calls(runtime)] == [["Enter"]]
    assert runtime._get(sid).verified_local_navigation_draft is None


async def test_exact_save_as_path_does_not_ground_enter_on_message_surface(
    runtime: Runtime,
) -> None:
    path = r"C:\PiKVM-Harness\workspace\codex-50"

    class MessageOCR:
        async def ocr(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(text="New message"),
                    OCRLine(text=path),
                    OCRLine(text="Send"),
                ]
            )

        async def ocr_precise(self, image_path, region=None):
            del image_path, region
            return OCRResult(lines=[OCRLine(text=path)])

    runtime._screen_parser.ocr = MessageOCR()
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    runtime._get(sid).verified_local_navigation_draft = {
        "text": path,
        "readback_frame_sha256": shot["image_sha256"],
        "post_action_image_sha256": shot["image_sha256"],
        "frame_screen_hash": shot["screen_hash"],
        "world_version": shot["world_version"],
        "control_epoch": shot["control_epoch"],
    }

    result = await runtime.run_burst(
        sid,
        [{"type": "key", "keys": ["ENTER"]}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="message-path-surface-stays-gated",
    )

    assert result["status"] == "needs_approval"
    assert result["approval_request"]["risk"] == "unknown"
    assert not _hid_calls(runtime)


async def test_confirmed_file_explorer_not_found_ok_dismisses_without_approval(
    runtime: Runtime,
) -> None:
    class NotFoundDialogOCR:
        def __init__(self) -> None:
            self.precise_regions: list[Region] = []

        async def ocr(self, image_path, region=None):
            del image_path
            if region is not None:
                return OCRResult(
                    lines=[
                        OCRLine(
                            text="(ox",
                            bbox=[170, 35, 210, 55],
                        )
                    ]
                )
            return OCRResult(lines=[OCRLine(text="File Explorer")])

        async def ocr_precise(self, image_path, region=None):
            del image_path
            assert region is not None
            self.precise_regions.append(region)
            return OCRResult(
                lines=[
                    OCRLine(text="File Explorer"),
                    OCRLine(
                        text=(
                            "Viindows can’t find "
                            r"'C:\PiKVM-Harness\workspace\codex-50'."
                        )
                    ),
                    OCRLine(text="Check the spelling and try again"),
                ]
            )

    ocr = NotFoundDialogOCR()
    runtime._screen_parser.ocr = ocr
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)

    result = await runtime.run_burst(
        sid,
        [
            {"type": "click", "x": 782, "y": 392, "button": "left"},
            {"type": "wait_for_change", "timeout_ms": 3000},
        ],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="dismiss-confirmed-file-explorer-not-found",
    )

    assert result["status"] == "completed"
    assert _hid_calls(runtime)[0][0] == "click"
    assert ocr.precise_regions


async def test_confirmed_save_as_replacement_enter_requests_local_edit_approval(
    runtime: Runtime,
) -> None:
    class ConfirmSaveAsOCR:
        def __init__(self) -> None:
            self.precise_regions: list[Region] = []

        async def ocr(self, image_path, region=None):
            del image_path, region
            return OCRResult(lines=[OCRLine(text="Confirm Save As")])

        async def ocr_precise(self, image_path, region=None):
            del image_path
            assert region is not None
            self.precise_regions.append(region)
            return OCRResult(
                lines=[
                    OCRLine(text="Confirm Save As"),
                    OCRLine(text="text-01.txt already exists."),
                    OCRLine(text="Do you want to replace it?"),
                    OCRLine(text="Yes No"),
                ]
            )

    ocr = ConfirmSaveAsOCR()
    runtime._screen_parser.ocr = ocr
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)

    result = await runtime.run_burst(
        sid,
        [
            {"type": "key", "keys": ["ENTER"]},
            {"type": "wait_for_change", "timeout_ms": 2000},
        ],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="confirm-local-file-overwrite",
    )

    assert result["status"] == "needs_approval"
    assert result["approval_request"]["risk"] == "local_file_edit"
    assert ocr.precise_regions
    assert not _hid_calls(runtime)


async def test_exact_draft_does_not_ground_enter_on_a_message_surface(
    runtime: Runtime,
) -> None:
    class MessageOCR:
        async def ocr(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(text="New message"),
                    OCRLine(text="This PC"),
                    OCRLine(text="Send"),
                ]
            )

        async def ocr_precise(self, image_path, region=None):
            return await self.ocr(image_path, region)

    runtime._screen_parser.ocr = MessageOCR()
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    runtime._get(sid).verified_local_navigation_draft = {
        "text": "This PC",
        "readback_frame_sha256": shot["image_sha256"],
        "post_action_image_sha256": shot["image_sha256"],
        "frame_screen_hash": shot["screen_hash"],
        "world_version": shot["world_version"],
        "control_epoch": shot["control_epoch"],
    }

    result = await runtime.run_burst(
        sid,
        [{"type": "key", "keys": ["ENTER"]}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="message-surface-stays-gated",
    )

    assert result["status"] == "needs_approval"
    assert result["approval_request"]["risk"] == "unknown"
    assert not _hid_calls(runtime)


async def test_exact_explorer_draft_rejects_a_changed_screen_fingerprint(
    runtime: Runtime,
) -> None:
    class ExplorerOCR:
        async def ocr(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(text="Home"),
                    OCRLine(text="This PC"),
                    OCRLine(text="Quick access"),
                    OCRLine(text="Downloads"),
                ]
            )

    runtime._screen_parser.ocr = ExplorerOCR()
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    runtime._get(sid).verified_local_navigation_draft = {
        "text": "This PC",
        "readback_frame_sha256": shot["image_sha256"],
        "post_action_image_sha256": shot["image_sha256"],
        "frame_screen_hash": "0" * 512,
        "world_version": shot["world_version"],
        "control_epoch": shot["control_epoch"],
    }

    result = await runtime.run_burst(
        sid,
        [{"type": "key", "keys": ["ENTER"]}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="changed-explorer-screen-stays-gated",
    )

    assert result["status"] == "needs_approval"
    assert result["approval_request"]["risk"] == "unknown"
    assert not _hid_calls(runtime)


async def test_save_as_draft_tolerates_new_encoding_of_same_surface(
    runtime: Runtime,
) -> None:
    path = r"C:\PiKVM-Harness\workspace\codex-50"

    class SaveAsOCR:
        async def ocr(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[
                    OCRLine(text="Save as"),
                    OCRLine(text="New folder"),
                    OCRLine(text="File name: text-01.txt"),
                ]
            )

        async def ocr_precise(self, image_path, region=None):
            del image_path, region
            return OCRResult(lines=[OCRLine(text=path)])

    runtime._screen_parser.ocr = SaveAsOCR()
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    runtime._get(sid).verified_local_navigation_draft = {
        "text": path,
        "readback_frame_sha256": shot["image_sha256"],
        "post_action_image_sha256": "0" * 64,
        "frame_screen_hash": shot["screen_hash"],
        "world_version": shot["world_version"],
        "control_epoch": shot["control_epoch"],
    }

    result = await runtime.run_burst(
        sid,
        [{"type": "key", "keys": ["ENTER"]}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="same-save-as-surface-new-encoding",
    )

    assert result["status"] == "completed"
    assert [call[1]["keys"] for call in _hid_calls(runtime)] == [["Enter"]]


async def test_calculator_surface_retries_precise_header_before_enter_approval(
    runtime: Runtime,
) -> None:
    class CalculatorHeaderOCR:
        def __init__(self) -> None:
            self.precise_regions: list[Region] = []

        async def ocr(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[OCRLine(text="= Standard", bbox=[40, 50, 110, 60])]
            )

        async def ocr_precise(self, image_path, region=None):
            del image_path
            assert region is not None
            self.precise_regions.append(region)
            return OCRResult(
                lines=[
                    OCRLine(text="Calculator", bbox=[40, 20, 100, 40]),
                    OCRLine(text="Standard", bbox=[40, 50, 110, 60]),
                    OCRLine(text="History Memory", bbox=[580, 50, 670, 60]),
                ]
            )

    ocr = CalculatorHeaderOCR()
    runtime._screen_parser.ocr = ocr
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    actions = [
        {"type": "key", "keys": ["Digit3"]},
        {"type": "key", "keys": ["Digit7"]},
        {"type": "key", "keys": ["NumpadMultiply"]},
        {"type": "key", "keys": ["Digit1"]},
        {"type": "key", "keys": ["Digit9"]},
        {"type": "key", "keys": ["Enter"]},
    ]

    result = await runtime.run_burst(
        sid,
        actions,
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="precise-grounded-calculator-expression",
    )

    assert result["status"] == "completed"
    assert len(ocr.precise_regions) == 1
    assert ocr.precise_regions[0] == Region(
        x=0,
        y=0,
        width=shot["width"],
        height=shot["height"] * 0.25,
    )
    assert [call[1]["keys"] for call in _hid_calls(runtime)] == [
        ["Digit3"],
        ["Digit7"],
        ["NumpadMultiply"],
        ["Digit1"],
        ["Digit9"],
        ["Enter"],
    ]


async def test_small_ui_click_retries_precise_ocr_before_failing_closed(
    runtime: Runtime,
) -> None:
    class SmallLabelOCR:
        def __init__(self) -> None:
            self.precise_calls = 0

        async def ocr(self, image_path, region=None):
            del image_path, region
            return OCRResult(
                lines=[OCRLine(text="Styles", bbox=[140, 73, 158, 80])]
            )

        async def ocr_precise(self, image_path, region=None):
            del image_path
            self.precise_calls += 1
            assert region is not None
            assert region.width <= 120
            assert region.height <= 70
            return OCRResult(
                lines=[OCRLine(text="Title", bbox=[30, 30, 70, 60])]
            )

    ocr = SmallLabelOCR()
    runtime._screen_parser.ocr = ocr
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)

    result = await runtime.run_burst(
        sid,
        [{"type": "click", "x": 50, "y": 60}],
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"],
        idempotency_key="click-small-title-label",
    )

    assert result["status"] == "completed"
    assert ocr.precise_calls == 1
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
