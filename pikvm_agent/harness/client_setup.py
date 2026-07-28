"""Secret-free client configuration for managed and guarded-direct MCP paths."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from pikvm_agent.daemon_access import (
    DAEMON_TOKEN_ENV,
    action_token_from_environment,
)
from pikvm_agent.harness.config import HarnessSettings

ClientKind = Literal["codex", "claude", "gemini", "opencode"]
ControlMode = Literal["managed", "direct"]
_CALLER_LABEL = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True)
class ClientLaunchSpec:
    command: str
    args: tuple[str, ...]
    forwarded_env: tuple[str, ...]


def normalize_caller_label(value: str) -> str:
    """Validate the non-secret client identity projected into the run UI."""

    label = value.strip()
    if not _CALLER_LABEL.fullmatch(label):
        raise ValueError(
            "caller_label must contain only letters, digits, _ or -"
        )
    return label


def valid_active_managed_mcp_arguments(
    arguments: Sequence[str],
) -> bool:
    """Accept only the bounded options exposed by active-managed-mcp."""

    remaining = list(arguments)
    caller_seen = False
    ready_seen = False
    while remaining:
        option = remaining.pop(0)
        if option == "--caller-label" and not caller_seen and remaining:
            value = remaining.pop(0)
            if not _CALLER_LABEL.fullmatch(value):
                return False
            caller_seen = True
            continue
        if option == "--require-ready" and not ready_seen:
            ready_seen = True
            continue
        return False
    return True


def harness_base_url(settings: HarnessSettings) -> str:
    host, port = settings.host_port()
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    rendered_host = (
        f"[{connect_host}]"
        if ":" in connect_host and not connect_host.startswith("[")
        else connect_host
    )
    return f"http://{rendered_host}:{port}"


def direct_mcp_environment(
    settings: HarnessSettings,
    *,
    mode: Literal["guarded", "observe"],
    caller_label: str,
) -> dict[str, str]:
    """Resolve runtime-only bridge values without writing them to config."""
    caller_label = normalize_caller_label(caller_label)
    return {
        "PIKVM_AGENT_DAEMON": settings.daemon_url(),
        DAEMON_TOKEN_ENV: action_token_from_environment(),
        "PIKVM_HARNESS_OBSERVER_URL": harness_base_url(settings),
        "PIKVM_HARNESS_OBSERVER_TOKEN": settings.observer_token(
            validate_distinct=False
        ),
        "PIKVM_HARNESS_OBSERVER_MODE": mode,
        "PIKVM_MCP_CALLER_LABEL": caller_label,
    }


def managed_mcp_environment(
    settings: HarnessSettings,
    *,
    caller_label: str = "mcp-client",
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve the scoped managed-harness connection at process start."""

    return {
        "PIKVM_HARNESS_URL": harness_base_url(settings),
        "PIKVM_HARNESS_AGENT_TOKEN": settings.agent_token(
            validate_distinct=False,
            environ=environ,
        ),
        "PIKVM_MCP_CALLER_LABEL": normalize_caller_label(caller_label),
    }


def _verify_scope(
    *,
    base_url: str,
    token: str,
    path: str,
    missing_detail: str,
    timeout_s: float,
    transport: httpx.BaseTransport | None,
) -> None:
    with httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout_s,
        transport=transport,
    ) as client:
        health = client.get(path)
        if health.status_code == 503:
            raise RuntimeError(missing_detail)
        health.raise_for_status()


