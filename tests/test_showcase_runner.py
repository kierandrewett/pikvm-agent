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
    ShowcaseCampaignAlreadyRunning,
    ShowcaseCampaignLease,
    ShowcaseCampaignRecoveryRequired,
    ShowcaseManifest,
    VncAdapter,
    _campaign_recovery_blockers,
    _hid_print_timeout_s,
    _merge_reboot_attempts,
    _quiesce_run,
    _repair_recovered_reboot_status,
    _task_error_before_reboot,
    _windows_desktop_taskbar_visible,
    approval_disposition,
    approval_is_safe,
    load_showcase_manifest,
    paused_recovery_action,
    repeated_paused_error_limit_reached,
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
    assert payload["tasks"][0]["quiescence"]["status"] == "pending"
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


def test_hid_print_timeout_scales_past_default_for_guarded_key_cadence() -> None:
    assert _hid_print_timeout_s("short command") == 30.0
    assert _hid_print_timeout_s("x" * 279) == pytest.approx(93.7)


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
    assert (
        _task_error_before_reboot(
            {
                "error": (
                    "reboot failed: TimeoutError: Windows did not reach "
                    "a stable desktop"
                )
            }
        )
        is None
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


def test_recovered_reboot_reconciles_completed_task_without_rerunning() -> None:
    record = {
        "status": "failed",
        "error": (
            "reboot failed: TimeoutError: Windows did not reach "
            "a stable desktop"
        ),
        "task_error": None,
        "result": {"status": "completed"},
        "reboot": {"status": "ready", "transition_observed": True},
        "recording": "task-1/recording.webm",
    }

    assert _repair_recovered_reboot_status(record) is True
    assert record["status"] == "passed"
    assert record["error"] is None
    assert _repair_recovered_reboot_status(record) is False


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


def test_read_only_campaign_never_auto_approves_navigation_or_save() -> None:
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

    assert not approval_is_safe(click, mutates_workspace=False)
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


def test_campaign_refuses_a_new_unknown_click_approval() -> None:
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
        == "refuse"
    )


def test_mutating_campaign_refuses_unknown_coordinate_click() -> None:
    pending = {
        "approval_id": "approval-unknown-click",
        "risk": "unknown",
        "reason": "commit target requires human review",
        "proposed_action": {
            "actions": [
                {"type": "click", "x": 64, "y": 213, "button": "left"}
            ]
        },
    }

    assert not approval_is_safe(pending, mutates_workspace=True)


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
            "--only-task",
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
    assert calls[0]["only_task_id"] == "task-1"


@pytest.mark.asyncio
async def test_showcase_only_task_filters_before_adapter_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        """
schema_version: 1
campaign_id: selected-campaign
title: Selected campaign
provider: codex-fast
tasks:
  - task_id: task-1
    title: First
    category: Observation
    prompt: Describe the desktop.
  - task_id: task-2
    title: Second
    category: Observation
    prompt: Describe the taskbar.
""".strip(),
        encoding="utf-8",
    )
    captured: list[ShowcaseManifest] = []

    async def selected_runner(**kwargs):
        captured.append(kwargs["manifest"])
        return {"campaign_id": "selected-campaign", "status": "paused"}

    monkeypatch.setattr(
        showcase_runner,
        "_run_showcase_campaign_locked",
        selected_runner,
    )

    await showcase_runner.run_showcase_campaign(
        manifest_path=manifest_path,
        output_root=tmp_path / "output",
        harness_url="http://127.0.0.1:48001",
        adapter_url="http://127.0.0.1:48002",
        agent_token="a" * 32,
        operator_token="b" * 32,
        operator_origin="http://127.0.0.1:48001",
        only_task_id="task-2",
    )

    assert [task.task_id for task in captured[0].tasks] == ["task-2"]


