from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness import showcase_runner
from pikvm_agent.harness.showcase_runner import (
    CampaignWriter,
    FrameRecorder,
    HarnessCampaignClient,
    ShowcaseManifest,
    VncAdapter,
    _merge_reboot_attempts,
    _task_error_before_reboot,
    approval_disposition,
    approval_is_safe,
    load_showcase_manifest,
)


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
    assert payload["tasks"][0]["reboot"]["status"] == "pending"
    assert payload["tasks"][0]["recoveries"] == []
    assert not writer.path.with_suffix(".json.tmp").exists()


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


def test_disposable_campaign_allows_bounded_navigation_keys() -> None:
    enter = {
        "approval_id": "approval-enter",
        "risk": "unknown",
        "reason": "bare Enter may commit the focused surface",
        "proposed_action": {
            "actions": [{"type": "key", "keys": ["ENTER"]}]
        },
    }

    assert approval_is_safe(enter, mutates_workspace=False)


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
            "--output-root",
            str(tmp_path / "output"),
            "--harness-url",
            "http://127.0.0.1:48001",
            "--adapter-url",
            "http://127.0.0.1:48002",
            "--operator-origin",
            "http://127.0.0.1:48001",
        ],
        env={
            "PIKVM_HARNESS_AGENT_TOKEN": "a" * 32,
            "PIKVM_HARNESS_TOKEN": "b" * 32,
        },
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "completed"
    assert calls[0]["adapter_url"] == "http://127.0.0.1:48002"
    assert calls[0]["max_same_run_recoveries"] == 8


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
async def test_reboot_replaces_any_existing_run_dialog_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, object]] = []
    printed: list[str] = []

    class Socket:
        acknowledgement = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def send(self, message: str) -> None:
            sent.append(json.loads(message))
            self.acknowledgement += 1

        async def recv(self) -> str:
            return json.dumps(
                {
                    "event_type": "lab_ack",
                    "event": {"sequence": self.acknowledgement},
                }
            )

    def connect(*_args: object, **_kwargs: object) -> Socket:
        return Socket()

    async def no_sleep(_seconds: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        printed.append(request.content.decode())
        return httpx.Response(200)

    monkeypatch.setattr(showcase_runner, "websocket_connect", connect)
    monkeypatch.setattr(showcase_runner.asyncio, "sleep", no_sleep)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        await VncAdapter(client, "http://127.0.0.1:48002")._reboot()

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
    assert printed == ["shutdown /r /t 0 /f"]


@pytest.mark.asyncio
async def test_show_desktop_acknowledges_each_key_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, object]] = []

    class Socket:
        acknowledgement = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def send(self, message: str) -> None:
            sent.append(json.loads(message))
            self.acknowledgement += 1

        async def recv(self) -> str:
            return json.dumps(
                {
                    "event_type": "lab_ack",
                    "event": {"sequence": self.acknowledgement},
                }
            )

    def connect(*_args: object, **_kwargs: object) -> Socket:
        return Socket()

    monkeypatch.setattr(showcase_runner, "websocket_connect", connect)
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