def verify_direct_harness_ready(
    settings: HarnessSettings,
    *,
    timeout_s: float = 3.0,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Fail before MCP startup unless the selected guarded harness is ready."""
    _verify_scope(
        base_url=harness_base_url(settings),
        token=settings.observer_token(validate_distinct=False),
        path="/api/direct/health",
        missing_detail=(
            "operator harness is running without direct-call visibility"
        ),
        timeout_s=timeout_s,
        transport=transport,
    )


def verify_managed_harness_ready(
    settings: HarnessSettings,
    *,
    timeout_s: float = 3.0,
    transport: httpx.BaseTransport | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Fail before MCP startup unless scoped managed control is reachable."""

    _verify_scope(
        base_url=harness_base_url(settings),
        token=settings.agent_token(
            validate_distinct=False,
            environ=environ,
        ),
        path="/api/agent/health",
        missing_detail="operator harness has no managed-agent control surface",
        timeout_s=timeout_s,
        transport=transport,
    )


def render_client_config(
    settings: HarnessSettings,
    *,
    client: ClientKind,
    executable: str,
    harness_config: Path,
    control_mode: ControlMode = "managed",
    server_name: str = "pikvm",
    managed_runtime: Path | None = None,
    active_runtime: bool = False,
) -> str:
    """Render a client config that forwards secret names, never secret values."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", server_name):
        raise ValueError("server_name must contain only letters, digits, _ or -")
    if control_mode not in {"managed", "direct"}:
        raise ValueError("control_mode must be managed or direct")
    if managed_runtime is not None and control_mode != "managed":
        raise ValueError("managed runtime is unavailable in direct mode")
    if active_runtime and control_mode != "managed":
        raise ValueError("active runtime is unavailable in direct mode")
    if active_runtime and managed_runtime is not None:
        raise ValueError(
            "active runtime and explicit managed runtime are mutually exclusive"
        )
    if active_runtime:
        args = [
            "-m",
            "pikvm_agent.cli",
            "harness",
            "active-managed-mcp",
            "--caller-label",
            f"{client}-cli",
        ]
    elif managed_runtime is None:
        command_name = (
            "managed-mcp" if control_mode == "managed" else "direct-mcp"
        )
        args = [
            "-m",
            "pikvm_agent.cli",
            "harness",
            command_name,
            "--config",
            str(harness_config.expanduser().resolve()),
            "--caller-label",
            f"{client}-cli",
        ]
    else:
        args = [
            "-m",
            "pikvm_agent.cli",
            "harness",
            "managed-runtime-mcp",
            "--runtime",
            str(managed_runtime.expanduser().absolute()),
            "--caller-label",
            f"{client}-cli",
        ]
    if control_mode == "direct":
        forwarded = [
            settings.daemon_url_env,
            DAEMON_TOKEN_ENV,
            settings.observer_token_env,
            "PIKVM_MCP_PROVIDER",
            "PIKVM_MCP_MODEL",
        ]
        required = {
            settings.daemon_url_env: f"${{{settings.daemon_url_env}}}",
            DAEMON_TOKEN_ENV: f"${{{DAEMON_TOKEN_ENV}}}",
            settings.observer_token_env: (
                f"${{{settings.observer_token_env}}}"
            ),
            "PIKVM_MCP_PROVIDER": "${PIKVM_MCP_PROVIDER:-unreported}",
            "PIKVM_MCP_MODEL": "${PIKVM_MCP_MODEL:-unreported}",
        }
    elif managed_runtime is None and not active_runtime:
        forwarded = [settings.agent_token_env]
        required = {
            settings.agent_token_env: f"${{{settings.agent_token_env}}}",
        }
    else:
        forwarded = []
        required = {}
    if client == "codex":
        return "\n".join(
            [
                f"[mcp_servers.{server_name}]",
                f"command = {json.dumps(executable)}",
                f"args = {json.dumps(args)}",
                f"env_vars = {json.dumps(forwarded)}",
                "",
            ]
        )

    if client == "opencode":
        return (
            json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "mcp": {
                        server_name: {
                            "type": "local",
                            "command": [executable, *args],
                            "enabled": True,
                            "environment": {
                                name: f"{{env:{name}}}"
                                for name in forwarded
                            },
                        }
                    },
                },
                indent=2,
            )
            + "\n"
        )

    return (
        json.dumps(
            {
                "mcpServers": {
                    server_name: {
                        "type": "stdio",
                        "command": executable,
                        "args": args,
                        "env": required,
                    }
                }
            },
            indent=2,
        )
        + "\n"
    )


def parse_client_launch_config(
    rendered: str,
    *,
    client: ClientKind,
    server_name: str = "pikvm",
) -> ClientLaunchSpec:
    """Read back the exact command represented by a generated client config."""

    try:
        if client == "codex":
            server = tomllib.loads(rendered)["mcp_servers"][server_name]
            command = server["command"]
            args = server["args"]
            environment = server["env_vars"]
        else:
            document = json.loads(rendered)
            server = (
                document["mcp"][server_name]
                if client == "opencode"
                else document["mcpServers"][server_name]
            )
            if client == "opencode":
                command, *args = server["command"]
                environment = server["environment"]
            else:
                command = server["command"]
                args = server["args"]
                environment = server["env"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"generated {client} client config has no usable launch command"
        ) from exc
    if (
        not isinstance(command, str)
        or not command
        or not isinstance(args, list)
        or not all(isinstance(arg, str) for arg in args)
    ):
        raise ValueError(
            f"generated {client} client config has no usable launch command"
        )
    command_name = command.replace("\\", "/").rsplit("/", 1)[-1].lower()
    python_command = bool(
        re.fullmatch(
            r"python(?:\d+(?:\.\d+)?)?(?:\.exe)?",
            command_name,
        )
    )
    console_command = command_name in {"pikvm-agent", "pikvm-agent.exe"}
    runtime_args: list[str] | None = None
    active_runtime_args: list[str] | None = None
    if python_command and tuple(args[:4]) == (
        "-m",
        "pikvm_agent.cli",
        "harness",
        "managed-runtime-mcp",
    ):
        runtime_args = args[4:]
    elif python_command and tuple(args[:4]) == (
        "-m",
        "pikvm_agent.cli",
        "harness",
        "active-managed-mcp",
    ):
        active_runtime_args = args[4:]
    elif console_command and tuple(args[:2]) == (
        "harness",
        "managed-runtime-mcp",
    ):
        runtime_args = args[2:]
    elif console_command and tuple(args[:2]) == (
        "harness",
        "active-managed-mcp",
    ):
        active_runtime_args = args[2:]
    runtime_indexes = (
        [
            index
            for index, value in enumerate(runtime_args)
            if value == "--runtime"
        ]
        if runtime_args is not None
        else []
    )
    runtime_backed = (
        runtime_args is not None
        and len(runtime_indexes) == 1
        and runtime_indexes[0] + 1 < len(runtime_args)
        and bool(runtime_args[runtime_indexes[0] + 1])
        and not runtime_args[runtime_indexes[0] + 1].startswith("-")
    )
    active_runtime_backed = (
        active_runtime_args is not None
        and valid_active_managed_mcp_arguments(active_runtime_args)
    )
    if client == "codex":
        if (
            not isinstance(environment, list)
            or not all(isinstance(name, str) and name for name in environment)
            or len(environment) != len(set(environment))
            or (
                not environment
                and not runtime_backed
                and not active_runtime_backed
            )
        ):
            raise ValueError(
                f"generated {client} client config has no usable environment"
            )
        forwarded_env = tuple(environment)
    else:
        if (
            not isinstance(environment, dict)
            or not all(
                isinstance(name, str)
                and name
                and isinstance(reference, str)
                and (
                    reference == f"{{env:{name}}}"
                    if client == "opencode"
                    else reference
                    in {f"${{{name}}}", f"${{{name}:-unreported}}"}
                )
                for name, reference in environment.items()
            )
            or (
                not environment
                and not runtime_backed
                and not active_runtime_backed
            )
        ):
            raise ValueError(
                f"generated {client} client config has no usable environment"
            )
        forwarded_env = tuple(environment)
    return ClientLaunchSpec(
        command=command,
        args=tuple(args),
        forwarded_env=forwarded_env,
    )
