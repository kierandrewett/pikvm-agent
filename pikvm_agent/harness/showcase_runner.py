"""Run and record an isolated campaign of real managed computer tasks."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

import httpx
import yaml
from PIL import Image, ImageStat
from pydantic import BaseModel, ConfigDict, Field, model_validator
from websockets.asyncio.client import connect as websocket_connect

from pikvm_agent.harness.local_process_lease import (
    LocalProcessLease,
    LocalProcessLeaseAlreadyHeld,
)
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
C:\\PiKVM-Harness\\workspace\\codex-50, which the harness prepares before
mutating tasks. Verify the result before finishing.
"""
CAMPAIGN_FRESH_INPUT_GUARD = """\
For this text/code acceptance, create a new blank document and type every
requested content character during this run. Do not treat restored or
pre-existing document content as task completion. Save and reopen only the
document that this run freshly populated.
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
ApprovalDisposition = Literal["approve", "refuse", "wait"]
CAMPAIGN_WORKSPACE = PureWindowsPath(
    r"C:\PiKVM-Harness\workspace\codex-50"
)
_OCR_MARKER_ALPHABET = "ABCDEFGHJKLMNPQR"


def _ocr_marker_token(hex_value: str) -> str:
    """Map 48 random bits to twelve OCR-safe unambiguous letters."""

    return "".join(
        _OCR_MARKER_ALPHABET[int(character, 16)]
        for character in hex_value[:12]
    )


def _cmd_marker_expression(marker: str) -> str:
    """Hide the literal marker in a cmd expression until expansion."""

    prefix, token = marker[:-12], marker[-12:]
    return f"{prefix}%PIKVMJOIN:X=%{token}"


def _bounded_marker_distance(left: str, right: str, *, limit: int) -> int:
    """Return a tiny bounded edit distance for short OCR nonce tokens."""

    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _ocr_marker_match_count(observed: str, expected: str) -> int:
    """Count marker-bearing OCR lines with one measured nonce glyph error."""

    marker = expected.upper()
    prefix, token = marker[:-12], marker[-12:]
    pattern = re.compile(
        rf"{re.escape(prefix)}([A-Z]{{{len(token) - 1},{len(token) + 1}}})"
    )
    matches = 0
    for line in observed.upper().splitlines():
        compact = re.sub(r"\s+", "", line)
        if marker in compact:
            matches += 1
        elif any(
            _bounded_marker_distance(match.group(1), token, limit=1) <= 1
            for match in pattern.finditer(compact)
        ):
            matches += 1
    return matches


def _ocr_marker_matches(observed: str, expected: str) -> bool:
    """Match a unique success marker with one measured OCR glyph error."""

    return _ocr_marker_match_count(observed, expected) > 0


class ShowcaseCampaignAlreadyRunning(LocalProcessLeaseAlreadyHeld):
    """Another local process already owns this campaign."""


class ShowcaseCampaignRecoveryRequired(RuntimeError):
    """A newer campaign still owes quiescence or a verified reboot."""


class ShowcaseCampaignLease(LocalProcessLease):
    """Exclusive showcase-root ownership before VNC or state mutation."""

    @classmethod
    def acquire(
        cls,
        output_root: Path,
        _campaign_id: str,
    ) -> "ShowcaseCampaignLease":
        return super().acquire(
            output_root / ".showcase-runner.lock",
            kind="showcase-campaign-runner",
            already_held_error=ShowcaseCampaignAlreadyRunning,
        )


def _campaign_recovery_blockers(
    output_root: Path,
    *,
    current_campaign_id: str,
) -> list[tuple[str, list[str]]]:
    """Find unfinished campaigns not superseded by a verified VM reboot."""

    campaigns: list[tuple[Path, dict[str, Any]]] = []
    latest_ready_at: datetime | None = None
    for path in sorted(output_root.glob("*/campaign.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        campaigns.append((path, payload))
        for task in payload.get("tasks", []):
            ready_at = (task.get("reboot") or {}).get("ready_at")
            if not ready_at:
                continue
            ready_timestamp = datetime.fromisoformat(str(ready_at))
            if (
                latest_ready_at is None
                or ready_timestamp > latest_ready_at
            ):
                latest_ready_at = ready_timestamp

    blockers: list[tuple[str, list[str]]] = []
    for path, payload in campaigns:
        campaign_id = str(
            payload.get("campaign_id") or path.parent.name
        )
        if campaign_id == current_campaign_id:
            continue
        unfinished_task_ids = [
            str(task.get("task_id") or "unknown-task")
            for task in payload.get("tasks", [])
            if task.get("status") != "queued"
            and (task.get("reboot") or {}).get("status") != "ready"
        ]
        if not unfinished_task_ids:
            continue
        updated_at = datetime.fromisoformat(str(payload["updated_at"]))
        if latest_ready_at is not None and updated_at <= latest_ready_at:
            continue
        blockers.append((campaign_id, unfinished_task_ids))
    return blockers


def _validate_fresh_artifact_path(value: str) -> str:
    """Constrain pre-run preservation to one literal campaign artifact."""

    if (
        not value
        or len(value) > 240
        or any(character in value for character in '"*?<>|&^%!\r\n')
    ):
        raise ValueError("fresh artifact path must be one literal safe path")
    candidate = PureWindowsPath(value)
    try:
        relative = candidate.relative_to(CAMPAIGN_WORKSPACE)
    except ValueError as exc:
        raise ValueError(
            "fresh artifact path must be inside the campaign workspace"
        ) from exc
    if relative == PureWindowsPath(".") or ".." in relative.parts:
        raise ValueError("fresh artifact path must name a workspace artifact")
    return str(candidate)


def _hid_print_timeout_s(text: str) -> float:
    """Bound the request by the slow, acknowledged Windows key cadence."""

    return max(30.0, 10.0 + (len(text) * 0.3))


def _windows_desktop_taskbar_visible(image: Image.Image) -> bool:
    """Reject boot/login frames until a real Windows taskbar is visible."""

    grayscale = image.convert("L")
    width, height = grayscale.size
    if width < 200 or height < 100:
        return False
    band_height = max(28, round(height * 0.08))
    band = grayscale.crop((0, height - band_height, width, height))
    pixels = band.tobytes()
    if not pixels:
        return False
    horizontal_edges = sum(
        abs(pixels[index] - pixels[index - 1]) > 15
        for row in range(band_height)
        for index in range(row * width + 1, (row + 1) * width)
    )
    vertical_edges = sum(
        abs(pixels[index] - pixels[index - width]) > 15
        for index in range(width, len(pixels))
    )
    horizontal_fraction = horizontal_edges / max(
        1,
        band_height * (width - 1),
    )
    vertical_fraction = vertical_edges / max(
        1,
        (band_height - 1) * width,
    )
    return bool(
        ImageStat.Stat(band).stddev[0] >= 10
        and max(horizontal_fraction, vertical_fraction) >= 0.01
    )


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
    if error.startswith(
        (
            "reboot command did not produce a visible boot transition",
            "reboot failed:",
        )
    ):
        return None
    return error or None


def _repair_recovered_reboot_status(record: dict[str, Any]) -> bool:
    """Reconcile a completed task after its reboot-only retry succeeds."""

    if not (
        record.get("status") == "failed"
        and (record.get("reboot") or {}).get("status") == "ready"
        and (record.get("result") or {}).get("status") == "completed"
        and record.get("recording")
        and _task_error_before_reboot(record) is None
    ):
        return False
    record["status"] = "passed"
    record["error"] = None
    return True


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
    fresh_artifacts: list[str] = Field(
        default_factory=list,
        max_length=16,
    )

    @model_validator(mode="after")
    def fresh_artifacts_are_bounded(self) -> "ShowcaseTaskSpec":
        if self.fresh_artifacts and not self.mutates_workspace:
            raise ValueError(
                "fresh artifacts require a workspace-mutating task"
            )
        self.fresh_artifacts = [
            _validate_fresh_artifact_path(path)
            for path in self.fresh_artifacts
        ]
        normalized = [path.casefold() for path in self.fresh_artifacts]
        if len(normalized) != len(set(normalized)):
            raise ValueError("fresh artifact paths must be unique")
        return self


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
    if str(pending.get("risk") or "").casefold() == "unknown":
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
    return mutates_workspace


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
                "preflight": None,
                "approvals": [],
                "recoveries": [],
                "quiescence": {
                    "status": "pending",
                    "requested_at": None,
                    "confirmed_at": None,
                    "run_status": None,
                    "attempts": 0,
                    "error": None,
                },
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
                "ready_gate": "stable Windows desktop with visible taskbar",
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
                desktop_ready = _windows_desktop_taskbar_visible(image)
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
            if luminance >= 8 and difference <= 2.5 and desktop_ready:
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
        raise TimeoutError(
            "Windows did not reach a stable desktop with a visible taskbar"
        )

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
            attempt_record = {
                "attempt": attempt,
                "duration_ms": 0,
                "transition_observed": transition,
                "ready_observed": False,
                "ready_frame_sha256": None,
            }
            attempts.append(attempt_record)
            try:
                ready = await self.wait_until_ready(
                    timeout_s=max(
                        10,
                        timeout_s - (time.monotonic() - attempt_started),
                    ),
                    stable_s=8,
                )
            except TimeoutError as exc:
                attempt_record.update(
                    {
                        "duration_ms": round(
                            (time.monotonic() - attempt_started) * 1000
                        ),
                        "error": str(exc),
                    }
                )
                return {
                    "ready": False,
                    "transition_observed": transition,
                    "duration_ms": round(
                        (time.monotonic() - started) * 1000
                    ),
                    "attempts": attempts,
                    "error": str(exc),
                }
            attempt_record.update(
                {
                    "duration_ms": round(
                        (time.monotonic() - attempt_started) * 1000
                    ),
                    "ready_observed": True,
                    "ready_frame_sha256": ready["frame_sha256"],
                }
            )
            if transition:
                break
        if attempts and attempts[-1]["transition_observed"]:
            await self.show_desktop()
            try:
                ready = await self.wait_until_ready(
                    timeout_s=max(
                        10,
                        timeout_s - (time.monotonic() - started),
                    ),
                    stable_s=2,
                )
            except TimeoutError as exc:
                attempts[-1].update(
                    {
                        "ready_observed": False,
                        "ready_frame_sha256": None,
                        "error": str(exc),
                    }
                )
                return {
                    "ready": False,
                    "transition_observed": True,
                    "duration_ms": round(
                        (time.monotonic() - started) * 1000
                    ),
                    "attempts": attempts,
                    "error": str(exc),
                }
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

    @staticmethod
    async def _send_key_and_wait(
        socket: Any,
        key: str,
        state: bool,
    ) -> None:
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

    async def _open_run_dialog(self, socket: Any) -> None:
        async def send_key(key: str, state: bool) -> None:
            await self._send_key_and_wait(socket, key, state)

        await send_key("Escape", True)
        await send_key("Escape", False)
        await asyncio.sleep(0.5)
        # A cold desktop has produced four consecutive acknowledged modifier
        # misses over VNC before the identical fifth chord succeeded. Keep the
        # retry bounded, while allowing that observed transient to recover.
        for _attempt in range(5):
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
            if await self._wait_for_run_dialog(
                baseline=baseline,
                timeout_s=4,
            ):
                # The Run dialog itself is the expected non-desktop state.
                # Its sustained two-frame transition above is the grounding
                # proof; a desktop/taskbar readiness gate is invalid here.
                await asyncio.sleep(0.5)
                return
            await send_key("Escape", True)
            await send_key("Escape", False)
            await asyncio.sleep(0.5)
        raise TimeoutError("Windows Run dialog did not visibly open")

    async def _type_run_command(self, command: str) -> None:
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
            await self._open_run_dialog(socket)
            for key, state in (
                ("ControlLeft", True),
                ("KeyA", True),
                ("KeyA", False),
                ("ControlLeft", False),
            ):
                await self._send_key_and_wait(socket, key, state)
            response = await self.client.post(
                f"{self.base_url}/api/hid/print",
                content=command,
                timeout=_hid_print_timeout_s(command),
            )
            response.raise_for_status()
            await asyncio.sleep(0.2)
            await self._send_key_and_wait(socket, "Enter", True)
            await self._send_key_and_wait(socket, "Enter", False)
            await asyncio.sleep(0.3)

    async def _type_focused_console_command(self, command: str) -> None:
        """Type one short command into the already-proven workspace shell."""

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
            await self._send_key_and_wait(socket, "Escape", True)
            await self._send_key_and_wait(socket, "Escape", False)
            fragments = command.split("-")
            for index, fragment in enumerate(fragments):
                if fragment:
                    response = await self.client.post(
                        f"{self.base_url}/api/hid/print",
                        content=fragment,
                        timeout=_hid_print_timeout_s(fragment),
                    )
                    response.raise_for_status()
                if index < len(fragments) - 1:
                    await self._send_key_and_wait(socket, "Minus", True)
                    await self._send_key_and_wait(socket, "Minus", False)
            await asyncio.sleep(0.2)
            await self._send_key_and_wait(socket, "Enter", True)
            await self._send_key_and_wait(socket, "Enter", False)
            await asyncio.sleep(0.3)

    async def _wait_for_ocr_marker(
        self,
        marker: str,
        *,
        timeout_s: float,
        minimum_matches: int = 1,
    ) -> bool:
        """Read one unique console marker before trusting visible setup."""

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                response = await self.client.get(
                    f"{self.base_url}/api/streamer/snapshot",
                    params={
                        "ocr": "1",
                        "ocr_left": "0",
                        "ocr_top": "0",
                        "ocr_right": "2048",
                        "ocr_bottom": "640",
                    },
                    timeout=min(10, max(1, deadline - time.monotonic())),
                )
                response.raise_for_status()
                if (
                    _ocr_marker_match_count(response.text, marker)
                    >= minimum_matches
                ):
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
        return False

    async def ensure_campaign_workspace(
        self,
        fresh_artifacts: list[str] | None = None,
    ) -> dict[str, Any]:
        """Prepare a clean task surface without deleting prior evidence."""

        path = str(CAMPAIGN_WORKSPACE)
        visible_path = path.replace("\\", "/")
        prepared: list[dict[str, str]] = []
        await self.show_desktop()
        await self._type_run_command(
            f"cmd /d /c mkdir {visible_path} 2>nul"
        )
        if fresh_artifacts:
            workspace_token = uuid.uuid4().hex
            workspace_marker = (
                f"PIKVMWORKSPACE{_ocr_marker_token(workspace_token)}"
            )
            await self._type_run_command(
                f"cmd /d /k cd /d {visible_path} "
                f"&& echo {workspace_marker}"
            )
            if not await self._wait_for_ocr_marker(
                workspace_marker,
                timeout_s=12,
            ):
                raise TimeoutError(
                    "campaign workspace did not produce a visible success "
                    "marker"
                )
            await self._type_focused_console_command("set PIKVMJOIN=X")
        for artifact in fresh_artifacts or []:
            safe_artifact = _validate_fresh_artifact_path(artifact)
            candidate = PureWindowsPath(safe_artifact)
            marker_token = uuid.uuid4().hex
            prior_name = f"pikvmprior{marker_token}{candidate.suffix}"
            prior = candidate.with_name(prior_name)
            ocr_token = _ocr_marker_token(marker_token)
            absent_marker = f"PIKVMABSENT{ocr_token}"
            await self._type_focused_console_command(
                f"if not exist {candidate.name} "
                f"echo {_cmd_marker_expression(absent_marker)}"
            )
            if await self._wait_for_ocr_marker(
                absent_marker,
                timeout_s=4,
            ):
                preservation_status = "verified_absent"
            else:
                preserved_marker = f"PIKVMPRESERVED{ocr_token}"
                await self._type_focused_console_command(
                    f"ren {candidate.name} {prior.name}"
                )
                await self._type_focused_console_command(
                    f"if exist {prior.name} "
                    f"if not exist {candidate.name} "
                    f"echo {_cmd_marker_expression(preserved_marker)}"
                )
                if not await self._wait_for_ocr_marker(
                    preserved_marker,
                    timeout_s=12,
                ):
                    raise TimeoutError(
                        "artifact preservation did not produce a visible "
                        "success marker"
                    )
                preservation_status = "verified_visible_marker"
            prepared.append(
                {
                    "path": str(candidate),
                    "preserved_as": str(prior),
                    "preservation_status": preservation_status,
                }
            )
        await self.show_desktop()
        ready = await self.wait_until_ready(timeout_s=15, stable_s=1)
        return {
            **ready,
            "path": path,
            "method": "visible_windows_run_segmented",
            "fresh_artifacts": prepared,
        }

    async def _reboot(self) -> None:
        await self._type_run_command("shutdown /r /t 0 /f")

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
        fresh_input_guard = (
            CAMPAIGN_FRESH_INPUT_GUARD
            if task.category in {"Text entry", "Code entry"}
            else ""
        )
        response = await self.client.post(
            f"{self.base_url}/api/runs",
            headers=self.agent_headers,
            json={
                "task": (
                    f"{CAMPAIGN_GUARD}{fresh_input_guard}"
                    f"\n\nTask:\n{task.prompt}"
                ),
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

    async def abort(self, run_id: str, reason: str) -> dict[str, Any]:
        response = await self.client.post(
            f"{self.base_url}/api/runs/{run_id}/abort",
            headers=self.agent_headers,
            json={"reason": reason},
        )
        response.raise_for_status()
        return response.json()


async def _quiesce_run(
    harness: HarnessCampaignClient,
    run_id: str,
    *,
    attempts: int = 3,
    retry_delay_s: float = 0.25,
) -> dict[str, Any]:
    """Revoke a managed run and require terminal state before VM reset."""

    reason = "campaign task concluded before mandatory reboot"
    last_error = "run did not reach a terminal state"
    for attempt in range(1, attempts + 1):
        try:
            stopped = await harness.abort(run_id, reason)
            status = str(stopped.get("status") or "")
            if status in TERMINAL_STATUSES:
                return {
                    **stopped,
                    "attempts": attempt,
                }
            last_error = f"abort returned non-terminal status {status!r}"
        except Exception as exc:  # noqa: BLE001 - bounded safety retry
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < attempts:
            await asyncio.sleep(retry_delay_s)
    raise RuntimeError(
        f"could not quiesce managed run {run_id} after {attempts} attempts: "
        f"{last_error}"
    )


def paused_recovery_action(
    *,
    event_count: int,
    observed_cursor: int | None,
    continued_cursor: int | None,
    active_activity: object | None = None,
) -> Literal["observe", "continue", "wait"]:
    """Schedule at most one continue request for one paused checkpoint."""

    if active_activity is not None:
        return "wait"
    if observed_cursor != event_count:
        return "observe"
    if continued_cursor == event_count:
        return "wait"
    return "continue"


def repeated_paused_error_limit_reached(
    recoveries: list[dict[str, Any]],
    *,
    error: object,
    limit: int = 2,
) -> bool:
    """Stop an unchanged paused loop before paying for another provider retry."""

    current = str(error or "").strip()
    if not current or limit < 1:
        return False
    consecutive = 0
    for recovery in reversed(recoveries):
        if str(recovery.get("error") or "").strip() != current:
            break
        consecutive += 1
    return consecutive >= limit


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
    reboot_timeout_s: float = 300,
    frame_interval_s: float = 0.5,
    max_same_run_recoveries: int = 8,
    only_task_id: str | None = None,
    stop_after_task_id: str | None = None,
) -> dict[str, Any]:
    manifest = load_showcase_manifest(manifest_path)
    if max_same_run_recoveries < 1:
        raise ValueError("max_same_run_recoveries must be positive")
    manifest_task_ids = {task.task_id for task in manifest.tasks}
    if (
        only_task_id is not None
        and only_task_id not in manifest_task_ids
    ):
        raise ValueError(f"only task is not in manifest: {only_task_id}")
    if (
        stop_after_task_id is not None
        and stop_after_task_id not in manifest_task_ids
    ):
        raise ValueError(
            f"stop-after task is not in manifest: {stop_after_task_id}"
        )
    if (
        only_task_id is not None
        and stop_after_task_id is not None
        and only_task_id != stop_after_task_id
    ):
        raise ValueError(
            "only-task and stop-after-task must name the same task"
        )
    if only_task_id is not None:
        manifest = manifest.model_copy(
            update={
                "tasks": [
                    task
                    for task in manifest.tasks
                    if task.task_id == only_task_id
                ]
            }
        )
    try:
        lease = ShowcaseCampaignLease.acquire(
            output_root,
            manifest.campaign_id,
        )
    except ShowcaseCampaignAlreadyRunning as exc:
        raise ShowcaseCampaignAlreadyRunning(
            "showcase campaign is already running in another local process"
        ) from exc
    with lease:
        blockers = _campaign_recovery_blockers(
            output_root,
            current_campaign_id=manifest.campaign_id,
        )
        if blockers:
            recovery_targets = ", ".join(
                f"{campaign_id} ({', '.join(task_ids)})"
                for campaign_id, task_ids in blockers
            )
            raise ShowcaseCampaignRecoveryRequired(
                "showcase cleanup recovery is required before a new "
                f"campaign can start; resume: {recovery_targets}"
            )
        return await _run_showcase_campaign_locked(
            manifest=manifest,
            output_root=output_root,
            harness_url=harness_url,
            adapter_url=adapter_url,
            agent_token=agent_token,
            operator_token=operator_token,
            operator_origin=operator_origin,
            task_timeout_s=task_timeout_s,
            reboot_timeout_s=reboot_timeout_s,
            frame_interval_s=frame_interval_s,
            max_same_run_recoveries=max_same_run_recoveries,
            stop_after_task_id=stop_after_task_id,
        )


async def _run_showcase_campaign_locked(
    *,
    manifest: ShowcaseManifest,
    output_root: Path,
    harness_url: str,
    adapter_url: str,
    agent_token: str,
    operator_token: str,
    operator_origin: str,
    task_timeout_s: float,
    reboot_timeout_s: float,
    frame_interval_s: float,
    max_same_run_recoveries: int,
    stop_after_task_id: str | None,
) -> dict[str, Any]:
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
            record.setdefault(
                "quiescence",
                {
                    "status": "pending",
                    "requested_at": None,
                    "confirmed_at": None,
                    "run_status": None,
                    "attempts": 0,
                    "error": None,
                },
            )
            if _repair_recovered_reboot_status(record):
                writer.flush()
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
            run_id = str(record.get("run_id") or "")
            try:
                if record["status"] != "rebooting":
                    if spec.mutates_workspace:
                        record["preflight"] = {
                            "status": "running",
                            "started_at": utc_now(),
                        }
                        writer.flush()
                        workspace = await adapter.ensure_campaign_workspace(
                            spec.fresh_artifacts
                        )
                        record["preflight"] = {
                            "status": "ready",
                            "started_at": record["preflight"]["started_at"],
                            "ready_at": utc_now(),
                            **workspace,
                        }
                        writer.flush()
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
                                active_activity=run.get("active_activity"),
                            )
                            if recovery_action == "observe":
                                paused_cursor = event_count
                                continued_paused_cursor = None
                                await asyncio.sleep(0.75)
                                continue
                            if recovery_action == "wait":
                                await asyncio.sleep(0.75)
                                continue
                            if repeated_paused_error_limit_reached(
                                record["recoveries"],
                                error=run.get("error"),
                            ):
                                run_error = (
                                    "identical paused error repeated twice; "
                                    "stopping before another provider retry: "
                                    f"{run.get('error') or 'unknown pause'!s}"
                                )
                                break
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
            quiescence_failed = False
            record["quiescence"].update(
                {
                    "status": "running" if run_id else "not_required",
                    "requested_at": utc_now() if run_id else None,
                    "confirmed_at": utc_now() if not run_id else None,
                    "run_status": None,
                    "attempts": 0,
                    "error": None,
                }
            )
            writer.flush()
            if run_id:
                try:
                    stopped = await _quiesce_run(harness, run_id)
                    record["quiescence"].update(
                        {
                            "status": "confirmed",
                            "confirmed_at": utc_now(),
                            "run_status": stopped.get("status"),
                            "attempts": stopped["attempts"],
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - fail closed
                    quiescence_failed = True
                    quiescence_error = (
                        f"pre-reboot quiescence failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    run_error = (
                        f"{run_error}; " if run_error else ""
                    ) + quiescence_error
                    record["quiescence"].update(
                        {
                            "status": "failed",
                            "error": quiescence_error,
                        }
                    )
                    record["reboot"]["status"] = "blocked"
            record["error"] = run_error
            writer.flush()
            if not quiescence_failed:
                record["status"] = "rebooting"
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
                                if (
                                    reboot["transition_observed"]
                                    and reboot["ready"]
                                )
                                else "failed"
                            ),
                            "ready_at": (
                                utc_now()
                                if reboot["transition_observed"]
                                and reboot["ready"]
                                else None
                            ),
                            "duration_ms": (
                                prior_reboot_duration_ms
                                + reboot["duration_ms"]
                            ),
                            "transition_observed": (
                                reboot["transition_observed"]
                            ),
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
                        ) + (
                            "reboot command did not produce a visible boot "
                            "transition"
                        )
                    elif not reboot["ready"]:
                        run_error = (
                            f"{run_error}; " if run_error else ""
                        ) + str(
                            reboot.get("error")
                            or (
                                "reboot did not reach a verified Windows "
                                "desktop"
                            )
                        )
                except Exception as exc:  # noqa: BLE001
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