@pytest.mark.asyncio
async def test_showcase_rejects_unknown_only_task_before_connecting(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        """
schema_version: 1
campaign_id: selected-campaign
title: Selected campaign
provider: codex-fast
tasks:
  - task_id: task-1
    title: Observe
    category: Observation
    prompt: Describe the desktop.
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="only task is not in manifest"):
        await showcase_runner.run_showcase_campaign(
            manifest_path=manifest_path,
            output_root=tmp_path / "output",
            harness_url="http://127.0.0.1:48001",
            adapter_url="http://127.0.0.1:48002",
            agent_token="a" * 32,
            operator_token="b" * 32,
            operator_origin="http://127.0.0.1:48001",
            only_task_id="missing-task",
        )


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


@pytest.mark.asyncio
async def test_showcase_refuses_a_second_campaign_runner_before_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        """
schema_version: 1
campaign_id: single-writer-campaign
title: Single writer campaign
provider: codex-fast
tasks:
  - task_id: task-1
    title: Observe
    category: Observation
    prompt: Describe the desktop.
""".strip(),
        encoding="utf-8",
    )
    entered_locked_runner = False

    async def must_not_run(**_kwargs: object) -> dict[str, object]:
        nonlocal entered_locked_runner
        entered_locked_runner = True
        return {}

    monkeypatch.setattr(
        showcase_runner,
        "_run_showcase_campaign_locked",
        must_not_run,
    )
    lease = ShowcaseCampaignLease.acquire(
        tmp_path / "output",
        "different-campaign-on-the-same-vm",
    )
    try:
        with pytest.raises(
            ShowcaseCampaignAlreadyRunning,
            match="already running in another local process",
        ):
            await showcase_runner.run_showcase_campaign(
                manifest_path=manifest_path,
                output_root=tmp_path / "output",
                harness_url="http://127.0.0.1:48001",
                adapter_url="http://127.0.0.1:48002",
                agent_token="a" * 32,
                operator_token="b" * 32,
                operator_origin="http://127.0.0.1:48001",
            )
    finally:
        lease.release()

    assert entered_locked_runner is False
    assert not (tmp_path / "output" / "single-writer-campaign").exists()


@pytest.mark.asyncio
async def test_showcase_releases_campaign_lease_after_runner_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        """
schema_version: 1
campaign_id: released-campaign
title: Released campaign
provider: codex-fast
tasks:
  - task_id: task-1
    title: Observe
    category: Observation
    prompt: Describe the desktop.
""".strip(),
        encoding="utf-8",
    )

    async def fail_locked_runner(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("synthetic runner failure")

    monkeypatch.setattr(
        showcase_runner,
        "_run_showcase_campaign_locked",
        fail_locked_runner,
    )
    with pytest.raises(RuntimeError, match="synthetic runner failure"):
        await showcase_runner.run_showcase_campaign(
            manifest_path=manifest_path,
            output_root=tmp_path / "output",
            harness_url="http://127.0.0.1:48001",
            adapter_url="http://127.0.0.1:48002",
            agent_token="a" * 32,
            operator_token="b" * 32,
            operator_origin="http://127.0.0.1:48001",
        )

    replacement = ShowcaseCampaignLease.acquire(
        tmp_path / "output",
        "released-campaign",
    )
    replacement.release()


def _write_campaign_cleanup_state(
    root: Path,
    *,
    campaign_id: str,
    updated_at: str,
    task_status: str,
    reboot_status: str,
    ready_at: str | None = None,
) -> None:
    campaign_root = root / campaign_id
    campaign_root.mkdir(parents=True)
    (campaign_root / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": campaign_id,
                "updated_at": updated_at,
                "tasks": [
                    {
                        "task_id": "task-1",
                        "status": task_status,
                        "reboot": {
                            "status": reboot_status,
                            "ready_at": ready_at,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_campaign_recovery_uses_the_latest_verified_reboot_as_watermark(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    _write_campaign_cleanup_state(
        output_root,
        campaign_id="last-clean",
        updated_at="2026-08-01T10:01:01+00:00",
        task_status="passed",
        reboot_status="ready",
        ready_at="2026-08-01T10:01:00+00:00",
    )
    _write_campaign_cleanup_state(
        output_root,
        campaign_id="interrupted",
        updated_at="2026-08-01T10:02:00+00:00",
        task_status="failed",
        reboot_status="blocked",
    )

    assert _campaign_recovery_blockers(
        output_root,
        current_campaign_id="new-campaign",
    ) == [("interrupted", ["task-1"])]
    _write_campaign_cleanup_state(
        output_root,
        campaign_id="later-cleanup",
        updated_at="2026-08-01T10:03:01+00:00",
        task_status="failed",
        reboot_status="ready",
        ready_at="2026-08-01T10:03:00+00:00",
    )

    assert _campaign_recovery_blockers(
        output_root,
        current_campaign_id="new-campaign",
    ) == []
    assert _campaign_recovery_blockers(
        output_root,
        current_campaign_id="interrupted",
    ) == []


@pytest.mark.asyncio
async def test_showcase_requires_recovery_before_connecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        """
schema_version: 1
campaign_id: new-campaign
title: New campaign
provider: codex-fast
tasks:
  - task_id: task-1
    title: Observe
    category: Observation
    prompt: Describe the desktop.
""".strip(),
        encoding="utf-8",
    )
    _write_campaign_cleanup_state(
        tmp_path / "output",
        campaign_id="interrupted",
        updated_at="2026-08-01T10:02:00+00:00",
        task_status="failed",
        reboot_status="blocked",
    )
    entered_locked_runner = False

    async def must_not_run(**_kwargs: object) -> dict[str, object]:
        nonlocal entered_locked_runner
        entered_locked_runner = True
        return {}

    monkeypatch.setattr(
        showcase_runner,
        "_run_showcase_campaign_locked",
        must_not_run,
    )

    with pytest.raises(
        ShowcaseCampaignRecoveryRequired,
        match=r"resume: interrupted \(task-1\)",
    ):
        await showcase_runner.run_showcase_campaign(
            manifest_path=manifest_path,
            output_root=tmp_path / "output",
            harness_url="http://127.0.0.1:48001",
            adapter_url="http://127.0.0.1:48002",
            agent_token="a" * 32,
            operator_token="b" * 32,
            operator_origin="http://127.0.0.1:48001",
        )

    assert entered_locked_runner is False
    assert not (tmp_path / "output" / "new-campaign").exists()


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
async def test_campaign_workspace_preflight_uses_segmented_visible_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, object]] = []
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
        result = await adapter.ensure_campaign_workspace()

    assert result["path"] == r"C:\PiKVM-Harness\workspace\codex-50"
    assert result["ready"] is True
    assert result["method"] == "visible_windows_run_segmented"
    assert printed == [
        r"cmd /d /c mkdir C:\PiKVM-Harness\workspace\codex-50 2>nul"
    ]
    assert {
        "key": "Enter",
        "state": True,
    } in [item["event"] for item in sent]


@pytest.mark.asyncio
async def test_campaign_workspace_preflight_preserves_a_prior_task_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, object]] = []
    printed: list[str] = []
    desktop_calls = 0
    monkeypatch.setattr(
        showcase_runner,
        "websocket_connect",
        _socket_factory(sent),
    )
    monkeypatch.setattr(
        showcase_runner.uuid,
        "uuid4",
        lambda: type("Uuid", (), {"hex": "a" * 32})(),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_snapshot_handler(printed))
    ) as client:
        adapter = VncAdapter(client, "http://127.0.0.1:48002")

        async def show_desktop() -> None:
            nonlocal desktop_calls
            desktop_calls += 1

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
        adapter.show_desktop = show_desktop  # type: ignore[method-assign]
        adapter.wait_until_ready = ready  # type: ignore[method-assign]
        result = await adapter.ensure_campaign_workspace(
            [
                (
                    r"C:\PiKVM-Harness\workspace\codex-50"
                    r"\text-10-exact.txt"
                )
            ]
        )

    preserved = (
        r"C:\PiKVM-Harness\workspace\codex-50"
        rf"\text-10-exact.txt.pikvm-prior-{'a' * 32}"
    )
    assert result["fresh_artifacts"] == [
        {
            "path": (
                r"C:\PiKVM-Harness\workspace\codex-50"
                r"\text-10-exact.txt"
            ),
            "preserved_as": preserved,
            "preservation_status": "requested_unverified",
        }
    ]
    assert desktop_calls == 2
    assert printed == [
        r"cmd /d /c mkdir C:\PiKVM-Harness\workspace\codex-50 2>nul",
        (
            r'cmd /d /c ren "C:\PiKVM-Harness\workspace\codex-50'
            r'\text-10-exact.txt" '
            rf'"text-10-exact.txt.pikvm-prior-{"a" * 32}"'
        ),
    ]
    assert max(map(len, printed)) < 200


@pytest.mark.parametrize(
    "fresh_artifact",
    [
        r"C:\Windows\system.ini",
        r"C:\PiKVM-Harness\workspace\codex-50",
        r"C:\PiKVM-Harness\workspace\codex-50\*.txt",
        r"C:\PiKVM-Harness\workspace\codex-50\..\private.txt",
    ],
)
def test_showcase_manifest_rejects_unbounded_fresh_artifacts(
    fresh_artifact: str,
) -> None:
    with pytest.raises(ValueError, match="fresh artifact"):
        ShowcaseManifest.model_validate(
            {
                "campaign_id": "bounded-fixture",
                "title": "Bounded fixture",
                "provider": "codex-fast",
                "tasks": [
                    {
                        "task_id": "text-10",
                        "title": "Type exact text",
                        "category": "Text entry",
                        "prompt": "Type exact text.",
                        "mutates_workspace": True,
                        "fresh_artifacts": [fresh_artifact],
                    }
                ],
            }
        )


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
async def test_run_dialog_survives_four_transient_vnc_modifier_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, object]] = []
    transitions = iter((False, False, False, False, True))
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
    ) == 5
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
async def test_reboot_preserves_transition_when_desktop_readiness_times_out() -> None:
    frame = Image.new("RGB", (1280, 800), "navy")
    buffer = BytesIO()
    frame.save(buffer, format="JPEG")
    async with httpx.AsyncClient() as client:
        adapter = VncAdapter(client, "http://127.0.0.1:48002")

        async def current_frame() -> bytes:
            return buffer.getvalue()

        async def reboot() -> None:
            return None

        async def transition(**_kwargs) -> bool:
            return True

        async def never_ready(**_kwargs):
            raise TimeoutError("slow Windows boot")

        adapter.frame = current_frame  # type: ignore[method-assign]
        adapter._reboot = reboot  # type: ignore[method-assign]
        adapter._wait_for_boot_transition = transition  # type: ignore[method-assign]
        adapter.wait_until_ready = never_ready  # type: ignore[method-assign]
        result = await adapter.reboot_and_wait(timeout_s=30)

    assert result["ready"] is False
    assert result["transition_observed"] is True
    assert result["error"] == "slow Windows boot"
    assert result["attempts"] == [
        {
            "attempt": 1,
            "duration_ms": result["attempts"][0]["duration_ms"],
            "transition_observed": True,
            "ready_observed": False,
            "ready_frame_sha256": None,
            "error": "slow Windows boot",
        }
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
async def test_campaign_client_aborts_managed_run_before_reset() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"run_id": "run-7", "status": "aborted"},
        )

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
        stopped = await harness.abort(
            "run-7",
            "campaign task concluded before mandatory reboot",
        )

    assert stopped["status"] == "aborted"
    assert [request.url.path for request in requests] == [
        "/api/runs/run-7/abort"
    ]
    assert json.loads(requests[0].content) == {
        "reason": "campaign task concluded before mandatory reboot"
    }


@pytest.mark.asyncio
async def test_quiescence_retries_until_run_is_terminal() -> None:
    statuses = iter(("running", "aborted"))
    calls: list[tuple[str, str]] = []

    class Harness:
        async def abort(self, run_id: str, reason: str) -> dict[str, str]:
            calls.append((run_id, reason))
            return {"run_id": run_id, "status": next(statuses)}

    result = await _quiesce_run(
        Harness(),  # type: ignore[arg-type]
        "run-8",
        retry_delay_s=0,
    )

    assert result["status"] == "aborted"
    assert result["attempts"] == 2
    assert calls == [
        ("run-8", "campaign task concluded before mandatory reboot"),
        ("run-8", "campaign task concluded before mandatory reboot"),
    ]


@pytest.mark.asyncio
async def test_campaign_quiesces_run_before_mandatory_reboot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        """
schema_version: 1
campaign_id: quiescence-campaign
title: Quiescence campaign
provider: codex-fast
tasks:
  - task_id: task-1
    title: Observe
    category: Observation
    prompt: Describe the desktop.
""".strip(),
        encoding="utf-8",
    )
    lifecycle: list[str] = []

    class FakeHarness:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def create(
            self,
            _task: object,
            _provider: str,
        ) -> dict[str, object]:
            return {"run_id": "run-9", "status": "running"}

        async def get(self, _run_id: str) -> dict[str, object]:
            return {
                "run_id": "run-9",
                "status": "completed",
                "event_count": 12,
            }

        async def performance(self, _run_id: str) -> None:
            return None

        async def abort(
            self,
            _run_id: str,
            _reason: str,
        ) -> dict[str, str]:
            lifecycle.append("abort")
            return {"run_id": "run-9", "status": "completed"}

    class FakeAdapter:
        frame_url = "http://adapter/frame"

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def wait_until_ready(
            self,
            **_kwargs: object,
        ) -> dict[str, object]:
            return {"ready": True}

        async def show_desktop(self) -> None:
            return None

        async def reboot_and_wait(
            self,
            **_kwargs: object,
        ) -> dict[str, object]:
            lifecycle.append("reboot")
            return {
                "transition_observed": True,
                "ready": True,
                "duration_ms": 25,
                "attempts": [],
            }

    class FakeRecorder:
        def __init__(
            self,
            *,
            output_dir: Path,
            **_kwargs: object,
        ) -> None:
            self.output_dir = output_dir

        async def start(self) -> None:
            return None

        async def capture_poster(self) -> None:
            return None

        async def stop(self) -> tuple[Path, Path]:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            recording = self.output_dir / "recording.webm"
            poster = self.output_dir / "poster.jpg"
            recording.write_bytes(b"video")
            poster.write_bytes(b"poster")
            return recording, poster

    monkeypatch.setattr(
        showcase_runner,
        "HarnessCampaignClient",
        FakeHarness,
    )
    monkeypatch.setattr(showcase_runner, "VncAdapter", FakeAdapter)
    monkeypatch.setattr(showcase_runner, "FrameRecorder", FakeRecorder)

    result = await showcase_runner.run_showcase_campaign(
        manifest_path=manifest_path,
        output_root=tmp_path / "output",
        harness_url="http://harness",
        adapter_url="http://adapter",
        agent_token="a" * 32,
        operator_token="b" * 32,
        operator_origin="http://harness",
    )

    task = result["tasks"][0]
    assert lifecycle == ["abort", "reboot"]
    assert task["status"] == "passed"
    assert task["quiescence"]["status"] == "confirmed"
    assert task["quiescence"]["run_status"] == "completed"
    assert task["reboot"]["status"] == "ready"


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


@pytest.mark.asyncio
async def test_text_showcase_requires_fresh_editor_input() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"run_id": "text-09", "status": "running"},
        )

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
        task = next(
            task
            for task in load_showcase_manifest(
                Path(__file__).parents[1] / "bench" / "codex-50-tasks.yaml"
            ).tasks
            if task.task_id == "text-09"
        )
        await harness.create(task, "codex-fast")

    prompt = json.loads(requests[0].content)["task"]
    normalized = " ".join(prompt.split())
    assert "new blank document" in normalized
    assert (
        "type every requested content character during this run"
        in normalized
    )
    assert "restored or pre-existing document content" in normalized
    assert prompt.endswith(f"Task:\n{task.prompt}")


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


def test_paused_checkpoint_waits_while_background_activity_is_in_flight() -> None:
    assert paused_recovery_action(
        event_count=231,
        observed_cursor=231,
        continued_cursor=223,
        active_activity={
            "kind": "tool",
            "tool": "pikvm_run_burst",
        },
    ) == "wait"


def test_identical_paused_error_stops_before_a_third_provider_retry() -> None:
    recoveries = [
        {"error": "unverified exact input"},
        {"error": "unverified exact input"},
    ]

    assert repeated_paused_error_limit_reached(
        recoveries,
        error="unverified exact input",
    )
    assert not repeated_paused_error_limit_reached(
        recoveries,
        error="different recoverable pause",
    )
    assert not repeated_paused_error_limit_reached(
        recoveries[:1],
        error="unverified exact input",
    )
