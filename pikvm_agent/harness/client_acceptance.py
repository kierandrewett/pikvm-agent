"""Target-free acceptance for generated managed MCP client launchers."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Sequence

import httpx
import uvicorn
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pikvm_agent.harness.agent import AgentHarness
from pikvm_agent.harness.agent_models import (
    ComputerObservation,
    HarnessConfig,
    ModelRequest,
    ModelResponse,
)
from pikvm_agent.harness.agent_store import SqliteRunStore
from pikvm_agent.harness.api import create_harness_app
from pikvm_agent.harness.client_setup import (
    ClientKind,
    parse_client_launch_config,
    render_client_config,
)
from pikvm_agent.harness.config import HarnessSettings
from pikvm_agent.harness.mcp_driver import unpack_tool_result
from pikvm_agent.harness.model_pool import ModelPool, RoleRoute

SUPPORTED_MANAGED_CLIENTS: tuple[ClientKind, ...] = (
    "codex",
    "claude",
    "gemini",
    "opencode",
)
REQUIRED_MANAGED_TOOLS = (
    "computer_abort",
    "computer_continue",
    "computer_pause",
    "computer_start_task",
    "computer_status",
)
_AGENT_TOKEN_ENV = "PIKVM_ACCEPTANCE_AGENT_TOKEN"
_OPERATOR_TOKEN_ENV = "PIKVM_ACCEPTANCE_OPERATOR_TOKEN"
_OBSERVER_TOKEN_ENV = "PIKVM_ACCEPTANCE_OBSERVER_TOKEN"
_DAEMON_URL_ENV = "PIKVM_ACCEPTANCE_ABSENT_DAEMON_URL"
_SAFE_SUBPROCESS_ENV = (
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "PYTHONPATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "VIRTUAL_ENV",
    "WINDIR",
)


class ManagedClientAcceptanceCase(BaseModel):
    """Failure-inclusive result for one generated client configuration."""

    model_config = ConfigDict(extra="forbid")

    client: ClientKind
    passed: bool
    scoped_environment_exact: bool
    tool_inventory_exact: bool
    task_completed: bool
    operator_run_visible: bool
    durable_run_recovered: bool
    outage_error_safe: bool
    mcp_process_survived_outage: bool
    startup_latency_ms: int | None = Field(default=None, ge=0)
    task_latency_ms: int | None = Field(default=None, ge=0)
    recovery_latency_ms: int | None = Field(default=None, ge=0)
    error_class: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def passing_result_has_complete_evidence(
        self,
    ) -> "ManagedClientAcceptanceCase":
        gates = (
            self.scoped_environment_exact,
            self.tool_inventory_exact,
            self.task_completed,
            self.operator_run_visible,
            self.durable_run_recovered,
            self.outage_error_safe,
            self.mcp_process_survived_outage,
        )
        if self.passed and (
            not all(gates)
            or self.error_class is not None
            or self.startup_latency_ms is None
            or self.task_latency_ms is None
            or self.recovery_latency_ms is None
        ):
            raise ValueError(
                "passing result requires all managed-client gates and latencies"
            )
        if not self.passed and not self.error_class:
            raise ValueError("failed result requires a safe error_class")
        return self


class ManagedClientAcceptanceReport(BaseModel):
    """Private evidence envelope safe to publish after human review."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    suite: Literal["managed-client-acceptance"] = (
        "managed-client-acceptance"
    )
    created_at: str
    clients_requested: int = Field(ge=1)
    clients_passed: int = Field(ge=0)
    clients_failed: int = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)
    evaluation_wall_ms: int = Field(ge=0)
    computer_target_contacted: bool = False
    computer_execution: Literal["deterministic-synthetic"] = (
        "deterministic-synthetic"
    )
    provider_execution: Literal["deterministic-synthetic"] = (
        "deterministic-synthetic"
    )
    external_provider_calls: int = Field(default=0, ge=0)
    cases: list[ManagedClientAcceptanceCase] = Field(min_length=1)

    @model_validator(mode="after")
    def totals_match_cases(self) -> "ManagedClientAcceptanceReport":
        passed = sum(case.passed for case in self.cases)
        if (
            self.clients_requested != len(self.cases)
            or self.clients_passed != passed
            or self.clients_failed != len(self.cases) - passed
        ):
            raise ValueError("managed-client report totals do not match cases")
        expected_rate = passed / len(self.cases)
        if abs(self.success_rate - expected_rate) > 1e-12:
            raise ValueError(
                "managed-client success rate does not match cases"
            )
        if self.computer_target_contacted or self.external_provider_calls:
            raise ValueError(
                "managed-client acceptance must remain target-free"
            )
        return self


