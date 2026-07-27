"""Visible live runner for artifact-backed Office acceptance tasks."""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pikvm_agent.harness.agent_models import RunSnapshot, RunStatus
from pikvm_agent.harness.config import load_harness_settings
from pikvm_agent.harness.lab import RunningLab, allocate_lab_ports
from pikvm_agent.harness.live_benchmark import McpDriver, VisualTrialOracle
from pikvm_agent.harness.office_acceptance import (
    OfficeAcceptanceSuite,
    OfficeRunResult,
    OfficeTaskSpec,
    build_office_run_result,
    load_office_suite,
    write_office_result,
)
from pikvm_agent.harness.performance import RunPerformanceReport

StatusSink = Callable[[str], None]
CreatedSink = Callable[[str], Awaitable[None]]
AsyncSleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


class ManagedHarnessApi(Protocol):
    async def create(self, task: str) -> dict[str, Any]: ...

    async def start(self, run_id: str) -> dict[str, Any]: ...

    async def get(self, run_id: str) -> dict[str, Any]: ...

    async def continue_run(self, run_id: str) -> dict[str, Any]: ...

    async def abort(self, run_id: str, reason: str) -> dict[str, Any]: ...

    async def performance(self, run_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class OfficeDriveOutcome:
    run: RunSnapshot
    continuation_cycles: int
    stop_reason: str


class HttpManagedHarnessApi:
    """Agent-scoped run client; it deliberately has no approval method."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kwargs = {} if payload is None else {"json": payload}
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def create(self, task: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/runs",
            # Publish the host-owned artifact contract before any provider can
            # hold the run's model/control lock. The polling loop advances this
            # initial paused checkpoint only after ``on_created`` succeeds.
            payload={"task": task, "auto_start": False},
        )

    async def get(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/runs/{run_id}")

    async def start(self, run_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/runs/{run_id}/start")

    async def continue_run(self, run_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/runs/{run_id}/continue?background=true",
        )

    async def performance(self, run_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/runs/{run_id}/performance",
        )

    async def abort(self, run_id: str, reason: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/runs/{run_id}/abort",
            payload={"reason": reason},
        )


async def _publish_artifact_acceptance(
    client: httpx.AsyncClient,
    run_id: str,
    payload: dict[str, Any],
) -> None:
    response = await client.post(
        f"/api/runs/{run_id}/artifact-acceptance",
        json=payload,
    )
    response.raise_for_status()


def _artifact_acceptance_label(task: OfficeTaskSpec) -> str:
    return f"{task.artifact.format.upper()} saved artifact"


def _artifact_acceptance_result(
    task: OfficeTaskSpec,
    result: OfficeRunResult,
) -> dict[str, Any]:
    checks_total = len(result.artifact.checks)
    checks_passed = sum(check.passed for check in result.artifact.checks)
    error_class = None
    if result.status != "passed":
        error_class = (
            result.artifact_capture_error
            or (
                "run-incomplete"
                if result.status == "run_incomplete"
                else "artifact-semantic-check-failed"
            )
        )
    return {
        "kind": "office_artifact",
        "label": _artifact_acceptance_label(task),
        "state": "passed" if result.status == "passed" else "failed",
        "artifact_format": result.artifact.format,
        "checks_passed": checks_passed,
        "checks_total": checks_total,
        "byte_count": result.artifact.byte_count,
        "sha256": result.artifact.sha256,
        "error_class": error_class,
    }


def _run_snapshot(payload: dict[str, Any]) -> RunSnapshot:
    return RunSnapshot.model_validate(payload)


async def _abort_outcome(
    api: ManagedHarnessApi,
    run: RunSnapshot,
    *,
    cycles: int,
    reason: str,
) -> OfficeDriveOutcome:
    aborted = _run_snapshot(
        await api.abort(
            run.run_id,
            f"Office acceptance runner stopped: {reason}",
        )
    )
    return OfficeDriveOutcome(aborted, cycles, reason)


async def drive_managed_office_run(
    api: ManagedHarnessApi,
    *,
    instruction: str,
    max_continuation_cycles: int,
    max_run_time_s: float,
    status_sink: StatusSink | None = None,
    on_created: CreatedSink | None = None,
    sleep: AsyncSleep = asyncio.sleep,
    monotonic: Clock = time.monotonic,
) -> OfficeDriveOutcome:
    """Drive bounded model slices while leaving every approval to the UI."""

    if max_continuation_cycles < 1:
        raise ValueError("max_continuation_cycles must be positive")
    if max_run_time_s <= 0:
        raise ValueError("max_run_time_s must be positive")
    created = await api.create(instruction)
    run_id = str(created["run_id"])
    if on_created is not None:
        try:
            await on_created(run_id)
        except Exception:
            try:
                await api.abort(
                    run_id,
                    "Office acceptance runner stopped: "
                    "artifact visibility unavailable",
                )
            except Exception:
                pass
            raise
    try:
        await api.start(run_id)
    except Exception:
        try:
            await api.abort(
                run_id,
                "Office acceptance runner stopped: model start unavailable",
            )
        except Exception:
            pass
        raise
    deadline = monotonic() + max_run_time_s
    cycles = 0
    last_status = ""
    approval_id = ""
    continuation_cursor: int | None = None
    while True:
        payload = await api.get(run_id)
        run = _run_snapshot(payload)
        status = run.status.value
        if status != last_status and status_sink is not None:
            status_sink(f"Office run {run_id}: {status}")
        last_status = status
        if run.status is RunStatus.COMPLETED:
            return OfficeDriveOutcome(run, cycles, "completed")
        if run.status in {
            RunStatus.FAILED,
            RunStatus.REJECTED,
            RunStatus.ABORTED,
        }:
            return OfficeDriveOutcome(run, cycles, status)
        if run.status is RunStatus.BLOCKED:
            return await _abort_outcome(
                api,
                run,
                cycles=cycles,
                reason="blocked",
            )
        if run.status is not RunStatus.PAUSED:
            continuation_cursor = None
        if monotonic() >= deadline:
            return await _abort_outcome(
                api,
                run,
                cycles=cycles,
                reason="runner-timeout",
            )
        if run.status is RunStatus.NEEDS_APPROVAL:
            pending = run.pending_approval or {}
            current_approval = str(pending.get("approval_id") or "")
            if (
                current_approval
                and current_approval != approval_id
                and status_sink is not None
            ):
                status_sink(
                    "Approval is waiting in the operator UI; the runner "
                    "cannot approve it."
                )
            approval_id = current_approval
            await sleep(0.25)
            continue
        approval_id = ""
        if run.status is RunStatus.PAUSED:
            if (
                continuation_cursor is not None
                and run.event_cursor <= continuation_cursor
            ):
                await sleep(0.25)
                continue
            if cycles >= max_continuation_cycles:
                return await _abort_outcome(
                    api,
                    run,
                    cycles=cycles,
                    reason="continuation-cycle-limit",
                )
            await api.continue_run(run_id)
            continuation_cursor = run.event_cursor
            cycles += 1
            continue
        await sleep(0.25)


def _normalise_windows_path(value: str) -> str:
    return value.replace("\\", "/").casefold()


def _fresh_artifact_path(filename: str, *, nonce: str) -> str:
    """Return a never-reused guest path for one Office acceptance attempt."""

    from pikvm_agent.harness.bootstrap_windows import (
        validate_observer_file_path,
    )

    if len(nonce) != 16 or any(
        character not in "0123456789abcdef" for character in nonce
    ):
        raise ValueError("Office artifact nonce must be 16 lowercase hex digits")
    suffix = Path(filename).suffix
    stem = filename[: -len(suffix)] if suffix else filename
    reserved = len(nonce) + len(suffix) + 1
    stem = stem[: 96 - reserved].rstrip("._-")
    if not stem:
        raise ValueError("Office artifact filename has no safe stem")
    guest_filename = f"{stem}-{nonce}{suffix}"
    return validate_observer_file_path(
        f"C:/PiKVM-Harness/workspace/{guest_filename}"
    )


async def _capture_visual_artifact(
    lab: RunningLab,
    *,
    expected_path: str,
    observer_token: str,
) -> bytes:
    """Read helper-reported file bytes through visible, guarded MCP calls."""

    env = dict(lab.env)
    env["PIKVM_AGENT_DAEMON"] = lab.daemon_url
    env["PIKVM_HARNESS_OBSERVER_URL"] = lab.harness_url
    env["PIKVM_HARNESS_OBSERVER_TOKEN"] = observer_token
    env["PIKVM_HARNESS_OBSERVER_MODE"] = "guarded"
    env["PIKVM_MCP_CALLER_LABEL"] = "office-artifact-verifier"
    env["PIKVM_MCP_PROVIDER"] = "host-verifier"
    env["PIKVM_MCP_MODEL"] = "none"
    params = StdioServerParameters(
        command=os.path.abspath(sys.executable),
        args=["-m", "pikvm_agent.cli", "mcp"],
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    with open(os.devnull, "w") as mcp_errors:
        transport = stdio_client(params, errlog=mcp_errors)
        async with transport as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                driver = McpDriver(session)
                await driver.open()
                try:
                    _score, snapshot = await VisualTrialOracle().seal(
                        driver,
                        object(),
                        intended="",
                        include_file=True,
                        key="office-artifact-proof",
                    )
                finally:
                    if driver.session_id:
                        await driver.call(
                            "pikvm_abort",
                            {
                                "session_id": driver.session_id,
                                "reason": "Office artifact proof captured",
                            },
                        )
    if snapshot.file is None:
        raise RuntimeError("observer snapshot contained no file evidence")
    if snapshot.file.error:
        raise RuntimeError(f"observer file read failed: {snapshot.file.error}")
    if _normalise_windows_path(snapshot.file.path) != _normalise_windows_path(
        expected_path
    ):
        raise RuntimeError("observer returned a different artifact path")
    try:
        return snapshot.file.content()
    except ValueError as exc:
        raise RuntimeError("observer returned invalid artifact bytes") from exc


def _write_private_artifact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ValueError("captured Office artifact already exists") from exc
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)


def _capture_error_class(exc: Exception) -> str:
    name = type(exc).__name__.casefold()
    if "visualoracle" in name:
        return "visual-oracle-error"
    if isinstance(exc, RuntimeError):
        return "observer-artifact-error"
    if isinstance(exc, (httpx.HTTPError, OSError)):
        return "artifact-transport-error"
    return "artifact-capture-error"


async def run_live_office_case(
    *,
    endpoint: str,
    harness_config: Path,
    suite_path: Path,
    task_id: str,
    output_dir: Path,
    artifact_url: str | None,
    skip_provision: bool,
    keymap: str,
    password: str | None,
    username: str | None,
    max_continuation_cycles: int,
    max_run_time_s: float,
    status_sink: StatusSink | None = None,
) -> OfficeRunResult:
    """Run one real managed task, capture its file, and publish scored evidence."""

    from pikvm_agent.harness.bootstrap_windows import deploy

    suite: OfficeAcceptanceSuite = load_office_suite(suite_path)
    task: OfficeTaskSpec = suite.task(task_id)
    artifact_path = _fresh_artifact_path(
        task.artifact.filename,
        nonce=secrets.token_hex(8),
    )
    if artifact_url is None and not skip_provision:
        raise ValueError(
            "Office visual verification requires --artifact-url "
            "unless --skip-provision is explicit"
        )
    deploy(
        endpoint=endpoint,
        artifact_url=artifact_url,
        file_path=artifact_path,
        password=password,
        username=username,
        reuse_installed=skip_provision,
        visible=False,
    )
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "result.json"
    if result_path.exists():
        raise ValueError("Office result already exists")
    ports = allocate_lab_ports()
    with RunningLab(
        endpoint=endpoint,
        root=output_dir / "lab",
        ports=ports,
        executable=os.path.abspath(sys.executable),
        keymap=keymap,
        password=password,
        username=username,
        keyboard_profile="windows",
        quiet=True,
        harness_config=harness_config,
        start_harness=True,
    ) as lab:
        assert lab.assets is not None
        settings = load_harness_settings(lab.assets.harness_config)
        if status_sink is not None:
            status_sink(f"Operator UI: {lab.harness_url}/app/")
        async with httpx.AsyncClient(
            base_url=lab.harness_url,
            headers={
                "Authorization": f"Bearer {settings.agent_token()}",
                "Accept": "application/json",
            },
            timeout=30,
        ) as client, httpx.AsyncClient(
            base_url=lab.harness_url,
            headers={
                "Authorization": f"Bearer {settings.observer_token()}",
                "Accept": "application/json",
            },
            timeout=30,
        ) as observer_client:
            api = HttpManagedHarnessApi(client)

            async def mark_pending(run_id: str) -> None:
                await _publish_artifact_acceptance(
                    observer_client,
                    run_id,
                    {
                        "kind": "office_artifact",
                        "label": _artifact_acceptance_label(task),
                        "state": "pending",
                    },
                )

            outcome = await drive_managed_office_run(
                api,
                instruction=task.render_instruction(artifact_path),
                max_continuation_cycles=max_continuation_cycles,
                max_run_time_s=max_run_time_s,
                status_sink=status_sink,
                on_created=mark_pending,
            )
            performance = RunPerformanceReport.model_validate(
                await api.performance(outcome.run.run_id)
            )
            artifact_bytes = b""
            capture_error: str | None = None
            if outcome.run.status is RunStatus.COMPLETED:
                await _publish_artifact_acceptance(
                    observer_client,
                    outcome.run.run_id,
                    {
                        "kind": "office_artifact",
                        "label": _artifact_acceptance_label(task),
                        "state": "capturing",
                    },
                )
                if status_sink is not None:
                    status_sink(
                        "Managed task completed; capturing host-verified "
                        "artifact bytes."
                    )
                try:
                    artifact_bytes = await _capture_visual_artifact(
                        lab,
                        expected_path=artifact_path,
                        observer_token=settings.observer_token(),
                    )
                except Exception as exc:  # preserve a scored failure artifact
                    capture_error = _capture_error_class(exc)
            else:
                capture_error = (
                    "artifact capture skipped because the managed run did "
                    "not complete"
                )
            result = build_office_run_result(
                suite=suite,
                task=task,
                run=outcome.run,
                artifact_bytes=artifact_bytes,
                environment="disposable-windows-vm",
                performance=performance,
                runner_stop_reason=outcome.stop_reason,
                artifact_capture_error=capture_error,
            )
            if artifact_bytes:
                _write_private_artifact(
                    output_dir / "artifacts" / task.artifact.filename,
                    artifact_bytes,
                )
            write_office_result(result_path, result)
            await _publish_artifact_acceptance(
                observer_client,
                outcome.run.run_id,
                _artifact_acceptance_result(task, result),
            )
            if status_sink is not None:
                status_sink(
                    "Host artifact acceptance "
                    + ("passed." if result.status == "passed" else "failed.")
                )
            return result
