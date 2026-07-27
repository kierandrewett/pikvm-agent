"""Isolated lab configuration and process supervision."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal

import httpx
import typer
import yaml

from pikvm_agent.config import AppConfig, PolicyConfig
from pikvm_agent.harness.client_setup import render_client_config
from pikvm_agent.harness.config import (
    HarnessSettings,
    ensure_provider_prerequisites,
    load_harness_settings,
)

PRODUCTION_DAEMON_PORT = 47615
DEFAULT_ADAPTER_PORT = 47640
DEFAULT_LAB_DAEMON_PORT = 47641
DEFAULT_LAB_HARNESS_PORT = 47642
LAB_DAEMON_URL_ENV = "PIKVM_LAB_DAEMON_URL"


def isolated_benchmark_policy() -> PolicyConfig:
    """Authorize reversible guest work while retaining one-shot risk gates.

    This profile is only for resettable benchmark VMs with no user accounts or
    production data. Unknown icon navigation, local edits, and reversible
    settings are task-scoped by the benchmark instruction. Outward,
    credential, destructive, privilege, payment, legal, install, and terminal
    mutations still require an exact human approval.
    """

    return PolicyConfig(
        default_profile="isolated_benchmark",
        allow_local_pointer_freshness=True,
        require_human_for=[
            "communication_send",
            "credential_entry",
            "sensitive_data_transmit",
            "account_or_permission_change",
            "software_installation",
            "power_or_firmware",
            "disk_or_partition",
            "financial_or_purchase",
            "legal_or_consent",
            "terminal_mutating",
            "sudo",
            "delete",
            "file_external_upload",
        ],
    )


@dataclass(frozen=True)
class LabPorts:
    adapter: int = DEFAULT_ADAPTER_PORT
    daemon: int = DEFAULT_LAB_DAEMON_PORT
    harness: int = DEFAULT_LAB_HARNESS_PORT

    def validate(self) -> None:
        values = (self.adapter, self.daemon, self.harness)
        if len(set(values)) != len(values):
            raise ValueError("adapter, daemon, and harness ports must differ")
        if PRODUCTION_DAEMON_PORT in values:
            raise ValueError(
                f"port {PRODUCTION_DAEMON_PORT} is reserved for the production daemon"
            )
        for value in values:
            if value < 1 or value > 65535:
                raise ValueError(f"invalid TCP port: {value}")


def allocate_lab_ports() -> LabPorts:
    """Reserve three fresh loopback ports, never the production daemon port."""
    listeners: list[socket.socket] = []
    values: list[int] = []
    try:
        while len(values) < 3:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            value = int(listener.getsockname()[1])
            if value == PRODUCTION_DAEMON_PORT or value in values:
                listener.close()
                continue
            listeners.append(listener)
            values.append(value)
    finally:
        for listener in listeners:
            listener.close()
    ports = LabPorts(adapter=values[0], daemon=values[1], harness=values[2])
    ports.validate()
    return ports


def _assert_port_available(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.1)
    try:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"lab port {port} is already in use")
    finally:
        probe.close()


@dataclass(frozen=True)
class LabAssets:
    root: Path
    config: Path
    harness_config: Path
    mcp_config: Path
    codex_mcp_config: Path
    opencode_mcp_config: Path
    log_dir: Path


class RunningLab:
    """Context-managed adapter + daemon pair for tests and benchmark runners."""

    def __init__(
        self,
        *,
        endpoint: str,
        root: Path,
        ports: LabPorts,
        executable: str,
        keymap: str,
        password_env: str = "PIKVM_LAB_VNC_PASSWORD",
        username_env: str = "PIKVM_LAB_VNC_USERNAME",
        password: str | None = None,
        username: str | None = None,
        keyboard_profile: Literal["generic", "windows"] = "generic",
        quiet: bool = False,
        transport: Literal["vnc", "in-guest"] = "vnc",
        policy: PolicyConfig | None = None,
        harness_config: Path | None = None,
        start_harness: bool = False,
    ) -> None:
        self.endpoint = endpoint
        self.root = root
        self.ports = ports
        self.executable = executable
        self.keymap = keymap
        self.password_env = password_env
        self.username_env = username_env
        self.password = password
        self.username = username
        self.keyboard_profile = keyboard_profile
        self.quiet = quiet
        self.transport = transport
        self.policy = policy
        self.harness_config = harness_config
        self.start_harness = start_harness
        self.assets: LabAssets | None = None
        self.env: dict[str, str] = {}
        self.adapter: subprocess.Popen[bytes] | None = None
        self.daemon: subprocess.Popen[bytes] | None = None
        self.harness: subprocess.Popen[bytes] | None = None
        self._log_handles: list[IO[bytes]] = []

    def __enter__(self) -> RunningLab:
        self.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def start(self) -> None:
        if self.adapter is not None:
            return
        self.ports.validate()
        _assert_port_available(self.ports.adapter)
        _assert_port_available(self.ports.daemon)
        if self.start_harness:
            _assert_port_available(self.ports.harness)
        self.assets = write_lab_assets(
            self.root,
            ports=self.ports,
            executable=self.executable,
            layout="uk" if self.keymap.lower().startswith("en-gb") else "us",
            policy=self.policy,
            harness_config=self.harness_config,
        )
        env = os.environ.copy()
        if self.password is not None:
            env[self.password_env] = self.password
        if self.username is not None:
            env[self.username_env] = self.username
        env["PIKVM_AGENT_CONFIG"] = str(self.assets.config)
        env[LAB_DAEMON_URL_ENV] = f"http://127.0.0.1:{self.ports.daemon}"
        if self.start_harness:
            settings = load_harness_settings(self.assets.harness_config)
            # Refuse before opening the VNC connection: a visible product lab
            # must not run with missing/shared role credentials or no usable
            # model route.
            settings.access_token()
            settings.agent_token()
            settings.observer_token()
            ensure_provider_prerequisites(settings)
        source_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = (
            source_root
            if not env.get("PYTHONPATH")
            else source_root + os.pathsep + env["PYTHONPATH"]
        )
        self.env = env
        adapter_args = [
                sys.executable,
                "-m",
                "pikvm_agent.harness.service",
                "--vnc" if self.transport == "vnc" else "--in-guest",
                self.endpoint,
                "--host",
                "127.0.0.1",
                "--port",
                str(self.ports.adapter),
                "--keymap",
                self.keymap,
                "--keyboard-profile",
                self.keyboard_profile,
                "--password-env",
                self.password_env,
                "--username-env",
                self.username_env,
            ]
        try:
            adapter_stdout, adapter_stderr = self._child_stdio("adapter")
            self.adapter = subprocess.Popen(
                adapter_args,
                env=env,
                stdout=adapter_stdout,
                stderr=adapter_stderr,
            )
            _wait_ready(
                f"http://127.0.0.1:{self.ports.adapter}/api/info",
                self.adapter,
                20,
            )
            if self.adapter.poll() is not None:
                raise RuntimeError(
                    f"lab adapter exited early with code {self.adapter.returncode}"
                )
            daemon_stdout, daemon_stderr = self._child_stdio("daemon")
            self.daemon = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "pikvm_agent.daemon:lab_app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.ports.daemon),
                    "--log-level",
                    "info",
                ],
                env=env,
                stdout=daemon_stdout,
                stderr=daemon_stderr,
            )
            _wait_ready(
                f"http://127.0.0.1:{self.ports.daemon}/healthz",
                self.daemon,
                30,
            )
            if self.daemon.poll() is not None:
                raise RuntimeError(
                    f"lab daemon exited early with code {self.daemon.returncode}"
                )
            if self.start_harness:
                harness_stdout, harness_stderr = self._child_stdio("harness")
                self.harness = subprocess.Popen(
                    [
                        self.executable,
                        "-m",
                        "pikvm_agent.cli",
                        "harness",
                        "serve",
                        "--config",
                        str(self.assets.harness_config),
                    ],
                    env=env,
                    stdout=harness_stdout,
                    stderr=harness_stderr,
                )
                _wait_ready(
                    f"http://127.0.0.1:{self.ports.harness}/api/health",
                    self.harness,
                    30,
                )
                if self.harness.poll() is not None:
                    raise RuntimeError(
                        "lab harness exited early with code "
                        f"{self.harness.returncode}"
                    )
        except BaseException:
            self.close()
            raise

    def _child_stdio(self, name: str) -> tuple[IO[bytes] | None, IO[bytes] | None]:
        if not self.quiet:
            return None, None
        if self.assets is None:
            raise RuntimeError("lab assets must exist before opening child logs")
        stdout = (self.assets.log_dir / f"{name}.stdout.log").open("ab")
        stderr = (self.assets.log_dir / f"{name}.stderr.log").open("ab")
        self._log_handles.extend((stdout, stderr))
        return stdout, stderr

    def close(self) -> None:
        if self.harness is not None:
            _stop_child(self.harness)
            self.harness = None
        if self.daemon is not None:
            _stop_child(self.daemon)
            self.daemon = None
        if self.adapter is not None:
            _stop_child(self.adapter)
            self.adapter = None
        for handle in self._log_handles:
            handle.close()
        self._log_handles.clear()

    @property
    def daemon_url(self) -> str:
        return f"http://127.0.0.1:{self.ports.daemon}"

    @property
    def harness_url(self) -> str:
        return f"http://127.0.0.1:{self.ports.harness}"


def build_lab_config(
    *,
    api_host: str,
    api_port: int,
    daemon_host: str,
    daemon_port: int,
    state_dir: Path,
    keymap: str,
    policy: PolicyConfig | None = None,
) -> AppConfig:
    """Build a daemon config that can only reach the local lab adapter."""
    state_dir = state_dir.expanduser().resolve()
    return AppConfig.model_validate(
        {
            "daemon": {
                "listen": f"{daemon_host}:{daemon_port}",
                "session_dir": str(state_dir / "sessions"),
                "sqlite_path": str(state_dir / "state.sqlite3"),
                "debug_log": True,
                "debug_log_path": str(state_dir / "debug.jsonl"),
                "debug_log_truncate": True,
            },
            "pikvm": {
                "base_url": f"http://{api_host}:{api_port}",
                "verify_tls": False,
                "layout": "uk" if keymap.lower().startswith("en-gb") else "us",
            },
            "omniparser": {"enabled": False, "required": False},
            "ocr": {"provider": "tesseract", "lang": "en", "device": "cpu"},
            "operator": {"provider": "fake"},
            "policy": (policy or PolicyConfig()).model_dump(),
        }
    )


def build_lab_harness_settings(
    *,
    root: Path,
    port: int,
    source: Path | None = None,
) -> HarnessSettings:
    """Build a loopback-only managed harness config for the selected lab.

    A caller-supplied config contributes only model providers, routes, limits,
    and credential *environment variable names*. Target selection, bind
    address, and state paths are always replaced with lab-owned values.
    """

    if source is None:
        raw: dict[str, Any] = {
            "providers": {
                "codex-account": {
                    "kind": "codex_cli",
                    "model": "account-default",
                },
                "claude-account": {
                    "kind": "claude_cli",
                    "model": "opus",
                },
            },
            "routes": {
                "reasoner": ["claude-account", "codex-account"],
                "controller": ["claude-account", "codex-account"],
                "verifier": ["claude-account", "codex-account"],
            },
        }
    else:
        raw = load_harness_settings(source).model_dump(mode="json")

    raw.update(
        {
            "listen": f"127.0.0.1:{port}",
            "allow_remote_bind": False,
            "daemon_url_env": LAB_DAEMON_URL_ENV,
            "allowed_origins": [],
            "state_path": str(root / "harness" / "state.sqlite3"),
            "artifact_dir": str(root / "harness" / "artifacts"),
        }
    )
    return HarnessSettings.model_validate(raw)


def write_lab_assets(
    root: Path,
    *,
    ports: LabPorts,
    executable: str,
    layout: str = "us",
    policy: PolicyConfig | None = None,
    harness_config: Path | None = None,
) -> LabAssets:
    """Write isolated daemon, managed harness, and MCP client configuration.

    The VNC endpoint deliberately does not belong in any generated file: only
    the adapter process receives it at runtime. This lets the same generated
    lab assets point at Windows, Linux, or another RFB target.
    """
    ports.validate()
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    state = root / "state"
    sessions = state / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    config_path = root / "lab-config.yaml"
    harness_config_path = root / "harness-config.yaml"
    mcp_path = root / "mcp.lab.json"
    codex_mcp_path = root / "mcp.lab.codex.toml"
    opencode_mcp_path = root / "mcp.lab.opencode.json"

    config = build_lab_config(
        api_host="127.0.0.1",
        api_port=ports.adapter,
        daemon_host="127.0.0.1",
        daemon_port=ports.daemon,
        state_dir=state,
        keymap="en-gb" if layout == "uk" else "en-us",
        policy=policy,
    )
    config_path.write_text(
        yaml.safe_dump(config.model_dump(exclude_none=True), sort_keys=False)
    )

    harness_settings = build_lab_harness_settings(
        root=root,
        port=ports.harness,
        source=harness_config,
    )
    harness_config_path.write_text(
        yaml.safe_dump(
            harness_settings.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
        )
    )
    mcp_path.write_text(
        render_client_config(
            harness_settings,
            client="claude",
            executable=executable,
            harness_config=harness_config_path,
            server_name="pikvm-lab",
        )
    )
    codex_mcp_path.write_text(
        render_client_config(
            harness_settings,
            client="codex",
            executable=executable,
            harness_config=harness_config_path,
            server_name="pikvm-lab",
        )
    )
    opencode_mcp_path.write_text(
        render_client_config(
            harness_settings,
            client="opencode",
            executable=executable,
            harness_config=harness_config_path,
            server_name="pikvm-lab",
        )
    )
    return LabAssets(
        root=root,
        config=config_path,
        harness_config=harness_config_path,
        mcp_config=mcp_path,
        codex_mcp_config=codex_mcp_path,
        opencode_mcp_config=opencode_mcp_path,
        log_dir=log_dir,
    )


def _wait_ready(url: str, child: subprocess.Popen[bytes], timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"lab child exited early with code {child.returncode}")
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code < 500:
                return
            last = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(0.15)
    raise RuntimeError(f"timed out waiting for {url}: {last}")


def _stop_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def run_lab(
    *,
    endpoint: str,
    root: Path,
    ports: LabPorts,
    executable: str,
    keymap: str,
    password_env: str,
    username_env: str,
    keyboard_profile: Literal["generic", "windows"] = "generic",
    harness_config: Path | None = None,
) -> None:
    """Run adapter, daemon, and visible harness; production config is never read."""
    with RunningLab(
        endpoint=endpoint,
        root=root,
        ports=ports,
        executable=executable,
        keymap=keymap,
        password_env=password_env,
        username_env=username_env,
        keyboard_profile=keyboard_profile,
        harness_config=harness_config,
        start_harness=True,
    ) as lab:
        assert lab.assets is not None
        typer.echo("Managed PiKVM lab is ready (production daemon untouched).")
        typer.echo(f"  adapter:   http://127.0.0.1:{ports.adapter}")
        typer.echo(f"  daemon:    http://127.0.0.1:{ports.daemon}")
        typer.echo(f"  harness:   http://127.0.0.1:{ports.harness}/app/")
        typer.echo(f"  config:    {lab.assets.config}")
        typer.echo(f"  harness config: {lab.assets.harness_config}")
        typer.echo(f"  Claude/Gemini MCP: {lab.assets.mcp_config}")
        typer.echo(f"  Codex MCP:         {lab.assets.codex_mcp_config}")
        typer.echo(f"  OpenCode MCP:      {lab.assets.opencode_mcp_config}")
        typer.echo("Press Ctrl+C to stop the isolated lab.")

        def stop_signal(_signum: int, _frame: object) -> None:
            raise KeyboardInterrupt

        old_int = signal.signal(signal.SIGINT, stop_signal)
        old_term = signal.signal(signal.SIGTERM, stop_signal)
        try:
            assert (
                lab.adapter is not None
                and lab.daemon is not None
                and lab.harness is not None
            )
            while (
                lab.adapter.poll() is None
                and lab.daemon.poll() is None
                and lab.harness.poll() is None
            ):
                time.sleep(0.5)
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
        raise RuntimeError("a lab process exited unexpectedly")