def build_managed_client_acceptance_report(
    *,
    cases: Sequence[ManagedClientAcceptanceCase],
    evaluation_wall_ms: int,
    created_at: datetime | None = None,
) -> ManagedClientAcceptanceReport:
    """Build an exact failure-inclusive report from one result per client."""

    if not cases:
        raise ValueError("at least one managed client is required")
    clients = [case.client for case in cases]
    if len(clients) != len(set(clients)):
        raise ValueError("managed client cases must be unique")
    passed = sum(case.passed for case in cases)
    timestamp = (created_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    return ManagedClientAcceptanceReport(
        created_at=timestamp,
        clients_requested=len(cases),
        clients_passed=passed,
        clients_failed=len(cases) - passed,
        success_rate=passed / len(cases),
        evaluation_wall_ms=evaluation_wall_ms,
        cases=list(cases),
    )


def write_managed_client_acceptance_report(
    path: Path,
    report: ManagedClientAcceptanceReport,
) -> None:
    """Create a mode-0600 JSON report and refuse accidental overwrite."""

    if not path.parent.is_dir():
        raise ValueError(
            "managed-client acceptance parent directory does not exist"
        )
    data = (report.model_dump_json(indent=2) + "\n").encode()
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ValueError(
            "managed-client acceptance output already exists"
        ) from exc
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)


class _AcceptanceProvider:
    name = "managed-acceptance-scripted"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "reasoner":
            data = {
                "summary": "Type and independently verify the requested text.",
                "steps": ["Type the text", "Verify the visible result"],
                "success_criteria": [
                    "The focused editor contains exactly hello world"
                ],
                "constraints": ["Do not submit or send anything"],
            }
        elif request.role == "controller":
            data = {
                "outcome": "act",
                "intent": "Type hello world into the focused editor.",
                "actions": [{"type": "type_text", "text": "hello world"}],
                "expected_evidence": [
                    "The focused editor visibly contains hello world"
                ],
            }
        else:
            data = {
                "verdict": "complete",
                "summary": "The exact requested text is visible.",
                "evidence": ["The focused editor shows hello world."],
                "criteria": [
                    {
                        "criterion_index": 0,
                        "satisfied": True,
                        "evidence": "The exact text is visible.",
                    }
                ],
            }
        return ModelResponse(
            provider=self.name,
            model="acceptance-v1",
            data=data,
        )


class _AcceptanceComputer:
    @staticmethod
    def _observation(
        *,
        session_id: str,
        status: str,
        frame_id: int,
        world_version: int,
        control_epoch: int | None,
    ) -> ComputerObservation:
        return ComputerObservation(
            session_id=session_id,
            status=status,
            frame_id=frame_id,
            world_version=world_version,
            control_epoch=control_epoch,
            machine={
                "alias": "Acceptance desktop",
                "fingerprint": "target:acceptance",
                "desktop_layer": "synthetic",
            },
        )

    async def open(self, label: str) -> ComputerObservation:
        return self._observation(
            session_id="acceptance-session",
            status="paused",
            frame_id=1,
            world_version=1,
            control_epoch=1,
        )

    async def burst(
        self,
        *,
        session_id: str,
        actions: list[dict[str, Any]],
        based_on_world_version: int | None,
        based_on_control_epoch: int | None,
        idempotency_key: str,
    ) -> ComputerObservation:
        return self._observation(
            session_id=session_id,
            status="completed",
            frame_id=2,
            world_version=2,
            control_epoch=based_on_control_epoch,
        )

    async def resolve_approval(
        self,
        *,
        session_id: str,
        approval_id: str,
        decision: dict[str, Any],
    ) -> ComputerObservation:
        raise AssertionError("acceptance run must not request approval")

    async def abort(
        self,
        *,
        session_id: str,
        reason: str,
    ) -> ComputerObservation:
        return ComputerObservation(session_id=session_id, status="aborted")


