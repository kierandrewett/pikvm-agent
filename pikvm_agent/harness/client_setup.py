"""Secret-free client configuration for managed and guarded-direct MCP paths."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
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
) -> dict[str, str]:
    """Resolve the scoped managed-harness connection at process start."""

    return {
        "PIKVM_HARNESS_URL": harness_base_url(settings),
        "PIKVM_HARNESS_AGENT_TOKEN": settings.agent_token(
            validate_distinct=False
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
) -> str:
    """Render a client config that forwards secret names, never secret values."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", server_name):
        raise ValueError("server_name must contain only letters, digits, _ or -")
    if control_mode not in {"managed", "direct"}:
        raise ValueError("control_mode must be managed or direct")
    command_name = "managed-mcp" if control_mode == "managed" else "direct-mcp"
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
    else:
        forwarded = [settings.agent_token_env]
        required = {
            settings.agent_token_env: f"${{{settings.agent_token_env}}}",
        }
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
    if client == "codex":
        if (
            not isinstance(environment, list)
            or not environment
            or not all(isinstance(name, str) and name for name in environment)
            or len(environment) != len(set(environment))
        ):
            raise ValueError(
                f"generated {client} client config has no usable environment"
            )
        forwarded_env = tuple(environment)
    else:
        if (
            not isinstance(environment, dict)
            or not environment
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
