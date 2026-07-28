"""Single-case OSWorld tracer through the real harness -> MCP -> daemon path.

The official VM image, task checkout, Docker image and model configuration are
all caller supplied and recorded. The model sees only the PiKVM-shaped computer
surface. Official setup and evaluation calls stay in this outer coordinator.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Literal

import httpx
from PIL import Image
from pydantic import BaseModel, ConfigDict

from pikvm_agent.daemon_access import DaemonAccess
from pikvm_agent.harness.agent import AgentHarness
from pikvm_agent.harness.agent_models import HarnessConfig, RunStatus
from pikvm_agent.harness.agent_store import SqliteRunStore
from pikvm_agent.harness.api import create_harness_app
from pikvm_agent.harness.config import (
    build_model_pool,
    ensure_safe_bind,
    ensure_provider_prerequisites,
    load_harness_settings,
)
from pikvm_agent.harness.lab import (
    RunningLab,
    allocate_lab_ports,
    isolated_benchmark_policy,
)
from pikvm_agent.harness.mcp_computer import (
    McpComputerDriver,
    PersistentMcpToolClient,
)
from pikvm_agent.harness.live_frames import DaemonLiveFrameSource
from pikvm_agent.harness.operator_console import (
    OperatorConsoleServer,
    wait_for_operator_approval,
    write_operator_console_descriptor,
)
from pikvm_agent.harness.performance import (
    RunPerformanceReport,
    summarize_run_performance,
)
from pikvm_agent.harness.public_desktop_suites import (
    discover_desktop_suite,
    verify_checkout_revision,
)

_POST_SETUP_ENDPOINTS: dict[str, tuple[str, float]] = {
    "activate_window": ("/setup/activate_window", 130),
    "launch": ("/setup/launch", 130),
    "open": ("/setup/open_file", 1810),
}
_EXECUTE_SETUP_TYPES = frozenset({"command", "execute"})
_SUPPORTED_SETUP_TYPES = frozenset(
    {*_EXECUTE_SETUP_TYPES, "download", *_POST_SETUP_ENDPOINTS}
)
ApprovalResolver = Callable[[Any], Awaitable[dict[str, str] | None]]
ApprovalWaiter = Callable[[Any], Awaitable[Any]]


class OSWorldCaseReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 4
    suite: str = "osworld-verified"
    suite_revision: str
    task_id: str
    domain: str
    instruction: str
    docker_image: str
    docker_image_id: str
    vm_image_sha256: str
    container_access: Literal["published_port", "direct_bridge"]
    container_publish_fallback_reason: str | None = None
    control_plane: str = "agent-harness -> MCP stdio -> isolated daemon"
    target_adapter: str = "official in-guest server through PiKVM-shaped lab"
    policy_profile: str = "isolated_benchmark"
    harness_status: str
    official_score: float
    evaluator: str
    approvals_required: int
    cycles: int
    model_run_budget_s: float
    model_run_timed_out: bool
    performance: RunPerformanceReport
    run_state_path: Path
    artifact_dir: Path
    report_path: Path


class DockerCommandError(RuntimeError):
    """Docker failure with stderr preserved for durable benchmark evidence."""

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        detail = (stderr or stdout or "no command output").strip()[:4000]
        summary = " ".join(command[:3])
        super().__init__(
            f"docker {summary} failed with exit {returncode}: {detail}"
        )


def _replace_placeholders(value: Any, replacements: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        for needle, replacement in replacements.items():
            value = value.replace(needle, replacement)
        return value
    if isinstance(value, list):
        return [_replace_placeholders(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_placeholders(item, replacements)
            for key, item in value.items()
        }
    return value


def validate_osworld_task_compatibility(task: dict[str, Any]) -> None:
    """Fail before VM startup when this bounded tracer cannot reproduce a task."""
    _validate_official_actions(task.get("config") or [], scope="setup")
    evaluator = task.get("evaluator") or {}
    _validate_official_actions(
        evaluator.get("postconfig") or [],
        scope="postconfig",
    )

    functions = evaluator.get("func")
    results = evaluator.get("result")
    expected = evaluator.get("expected")
    if functions == "exact_match":
        _validate_exact_match_item(results or {}, expected or {})
        return
    if functions == "is_utc_0":
        _validate_vm_command_line(results or {})
        return
    if (
        isinstance(functions, list)
        and functions
        and all(function == "exact_match" for function in functions)
        and evaluator.get("conj", "and") in {"and", "or"}
        and isinstance(results, list)
        and isinstance(expected, list)
        and len(functions) == len(results) == len(expected)
    ):
        for result, wanted in zip(results, expected, strict=True):
            _validate_exact_match_item(result, wanted)
        return
    raise ValueError(
        "single-case tracer requires exact_match or an exact_match and/or list"
    )


def _validate_official_actions(
    actions: list[dict[str, Any]],
    *,
    scope: str,
) -> None:
    for index, item in enumerate(actions):
        setup_type = item.get("type")
        if setup_type not in _SUPPORTED_SETUP_TYPES:
            raise ValueError(
                f"unsupported OSWorld {scope} type at index {index}: "
                f"{setup_type!r}"
            )


async def apply_official_setup(
    client: httpx.AsyncClient,
    task: dict[str, Any],
    *,
    width: int,
    height: int,
    client_password: str = "password",
    download_client: httpx.AsyncClient | None = None,
) -> None:
    """Apply bounded official setup records outside the model/MCP boundary."""
    validate_osworld_task_compatibility(task)
    await _apply_official_actions(
        client,
        task.get("config") or [],
        width=width,
        height=height,
        client_password=client_password,
        download_client=download_client,
    )


async def apply_official_postconfig(
    client: httpx.AsyncClient,
    task: dict[str, Any],
    *,
    width: int,
    height: int,
    client_password: str = "password",
    download_client: httpx.AsyncClient | None = None,
) -> None:
    """Apply official evaluator prerequisites outside the model/MCP boundary."""
    validate_osworld_task_compatibility(task)
    evaluator = task.get("evaluator") or {}
    await _apply_official_actions(
        client,
        evaluator.get("postconfig") or [],
        width=width,
        height=height,
        client_password=client_password,
        download_client=download_client,
    )


async def _apply_official_actions(
    client: httpx.AsyncClient,
    actions: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    client_password: str,
    download_client: httpx.AsyncClient | None,
) -> None:
    replacements = {
        "{SCREEN_WIDTH}": str(width),
        "{SCREEN_HEIGHT}": str(height),
        "{SCREEN_WIDTH_HALF}": str(width // 2),
        "{SCREEN_HEIGHT_HALF}": str(height // 2),
        "{CLIENT_PASSWORD}": client_password,
    }
    owned_download_client: httpx.AsyncClient | None = None
    try:
        for index, item in enumerate(actions):
            parameters = _replace_placeholders(
                item.get("parameters") or {},
                replacements,
            )
            if item.get("type") == "download":
                if download_client is None:
                    owned_download_client = httpx.AsyncClient(
                        follow_redirects=True,
                    )
                    download_client = owned_download_client
                await _apply_download_setup(
                    client,
                    download_client,
                    parameters,
                    index=index,
                )
                continue
            endpoint = _POST_SETUP_ENDPOINTS.get(str(item.get("type")))
            if endpoint is not None:
                path, timeout = endpoint
                response = await client.post(
                    path,
                    json=parameters,
                    timeout=timeout,
                )
                response.raise_for_status()
                continue

            response = await client.post("/execute", json=parameters, timeout=130)
            response.raise_for_status()
            result = response.json()
            if result.get("status") != "success" or result.get("returncode", 0) != 0:
                raise RuntimeError(f"official setup step {index} failed")
    finally:
        if owned_download_client is not None:
            await owned_download_client.aclose()


async def _apply_download_setup(
    guest: httpx.AsyncClient,
    downloader: httpx.AsyncClient,
    parameters: dict[str, Any],
    *,
    index: int,
) -> None:
    files = parameters.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError(f"official download setup step {index} has no files")
    for file_index, item in enumerate(files):
        url = str(item.get("url") or "")
        target_path = str(item.get("path") or "")
        parsed = httpx.URL(url)
        if parsed.scheme not in {"http", "https"} or not parsed.host or not target_path:
            raise ValueError(
                f"official download setup step {index} file {file_index} "
                "requires an HTTP(S) URL and target path"
            )
        async with downloader.stream("GET", parsed, timeout=300) as response:
            response.raise_for_status()
            with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as handle:
                async for chunk in response.aiter_bytes():
                    handle.write(chunk)
                handle.seek(0)
                upload = await guest.post(
                    "/setup/upload",
                    data={"file_path": target_path},
                    files={
                        "file_data": (
                            Path(target_path).name or "task-file",
                            handle,
                            response.headers.get("content-type"),
                        )
                    },
                    timeout=600,
                )
                upload.raise_for_status()


async def evaluate_official_exact_match(
    client: httpx.AsyncClient,
    task: dict[str, Any],
) -> tuple[float, str]:
    """Run the official exact-match/vm-command-line evaluator without model access."""
    validate_osworld_task_compatibility(task)
    evaluator = task.get("evaluator") or {}
    functions = evaluator.get("func")
    results = evaluator.get("result")
    expected = evaluator.get("expected")
    if functions == "exact_match":
        score = await _evaluate_exact_match_item(
            client,
            results or {},
            expected or {},
        )
        return score, "exact_match(vm_command_line, rule)"
    if functions == "is_utc_0":
        actual = await _vm_command_line_output(client, results or {})
        lines = actual.split("\n")
        score = 1.0 if len(lines) > 3 and lines[3].endswith("+0000)") else 0.0
        return score, "is_utc_0(vm_command_line)"
    if (
        isinstance(functions, list)
        and functions
        and all(function == "exact_match" for function in functions)
        and evaluator.get("conj", "and") in {"and", "or"}
        and isinstance(results, list)
        and isinstance(expected, list)
        and len(functions) == len(results) == len(expected)
    ):
        scores = [
            await _evaluate_exact_match_item(client, result, wanted)
            for result, wanted in zip(results, expected, strict=True)
        ]
        conjunction = str(evaluator.get("conj", "and"))
        passed = all(scores) if conjunction == "and" else any(scores)
        return (
            1.0 if passed else 0.0,
            f"{conjunction}({len(scores)} x exact_match(vm_command_line, rule))",
        )
    raise ValueError(
        "single-case tracer requires exact_match or an exact_match and/or list"
    )


async def _evaluate_exact_match_item(
    client: httpx.AsyncClient,
    result: dict[str, Any],
    expected: dict[str, Any],
) -> float:
    _validate_exact_match_item(result, expected)
    rules = expected["rules"]
    actual = await _vm_command_line_output(client, result)
    wanted = str(rules["expected"])
    return 1.0 if actual == wanted else 0.0


async def _vm_command_line_output(
    client: httpx.AsyncClient,
    result: dict[str, Any],
) -> str:
    _validate_vm_command_line(result)
    response = await client.post(
        "/execute",
        json={
            "command": result.get("command"),
            "shell": bool(result.get("shell", False)),
        },
        timeout=130,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success" or payload.get("returncode", 0) != 0:
        return ""
    return str(payload.get("output") or "")


def _validate_exact_match_item(
    result: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    _validate_vm_command_line(result)
    rules = expected.get("rules") or {}
    if expected.get("type") != "rule" or "expected" not in rules:
        raise ValueError("single-case tracer currently requires a literal rule")


def _validate_vm_command_line(result: dict[str, Any]) -> None:
    if result.get("type") != "vm_command_line":
        raise ValueError("single-case tracer currently requires vm_command_line")


def _docker(*args: str, timeout: float = 600) -> str:
    try:
        completed = subprocess.run(
            ["docker", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        raise DockerCommandError(
            tuple(args),
            returncode=exc.returncode,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
        ) from exc
    return completed.stdout.strip()


def is_docker_publish_failure(error: DockerCommandError) -> bool:
    """Recognize a failed Docker published-port setup safe to retry privately."""
    detail = f"{error.stderr}\n{error.stdout}".lower()
    has_publish_failure = (
        "failed to set up container networking" in detail
        or "unable to enable dnat rule" in detail
    )
    has_nat_detail = (
        "iptables" in detail
        or "dnat" in detail
        or "no chain/target/match" in detail
    )
    return error.returncode == 125 and has_publish_failure and has_nat_detail


def docker_run_args(
    *,
    container_name: str,
    qcow: Path,
    docker_image: str,
    kvm_available: bool,
    publish_guest_port: bool = True,
) -> list[str]:
    """Build the official container launch while preserving port 5000 fallback."""
    arguments = [
        "run",
        "-d",
        "--rm",
        "--name",
        container_name,
        "--cap-add",
        "NET_ADMIN",
        "-e",
        "DISK_SIZE=32G",
        "-e",
        "RAM_SIZE=4G",
        "-e",
        "CPU_CORES=4",
        # qemu-docker's iptables fallback otherwise forwards only SSH/RDP.
        # OSWorld's guest API must remain reachable even on a host without the
        # ip_tables modules.
        "-e",
        "USER_PORTS=5000",
        "-v",
        f"{qcow}:/System.qcow2:ro",
    ]
    if publish_guest_port:
        arguments.extend(["-p", "127.0.0.1::5000"])
    if kvm_available:
        arguments.extend(["--device", "/dev/kvm"])
    else:
        arguments.extend(["-e", "KVM=N"])
    arguments.append(docker_image)
    return arguments


def _start_osworld_container(
    *,
    container_name: str,
    qcow: Path,
    docker_image: str,
    kvm_available: bool,
    timeout_s: float,
) -> tuple[str, Literal["published_port", "direct_bridge"], str | None]:
    try:
        container_id = _docker(
            *docker_run_args(
                container_name=container_name,
                qcow=qcow,
                docker_image=docker_image,
                kvm_available=kvm_available,
            ),
            timeout=timeout_s,
        )
        return container_id, "published_port", None
    except DockerCommandError as error:
        if not is_docker_publish_failure(error):
            raise
        container_id = _docker(
            *docker_run_args(
                container_name=container_name,
                qcow=qcow,
                docker_image=docker_image,
                kvm_available=kvm_available,
                publish_guest_port=False,
            ),
            timeout=timeout_s,
        )
        return container_id, "direct_bridge", str(error)


def docker_guest_endpoint(
    container_id: str,
    *,
    access: Literal["published_port", "direct_bridge"],
) -> str:
    if access == "published_port":
        port_output = _docker("port", container_id, "5000/tcp")
        guest_port = int(port_output.rsplit(":", 1)[-1])
        return f"http://127.0.0.1:{guest_port}"
    if access == "direct_bridge":
        address = _docker(
            "inspect",
            container_id,
            "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
        ).strip()
        if not address or any(
            character not in "0123456789abcdefABCDEF:." for character in address
        ):
            raise RuntimeError(
                "Docker did not expose a usable private bridge address"
            )
        host = f"[{address}]" if ":" in address else address
        return f"http://{host}:5000"
    raise ValueError(f"unsupported Docker guest access mode: {access}")


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_unscored_failure(
    output_dir: Path,
    *,
    suite_revision: str,
    task_id: str,
    stage: str,
    error: BaseException,
    started: float,
) -> None:
    payload = {
        "schema_version": 1,
        "suite": "osworld-verified",
        "suite_revision": suite_revision,
        "task_id": task_id,
        "status": "unscored",
        "stage": stage,
        "error_type": type(error).__name__,
        "error": str(error)[:4000],
        "official_score": None,
        "harness_status": None,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
    (output_dir / "failure.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


async def _wait_guest(endpoint: str, timeout_s: float) -> tuple[int, int]:
    deadline = time.monotonic() + timeout_s
    last = ""
    async with httpx.AsyncClient(base_url=endpoint) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get("/screenshot", timeout=10)
                response.raise_for_status()
                with Image.open(__import__("io").BytesIO(response.content)) as image:
                    image.load()
                    return image.size
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
            await asyncio.sleep(2)
    raise TimeoutError(f"OSWorld guest did not become ready: {last}")


async def _close_models(pool: Any) -> None:
    for provider in pool.providers.values():
        close = getattr(provider, "aclose", None)
        if close is not None:
            await close()


def update_stagnant_cycle_count(
    *,
    previous_action_index: int,
    current_action_index: int,
    stagnant_cycles: int,
    operational_outage: bool = False,
    limit: int = 2,
) -> tuple[int, bool]:
    """Track outer-loop cycles that made no durable HID action progress."""
    if current_action_index > previous_action_index:
        return 0, False
    if operational_outage:
        return stagnant_cycles, False
    updated = stagnant_cycles + 1
    return updated, updated >= limit


async def _run_bounded_harness(
    harness: Any,
    task: str,
    *,
    max_cycles: int,
    max_run_time_s: float,
    approval_resolver: ApprovalResolver | None = None,
    approval_waiter: ApprovalWaiter | None = None,
    initial_run: Any | None = None,
    run_locks: dict[str, asyncio.Lock] | None = None,
) -> tuple[Any, int, bool]:
    run = None
    cycles = 0
    timed_out = False

    async def guarded(run_id: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        if run_locks is None:
            return await operation()
        lock = run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            return await operation()

    try:
        async with asyncio.timeout(max_run_time_s):
            if initial_run is None:
                run = await harness.start(task)
            else:
                run = initial_run
                if run.status is RunStatus.RUNNING:
                    run = await guarded(
                        run.run_id,
                        lambda: harness.continue_run(run.run_id),
                    )
            cycles = 1
            stagnant_cycles = 0
            previous_action_index = run.next_action_index
            while (
                run.status in {RunStatus.PAUSED, RunStatus.NEEDS_APPROVAL}
                and cycles < max_cycles
            ):
                previous_event_count = len(getattr(run, "events", []))
                if run.status is RunStatus.NEEDS_APPROVAL:
                    if approval_waiter is not None:
                        run = await approval_waiter(run)
                    elif approval_resolver is None:
                        break
                    else:
                        pending = run.pending_approval or {}
                        approval_id = str(
                            pending.get("approval_id") or ""
                        )
                        if not approval_id:
                            raise ValueError(
                                "approval-required run has no approval_id"
                            )
                        decision = await approval_resolver(run)
                        if decision is None:
                            break
                        run = await guarded(
                            run.run_id,
                            lambda: harness.resolve_approval(
                                run.run_id,
                                approval_id,
                                decision,
                            ),
                        )
                else:
                    run = await guarded(
                        run.run_id,
                        lambda: harness.continue_run(run.run_id),
                    )
                cycles += 1
                cycle_events = getattr(run, "events", [])[previous_event_count:]
                stagnant_cycles, stopped = update_stagnant_cycle_count(
                    previous_action_index=previous_action_index,
                    current_action_index=run.next_action_index,
                    stagnant_cycles=stagnant_cycles,
                    operational_outage=any(
                        event.kind
                        in {
                            "model.failed",
                            "action.transport_uncertain",
                        }
                        for event in cycle_events
                    ),
                )
                previous_action_index = run.next_action_index
                if stopped and run.status is RunStatus.PAUSED:
                    run.status = RunStatus.BLOCKED
                    run.error = (
                        "benchmark stopped after two cycles without "
                        "durable action progress"
                    )
                    run.record(
                        "run.stagnation_stopped",
                        stagnant_cycles=stagnant_cycles,
                    )
                    await harness.store.save(run)
                    break
    except TimeoutError:
        if run is None:
            raise
        run = await guarded(
            run.run_id,
            lambda: harness.abort(
                run.run_id,
                "OSWorld model wall-time budget reached",
            ),
        )
        timed_out = True
    except asyncio.CancelledError:
        if run is not None:
            await asyncio.shield(
                guarded(
                    run.run_id,
                    lambda: harness.abort(
                        run.run_id,
                        "OSWorld benchmark cancelled by caller",
                    ),
                )
            )
        raise
    return run, cycles, timed_out


async def run_osworld_case(
    *,
    repo: Path,
    suite_revision: str,
    qcow: Path,
    docker_image: str,
    task_id: str,
    harness_config: Path,
    output_dir: Path,
    startup_timeout_s: float = 900,
    max_cycles: int = 20,
    max_run_time_s: float = 900,
    approval_resolver: ApprovalResolver | None = None,
    approval_waiter: ApprovalWaiter | None = None,
    operator_console: bool = False,
) -> OSWorldCaseReport:
    """Run one pinned official task through the production orchestration seams."""
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    stage = "preflight"
    try:
        verify_checkout_revision(repo, suite_revision)
        inventory = discover_desktop_suite(
            "osworld-verified",
            repo,
            revision=suite_revision,
        )
        selected = next(
            (task for task in inventory.tasks if task.task_id == task_id),
            None,
        )
        if selected is None:
            raise ValueError(f"task is not in official test_all.json: {task_id}")
        task = json.loads(selected.config_path.read_text(encoding="utf-8"))
        validate_osworld_task_compatibility(task)
        qcow = qcow.expanduser().resolve()
        if not qcow.is_file():
            raise FileNotFoundError(qcow)

        stage = "provider_initialization"
        settings = load_harness_settings(harness_config)
        if operator_console and (
            approval_resolver is not None or approval_waiter is not None
        ):
            raise ValueError(
                "operator_console cannot be combined with another approval handler"
            )
        operator_token = ""
        if operator_console:
            ensure_safe_bind(settings)
            operator_token = settings.access_token()
        ensure_provider_prerequisites(settings)
        pool = build_model_pool(settings)
    except Exception as exc:
        _write_unscored_failure(
            output_dir,
            suite_revision=suite_revision,
            task_id=task_id,
            stage=stage,
            error=exc,
            started=started,
        )
        raise

    container_name = f"pikvm-osworld-{uuid.uuid4().hex[:12]}"
    container_id = ""
    container_access: Literal["published_port", "direct_bridge"] = (
        "published_port"
    )
    container_publish_fallback_reason: str | None = None
    report_path = output_dir / "report.json"
    state_path = output_dir / "harness.sqlite3"
    artifacts = output_dir / "artifacts"
    loop = asyncio.get_running_loop()
    current_task = asyncio.current_task()
    sigterm_handler_installed = False
    if current_task is not None:
        try:
            loop.add_signal_handler(signal.SIGTERM, current_task.cancel)
            sigterm_handler_installed = True
        except (NotImplementedError, RuntimeError):
            pass
    try:
        stage = "container_start"
        (
            container_id,
            container_access,
            container_publish_fallback_reason,
        ) = _start_osworld_container(
            container_name=container_name,
            qcow=qcow,
            docker_image=docker_image,
            kvm_available=Path("/dev/kvm").exists(),
            timeout_s=startup_timeout_s,
        )
        guest_endpoint = docker_guest_endpoint(
            container_id,
            access=container_access,
        )
        stage = "guest_startup"
        width, height = await _wait_guest(guest_endpoint, startup_timeout_s)

        async with (
            httpx.AsyncClient(base_url=guest_endpoint) as coordinator,
            httpx.AsyncClient(follow_redirects=True) as download_client,
        ):
            stage = "official_setup"
            await apply_official_setup(
                coordinator,
                task,
                width=width,
                height=height,
                download_client=download_client,
            )
            await asyncio.sleep(2)
            stage = "model_harness"
            ports = allocate_lab_ports()
            with RunningLab(
                endpoint=guest_endpoint,
                root=output_dir / "lab",
                ports=ports,
                executable=os.path.abspath(sys.executable),
                keymap="en-us",
                quiet=True,
                transport="in-guest",
                policy=isolated_benchmark_policy(),
            ) as lab:
                daemon_access = DaemonAccess.from_environment(lab.env)
                client = PersistentMcpToolClient(
                    daemon_url=lab.daemon_url,
                    artifact_dir=artifacts,
                    daemon_access=daemon_access,
                )
                store = SqliteRunStore(state_path)
                harness = AgentHarness(
                    computer=McpComputerDriver(client),
                    models=pool,
                    store=store,
                    config=HarnessConfig(
                        max_actions_per_advance=settings.max_actions_per_advance,
                        max_actions_per_burst=settings.max_actions_per_burst,
                        max_total_actions=settings.max_total_actions,
                    ),
                )
                console: OperatorConsoleServer | None = None
                live_frames: DaemonLiveFrameSource | None = None
                prepared_run: Any | None = None
                shared_run_locks: dict[str, asyncio.Lock] | None = None
                try:
                    effective_approval_waiter = approval_waiter
                    if operator_console:
                        prepared_run = await harness.create(
                            selected.instruction
                        )
                        shared_run_locks = {}
                        host, port = settings.host_port()
                        live_frames = DaemonLiveFrameSource(
                            lab.daemon_url,
                            bearer_token=daemon_access.harness_token,
                        )
                        operator_app = create_harness_app(
                            harness=harness,
                            store=store,
                            models=pool,
                            access_token=operator_token,
                            allowed_origins=settings.resolved_origins(),
                            live_frames=live_frames,
                            run_locks=shared_run_locks,
                            external_driver=True,
                        )
                        console = OperatorConsoleServer(
                            operator_app,
                            host=host,
                            port=port,
                        )
                        await console.start()
                        write_operator_console_descriptor(
                            output_dir / "operator-console.json",
                            url=console.url,
                            access_token_env=settings.access_token_env,
                        )
                        print(
                            f"Operator UI: {console.url} "
                            f"(token: ${settings.access_token_env})",
                            flush=True,
                        )

                        async def wait_for_console(run: Any) -> Any:
                            return await wait_for_operator_approval(store, run)

                        effective_approval_waiter = wait_for_console

                    run, cycles, model_run_timed_out = (
                        await _run_bounded_harness(
                            harness,
                            selected.instruction,
                            max_cycles=max_cycles,
                            max_run_time_s=max_run_time_s,
                            approval_resolver=approval_resolver,
                            approval_waiter=effective_approval_waiter,
                            initial_run=prepared_run,
                            run_locks=shared_run_locks,
                        )
                    )
                finally:
                    if console is not None:
                        await console.close()
                    if live_frames is not None:
                        await live_frames.aclose()
                    await client.close()

            stage = "official_postconfig"
            await apply_official_postconfig(
                coordinator,
                task,
                width=width,
                height=height,
                download_client=download_client,
            )
            stage = "official_evaluator"
            official_score, evaluator = await evaluate_official_exact_match(
                coordinator,
                task,
            )

        stage = "report"
        image_id = _docker("image", "inspect", docker_image, "--format", "{{.Id}}")
        report = OSWorldCaseReport(
            suite_revision=suite_revision,
            task_id=selected.task_id,
            domain=selected.domain,
            instruction=selected.instruction,
            docker_image=docker_image,
            docker_image_id=image_id,
            vm_image_sha256=_sha256(qcow),
            container_access=container_access,
            container_publish_fallback_reason=(
                container_publish_fallback_reason
            ),
            harness_status=run.status.value,
            official_score=official_score,
            evaluator=evaluator,
            approvals_required=sum(
                event.kind == "approval.required" for event in run.events
            ),
            cycles=cycles,
            model_run_budget_s=max_run_time_s,
            model_run_timed_out=model_run_timed_out,
            performance=summarize_run_performance(run),
            run_state_path=state_path,
            artifact_dir=artifacts,
            report_path=report_path,
        )
        report_path.write_text(report.model_dump_json(indent=2) + "\n")
        return report
    except (Exception, asyncio.CancelledError) as exc:
        _write_unscored_failure(
            output_dir,
            suite_revision=suite_revision,
            task_id=task_id,
            stage=stage,
            error=exc,
            started=started,
        )
        raise
    finally:
        await _close_models(pool)
        if container_id:
            try:
                _docker("stop", "--time", "10", container_id, timeout=30)
            except Exception:
                pass
        elapsed = time.monotonic() - started
        (output_dir / "elapsed-seconds.txt").write_text(f"{elapsed:.3f}\n")
        if sigterm_handler_installed:
            loop.remove_signal_handler(signal.SIGTERM)
