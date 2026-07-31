"""Run and record an isolated campaign of real managed computer tasks."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

import httpx
import yaml
from PIL import Image, ImageStat
from pydantic import BaseModel, ConfigDict, Field, model_validator
from websockets.asyncio.client import connect as websocket_connect

from pikvm_agent.vision.frame_diff import fingerprint, fp_meaningful_change

TERMINAL_STATUSES = {"completed", "failed", "rejected", "blocked", "aborted"}
CAMPAIGN_GUARD = """\
This is one isolated acceptance task on a disposable Windows VM.
Use the managed computer tools and visible Windows UI to perform the task.
Begin from the surfaced Windows desktop. To launch an app, always use Win+R
and type its executable; never use the bare Windows key, Start-menu clicks,
taskbar icons, restored windows, or the Alt+Tab switcher.
Do not use email, chat, social, cloud consoles, downloads, or external network
services. Do not delete data. Any file mutation must remain strictly inside
C:\\PiKVM-Harness\\workspace\\codex-50. Verify the result before finishing.
"""
FORBIDDEN_APPROVAL_TERMS = frozenset(
    {
        "delete",
        "email",
        "erase",
        "format",
        "message",
        "network",
        "registry",
        "remove",
        "send",
        "shutdown",
        "teams",
        "upload",
    }
)
ALLOWED_ACTION_TYPES = frozenset(
    {
        "click",
        "double_click",
        "key",
        "move",
        "scroll",
        "spreadsheet_grid",
        "type_text",
        "wait",
        "wait_for_change",
        "wait_for_stable_screen",
    }
)
READ_ONLY_NAVIGATION_ACTION_TYPES = frozenset(
    {
        "click",
        "double_click",
        "move",
        "scroll",
        "wait",
        "wait_for_change",
        "wait_for_stable_screen",
    }
)
READ_ONLY_NAVIGATION_KEYS = frozenset(
    {
        frozenset({"ALT", "TAB"}),
        frozenset({"CTRL", "A"}),
        frozenset({"CTRL", "C"}),
        frozenset({"ESC"}),
        frozenset({"META"}),
        frozenset({"META", "R"}),
        frozenset({"SHIFT", "TAB"}),
        frozenset({"TAB"}),
        frozenset({"WIN"}),
        frozenset({"WIN", "R"}),
    }
)
ApprovalDisposition = Literal["approve", "refuse", "wait"]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def default_showcase_output_root() -> Path:
    """Return durable per-user storage for campaign state and recordings."""

    data_home = Path(
        os.environ.get(
            "XDG_DATA_HOME",
            str(Path.home() / ".local" / "share"),
        )
    )
    return data_home / "pikvm-agent" / "showcases"


def _task_error_before_reboot(record: dict[str, Any]) -> str | None:
    task_error = record.get("task_error")
    if task_error is not None:
        return str(task_error) or None
    error = str(record.get("error") or "")
    for suffix in (
        "; reboot command did not produce a visible boot transition",
        "; reboot failed:",
    ):
        marker = error.find(suffix)
        if marker >= 0:
            error = error[:marker]
            break
    return error or None


def _merge_reboot_attempts(
    existing: list[dict[str, Any]],
    latest: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = [dict(attempt) for attempt in existing]
    for attempt in latest:
        merged.append(
            {
                **attempt,
                "attempt": len(merged) + 1,
            }
        )
    return merged


class ShowcaseTaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    title: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=20_000)
    mutates_workspace: bool = False


class ShowcaseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    campaign_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    title: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=128)
    tasks: list[ShowcaseTaskSpec] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def task_ids_are_unique(self) -> "ShowcaseManifest":
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("showcase task IDs must be unique")
        return self


def load_showcase_manifest(path: Path) -> ShowcaseManifest:
    return ShowcaseManifest.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def approval_is_safe(
    pending: dict[str, Any],
    *,
    mutates_workspace: bool,
) -> bool:
    serialized = json.dumps(pending, sort_keys=True).lower()
    if any(term in serialized for term in FORBIDDEN_APPROVAL_TERMS):
        return False
    proposed = pending.get("proposed_action")
    actions = proposed.get("actions") if isinstance(proposed, dict) else None
    if not isinstance(actions, list) or not actions:
        return False
    for action in actions:
        if not isinstance(action, dict):
            return False
        if str(action.get("type") or "") not in ALLOWED_ACTION_TYPES:
            return False
    if mutates_workspace:
        return True
    return all(
        (
            str(action.get("type") or "")
            in READ_ONLY_NAVIGATION_ACTION_TYPES
        )
        or (
            str(action.get("type") or "") == "key"
            and frozenset(
                str(key).upper()
                for key in action.get("keys") or []
            )
            in READ_ONLY_NAVIGATION_KEYS
        )
        for action in actions
    )


def approval_disposition(
    pending: dict[str, Any] | None,
    *,
    approved_ids: set[str],
    mutates_workspace: bool,
) -> ApprovalDisposition:
    approval_id = (
        str(pending.get("approval_id") or "")
        if isinstance(pending, dict)
        else ""
    )
    if approval_id and approval_id in approved_ids:
        return "wait"
    if (
        approval_id
        and isinstance(pending, dict)
        and approval_is_safe(
            pending,
            mutates_workspace=mutates_workspace,
        )
    ):
        return "approve"
    return "refuse"


class CampaignWriter:
    def __init__(self, manifest: ShowcaseManifest, root: Path) -> None:
        self.manifest = manifest
        self.root = root / manifest.campaign_id
        self.path = self.root / "campaign.json"
        self.root.mkdir(parents=True, exist_ok=True)
        expected_tasks = [
            {
                **task.model_dump(mode="json"),
                "status": "queued",
                "run_id": None,
                "started_at": None,
                "finished_at": None,
                "duration_ms": None,
                "result": None,
                "error": None,
                "task_error": None,
                "performance": None,
                "approvals": [],
                "recoveries": [],
                "reboot": {
                    "status": "pending",
                    "requested_at": None,
                    "ready_at": None,
                    "duration_ms": None,
                    "transition_observed": False,
                    "attempts": [],
                },
                "recording": None,
                "poster": None,
            }
            for task in manifest.tasks
        ]
        initial: dict[str, Any] = {
            "schema_version": 1,
            "campaign_id": manifest.campaign_id,
            "title": manifest.title,
            "status": "queued",
            "model": {"provider": manifest.provider},
            "isolation": {
                "reboot_after_every_task": True,
                "desktop_surfaced_after_reboot": True,
                "ready_gate": "stable non-blank Windows frame",
            },
            "total": len(manifest.tasks),
            "completed": 0,
            "passed": 0,
            "failed": 0,
            "current_task_id": None,
            "current_run_id": None,
            "started_at": None,
            "finished_at": None,
            "updated_at": utc_now(),
            "tasks": expected_tasks,
        }
        if self.path.is_file():
            existing = json.loads(self.path.read_text(encoding="utf-8"))
            existing_identity = [
                (
                    task.get("task_id"),
                    task.get("title"),
                    task.get("prompt"),
                    task.get("mutates_workspace"),
                )
                for task in existing.get("tasks", [])
            ]
            expected_identity = [
                (
                    task["task_id"],
                    task["title"],
                    task["prompt"],
                    task["mutates_workspace"],
                )
                for task in expected_tasks
            ]
            if (
                existing.get("campaign_id") != manifest.campaign_id
                or existing_identity != expected_identity
            ):
                raise ValueError(
                    "existing campaign does not match the supplied manifest"
                )
            self.payload = existing
        else:
            self.payload = initial
        self.flush()

    def task(self, task_id: str) -> dict[str, Any]:
        return next(
            task
            for task in self.payload["tasks"]
            if task["task_id"] == task_id
        )

    def flush(self) -> None:
        self.payload["completed"] = sum(
            task["status"] in {"passed", "failed"}
            for task in self.payload["tasks"]
        )
        self.payload["passed"] = sum(
            task["status"] == "passed"
            for task in self.payload["tasks"]
        )
        self.payload["failed"] = sum(
            task["status"] == "failed"
            for task in self.payload["tasks"]
        )
        self.payload["updated_at"] = utc_now()
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.payload, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


class FrameRecorder:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        frame_url: str,
        output_dir: Path,
        interval_s: float,
    ) -> None:
        self.client = client
        self.frame_url = frame_url
        self.output_dir = output_dir
        self.interval_s = interval_s
        self.frames_dir = output_dir / "frames"
        self.poster = output_dir / "poster.jpg"
        self.recording = output_dir / "recording.webm"
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._count = 0

    async def start(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(self.frames_dir.glob("frame-*.jpg"))
        if existing:
            self._count = int(existing[-1].stem.rsplit("-", 1)[-1]) + 1
        self._task = asyncio.create_task(self._record())

    async def capture_poster(self) -> bool:
        try:
            response = await self.client.get(self.frame_url)
            response.raise_for_status()
        except httpx.HTTPError:
            return False
        temporary = self.poster.with_suffix(".jpg.tmp")
        temporary.write_bytes(response.content)
        os.replace(temporary, self.poster)
        return True

    async def stop(self) -> tuple[Path | None, Path | None]:
        self._stop.set()
        if self._task is not None:
            await self._task
        if self._count == 0:
            return None, self.poster if self.poster.is_file() else None
        try:
            await asyncio.to_thread(self._encode)
        except (OSError, subprocess.CalledProcessError):
            return None, self.poster if self.poster.is_file() else None
        shutil.rmtree(self.frames_dir, ignore_errors=True)
        return (
            self.recording if self.recording.is_file() else None,
            self.poster if self.poster.is_file() else None,
        )

    async def _record(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                response = await self.client.get(self.frame_url)
                response.raise_for_status()
                Image.open(BytesIO(response.content)).verify()
                path = self.frames_dir / f"frame-{self._count:06d}.jpg"
                path.write_bytes(response.content)
                self._count += 1
            except (httpx.HTTPError, OSError):
                pass
            except RuntimeError:
                # The campaign client can close first during Ctrl-C or process
                # shutdown. End the recorder quietly instead of leaking a
                # background-task exception after the runner has stopped.
                return
            elapsed = time.monotonic() - started
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(0.01, self.interval_s - elapsed),
                )

    def _encode(self) -> None:
        frame_rate = max(1, round(1 / self.interval_s))
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-framerate",
                str(frame_rate),
                "-i",
                str(self.frames_dir / "frame-%06d.jpg"),
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
                "-c:v",
                "libvpx-vp9",
                "-crf",
                "36",
                "-b:v",
                "0",
                "-deadline",
                "realtime",
                "-cpu-used",
                "6",
                str(self.recording),
            ],
            check=True,
        )


class VncAdapter:
    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.frame_url = f"{self.base_url}/api/streamer/snapshot"

    async def frame(self) -> bytes:
        response = await self.client.get(self.frame_url)
        response.raise_for_status()
        return response.content

    async def wait_until_ready(
        self,
        *,
        timeout_s: float,
        stable_s: float = 5.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        stable_since: float | None = None
        prior_signature: bytes | None = None
        samples = 0
        while time.monotonic() < deadline:
            try:
                data = await self.frame()
                image = Image.open(BytesIO(data)).convert("RGB")
                luminance = ImageStat.Stat(image.convert("L")).mean[0]
                signature = image.resize((16, 10)).convert("L").tobytes()
            except (httpx.HTTPError, OSError):
                stable_since = None
                prior_signature = None
                await asyncio.sleep(1)
                continue
            samples += 1
            difference = (
                sum(abs(left - right) for left, right in zip(signature, prior_signature))
                / len(signature)
                if prior_signature is not None
                else 255
            )
            if luminance >= 8 and difference <= 2.5:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= stable_s:
                    return {
                        "ready": True,
                        "luminance": round(luminance, 2),
                        "samples": samples,
                        "frame_sha256": hashlib.sha256(data).hexdigest(),
                    }
            else:
                stable_since = None
            prior_signature = signature
            await asyncio.sleep(1)
        raise TimeoutError("Windows did not reach a stable non-blank frame")

    async def reboot_and_wait(
        self,
        *,
        timeout_s: float,
        max_attempts: int = 2,
    ) -> dict[str, Any]:
        started = time.monotonic()
        attempts: list[dict[str, Any]] = []
        ready: dict[str, Any] = {}
        for attempt in range(1, max_attempts + 1):
            attempt_started = time.monotonic()
            baseline = await self.frame()
            await self._reboot()
            transition = await self._wait_for_boot_transition(
                baseline=baseline,
                timeout_s=min(45, timeout_s / 2),
            )
            ready = await self.wait_until_ready(
                timeout_s=max(
                    10,
                    timeout_s - (time.monotonic() - attempt_started),
                ),
                stable_s=8,
            )
            attempts.append(
                {
                    "attempt": attempt,
                    "duration_ms": round(
                        (time.monotonic() - attempt_started) * 1000
                    ),
                    "transition_observed": transition,
                    "ready_frame_sha256": ready["frame_sha256"],
                }
            )
            if transition:
                break
        if attempts and attempts[-1]["transition_observed"]:
            await self.show_desktop()
            ready = await self.wait_until_ready(
                timeout_s=max(
                    10,
                    timeout_s - (time.monotonic() - started),
                ),
                stable_s=2,
            )
            attempts[-1]["ready_frame_sha256"] = ready["frame_sha256"]
        return {
            **ready,
            "transition_observed": bool(
                attempts and attempts[-1]["transition_observed"]
            ),
            "duration_ms": round((time.monotonic() - started) * 1000),
            "attempts": attempts,
        }

    async def show_desktop(self) -> None:
        parsed = urlparse(self.base_url)
        websocket_url = urlunparse(
            (
                "wss" if parsed.scheme == "https" else "ws",
                parsed.netloc,
                "/api/ws",
                "",
                "",
                "",
            )
        )
        async with websocket_connect(websocket_url, open_timeout=10) as socket:
            acknowledgement = 0

            async def send_key(key: str, state: bool) -> None:
                nonlocal acknowledgement
                acknowledgement += 1
                await socket.send(
                    json.dumps(
                        {
                            "event_type": "key",
                            "event": {"key": key, "state": state},
                        }
                    )
                )
                while True:
                    response = json.loads(await socket.recv())
                    if (
                        response.get("event_type") == "lab_ack"
                        and int(
                            (response.get("event") or {}).get(
                                "sequence",
                                acknowledgement,
                            )
                        )
                        >= acknowledgement
                    ):
                        return

            for key, state in (
                ("Escape", True),
                ("Escape", False),
                ("MetaLeft", True),
                ("KeyD", True),
                ("KeyD", False),
                ("MetaLeft", False),
            ):
                await send_key(key, state)

    async def _reboot(self) -> None:
        parsed = urlparse(self.base_url)
        websocket_url = urlunparse(
            (
                "wss" if parsed.scheme == "https" else "ws",
                parsed.netloc,
                "/api/ws",
                "",
                "",
                "",
            )
        )
        async with websocket_connect(websocket_url, open_timeout=10) as socket:
            async def send_key(key: str, state: bool) -> None:
                await socket.send(
                    json.dumps(
                        {
                            "event_type": "key",
                            "event": {"key": key, "state": state},
                        }
                    )
                )
                while True:
                    response = json.loads(await socket.recv())
                    if response.get("event_type") == "lab_ack":
                        return

            await send_key("Escape", True)
            await send_key("Escape", False)
            await asyncio.sleep(0.5)
            run_opened = False
            for _attempt in range(3):
                baseline = await self.frame()
                await send_key("MetaLeft", True)
                # Some VNC servers acknowledge the modifier before Windows has
                # incorporated it. Without this dwell, R intermittently arrives
                # as a bare key and the Run dialog never opens.
                await asyncio.sleep(0.25)
                await send_key("KeyR", True)
                await asyncio.sleep(0.1)
                await send_key("KeyR", False)
                await asyncio.sleep(0.1)
                await send_key("MetaLeft", False)
                run_opened = await self._wait_for_run_dialog(
                    baseline=baseline,
                    timeout_s=4,
                )
                if run_opened:
                    await self.wait_until_ready(
                        timeout_s=8,
                        stable_s=0.5,
                    )
                    break
                await send_key("Escape", True)
                await send_key("Escape", False)
                await asyncio.sleep(0.5)
            if not run_opened:
                raise TimeoutError(
                    "Windows Run dialog did not visibly open for reboot"
                )
            for key, state in (
                ("ControlLeft", True),
                ("KeyA", True),
                ("KeyA", False),
                ("ControlLeft", False),
            ):
                await send_key(key, state)
            response = await self.client.post(
                f"{self.base_url}/api/hid/print",
                content="shutdown /r /t 0 /f",
            )
            response.raise_for_status()
            await asyncio.sleep(0.2)
            await send_key("Enter", True)
            await send_key("Enter", False)

    async def _wait_for_run_dialog(
        self,
        *,
        baseline: bytes,
        timeout_s: float,
    ) -> bool:
        """Prove a sustained lower-left dialog change before typing shutdown."""

        def region_signature(data: bytes) -> tuple[tuple[int, int], bytes]:
            image = Image.open(BytesIO(data)).convert("L")
            width, height = image.size
            region = image.crop(
                (
                    0,
                    round(height * 0.55),
                    round(width * 0.45),
                    height,
                )
            )
            return image.size, region.resize((48, 32)).tobytes()

        baseline_size, baseline_signature = region_signature(baseline)
        changed_samples = 0
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                current_size, signature = region_signature(await self.frame())
            except (httpx.HTTPError, OSError):
                changed_samples = 0
                await asyncio.sleep(0.25)
                continue
            if current_size != baseline_size:
                changed_samples = 0
                await asyncio.sleep(0.25)
                continue
            deltas = [
                abs(left - right)
                for left, right in zip(baseline_signature, signature)
            ]
            mean_delta = sum(deltas) / len(deltas) / 255
            changed_fraction = sum(delta > 20 for delta in deltas) / len(deltas)
            if mean_delta >= 0.015 or changed_fraction >= 0.035:
                changed_samples += 1
                if changed_samples >= 2:
                    return True
            else:
                changed_samples = 0
            await asyncio.sleep(0.25)
        return False

    async def _wait_for_boot_transition(
        self,
        *,
        baseline: bytes,
        timeout_s: float,
    ) -> bool:
        baseline_image = Image.open(BytesIO(baseline))
        baseline_size = baseline_image.size
        baseline_fingerprint = fingerprint(baseline)
        meaningfully_changed_samples = 0
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                data = await self.frame()
                source = Image.open(BytesIO(data))
                if source.size != baseline_size:
                    return True
                image = source.convert("L")
                if ImageStat.Stat(image).mean[0] < 7:
                    return True
                if fp_meaningful_change(
                    baseline_fingerprint,
                    fingerprint(data),
                ):
                    meaningfully_changed_samples += 1
                    if meaningfully_changed_samples >= 3:
                        return True
                else:
                    meaningfully_changed_samples = 0
            except (httpx.HTTPError, OSError):
                return True
            await asyncio.sleep(0.5)
        return False


class HarnessCampaignClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        agent_token: str,
        operator_token: str,
        operator_origin: str,
    ) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.agent_headers = {
            "Authorization": f"Bearer {agent_token}",
        }
        self.operator_headers = {
            "Authorization": f"Bearer {operator_token}",
            "Origin": operator_origin,
        }

    async def create(
        self,
        task: ShowcaseTaskSpec,
        provider: str,
    ) -> dict[str, Any]:
        response = await self.client.post(
            f"{self.base_url}/api/runs",
            headers=self.agent_headers,
            json={
                "task": f"{CAMPAIGN_GUARD}\n\nTask:\n{task.prompt}",
                "mode": "computer",
                "auto_start": True,
                "model_preferences": {
                    "reasoner": provider,
                    "controller": provider,
                    "verifier": provider,
                },
                "source_client": "codex-showcase",
                "client_request_id": (
                    f"showcase:{task.task_id}:{uuid.uuid4().hex}"
                ),
            },
        )
        response.raise_for_status()
        return response.json()

    async def get(self, run_id: str) -> dict[str, Any]:
        response = await self.client.get(
            f"{self.base_url}/api/runs/{run_id}",
            headers=self.agent_headers,
        )
        response.raise_for_status()
        return response.json()

    async def performance(self, run_id: str) -> dict[str, Any] | None:
        response = await self.client.get(
            f"{self.base_url}/api/runs/{run_id}/performance",
            headers=self.agent_headers,
        )
        return response.json() if response.is_success else None

    async def approve(
        self,
        run_id: str,
        approval_id: str,
    ) -> dict[str, Any]:
        response = await self.client.post(
            (
                f"{self.base_url}/api/runs/{run_id}/approvals/"
                f"{approval_id}?background=true"
            ),
            headers={
                **self.operator_headers,
                "X-PiKVM-Approval-Intent": approval_id,
            },
            json={
                "type": "approve",
                "reason": (
                    "pre-authorized bounded workspace edit in the disposable "
                    "50-task Codex acceptance campaign"
                ),
            },
        )
        response.raise_for_status()
        return response.json()

    async def continue_run(self, run_id: str) -> bool:
        response = await self.client.post(
            f"{self.base_url}/api/runs/{run_id}/continue?background=true",
            headers=self.agent_headers,
        )
        if response.status_code == 409:
            return False
        response.raise_for_status()
        return True


def paused_recovery_action(
    *,
    event_count: int,
    observed_cursor: int | None,
    continued_cursor: int | None,
) -> Literal["observe", "continue", "wait"]:
    """Schedule at most one continue request for one paused checkpoint."""

    if observed_cursor != event_count:
        return "observe"
    if continued_cursor == event_count:
        return "wait"
    return "continue"


async def run_showcase_campaign(
    *,
    manifest_path: Path,
    output_root: Path,
    harness_url: str,
    adapter_url: str,
    agent_token: str,
    operator_token: str,
    operator_origin: str,
    task_timeout_s: float = 300,
    reboot_timeout_s: float = 180,
    frame_interval_s: float = 0.5,
    max_same_run_recoveries: int = 8,
    stop_after_task_id: str | None = None,
) -> dict[str, Any]:
    if max_same_run_recoveries < 1:
        raise ValueError("max_same_run_recoveries must be positive")
    manifest = load_showcase_manifest(manifest_path)
    if (
        stop_after_task_id is not None
        and stop_after_task_id not in {
            task.task_id for task in manifest.tasks
        }
    ):
        raise ValueError(
            f"stop-after task is not in manifest: {stop_after_task_id}"
        )
    writer = CampaignWriter(manifest, output_root)
    writer.payload["limits"] = {
        "task_timeout_s": task_timeout_s,
        "max_same_run_recoveries": max_same_run_recoveries,
    }
    writer.payload["status"] = "running"
    writer.payload["started_at"] = writer.payload.get("started_at") or utc_now()
    writer.payload["finished_at"] = None
    writer.flush()
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
    timeout = httpx.Timeout(30, connect=10)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        harness = HarnessCampaignClient(
            client,
            base_url=harness_url,
            agent_token=agent_token,
            operator_token=operator_token,
            operator_origin=operator_origin,
        )
        adapter = VncAdapter(client, adapter_url)
        await adapter.wait_until_ready(timeout_s=reboot_timeout_s)
        await adapter.show_desktop()
        await adapter.wait_until_ready(
            timeout_s=reboot_timeout_s,
            stable_s=2,
        )
        for spec in manifest.tasks:
            record = writer.task(spec.task_id)
            record.setdefault("recoveries", [])
            record.setdefault("task_error", _task_error_before_reboot(record))
            if (
                record["status"] in {"passed", "failed"}
                and record["reboot"]["status"] == "ready"
                and record.get("recording")
            ):
                continue
            reboot_only = (
                record["status"] in {"passed", "failed", "rebooting"}
                and record["reboot"]["status"] != "ready"
                and bool(record.get("run_id"))
            )
            existing_recording = (
                writer.root / str(record["recording"])
                if reboot_only and record.get("recording")
                else None
            )
            existing_poster = (
                writer.root / str(record["poster"])
                if reboot_only and record.get("poster")
                else None
            )
            preserve_recording = bool(
                existing_recording is not None
                and existing_recording.is_file()
            )
            prior_duration_ms = int(record.get("duration_ms") or 0)
            prior_reboot_duration_ms = int(
                record["reboot"].get("duration_ms") or 0
            )
            prior_reboot_attempts = list(
                record["reboot"].get("attempts") or []
            )
            task_started = time.monotonic()
            task_dir = writer.root / spec.task_id
            recorder = FrameRecorder(
                client=client,
                frame_url=adapter.frame_url,
                output_dir=task_dir,
                interval_s=frame_interval_s,
            )
            record["status"] = "rebooting" if reboot_only else "running"
            record["started_at"] = record.get("started_at") or utc_now()
            writer.payload["current_task_id"] = spec.task_id
            writer.payload["current_run_id"] = record.get("run_id")
            writer.flush()
            if not preserve_recording:
                await recorder.start()
            run_status = "failed"
            run_error: str | None = None
            try:
                run_id = str(record.get("run_id") or "")
                if record["status"] != "rebooting":
                    if run_id:
                        run = await harness.get(run_id)
                    else:
                        run = await harness.create(spec, manifest.provider)
                        run_id = str(run["run_id"])
                        record["run_id"] = run_id
                        writer.payload["current_run_id"] = run_id
                        writer.flush()
                    deadline = time.monotonic() + task_timeout_s
                    approved_ids = {
                        str(approval.get("approval_id") or "")
                        for approval in record["approvals"]
                    }
                    paused_cursor: int | None = None
                    continued_paused_cursor: int | None = None
                    while time.monotonic() < deadline:
                        run = await harness.get(run_id)
                        run_status = str(run.get("status") or "")
                        record["result"] = {
                            "status": run_status,
                            "error": run.get("error"),
                            "event_count": run.get("event_count"),
                            "active_activity": run.get("active_activity"),
                        }
                        writer.flush()
                        if run_status in TERMINAL_STATUSES:
                            break
                        if run_status == "paused":
                            event_count = int(run.get("event_count") or 0)
                            recovery_action = paused_recovery_action(
                                event_count=event_count,
                                observed_cursor=paused_cursor,
                                continued_cursor=continued_paused_cursor,
                            )
                            if recovery_action == "observe":
                                paused_cursor = event_count
                                continued_paused_cursor = None
                                await asyncio.sleep(0.75)
                                continue
                            if recovery_action == "wait":
                                await asyncio.sleep(0.75)
                                continue
                            if (
                                len(record["recoveries"])
                                >= max_same_run_recoveries
                            ):
                                run_error = (
                                    "same-run recovery limit reached at a "
                                    "paused checkpoint"
                                )
                                break
                            continued = await harness.continue_run(run_id)
                            if continued:
                                continued_paused_cursor = event_count
                                record["recoveries"].append(
                                    {
                                        "continued_at": utc_now(),
                                        "event_count": event_count,
                                        "error": run.get("error"),
                                    }
                                )
                                writer.flush()
                            await asyncio.sleep(0.75)
                            continue
                        paused_cursor = None
                        continued_paused_cursor = None
                        if run_status == "needs_approval":
                            pending = run.get("pending_approval")
                            approval_id = (
                                str(pending.get("approval_id") or "")
                                if isinstance(pending, dict)
                                else ""
                            )
                            disposition = approval_disposition(
                                pending,
                                approved_ids=approved_ids,
                                mutates_workspace=spec.mutates_workspace,
                            )
                            if disposition == "approve":
                                await harness.approve(run_id, approval_id)
                                approved_ids.add(approval_id)
                                record["approvals"].append(
                                    {
                                        "approval_id": approval_id,
                                        "approved_at": utc_now(),
                                        "scope": "bounded_workspace_edit",
                                    }
                                )
                                writer.flush()
                            elif disposition == "refuse":
                                run_error = (
                                    "campaign stopped at a non-allowlisted "
                                    "approval"
                                )
                                break
                        await asyncio.sleep(0.75)
                    else:
                        run_error = "task exceeded the campaign time limit"
                    await recorder.capture_poster()
                    record["performance"] = await harness.performance(run_id)
                    if run_status != "completed" and run_error is None:
                        run_error = str(run.get("error") or run_status)
                    record["task_error"] = run_error
                else:
                    run_status = str(
                        (record.get("result") or {}).get("status") or "failed"
                    )
                    run_error = _task_error_before_reboot(record)
            except Exception as exc:  # noqa: BLE001 - retain failure evidence
                run_error = f"{type(exc).__name__}: {exc}"
                record["task_error"] = run_error
            record["status"] = "rebooting"
            record["error"] = run_error
            record["reboot"]["status"] = "running"
            record["reboot"]["requested_at"] = (
                record["reboot"].get("requested_at") or utc_now()
            )
            writer.flush()
            try:
                reboot = await adapter.reboot_and_wait(
                    timeout_s=reboot_timeout_s
                )
                record["reboot"].update(
                    {
                        "status": (
                            "ready"
                            if reboot["transition_observed"]
                            else "failed"
                        ),
                        "ready_at": utc_now(),
                        "duration_ms": (
                            prior_reboot_duration_ms + reboot["duration_ms"]
                        ),
                        "transition_observed": reboot["transition_observed"],
                        "attempts": _merge_reboot_attempts(
                            prior_reboot_attempts,
                            reboot["attempts"],
                        ),
                    }
                )
                writer.flush()
                if not reboot["transition_observed"]:
                    run_error = (
                        f"{run_error}; " if run_error else ""
                    ) + "reboot command did not produce a visible boot transition"
            except Exception as exc:  # noqa: BLE001 - retain isolation failure
                record["reboot"].update(
                    {
                        "status": "failed",
                        "ready_at": None,
                    }
                )
                writer.flush()
                run_error = (
                    f"{run_error}; " if run_error else ""
                ) + f"reboot failed: {type(exc).__name__}: {exc}"
            if preserve_recording:
                recording, poster = existing_recording, existing_poster
            else:
                recording, poster = await recorder.stop()
            record["recording"] = (
                str(recording.relative_to(writer.root))
                if recording is not None
                else None
            )
            record["poster"] = (
                str(poster.relative_to(writer.root))
                if poster is not None
                else None
            )
            record["duration_ms"] = prior_duration_ms + round(
                (time.monotonic() - task_started) * 1000
            )
            record["finished_at"] = utc_now()
            passed = (
                run_status == "completed"
                and run_error is None
                and record["reboot"]["status"] == "ready"
                and recording is not None
            )
            record["status"] = "passed" if passed else "failed"
            record["error"] = run_error
            writer.payload["current_task_id"] = None
            writer.payload["current_run_id"] = None
            writer.flush()
            if record["reboot"]["status"] != "ready":
                writer.payload["status"] = "failed"
                writer.payload["finished_at"] = utc_now()
                writer.flush()
                return writer.payload
            if spec.task_id == stop_after_task_id:
                writer.payload["status"] = "paused"
                writer.payload["finished_at"] = None
                writer.flush()
                return writer.payload
    writer.payload["status"] = "completed"
    writer.payload["finished_at"] = utc_now()
    writer.flush()
    return writer.payload