def build_managed_client_acceptance_app(
    *,
    state_path: Path,
    port: int,
    operator_token: str,
    agent_token: str,
) -> Any:
    """Build the real harness API around deterministic target-free adapters."""

    provider = _AcceptanceProvider()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            role: RoleRoute(providers=[provider.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    store = SqliteRunStore(state_path)
    harness = AgentHarness(
        computer=_AcceptanceComputer(),
        models=pool,
        store=store,
        config=HarnessConfig(max_actions_per_advance=1),
    )
    app = create_harness_app(
        harness=harness,
        store=store,
        models=pool,
        access_token=operator_token,
        agent_token=agent_token,
        allowed_origins={f"http://127.0.0.1:{port}"},
    )
    app.state.harness_store = store
    app.state.synthetic_managed_client_acceptance = True
    return app


def _allocate_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _acceptance_settings(
    *,
    root: Path,
    port: int,
) -> HarnessSettings:
    return HarnessSettings.model_validate(
        {
            "listen": f"127.0.0.1:{port}",
            "daemon_url_env": _DAEMON_URL_ENV,
            "access_token_env": _OPERATOR_TOKEN_ENV,
            "agent_token_env": _AGENT_TOKEN_ENV,
            "observer_token_env": _OBSERVER_TOKEN_ENV,
            "state_path": root / "managed-runs.sqlite3",
            "artifact_dir": root / "artifacts",
            "providers": {
                "synthetic": {
                    "kind": "subprocess_json",
                    "model": "acceptance-v1",
                    "argv": ["never-invoked"],
                }
            },
            "routes": {
                "reasoner": ["synthetic"],
                "controller": ["synthetic"],
                "verifier": ["synthetic"],
            },
        }
    )


def _write_acceptance_config(
    path: Path,
    settings: HarnessSettings,
) -> None:
    payload = {
        "listen": settings.listen,
        "daemon_url_env": settings.daemon_url_env,
        "access_token_env": settings.access_token_env,
        "agent_token_env": settings.agent_token_env,
        "observer_token_env": settings.observer_token_env,
        "state_path": str(settings.state_path),
        "artifact_dir": str(settings.artifact_dir),
        "providers": {
            "synthetic": {
                "kind": "subprocess_json",
                "model": "acceptance-v1",
                "argv": ["never-invoked"],
            }
        },
        "routes": {
            "reasoner": ["synthetic"],
            "controller": ["synthetic"],
            "verifier": ["synthetic"],
        },
    }
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        yaml.safe_dump(payload, output, sort_keys=False)


def _safe_subprocess_environment() -> dict[str, str]:
    """Carry process essentials without ambient target/provider variables."""

    return {
        name: os.environ[name]
        for name in _SAFE_SUBPROCESS_ENV
        if name in os.environ
    }


def _acceptance_server_environment(
    *,
    operator_token: str,
    agent_token: str,
) -> dict[str, str]:
    """Add only synthetic-server credentials to the safe process baseline."""

    environment = _safe_subprocess_environment()
    environment.update(
        {
            _OPERATOR_TOKEN_ENV: operator_token,
            _AGENT_TOKEN_ENV: agent_token,
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


def _start_acceptance_server(
    *,
    port: int,
    state_path: Path,
    operator_token: str,
    agent_token: str,
    timeout_s: float,
) -> subprocess.Popen[bytes]:
    environment = _acceptance_server_environment(
        operator_token=operator_token,
        agent_token=agent_token,
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pikvm_agent.harness.client_acceptance",
            "--serve",
            "--port",
            str(port),
            "--state",
            str(state_path),
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout_s
    base_url = f"http://127.0.0.1:{port}"
    headers = {"Authorization": f"Bearer {agent_token}"}
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("synthetic harness process exited")
        try:
            response = httpx.get(
                f"{base_url}/api/agent/health",
                headers=headers,
                timeout=0.2,
            )
            if response.status_code == 200:
                return process
        except httpx.RequestError:
            pass
        time.sleep(0.02)
    _stop_acceptance_server(process)
    raise TimeoutError("synthetic harness did not become ready")


def _stop_acceptance_server(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _safe_error_class(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        classes = sorted(
            {
                _safe_error_class(child)
                for child in exc.exceptions
            }
        )
        return "group-" + "-".join(classes)[:90]
    if isinstance(exc, TimeoutError):
        return "timeout"
    name = type(exc).__name__
    safe = "".join(
        character.lower() if character.isalnum() else "-"
        for character in name
    ).strip("-")
    return safe[:100] or "acceptance-error"


async def _poll_completed(
    session: ClientSession,
    *,
    run_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    state: dict[str, Any] | None = None
    while asyncio.get_running_loop().time() < deadline:
        result = unpack_tool_result(
            await asyncio.wait_for(
                session.call_tool(
                    "computer_status",
                    arguments={"run_id": run_id},
                ),
                timeout=timeout_s,
            )
        )
        state = result["state"]
        if state is not None and state.get("status") == "completed":
            return state
        await asyncio.sleep(0.02)
    raise TimeoutError("synthetic managed task did not complete")


async def _operator_visible(
    *,
    base_url: str,
    operator_token: str,
    run_id: str,
    expected_label: str,
) -> bool:
    headers = {"Authorization": f"Bearer {operator_token}"}
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=3,
    ) as client:
        page = await client.get("/app/")
        inventory = await client.get("/api/runs")
        run = await client.get(f"/api/runs/{run_id}")
    if page.status_code != 200 or inventory.status_code != 200:
        return False
    if run.status_code != 200:
        return False
    body = run.json()
    kinds = {event["kind"] for event in body.get("events", [])}
    return (
        any(item.get("run_id") == run_id for item in inventory.json())
        and body.get("caller")
        == {"interface": "managed_mcp", "label": expected_label}
        and {
            "run.created",
            "computer.opened",
            "model.provider_started",
            "action.attempted",
            "action.completed",
            "run.completed",
        }
        <= kinds
    )


def _failed_acceptance_case(
    client: str,
    error_class: str,
) -> ManagedClientAcceptanceCase:
    return ManagedClientAcceptanceCase(
        client=client,  # type: ignore[arg-type]
        passed=False,
        scoped_environment_exact=False,
        tool_inventory_exact=False,
        task_completed=False,
        operator_run_visible=False,
        durable_run_recovered=False,
        outage_error_safe=False,
        mcp_process_survived_outage=False,
        error_class=error_class,
    )


async def run_managed_client_acceptance(
    *,
    clients: Sequence[str] | None = None,
    timeout_s: float = 15,
    executable: str | None = None,
) -> ManagedClientAcceptanceReport:
    """Exercise exact generated configs without contacting a computer/model."""

    selected = list(clients or SUPPORTED_MANAGED_CLIENTS)
    if not selected:
        raise ValueError("at least one managed client is required")
    if len(selected) != len(set(selected)):
        raise ValueError("managed clients must be unique")
    invalid = sorted(set(selected) - set(SUPPORTED_MANAGED_CLIENTS))
    if invalid:
        raise ValueError(
            "unsupported managed clients: " + ", ".join(invalid)
        )
    if not 3 <= timeout_s <= 120:
        raise ValueError("timeout_s must be between 3 and 120")
    started_at = time.perf_counter()
    cases: list[ManagedClientAcceptanceCase] = []
    with tempfile.TemporaryDirectory(
        prefix="pikvm-managed-client-acceptance-"
    ) as temporary:
        root = Path(temporary)
        try:
            port = _allocate_loopback_port()
            settings = _acceptance_settings(root=root, port=port)
            config_path = root / "harness.yaml"
            _write_acceptance_config(config_path, settings)
            state_path = settings.state_path
            operator_token = secrets.token_hex(32)
            agent_token = secrets.token_hex(32)
            available_client_environment = {
                _AGENT_TOKEN_ENV: agent_token,
            }
            server: subprocess.Popen[bytes] | None = (
                _start_acceptance_server(
                    port=port,
                    state_path=state_path,
                    operator_token=operator_token,
                    agent_token=agent_token,
                    timeout_s=timeout_s,
                )
            )
        except Exception as exc:
            error_class = _safe_error_class(exc)
            cases.extend(
                _failed_acceptance_case(client, error_class)
                for client in selected
            )
            return build_managed_client_acceptance_report(
                cases=cases,
                evaluation_wall_ms=round(
                    (time.perf_counter() - started_at) * 1000
                ),
            )
        try:
            for client_index, raw_client in enumerate(selected):
                client = raw_client
                gates = {
                    "scoped_environment_exact": False,
                    "tool_inventory_exact": False,
                    "task_completed": False,
                    "operator_run_visible": False,
                    "durable_run_recovered": False,
                    "outage_error_safe": False,
                    "mcp_process_survived_outage": False,
                }
                startup_latency_ms: int | None = None
                task_latency_ms: int | None = None
                recovery_latency_ms: int | None = None
                error_class: str | None = None
                try:
                    rendered = render_client_config(
                        settings,
                        client=client,  # type: ignore[arg-type]
                        executable=executable or sys.executable,
                        harness_config=config_path,
                    )
                    launch = parse_client_launch_config(
                        rendered,
                        client=client,  # type: ignore[arg-type]
                    )
                    gates["scoped_environment_exact"] = (
                        launch.forwarded_env == (_AGENT_TOKEN_ENV,)
                    )
                    if not gates["scoped_environment_exact"]:
                        raise RuntimeError(
                            "generated client environment is not scoped"
                        )
                    child_environment = _safe_subprocess_environment()
                    child_environment.update(
                        {
                            "PYTHONUNBUFFERED": "1",
                            **{
                                name: available_client_environment[name]
                                for name in launch.forwarded_env
                            },
                        }
                    )
                    parameters = StdioServerParameters(
                        command=launch.command,
                        args=list(launch.args),
                        env=child_environment,
                        cwd=root,
                    )
                    startup_started = time.perf_counter()
                    async with stdio_client(parameters) as (reader, writer):
                        async with ClientSession(reader, writer) as session:
                            await asyncio.wait_for(
                                session.initialize(),
                                timeout=timeout_s,
                            )
                            tools = await asyncio.wait_for(
                                session.list_tools(),
                                timeout=timeout_s,
                            )
                            startup_latency_ms = round(
                                (time.perf_counter() - startup_started) * 1000
                            )
                            gates["tool_inventory_exact"] = (
                                tuple(
                                    sorted(tool.name for tool in tools.tools)
                                )
                                == REQUIRED_MANAGED_TOOLS
                            )
                            task_started = time.perf_counter()
                            started = unpack_tool_result(
                                await asyncio.wait_for(
                                    session.call_tool(
                                        "computer_start_task",
                                        arguments={
                                            "task": (
                                                "Type hello world and verify "
                                                f"the result for {client}"
                                            )
                                        },
                                    ),
                                    timeout=timeout_s,
                                )
                            )
                            if started["is_error"] or started["state"] is None:
                                raise RuntimeError(
                                    "managed task creation failed"
                                )
                            run_id = str(started["state"]["run_id"])
                            completed = await _poll_completed(
                                session,
                                run_id=run_id,
                                timeout_s=timeout_s,
                            )
                            task_latency_ms = round(
                                (time.perf_counter() - task_started) * 1000
                            )
                            gates["task_completed"] = (
                                completed.get("status") == "completed"
                            )
                            gates["operator_run_visible"] = (
                                await _operator_visible(
                                    base_url=(
                                        f"http://127.0.0.1:{port}"
                                    ),
                                    operator_token=operator_token,
                                    run_id=run_id,
                                    expected_label=f"{client}-cli",
                                )
                            )

                            assert server is not None
                            _stop_acceptance_server(server)
                            server = None
                            offline = unpack_tool_result(
                                await asyncio.wait_for(
                                    session.call_tool(
                                        "computer_status",
                                        arguments={"run_id": run_id},
                                    ),
                                    timeout=timeout_s,
                                )
                            )
                            offline_text = json.dumps(offline)
                            gates["outage_error_safe"] = (
                                offline["is_error"]
                                and agent_token not in offline_text
                                and "127.0.0.1" not in offline_text
                            )
                            recovery_started = time.perf_counter()
                            server = _start_acceptance_server(
                                port=port,
                                state_path=state_path,
                                operator_token=operator_token,
                                agent_token=agent_token,
                                timeout_s=timeout_s,
                            )
                            recovered = unpack_tool_result(
                                await asyncio.wait_for(
                                    session.call_tool(
                                        "computer_status",
                                        arguments={"run_id": run_id},
                                    ),
                                    timeout=timeout_s,
                                )
                            )
                            recovery_latency_ms = round(
                                (
                                    time.perf_counter() - recovery_started
                                )
                                * 1000
                            )
                            gates["durable_run_recovered"] = (
                                not recovered["is_error"]
                                and recovered["state"] is not None
                                and recovered["state"].get("run_id") == run_id
                                and recovered["state"].get("status")
                                == "completed"
                            )
                            gates["mcp_process_survived_outage"] = (
                                gates["durable_run_recovered"]
                            )
                except Exception as exc:
                    error_class = _safe_error_class(exc)
                    if server is None:
                        try:
                            server = _start_acceptance_server(
                                port=port,
                                state_path=state_path,
                                operator_token=operator_token,
                                agent_token=agent_token,
                                timeout_s=timeout_s,
                            )
                        except Exception:
                            pass
                passed = all(gates.values()) and error_class is None
                if not passed and error_class is None:
                    error_class = "acceptance-gate-failed"
                cases.append(
                    ManagedClientAcceptanceCase(
                        client=client,  # type: ignore[arg-type]
                        passed=passed,
                        startup_latency_ms=startup_latency_ms,
                        task_latency_ms=task_latency_ms,
                        recovery_latency_ms=recovery_latency_ms,
                        error_class=error_class,
                        **gates,
                    )
                )
                if server is None:
                    cases.extend(
                        _failed_acceptance_case(
                            remaining,
                            "harness-restart-failed",
                        )
                        for remaining in selected[client_index + 1 :]
                    )
                    break
        finally:
            if server is not None:
                _stop_acceptance_server(server)
    return build_managed_client_acceptance_report(
        cases=cases,
        evaluation_wall_ms=round(
            (time.perf_counter() - started_at) * 1000
        ),
    )


def _serve_from_cli(port: int, state_path: Path) -> None:
    operator_token = os.environ.get(_OPERATOR_TOKEN_ENV, "")
    agent_token = os.environ.get(_AGENT_TOKEN_ENV, "")
    app = build_managed_client_acceptance_app(
        state_path=state_path,
        port=port,
        operator_token=operator_token,
        agent_token=agent_token,
    )
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int)
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    if not args.serve or args.port is None or args.state is None:
        parser.error("internal acceptance server requires --serve/--port/--state")
    _serve_from_cli(args.port, args.state)


if __name__ == "__main__":
    main()
