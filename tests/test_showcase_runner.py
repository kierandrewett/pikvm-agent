from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import httpx
import pytest
import yaml
from PIL import Image, ImageDraw
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness import showcase_runner
from pikvm_agent.harness.showcase_runner import (
    CampaignWriter,
    FrameRecorder,
    HarnessCampaignClient,
    ShowcaseManifest,
    VncAdapter,
    _windows_desktop_taskbar_visible,
    _merge_reboot_attempts,
    _task_error_before_reboot,
    approval_disposition,
    approval_is_safe,
    load_showcase_manifest,
    paused_recovery_action,
)


class _AcknowledgingSocket:
    def __init__(self, sent: list[dict[str, object]]) -> None:
        self.sent = sent
        self.acknowledgement = 0

    async def __aenter__(self) -> _AcknowledgingSocket:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))
        self.acknowledgement += 1

    async def recv(self) -> str:
        return json.dumps(
            {
                "event_type": "lab_ack",
                "event": {"sequence": self.acknowledgement},
            }
        )


def _socket_factory(
    sent: list[dict[str, object]],
) -> Callable[..., _AcknowledgingSocket]:
    def connect(*_args: object, **_kwargs: object) -> _AcknowledgingSocket:
        return _AcknowledgingSocket(sent)

    return connect


