"""Isolated launch plans that force stable coding clients through managed MCP.

The module does not edit persisted client configuration. Codex receives an
inline override for the PiKVM server and its exact effective inventory is
audited before launch. Claude receives one explicit MCP document together with
its native strict-config flag. OpenCode receives a pure, inline configuration
whose resolved MCP inventory is audited before launch.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import httpx

from pikvm_agent.endpoint import httpx_client_kwargs
from pikvm_agent.harness.client_config_audit import (
    ClientConfigAuditReport,
    ClientConfigDocument,
    audit_client_configs,
    read_codex_effective_inventory,
)
from pikvm_agent.harness.client_setup import (
    parse_client_launch_config,
    render_client_config,
)
from pikvm_agent.harness.config import HarnessSettings

StableLaunchClient = Literal["codex", "claude", "gemini", "opencode"]
IsolationMode = Literal[
    "isolated-auth-link",
    "strict-explicit-config",
    "system-policy-allowlist",
    "pure-inline-config",
]
_HELP_ENV = (
    "HOME",
    "PATH",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
)
_OPENCODE_ENV = (
    "PATH",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
)
_CODEX_ENV = (
    "PATH",
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
)
_CLAUDE_ENV = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "CLAUDE_CONFIG_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
)
_GEMINI_ENV = (
    "PATH",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
)
_GEMINI_AUTH_FILES = (
    "oauth_creds.json",
    "google_accounts.json",
)
_CLAUDE_ALLOWED_MANAGED_TOOLS = (
    "mcp__pikvm__computer_start_task",
    "mcp__pikvm__computer_status",
    "mcp__pikvm__computer_continue",
    "mcp__pikvm__computer_pause",
)


class ClientIsolationError(RuntimeError):
    """The selected client cannot prove a managed-only PiKVM surface."""


class ManagedTaskCompletionError(RuntimeError):
    """The delegated harness run did not reach verified completion."""


@dataclass(frozen=True)
class ManagedRunCompletion:
    """Safe proof that the delegated harness run reached verified completion."""

    run_id: str
    status: str
    verification_verdict: str
    event_cursor: int
    action_count: int

    def summary(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "verification_verdict": self.verification_verdict,
            "event_cursor": self.event_cursor,
            "action_count": self.action_count,
        }


class ManagedTaskCompletionWatch(Protocol):
    """Boundary used to correlate and await one delegated managed run."""

    def capture_baseline(self) -> frozenset[str]: ...

    def wait_for_completion(
        self,
        baseline: frozenset[str],
        *,
        timeout_s: float,
    ) -> ManagedRunCompletion: ...


class HarnessTaskCompletionWatch:
    """Correlate one newly delegated run and wait for verified completion."""

    _ACTIVE_STATUSES = frozenset(
        {"created", "planning", "running", "executing", "verifying"}
    )
    _INCOMPLETE_STATUSES = frozenset(
        {
            "needs_approval",
            "paused",
            "failed",
            "blocked",
            "rejected",
            "aborted",
        }
    )

    def __init__(
        self,
        *,
        base_url: str,
        agent_token: str,
        caller_label: str,
        transport: httpx.BaseTransport | None = None,
        poll_interval_s: float = 0.5,
    ) -> None:
        if not base_url.strip():
            raise ValueError("managed harness base URL is required")
        if len(agent_token) < 32:
            raise ValueError("managed harness agent token is too short")
        if not caller_label.strip():
            raise ValueError("managed harness caller label is required")
        if not 0 <= poll_interval_s <= 10:
            raise ValueError("completion poll interval must be 0..10 seconds")
        self._base_url = base_url.rstrip("/")
        self._agent_token = agent_token
        self._caller_label = caller_label
        self._transport = transport
        self._poll_interval_s = poll_interval_s

    def _get_json(
        self,
        path: str,
        *,
        params: dict[str, int] | None = None,
    ) -> object:
        try:
            with httpx.Client(
                **httpx_client_kwargs(
                    self._base_url, self._transport, sync=True
                ),
                headers={
                    "Authorization": f"Bearer {self._agent_token}",
                },
                timeout=5.0,
            ) as client:
                response = client.get(path, params=params)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ManagedTaskCompletionError(
                "managed harness completion probe failed"
            ) from exc

    def _list_runs(self) -> list[dict[str, object]]:
        payload = self._get_json("/api/runs", params={"limit": 500})
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise ManagedTaskCompletionError(
                "managed harness returned an invalid run inventory"
            )
        return payload

    def capture_baseline(self) -> frozenset[str]:
        return frozenset(
            run_id
            for item in self._list_runs()
            for run_id in (item.get("run_id"),)
            if isinstance(run_id, str) and run_id
        )

    def _new_matching_runs(
        self,
        baseline: frozenset[str],
    ) -> list[str]:
        matched: list[str] = []
        for item in self._list_runs():
            run_id = item.get("run_id")
            caller = item.get("caller")
            if (
                not isinstance(run_id, str)
                or not run_id
                or run_id in baseline
                or item.get("origin") != "managed"
                or not isinstance(caller, dict)
                or caller.get("interface") != "managed_mcp"
                or caller.get("label") != self._caller_label
            ):
                continue
            matched.append(run_id)
        return matched

    def _run_detail(self, run_id: str) -> dict[str, object]:
        payload = self._get_json(f"/api/runs/{run_id}")
        if not isinstance(payload, dict):
            raise ManagedTaskCompletionError(
                "managed harness returned an invalid run state"
            )
        return payload

    def wait_for_completion(
        self,
        baseline: frozenset[str],
        *,
        timeout_s: float,
    ) -> ManagedRunCompletion:
        if timeout_s <= 0:
            raise ValueError("completion timeout must be positive")
        deadline = time.monotonic() + timeout_s
        run_id: str | None = None
        while time.monotonic() < deadline:
            if run_id is None:
                matched = self._new_matching_runs(baseline)
                if len(matched) > 1:
                    raise ManagedTaskCompletionError(
                        "managed client delegated more than one computer run"
                    )
                if matched:
                    run_id = matched[0]
            if run_id is not None:
                detail = self._run_detail(run_id)
                status = detail.get("status")
                if status == "completed":
                    verification = detail.get("last_verification")
                    verdict = (
                        verification.get("verdict")
                        if isinstance(verification, dict)
                        else None
                    )
                    if verdict not in {"verified", "complete"}:
                        raise ManagedTaskCompletionError(
                            "managed run completed without independent "
                            "verification"
                        )
                    event_cursor = detail.get("event_cursor")
                    action_count = detail.get("next_action_index")
                    return ManagedRunCompletion(
                        run_id=run_id,
                        status=status,
                        verification_verdict=str(verdict),
                        event_cursor=(
                            event_cursor
                            if isinstance(event_cursor, int)
                            else 0
                        ),
                        action_count=(
                            action_count
                            if isinstance(action_count, int)
                            else 0
                        ),
                    )
                if status in self._INCOMPLETE_STATUSES:
                    raise ManagedTaskCompletionError(
                        f"managed run stopped with status {status}"
                    )
                if status not in self._ACTIVE_STATUSES:
                    raise ManagedTaskCompletionError(
                        "managed harness returned an unknown run status"
                    )
            if self._poll_interval_s:
                time.sleep(
                    min(
                        self._poll_interval_s,
                        max(0, deadline - time.monotonic()),
                    )
                )
        if run_id is None:
            raise ManagedTaskCompletionError(
                "managed client did not delegate exactly one computer run"
            )
        raise ManagedTaskCompletionError(
            "managed run did not finish before the runtime limit"
        )


@dataclass(frozen=True)
class ManagedClientLaunch:
    """Internal exact launch shape plus a small secret-free reporting surface."""

    client: StableLaunchClient
    server_name: str
    isolation_mode: IsolationMode
    argv: tuple[str, ...]
    project_dir: Path
    rendered_config: str
    inventory_config_overrides: tuple[str, ...]
    forwarded_env: tuple[str, ...]
    preserve_unrelated_mcp: bool
    modifies_persisted_config: bool = False

    def summary(
        self,
        *,
        report: ClientConfigAuditReport,
        would_launch: bool,
    ) -> dict[str, object]:
        digest = hashlib.sha256(
            json.dumps(self.argv, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "schema_version": 1,
            "client": self.client,
            "isolation_mode": self.isolation_mode,
            "safe": report.safe,
            "managed_count": report.managed_count,
            "failures": list(report.failures),
            "preserve_unrelated_mcp": self.preserve_unrelated_mcp,
            "modifies_persisted_config": self.modifies_persisted_config,
            "mcp_forwarded_env_names": list(self.forwarded_env),
            "launch_argv_sha256": digest,
            "would_launch": would_launch,
        }


@dataclass(frozen=True)
class ManagedClientTaskResult:
    """Safe execution metadata; task and client output remain private."""

    client: StableLaunchClient
    exit_code: int
    elapsed_ms: int
    task_bytes: int
    task_sha256: str
    completion: ManagedRunCompletion | None = None

    def summary(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": 1,
            "client": self.client,
            "exit_code": self.exit_code,
            "elapsed_ms": self.elapsed_ms,
            "task_bytes": self.task_bytes,
            "task_sha256": self.task_sha256,
        }
        if self.completion is not None:
            result["managed_run"] = self.completion.summary()
        return result


def _codex_config_prefix(server_name: str) -> str:
    if server_name.replace("_", "").isalnum():
        return f"mcp_servers.{server_name}"
    return f"mcp_servers.{json.dumps(server_name)}"


def _codex_overrides(
    *,
    server_name: str,
    command: str,
    args: tuple[str, ...],
    forwarded_env: tuple[str, ...],
) -> tuple[str, ...]:
    prefix = _codex_config_prefix(server_name)
    supervised_tools = (
        "computer_start_task",
        "computer_status",
        "computer_continue",
        "computer_pause",
        "computer_abort",
    )
    preapproved_tools = supervised_tools[:-1]
    values = (
        f"{prefix}.command={json.dumps(command)}",
        f"{prefix}.args={json.dumps(list(args))}",
        f"{prefix}.env_vars={json.dumps(list(forwarded_env))}",
        f"{prefix}.enabled=true",
        f"{prefix}.required=true",
        f"{prefix}.tool_timeout_sec=300",
        f"{prefix}.enabled_tools={json.dumps(list(supervised_tools))}",
        f'{prefix}.default_tools_approval_mode="prompt"',
        *(
            f'{prefix}.tools.{tool}.approval_mode="approve"'
            for tool in preapproved_tools
        ),
    )
    return tuple(token for value in values for token in ("-c", value))


@contextmanager
def _codex_child_environment(
    plan: ManagedClientLaunch,
    *,
    environ: Mapping[str, str] | None,
) -> Iterator[dict[str, str]]:
    """Create clean Codex state while retaining only client-owned OAuth.

    A normal Codex home can contain unrelated MCP servers, plugins, rules,
    histories, and writable runtime state. The managed task links only the
    CLI-owned ``auth.json`` into a new private home, applies the one inline
    managed server, and forwards only a bounded runtime allow-list plus the
    scoped harness agent credential.
    """

    source = os.environ if environ is None else environ
    for name in plan.forwarded_env:
        if not source.get(name):
            raise ClientIsolationError(
                "Codex managed MCP environment is incomplete"
            )
    source_home_value = source.get("CODEX_HOME")
    if source_home_value:
        source_home = Path(source_home_value).expanduser()
    else:
        ordinary_home = source.get("HOME") or source.get("USERPROFILE")
        source_home = (
            Path(ordinary_home).expanduser() / ".codex"
            if ordinary_home
            else None
        )
    with tempfile.TemporaryDirectory(prefix="pikvm-codex-client-") as root_value:
        isolated_home = Path(root_value) / "codex-home"
        isolated_home.mkdir()
        if source_home is not None:
            source_auth = source_home / "auth.json"
            if source_auth.is_file():
                (isolated_home / "auth.json").symlink_to(
                    source_auth.resolve()
                )
        child = {
            name: source[name]
            for name in _CODEX_ENV
            if source.get(name)
        }
        child["CODEX_HOME"] = str(isolated_home)
        child["NO_COLOR"] = "1"
        child.update(
            {
                name: source[name]
                for name in plan.forwarded_env
            }
        )
        yield child


@contextmanager
def _opencode_child_environment(
    plan: ManagedClientLaunch,
    *,
    environ: Mapping[str, str] | None,
) -> Iterator[dict[str, str]]:
    """Create a process-private OpenCode home while retaining its OAuth store."""

    source = os.environ if environ is None else environ
    source_home = source.get("HOME") or source.get("USERPROFILE")
    if not source_home:
        raise ClientIsolationError(
            "OpenCode isolation requires HOME or USERPROFILE"
        )
    source_data_home = source.get("XDG_DATA_HOME")
    if not source_data_home:
        source_data_home = str(Path(source_home) / ".local" / "share")
    source_auth = Path(source_data_home) / "opencode" / "auth.json"
    real_binary_values = [
        source.get("OPENCODE_REAL_BIN"),
        str(Path(source_home) / ".opencode" / "bin" / "opencode"),
    ]
    real_binary = next(
        (
            candidate.resolve()
            for value in real_binary_values
            if value
            for candidate in (Path(value).expanduser(),)
            if candidate.is_absolute()
            and candidate.is_file()
            and os.access(candidate, os.X_OK)
        ),
        None,
    )
    for name in plan.forwarded_env:
        if not source.get(name):
            raise ClientIsolationError(
                "OpenCode managed MCP environment is incomplete"
            )
    with tempfile.TemporaryDirectory(prefix="pikvm-opencode-") as root_value:
        root = Path(root_value)
        directories = {
            "HOME": root / "home",
            "XDG_CONFIG_HOME": root / "config",
            "XDG_CACHE_HOME": root / "cache",
            "XDG_STATE_HOME": root / "state",
            "XDG_DATA_HOME": root / "data",
            "OPENCODE_CONFIG_DIR": root / "opencode-config",
        }
        for directory in directories.values():
            directory.mkdir(parents=True, exist_ok=True)
        child = {
            name: source[name]
            for name in _OPENCODE_ENV
            if source.get(name)
        }
        child.update(
            {
                name: str(value)
                for name, value in directories.items()
            }
        )
        # OpenCode owns its credential file. Link it into otherwise isolated,
        # writable runtime state without reading or copying the token.
        if source_auth.is_file():
            isolated_auth = directories["XDG_DATA_HOME"] / "opencode" / "auth.json"
            isolated_auth.parent.mkdir(parents=True, exist_ok=True)
            isolated_auth.symlink_to(source_auth)
        child["OPENCODE_CONFIG_CONTENT"] = plan.rendered_config
        child["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        child["OPENCODE_DISABLE_PRUNE"] = "1"
        child["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "1"
        child["OPENCODE_DISABLE_CLAUDE_CODE"] = "1"
        if real_binary is not None:
            # Some user-facing launchers resolve the installed binary through
            # HOME. Capture that client-owned path before HOME becomes the
            # process-private profile rather than weakening profile isolation.
            child["OPENCODE_REAL_BIN"] = str(real_binary)
        child.update(
            {
                name: source[name]
                for name in plan.forwarded_env
            }
        )
        yield child


def _claude_child_environment(
    plan: ManagedClientLaunch,
    *,
    environ: Mapping[str, str] | None,
) -> dict[str, str]:
    """Keep provider-owned login state without forwarding ambient secrets."""

    source = os.environ if environ is None else environ
    for name in plan.forwarded_env:
        if not source.get(name):
            raise ClientIsolationError(
                "Claude managed MCP environment is incomplete"
            )
    child = {
        name: source[name]
        for name in _CLAUDE_ENV
        if source.get(name)
    }
    child.update(
        {
            "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
            "NO_COLOR": "1",
        }
    )
    child.update(
        {
            name: source[name]
            for name in plan.forwarded_env
        }
    )
    return child


@contextmanager
def _claude_child_runtime(
    plan: ManagedClientLaunch,
    *,
    environ: Mapping[str, str] | None,
) -> Iterator[tuple[dict[str, str], Path]]:
    """Run Claude from an empty workspace with only its owned login state."""

    child = _claude_child_environment(plan, environ=environ)
    with tempfile.TemporaryDirectory(prefix="pikvm-claude-client-") as root_value:
        workspace = Path(root_value) / "workspace"
        workspace.mkdir()
        yield child, workspace


def _gemini_admin_policy(server_name: str) -> str:
    return (
        '[[rule]]\n'
        'toolName = "*"\n'
        'decision = "deny"\n'
        "priority = 900\n"
        'deny_message = "Only the managed PiKVM MCP surface is available."\n'
        "\n"
        '[[rule]]\n'
        f"mcpName = {json.dumps(server_name)}\n"
        'decision = "allow"\n'
        "priority = 999\n"
    )


@contextmanager
def _gemini_child_runtime(
    plan: ManagedClientLaunch,
    *,
    environ: Mapping[str, str] | None,
) -> Iterator[tuple[dict[str, str], Path]]:
    """Build a clean workspace around linked Gemini CLI OAuth state.

    ``GEMINI_CLI_HOME`` is a home-directory override, not the ``.gemini``
    directory itself. Treat an existing override as an optional credential
    source, then expose only Gemini's provider-owned login files inside a new
    process-private profile. The client can write installation IDs, histories,
    caches, and settings without mutating the operator's profile.
    """

    source = os.environ if environ is None else environ
    ordinary_home = source.get("HOME") or source.get("USERPROFILE")
    source_profile_value = source.get("GEMINI_CLI_HOME") or ordinary_home
    if not source_profile_value:
        raise ClientIsolationError(
            "Gemini isolation requires HOME, USERPROFILE, or GEMINI_CLI_HOME"
        )
    source_profile_home = Path(source_profile_value).expanduser()
    if not source_profile_home.is_absolute():
        raise ClientIsolationError(
            "Gemini OAuth source home must be absolute"
        )
    if source.get("GEMINI_CLI_HOME") and not source_profile_home.is_dir():
        raise ClientIsolationError(
            "Gemini OAuth source profile is unavailable"
        )
    for name in plan.forwarded_env:
        if not source.get(name):
            raise ClientIsolationError(
                "Gemini managed MCP environment is incomplete"
            )
    with tempfile.TemporaryDirectory(prefix="pikvm-gemini-client-") as root_value:
        root = Path(root_value)
        workspace = root / "workspace"
        control = root / "control"
        profile_home = root / "gemini-profile"
        profile_data = profile_home / ".gemini"
        runtime_directories = {
            "HOME": root / "home",
            "USERPROFILE": root / "home",
            "XDG_CACHE_HOME": root / "cache",
            "XDG_CONFIG_HOME": root / "config",
            "XDG_DATA_HOME": root / "data",
            "XDG_STATE_HOME": root / "state",
            "APPDATA": root / "config",
            "LOCALAPPDATA": root / "data",
        }
        for directory in {
            workspace,
            control,
            profile_data,
            *runtime_directories.values(),
        }:
            directory.mkdir(parents=True, exist_ok=True)
        source_profile_data = source_profile_home / ".gemini"
        for filename in _GEMINI_AUTH_FILES:
            source_auth = source_profile_data / filename
            if source_auth.is_file():
                (profile_data / filename).symlink_to(source_auth.resolve())
        policy = control / "managed-only-policy.toml"
        system_settings = control / "system-settings.json"
        system_defaults = control / "system-defaults.json"
        policy.write_text(
            _gemini_admin_policy(plan.server_name),
            encoding="utf-8",
        )
        settings_document = json.loads(plan.rendered_config)
        settings_document["adminPolicyPaths"] = [str(policy)]
        system_settings.write_text(
            json.dumps(settings_document, sort_keys=True),
            encoding="utf-8",
        )
        system_defaults.write_text("{}", encoding="utf-8")
        child = {
            name: source[name]
            for name in _GEMINI_ENV
            if source.get(name)
        }
        child.update(
            {
                "GEMINI_CLI_HOME": str(profile_home),
                "GEMINI_CLI_SURFACE": "pikvm-harness-client",
                "GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(system_settings),
                "GEMINI_CLI_SYSTEM_DEFAULTS_PATH": str(system_defaults),
                "NO_COLOR": "1",
                **{
                    name: str(directory)
                    for name, directory in runtime_directories.items()
                },
            }
        )
        child.update(
            {
                name: source[name]
                for name in plan.forwarded_env
            }
        )
        yield child, workspace


_GEMINI_SETTINGS_PROBE = """
const moduleUrl = process.argv[1];
const workspace = process.argv[2];
const { loadSettings } = await import(moduleUrl);
const settings = loadSettings(workspace);
process.stdout.write(JSON.stringify({
  mcp: settings.merged.mcp,
  mcpServers: settings.merged.mcpServers,
  security: settings.merged.security,
  skills: settings.merged.skills,
  hooksConfig: settings.merged.hooksConfig,
  context: settings.merged.context,
  adminPolicyPaths: settings.merged.adminPolicyPaths,
  errors: settings.errors,
}));
""".strip()


def _gemini_settings_module(
    executable: str,
    *,
    environ: Mapping[str, str],
) -> Path:
    resolved = shutil.which(executable, path=environ.get("PATH"))
    executable_path = Path(resolved or executable).expanduser()
    try:
        real_executable = executable_path.resolve(strict=True)
    except OSError as exc:
        raise ClientIsolationError(
            "Gemini native settings loader is unavailable"
        ) from exc
    candidates = [
        real_executable.parent / "src" / "config" / "settings.js",
        real_executable.parent / "config" / "settings.js",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ClientIsolationError(
        "Gemini native settings loader is unavailable"
    )


def _read_gemini_effective_settings(
    plan: ManagedClientLaunch,
    *,
    child_environment: Mapping[str, str],
    workspace: Path,
) -> dict[str, object]:
    module_path = _gemini_settings_module(
        plan.argv[0],
        environ=child_environment,
    )
    node = shutil.which("node", path=child_environment.get("PATH"))
    if not node:
        raise ClientIsolationError(
            "Gemini native settings loader is unavailable"
        )
    try:
        completed = subprocess.run(
            [
                node,
                "--input-type=module",
                "--eval",
                _GEMINI_SETTINGS_PROBE,
                module_path.as_uri(),
                str(workspace),
            ],
            cwd=workspace,
            env=dict(child_environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClientIsolationError(
            "Gemini native settings probe failed"
        ) from exc
    if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
        raise ClientIsolationError(
            "Gemini native settings probe failed"
        )
    try:
        document = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientIsolationError(
            "Gemini native settings probe failed"
        ) from exc
    if not isinstance(document, dict):
        raise ClientIsolationError(
            "Gemini native settings probe failed"
        )
    return document


@contextmanager
def _managed_client_runtime(
    plan: ManagedClientLaunch,
    *,
    environ: Mapping[str, str] | None,
) -> Iterator[tuple[dict[str, str], Path]]:
    if plan.client == "codex":
        with _codex_child_environment(plan, environ=environ) as child:
            yield child, plan.project_dir
        return
    if plan.client == "gemini":
        with _gemini_child_runtime(plan, environ=environ) as runtime:
            yield runtime
        return
    if plan.client == "opencode":
        with _opencode_child_environment(plan, environ=environ) as child:
            yield child, plan.project_dir
        return
    with _claude_child_runtime(plan, environ=environ) as runtime:
        yield runtime


def build_managed_client_launch(
    settings: HarnessSettings,
    *,
    client: str,
    client_executable: str,
    mcp_executable: str,
    harness_config: Path,
    project_dir: Path,
    server_name: str = "pikvm",
    managed_runtime: Path | None = None,
) -> ManagedClientLaunch:
    """Build a secret-free launch plan without mutating client state."""

    normalized = client.strip().lower()
    if normalized not in {"codex", "claude", "gemini", "opencode"}:
        raise ClientIsolationError(
            "stable isolated launch currently supports codex, claude, "
            "gemini, and opencode"
        )
    project = project_dir.expanduser().resolve()
    rendered = render_client_config(
        settings,
        client=normalized,  # type: ignore[arg-type]
        executable=mcp_executable,
        harness_config=harness_config,
        control_mode="managed",
        server_name=server_name,
        managed_runtime=managed_runtime,
    )
    launch = parse_client_launch_config(
        rendered,
        client=normalized,  # type: ignore[arg-type]
        server_name=server_name,
    )
    if normalized == "codex":
        overrides = _codex_overrides(
            server_name=server_name,
            command=launch.command,
            args=launch.args,
            forwarded_env=launch.forwarded_env,
        )
        argv = (
            client_executable,
            *overrides,
            "-C",
            str(project),
        )
        return ManagedClientLaunch(
            client="codex",
            server_name=server_name,
            isolation_mode="isolated-auth-link",
            argv=argv,
            project_dir=project,
            rendered_config=rendered,
            inventory_config_overrides=overrides,
            forwarded_env=launch.forwarded_env,
            preserve_unrelated_mcp=False,
        )

    if normalized == "opencode":
        document = json.loads(rendered)
        document["permission"] = {
            "*": "deny",
            f"{server_name}_*": "allow",
        }
        rendered = json.dumps(document, indent=2) + "\n"
        return ManagedClientLaunch(
            client="opencode",
            server_name=server_name,
            isolation_mode="pure-inline-config",
            argv=(client_executable, "--pure"),
            project_dir=project,
            rendered_config=rendered,
            inventory_config_overrides=(),
            forwarded_env=launch.forwarded_env,
            preserve_unrelated_mcp=False,
        )

    if normalized == "gemini":
        if "_" in server_name:
            raise ClientIsolationError(
                "Gemini managed MCP server names must not contain underscores"
            )
        document = json.loads(rendered)
        document.update(
            {
                "mcp": {"allowed": [server_name]},
                "security": {
                    "disableYoloMode": True,
                    "disableAlwaysAllow": True,
                    "enablePermanentToolApproval": False,
                    "auth": {
                        "selectedType": "oauth-personal",
                    },
                },
                "skills": {"enabled": False},
                "context": {
                    "fileName": "PIKVM_HARNESS_CONTEXT_DISABLED.md",
                    "includeDirectoryTree": False,
                    "loadMemoryFromIncludeDirectories": False,
                },
                "hooksConfig": {"enabled": False},
            }
        )
        rendered = json.dumps(document, indent=2) + "\n"
        return ManagedClientLaunch(
            client="gemini",
            server_name=server_name,
            isolation_mode="system-policy-allowlist",
            argv=(
                client_executable,
                "--allowed-mcp-server-names",
                server_name,
                "--extensions",
                "none",
            ),
            project_dir=project,
            rendered_config=rendered,
            inventory_config_overrides=(),
            forwarded_env=launch.forwarded_env,
            preserve_unrelated_mcp=False,
        )

    return ManagedClientLaunch(
        client="claude",
        server_name=server_name,
        isolation_mode="strict-explicit-config",
        argv=(
            client_executable,
            "--mcp-config",
            rendered.strip(),
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--no-chrome",
        ),
        project_dir=project,
        rendered_config=rendered,
        inventory_config_overrides=(),
        forwarded_env=launch.forwarded_env,
        preserve_unrelated_mcp=False,
    )


def audit_managed_client_launch(
    plan: ManagedClientLaunch,
    *,
    environ: dict[str, str] | None = None,
) -> ClientConfigAuditReport:
    """Prove the exact planned PiKVM surface before a client can start."""

    if plan.client == "codex":
        with _codex_child_environment(plan, environ=environ) as child:
            document = read_codex_effective_inventory(
                executable=plan.argv[0],
                project_dir=plan.project_dir,
                environ=child,
                config_overrides=plan.inventory_config_overrides,
            )
    elif plan.client == "claude":
        source_environment = os.environ if environ is None else environ
        child_environment = {
            name: source_environment[name]
            for name in _HELP_ENV
            if source_environment.get(name)
        }
        try:
            completed = subprocess.run(
                [plan.argv[0], "--help"],
                cwd=plan.project_dir,
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ClientIsolationError(
                "Claude strict MCP capability probe failed"
            ) from exc
        if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
            raise ClientIsolationError(
                "Claude strict MCP capability probe failed"
            )
        try:
            help_text = completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ClientIsolationError(
                "Claude strict MCP capability probe failed"
            ) from exc
        if not all(
            flag in help_text
            for flag in ("--mcp-config", "--strict-mcp-config")
        ):
            raise ClientIsolationError(
                "Claude does not expose strict MCP isolation"
            )
        document = ClientConfigDocument(
            source_label="strict-launch",
            rendered=plan.rendered_config,
        )
    elif plan.client == "gemini":
        try:
            with _gemini_child_runtime(
                plan,
                environ=environ,
            ) as (child_environment, workspace):
                completed = subprocess.run(
                    [plan.argv[0], "--help"],
                    cwd=workspace,
                    env=child_environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10.0,
                    check=False,
                )
                effective = _read_gemini_effective_settings(
                    plan,
                    child_environment=child_environment,
                    workspace=workspace,
                )
                policy_paths = effective.get("adminPolicyPaths")
                if (
                    not isinstance(policy_paths, list)
                    or len(policy_paths) != 1
                    or not isinstance(policy_paths[0], str)
                    or Path(policy_paths[0]).read_text(encoding="utf-8")
                    != _gemini_admin_policy(plan.server_name)
                ):
                    raise ClientIsolationError(
                        "Gemini effective admin policy is not managed-only"
                    )
        except ClientIsolationError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise ClientIsolationError(
                "Gemini strict MCP capability probe failed"
            ) from exc
        if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
            raise ClientIsolationError(
                "Gemini strict MCP capability probe failed"
            )
        try:
            help_text = completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ClientIsolationError(
                "Gemini strict MCP capability probe failed"
            ) from exc
        required_flags = (
            "--admin-policy",
            "--allowed-mcp-server-names",
            "--approval-mode",
            "--extensions",
            "--output-format",
            "--prompt",
        )
        if not all(flag in help_text for flag in required_flags):
            raise ClientIsolationError(
                "Gemini does not expose the required MCP policy interface"
            )
        effective_mcp = effective.get("mcp")
        effective_security = effective.get("security")
        effective_skills = effective.get("skills")
        effective_context = effective.get("context")
        if (
            not isinstance(effective_mcp, dict)
            or effective_mcp.get("allowed") != [plan.server_name]
            or effective.get("errors") not in (None, [])
            or not isinstance(effective_security, dict)
            or effective_security.get("disableYoloMode") is not True
            or effective_security.get("disableAlwaysAllow") is not True
            or effective_security.get("enablePermanentToolApproval")
            is not False
            or not isinstance(effective_skills, dict)
            or effective_skills.get("enabled") is not False
            or not isinstance(effective.get("hooksConfig"), dict)
            or effective["hooksConfig"].get("enabled") is not False
            or not isinstance(effective_context, dict)
            or effective_context.get("fileName")
            != "PIKVM_HARNESS_CONTEXT_DISABLED.md"
            or effective_context.get("includeDirectoryTree") is not False
            or effective_context.get(
                "loadMemoryFromIncludeDirectories"
            )
            is not False
        ):
            raise ClientIsolationError(
                "Gemini effective settings do not enforce managed-only MCP"
            )
        document = ClientConfigDocument(
            source_label="native-effective-settings",
            rendered=json.dumps(
                {"mcpServers": effective.get("mcpServers")}
            ),
        )
    else:
        try:
            with _opencode_child_environment(
                plan,
                environ=environ,
            ) as child_environment:
                completed = subprocess.run(
                    [
                        plan.argv[0],
                        "debug",
                        "config",
                        "--pure",
                    ],
                    cwd=plan.project_dir,
                    env=child_environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10.0,
                    check=False,
                )
        except ClientIsolationError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise ClientIsolationError(
                "OpenCode resolved-config probe failed"
            ) from exc
        if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
            raise ClientIsolationError(
                "OpenCode resolved-config probe failed"
            )
        try:
            resolved = completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ClientIsolationError(
                "OpenCode resolved-config probe failed"
            ) from exc
        try:
            resolved_document = json.loads(resolved)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ClientIsolationError(
                "OpenCode resolved-config probe failed"
            ) from exc
        if (
            not isinstance(resolved_document, dict)
            or resolved_document.get("permission")
            != {
                "*": "deny",
                f"{plan.server_name}_*": "allow",
            }
        ):
            raise ClientIsolationError(
                "OpenCode resolved config weakened default-deny permissions"
            )
        document = ClientConfigDocument(
            source_label="resolved-pure-config",
            rendered=resolved,
        )
    report = audit_client_configs(
        client=plan.client,
        documents=[document],
    )
    if report.safe:
        return report
    if "competing_raw_or_direct" in report.failures:
        raise ClientIsolationError(
            "isolated client preflight found a competing PiKVM surface"
        )
    raise ClientIsolationError(
        "isolated client preflight did not resolve exactly one managed "
        "PiKVM surface"
    )


def run_managed_client_launch(
    plan: ManagedClientLaunch,
    *,
    environ: dict[str, str] | None = None,
) -> int:
    """Run an already-audited plan with inherited client authentication."""

    with _managed_client_runtime(
        plan,
        environ=environ,
    ) as (child_environment, cwd):
        completed = subprocess.run(
            list(plan.argv),
            cwd=cwd,
            env=child_environment,
            check=False,
        )
    return completed.returncode


def build_managed_client_task_argv(
    plan: ManagedClientLaunch,
) -> tuple[str, ...]:
    """Return a non-interactive managed task command that reads stdin.

    The private task never enters argv or the launch audit. Both clients retain
    their normal authentication owner, exact managed-MCP isolation, and native
    permission handling. Codex receives a read-only local sandbox; Claude
    refuses permission prompts instead of bypassing them.
    """

    if plan.client == "codex":
        return (
            *plan.argv,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "-",
        )
    if plan.client == "opencode":
        return (
            *plan.argv,
            "run",
            "--format",
            "json",
        )
    if plan.client == "gemini":
        return (
            *plan.argv,
            "--prompt",
            "",
            "--output-format",
            "stream-json",
            "--approval-mode",
            "default",
        )
    return (
        *plan.argv,
        "--allowedTools",
        ",".join(_CLAUDE_ALLOWED_MANAGED_TOOLS),
        "--verbose",
        "--print",
        "--output-format",
        "stream-json",
        "--no-session-persistence",
        "--permission-mode",
        "dontAsk",
    )


def run_managed_client_task(
    plan: ManagedClientLaunch,
    *,
    task: str,
    timeout_s: float,
    environ: dict[str, str] | None = None,
    completion_watch: ManagedTaskCompletionWatch | None = None,
) -> ManagedClientTaskResult:
    """Run one non-interactive task without placing its text in argv."""

    task_bytes = task.encode("utf-8")
    if not task.strip():
        raise ValueError("managed client task must not be empty")
    if b"\0" in task_bytes:
        raise ValueError("managed client task must not contain NUL")
    if len(task_bytes) > 32 * 1024:
        raise ValueError("managed client task exceeds 32 KiB")
    if not 1 <= timeout_s <= 86_400:
        raise ValueError("managed client task timeout must be 1..86400 seconds")
    started = time.monotonic()
    baseline = (
        completion_watch.capture_baseline()
        if completion_watch is not None
        else None
    )
    try:
        with _managed_client_runtime(
            plan,
            environ=environ,
        ) as (child_environment, cwd):
            with (
                tempfile.TemporaryFile() as client_stdout,
                tempfile.TemporaryFile() as client_stderr,
            ):
                completed = subprocess.run(
                    list(build_managed_client_task_argv(plan)),
                    cwd=cwd,
                    env=child_environment,
                    input=task_bytes,
                    stdout=client_stdout,
                    stderr=client_stderr,
                    timeout=timeout_s,
                    check=False,
                )
    except subprocess.TimeoutExpired:
        raise ClientIsolationError(
            "managed client task exceeded its runtime limit"
        ) from None
    except OSError as exc:
        raise ClientIsolationError("managed client task could not start") from exc
    completion = None
    if completed.returncode == 0 and completion_watch is not None:
        remaining_s = timeout_s - (time.monotonic() - started)
        if remaining_s <= 0:
            raise ClientIsolationError(
                "managed client task exceeded its runtime limit"
            )
        assert baseline is not None
        completion = completion_watch.wait_for_completion(
            baseline,
            timeout_s=remaining_s,
        )
    elapsed_ms = round((time.monotonic() - started) * 1000)
    return ManagedClientTaskResult(
        client=plan.client,
        exit_code=completed.returncode,
        elapsed_ms=max(0, elapsed_ms),
        task_bytes=len(task_bytes),
        task_sha256=hashlib.sha256(task_bytes).hexdigest(),
        completion=completion,
    )