def _snapshot_handler(
    printed: list[str],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/streamer/snapshot":
            buffer = BytesIO()
            Image.new("RGB", (320, 200), "navy").save(
                buffer,
                format="JPEG",
            )
            return httpx.Response(200, content=buffer.getvalue())
        printed.append(request.content.decode())
        return httpx.Response(200)

    return handler


def test_public_showcase_manifest_contains_fifty_distinct_codex_tasks() -> None:
    manifest = load_showcase_manifest(
        Path(__file__).parents[1] / "bench" / "codex-50-tasks.yaml"
    )

    assert manifest.provider == "codex-fast"
    assert len(manifest.tasks) == 50
    assert len({task.task_id for task in manifest.tasks}) == 50
    assert {
        "Observation",
        "Calculator",
        "Text entry",
        "Code entry",
        "File management",
        "Microsoft Excel",
        "Microsoft Word",
    } == {task.category for task in manifest.tasks}


def test_live_showcase_config_locks_every_role_to_codex_fast() -> None:
    config = yaml.safe_load(
        (
            Path(__file__).parents[1]
            / "bench"
            / "configs"
            / "codex-vnc-showcase.yaml"
        ).read_text(encoding="utf-8")
    )

    assert config["providers"]["codex-fast"]["model"] == "gpt-5.6-terra"
    assert config["providers"]["codex-fast"]["reasoning_effort"] == "low"
    assert config["providers"]["codex-fast"]["service_tier"] == "priority"
    assert config["routes"] == {
        "reasoner": ["codex-fast"],
        "controller": ["codex-fast"],
        "verifier": ["codex-fast"],
    }


def test_campaign_writer_declares_reboot_isolation_before_first_task(
    tmp_path: Path,
) -> None:
    manifest = ShowcaseManifest.model_validate(
        {
            "schema_version": 1,
            "campaign_id": "campaign-1",
            "title": "One task",
            "provider": "codex-fast",
            "tasks": [
                {
                    "task_id": "task-1",
                    "title": "Observe",
                    "category": "Observation",
                    "prompt": "Describe the desktop.",
                }
            ],
        }
    )

    writer = CampaignWriter(manifest, tmp_path)
    payload = json.loads(writer.path.read_text(encoding="utf-8"))

    assert payload["isolation"]["reboot_after_every_task"] is True
    assert payload["isolation"]["ready_gate"] == (
        "stable Windows desktop with visible taskbar"
    )
    assert payload["tasks"][0]["reboot"]["status"] == "pending"
    assert payload["tasks"][0]["recoveries"] == []
    assert not writer.path.with_suffix(".json.tmp").exists()


def test_windows_desktop_gate_rejects_login_and_black_frames() -> None:
    login = Image.new("RGB", (1280, 800), (14, 44, 105))
    login_draw = ImageDraw.Draw(login)
    login_draw.ellipse((600, 250, 680, 330), fill=(235, 235, 240))
    black = Image.new("RGB", (1280, 800), (20, 20, 20))

    assert not _windows_desktop_taskbar_visible(login)
    assert not _windows_desktop_taskbar_visible(black)


def test_windows_desktop_gate_accepts_a_visible_taskbar() -> None:
    desktop = Image.new("RGB", (1280, 800), (20, 55, 120))
    draw = ImageDraw.Draw(desktop)
    draw.rectangle((0, 760, 1279, 799), fill=(18, 20, 24))
    for left in (12, 55, 100, 145, 190):
        draw.rectangle((left, 770, left + 18, 790), fill=(210, 220, 235))

    assert _windows_desktop_taskbar_visible(desktop)


def test_campaign_writer_restores_existing_run_without_replacing_it(
    tmp_path: Path,
) -> None:
    manifest = ShowcaseManifest.model_validate(
        {
            "schema_version": 1,
            "campaign_id": "campaign-1",
            "title": "One task",
            "provider": "codex-fast",
            "tasks": [
                {
                    "task_id": "task-1",
                    "title": "Observe",
                    "category": "Observation",
                    "prompt": "Describe the desktop.",
                }
            ],
        }
    )
    first = CampaignWriter(manifest, tmp_path)
    first.task("task-1")["status"] = "running"
    first.task("task-1")["run_id"] = "durable-run-7"
    first.payload["current_task_id"] = "task-1"
    first.payload["current_run_id"] = "durable-run-7"
    first.flush()

    restored = CampaignWriter(manifest, tmp_path)

    assert restored.task("task-1")["status"] == "running"
    assert restored.task("task-1")["run_id"] == "durable-run-7"
    assert restored.payload["current_run_id"] == "durable-run-7"


def test_reboot_retry_preserves_task_error_and_prior_attempts() -> None:
    record = {
        "error": (
            "task exceeded the campaign time limit; "
            "reboot command did not produce a visible boot transition"
        )
    }

    assert _task_error_before_reboot(record) == (
        "task exceeded the campaign time limit"
    )
    assert _merge_reboot_attempts(
        [{"attempt": 1, "transition_observed": False}],
        [
            {"attempt": 1, "transition_observed": False},
            {"attempt": 2, "transition_observed": True},
        ],
    ) == [
        {"attempt": 1, "transition_observed": False},
        {"attempt": 2, "transition_observed": False},
        {"attempt": 3, "transition_observed": True},
    ]


def test_workspace_approval_allowlist_rejects_communications_and_shutdown() -> None:
    local_edit = {
        "approval_id": "approval-1",
        "risk": "medium",
        "proposed_action": {
            "actions": [
                {"type": "type_text", "text": "proof"},
                {"type": "key", "keys": ["CTRL", "S"]},
            ]
        },
    }
    send_message = {
        **local_edit,
        "reason": "Send a Teams message",
    }
    shutdown = {
        **local_edit,
        "proposed_action": {
            "actions": [
                {
                    "type": "type_text",
                    "text": "shutdown /r /t 0",
                }
            ]
        },
    }

    assert approval_is_safe(local_edit, mutates_workspace=True)
    assert not approval_is_safe(local_edit, mutates_workspace=False)
    assert not approval_is_safe(send_message, mutates_workspace=True)
    assert not approval_is_safe(shutdown, mutates_workspace=True)


def test_read_only_campaign_approves_navigation_but_not_save_shortcut() -> None:
    click = {
        "approval_id": "approval-click",
        "risk": "local_file_edit",
        "reason": "commit target requires human review",
        "proposed_action": {
            "actions": [
                {"type": "click", "x": 605, "y": 722, "button": "left"}
            ]
        },
    }
    shortcut = {
        **click,
        "proposed_action": {
            "actions": [{"type": "key", "keys": ["CTRL", "S"]}]
        },
    }

    assert approval_is_safe(click, mutates_workspace=False)
    assert not approval_is_safe(shortcut, mutates_workspace=False)


def test_read_only_campaign_refuses_unknown_bare_enter() -> None:
    enter = {
        "approval_id": "approval-enter",
        "risk": "unknown",
        "reason": "bare Enter may commit the focused surface",
        "proposed_action": {
            "actions": [{"type": "key", "keys": ["ENTER"]}]
        },
    }

    assert not approval_is_safe(enter, mutates_workspace=False)


def test_campaign_waits_while_an_approved_request_is_still_resolving() -> None:
    pending = {
        "approval_id": "approval-click",
        "risk": "unknown",
        "reason": "commit target requires human review",
        "proposed_action": {
            "actions": [
                {"type": "click", "x": 774, "y": 389, "button": "left"}
            ]
        },
    }

    assert (
        approval_disposition(
            pending,
            approved_ids={"approval-click"},
            mutates_workspace=False,
        )
        == "wait"
    )
    assert (
        approval_disposition(
            pending,
            approved_ids=set(),
            mutates_workspace=False,
        )
        == "approve"
    )


def test_showcase_cli_runs_async_campaign(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        """
schema_version: 1
campaign_id: cli-campaign
title: CLI campaign
provider: codex-fast
tasks:
  - task_id: task-1
    title: Observe
    category: Observation
    prompt: Describe the desktop.
""".strip(),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return {"campaign_id": "cli-campaign", "status": "completed"}

    monkeypatch.setattr(showcase_runner, "run_showcase_campaign", fake_run)
    result = CliRunner().invoke(
        app,
        [
            "harness",
            "showcase-run",
            "--manifest",
            str(manifest_path),
            "--harness-url",
            "http://127.0.0.1:48001",
            "--adapter-url",
            "http://127.0.0.1:48002",
            "--operator-origin",
            "http://127.0.0.1:48001",
            "--stop-after-task",
            "task-1",
        ],
        env={
            "PIKVM_HARNESS_AGENT_TOKEN": "a" * 32,
            "PIKVM_HARNESS_TOKEN": "b" * 32,
            "XDG_DATA_HOME": str(tmp_path / "xdg"),
        },
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "completed"
    assert calls[0]["adapter_url"] == "http://127.0.0.1:48002"
    assert calls[0]["output_root"] == (
        tmp_path / "xdg" / "pikvm-agent" / "showcases"
    )
    assert calls[0]["max_same_run_recoveries"] == 8
    assert calls[0]["stop_after_task_id"] == "task-1"


@pytest.mark.asyncio
async def test_showcase_rejects_unknown_stop_after_task_before_connecting(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        """
schema_version: 1
campaign_id: checkpoint-campaign
title: Checkpoint campaign
provider: codex-fast
tasks:
  - task_id: task-1
    title: Observe
    category: Observation
    prompt: Describe the desktop.
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="stop-after task is not in manifest",
    ):
        await showcase_runner.run_showcase_campaign(
            manifest_path=manifest_path,
            output_root=tmp_path / "output",
            harness_url="http://127.0.0.1:48001",
            adapter_url="http://127.0.0.1:48002",
            agent_token="a" * 32,
            operator_token="b" * 32,
            operator_origin="http://127.0.0.1:48001",
            stop_after_task_id="missing-task",
        )


def test_frame_recorder_encodes_browser_native_webm(tmp_path: Path) -> None:
    recorder = FrameRecorder(
        client=None,  # type: ignore[arg-type]
        frame_url="http://127.0.0.1/frame",
        output_dir=tmp_path,
        interval_s=0.5,
    )
    recorder.frames_dir.mkdir()
    for index, color in enumerate(("navy", "white")):
        Image.new("RGB", (320, 200), color).save(
            recorder.frames_dir / f"frame-{index:06d}.jpg"
        )

    recorder._encode()

    assert recorder.recording.suffix == ".webm"
    assert recorder.recording.stat().st_size > 0


@pytest.mark.asyncio
async def test_frame_recorder_exits_quietly_when_client_closes(
    tmp_path: Path,
) -> None:
    class ClosedClient:
        async def get(self, _url: str):
            raise RuntimeError(
                "Cannot send a request, as the client has been closed."
            )

    recorder = FrameRecorder(
        client=ClosedClient(),  # type: ignore[arg-type]
        frame_url="http://adapter.test/frame",
        output_dir=tmp_path,
        interval_s=0.01,
    )

    await recorder.start()
    assert recorder._task is not None
    await asyncio.wait_for(recorder._task, timeout=0.5)

    assert recorder._task.exception() is None


@pytest.mark.asyncio
async def test_reboot_replaces_any_existing_run_dialog_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, object]] = []
    printed: list[str] = []
    slept: list[float] = []

    async def no_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(
        showcase_runner,
        "websocket_connect",
        _socket_factory(sent),
    )
    monkeypatch.setattr(showcase_runner.asyncio, "sleep", no_sleep)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_snapshot_handler(printed))
    ) as client:
        adapter = VncAdapter(client, "http://127.0.0.1:48002")

        async def visible_transition(**_kwargs: object) -> bool:
            return True

        async def ready(**_kwargs: object) -> dict[str, object]:
            return {
                "ready": True,
                "frame_sha256": "f" * 64,
            }

        adapter._wait_for_run_dialog = (  # type: ignore[method-assign]
            visible_transition
        )
        adapter.wait_until_ready = ready  # type: ignore[method-assign]
        await adapter._reboot()

    key_events = [
        item["event"]
        for item in sent
        if item.get("event_type") == "key"
    ]
    select_all = [
        {"key": "ControlLeft", "state": True},
        {"key": "KeyA", "state": True},
        {"key": "KeyA", "state": False},
        {"key": "ControlLeft", "state": False},
    ]
    start = key_events.index(select_all[0])
    assert key_events[start : start + 4] == select_all
    assert slept[:4] == [0.5, 0.25, 0.1, 0.1]
    assert printed == ["shutdown /r /t 0 /f"]


@pytest.mark.asyncio
async def test_reboot_retries_run_until_the_dialog_visibly_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, object]] = []
    transitions = iter((False, True))
    printed: list[str] = []

    monkeypatch.setattr(
        showcase_runner,
        "websocket_connect",
        _socket_factory(sent),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_snapshot_handler(printed))
    ) as client:
        adapter = VncAdapter(client, "http://127.0.0.1:48002")

        async def visible_transition(**_kwargs: object) -> bool:
            return next(transitions)

        async def ready(**_kwargs: object) -> dict[str, object]:
            return {
                "ready": True,
                "frame_sha256": "f" * 64,
            }

        adapter._wait_for_run_dialog = (  # type: ignore[method-assign]
            visible_transition
        )
        adapter.wait_until_ready = ready  # type: ignore[method-assign]
        await adapter._reboot()

    key_events = [
        item["event"]
        for item in sent
        if item.get("event_type") == "key"
    ]
    assert sum(
        event == {"key": "KeyR", "state": True}
        for event in key_events
    ) == 2
    assert printed == ["shutdown /r /t 0 /f"]


@pytest.mark.asyncio
async def test_run_dialog_detection_accepts_a_sustained_lower_left_change() -> None:
    baseline = Image.new("RGB", (1280, 800), "navy")
    changed = baseline.copy()
    changed.paste("white", (10, 610, 300, 790))
    baseline_buffer = BytesIO()
    changed_buffer = BytesIO()
    baseline.save(baseline_buffer, format="JPEG")
    changed.save(changed_buffer, format="JPEG")
    frames = iter((changed_buffer.getvalue(), changed_buffer.getvalue()))
    async with httpx.AsyncClient() as client:
        adapter = VncAdapter(client, "http://127.0.0.1:48002")

        async def changed_frame() -> bytes:
            return next(frames, changed_buffer.getvalue())

        adapter.frame = changed_frame  # type: ignore[method-assign]
        observed = await adapter._wait_for_run_dialog(
            baseline=baseline_buffer.getvalue(),
            timeout_s=1,
        )

    assert observed is True


@pytest.mark.asyncio
async def test_run_dialog_detection_rejects_a_transient_lower_left_change() -> None:
    baseline = Image.new("RGB", (1280, 800), "navy")
    changed = baseline.copy()
    changed.paste("white", (10, 610, 300, 790))
    baseline_buffer = BytesIO()
    changed_buffer = BytesIO()
    baseline.save(baseline_buffer, format="JPEG")
    changed.save(changed_buffer, format="JPEG")
    frames = iter(
        (
            changed_buffer.getvalue(),
            baseline_buffer.getvalue(),
            baseline_buffer.getvalue(),
        )
    )
    async with httpx.AsyncClient() as client:
        adapter = VncAdapter(client, "http://127.0.0.1:48002")

        async def transient_frame() -> bytes:
            return next(frames, baseline_buffer.getvalue())

        adapter.frame = transient_frame  # type: ignore[method-assign]
        observed = await adapter._wait_for_run_dialog(
            baseline=baseline_buffer.getvalue(),
            timeout_s=0.8,
        )

    assert observed is False


@pytest.mark.asyncio
async def test_show_desktop_acknowledges_each_key_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, object]] = []

    monkeypatch.setattr(
        showcase_runner,
        "websocket_connect",
        _socket_factory(sent),
    )
    async with httpx.AsyncClient() as client:
        await VncAdapter(
            client,
            "http://127.0.0.1:48002",
        ).show_desktop()

    assert [item["event"] for item in sent] == [
        {"key": "Escape", "state": True},
        {"key": "Escape", "state": False},
        {"key": "MetaLeft", "state": True},
        {"key": "KeyD", "state": True},
        {"key": "KeyD", "state": False},
        {"key": "MetaLeft", "state": False},
    ]


@pytest.mark.asyncio
async def test_reboot_transition_accepts_a_console_resolution_change() -> None:
    baseline = Image.new("RGB", (1280, 800), "navy")
    changed = Image.new("RGB", (2048, 1280), "navy")
    baseline_buffer = BytesIO()
    changed_buffer = BytesIO()
    baseline.save(baseline_buffer, format="JPEG")
    changed.save(changed_buffer, format="JPEG")
    async with httpx.AsyncClient() as client:
        adapter = VncAdapter(client, "http://127.0.0.1:48002")

        async def changed_frame() -> bytes:
            return changed_buffer.getvalue()

        adapter.frame = changed_frame  # type: ignore[method-assign]
        observed = await adapter._wait_for_boot_transition(
            baseline=baseline_buffer.getvalue(),
            timeout_s=1,
        )

    assert observed is True


@pytest.mark.asyncio
async def test_reboot_transition_accepts_a_sustained_meaningful_frame_change() -> None:
    baseline = Image.new("RGB", (1280, 800), "navy")
    changed = Image.new("RGB", (1280, 800), "white")
    baseline_buffer = BytesIO()
    changed_buffer = BytesIO()
    baseline.save(baseline_buffer, format="JPEG")
    changed.save(changed_buffer, format="JPEG")
    async with httpx.AsyncClient() as client:
        adapter = VncAdapter(client, "http://127.0.0.1:48002")

        async def changed_frame() -> bytes:
            return changed_buffer.getvalue()

        adapter.frame = changed_frame  # type: ignore[method-assign]
        observed = await adapter._wait_for_boot_transition(
            baseline=baseline_buffer.getvalue(),
            timeout_s=2,
        )

    assert observed is True


@pytest.mark.asyncio
async def test_reboot_transition_rejects_a_transient_dialog_change() -> None:
    baseline = Image.new("RGB", (1280, 800), "navy")
    changed = Image.new("RGB", (1280, 800), "white")
    baseline_buffer = BytesIO()
    changed_buffer = BytesIO()
    baseline.save(baseline_buffer, format="JPEG")
    changed.save(changed_buffer, format="JPEG")
    frames = iter(
        (
            changed_buffer.getvalue(),
            changed_buffer.getvalue(),
            baseline_buffer.getvalue(),
            baseline_buffer.getvalue(),
        )
    )
    async with httpx.AsyncClient() as client:
        adapter = VncAdapter(client, "http://127.0.0.1:48002")

        async def transient_frame() -> bytes:
            return next(frames, baseline_buffer.getvalue())

        adapter.frame = transient_frame  # type: ignore[method-assign]
        observed = await adapter._wait_for_boot_transition(
            baseline=baseline_buffer.getvalue(),
            timeout_s=1.6,
        )

    assert observed is False


@pytest.mark.asyncio
async def test_reboot_retries_until_a_transition_is_proven() -> None:
    frame = Image.new("RGB", (1280, 800), "navy")
    buffer = BytesIO()
    frame.save(buffer, format="JPEG")
    transitions = iter((False, True))
    reboot_calls = 0
    async with httpx.AsyncClient() as client:
        adapter = VncAdapter(client, "http://127.0.0.1:48002")

        async def current_frame() -> bytes:
            return buffer.getvalue()

        async def reboot() -> None:
            nonlocal reboot_calls
            reboot_calls += 1

        async def transition(**_kwargs) -> bool:
            return next(transitions)

        async def ready(**_kwargs):
            return {
                "ready": True,
                "frame_sha256": "f" * 64,
                "luminance": 20,
                "samples": 8,
            }

        async def show_desktop() -> None:
            return None

        adapter.frame = current_frame  # type: ignore[method-assign]
        adapter._reboot = reboot  # type: ignore[method-assign]
        adapter._wait_for_boot_transition = transition  # type: ignore[method-assign]
        adapter.wait_until_ready = ready  # type: ignore[method-assign]
        adapter.show_desktop = show_desktop  # type: ignore[method-assign]
        result = await adapter.reboot_and_wait(timeout_s=30)

    assert reboot_calls == 2
    assert result["transition_observed"] is True
    assert [attempt["transition_observed"] for attempt in result["attempts"]] == [
        False,
        True,
    ]


@pytest.mark.asyncio
async def test_same_run_recovery_uses_continue_without_creating_a_task() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"run_id": "run-7", "status": "paused"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        harness = HarnessCampaignClient(
            client,
            base_url="http://harness",
            agent_token="a" * 32,
            operator_token="b" * 32,
            operator_origin="http://harness",
        )
        continued = await harness.continue_run("run-7")

    assert continued is True
    assert [request.url.path for request in requests] == [
        "/api/runs/run-7/continue"
    ]
    assert requests[0].url.params["background"] == "true"


@pytest.mark.asyncio
async def test_showcase_creates_a_computer_run_without_assistant_routing() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"run_id": "run-8", "status": "running"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        harness = HarnessCampaignClient(
            client,
            base_url="http://harness",
            agent_token="a" * 32,
            operator_token="b" * 32,
            operator_origin="http://harness",
        )
        task = load_showcase_manifest(
            Path(__file__).parents[1] / "bench" / "codex-50-tasks.yaml"
        ).tasks[0]
        await harness.create(task, "codex-fast")

    body = json.loads(requests[0].content)
    assert body["mode"] == "computer"
    assert body["task"].endswith(f"Task:\n{task.prompt}")


def test_paused_checkpoint_is_continued_only_once_until_it_advances() -> None:
    assert paused_recovery_action(
        event_count=59,
        observed_cursor=None,
        continued_cursor=None,
    ) == "observe"
    assert paused_recovery_action(
        event_count=59,
        observed_cursor=59,
        continued_cursor=None,
    ) == "continue"
    assert paused_recovery_action(
        event_count=59,
        observed_cursor=59,
        continued_cursor=59,
    ) == "wait"
    assert paused_recovery_action(
        event_count=64,
        observed_cursor=59,
        continued_cursor=59,
    ) == "observe"
