"""Owner-only runtime handoff for persistent managed MCP clients."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pikvm_agent.harness.config import (
    HarnessSettings,
    load_harness_settings,
)

_MAX_RUNTIME_BYTES = 16 * 1024
ACTIVE_MANAGED_RUNTIME_ENV = "PIKVM_MANAGED_CLIENT_RUNTIME"
_ACTIVE_RUNTIME_DIRECTORY = "pikvm-agent"
_ACTIVE_RUNTIME_NAME = "managed-client-runtime.json"


@dataclass(frozen=True)
class LoadedManagedClientRuntime:
    """Validated harness settings plus the agent-scoped child environment."""

    harness_config: Path
    settings: HarnessSettings
    environment: dict[str, str]


def active_managed_client_runtime_path(
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the stable per-user handoff consumed by ordinary MCP clients."""

    source = os.environ if environ is None else environ
    configured = source.get(ACTIVE_MANAGED_RUNTIME_ENV, "").strip()
    if configured:
        selected = Path(configured).expanduser()
        if not selected.is_absolute():
            raise ValueError(
                f"{ACTIVE_MANAGED_RUNTIME_ENV} must be an absolute path"
            )
        return selected

    runtime_directory = source.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_directory:
        selected_runtime = Path(runtime_directory).expanduser()
        if not selected_runtime.is_absolute():
            raise ValueError("XDG_RUNTIME_DIR must be an absolute path")
        return (
            selected_runtime
            / _ACTIVE_RUNTIME_DIRECTORY
            / _ACTIVE_RUNTIME_NAME
        )

    if os.name == "nt":
        local_app_data = source.get("LOCALAPPDATA", "").strip()
        if not local_app_data:
            raise ValueError(
                "LOCALAPPDATA is required for the managed client runtime"
            )
        selected_app_data = Path(local_app_data).expanduser()
        if not selected_app_data.is_absolute():
            raise ValueError("LOCALAPPDATA must be an absolute path")
        return (
            selected_app_data
            / "PiKVM Agent"
            / _ACTIVE_RUNTIME_NAME
        )

    uid = os.geteuid() if hasattr(os, "geteuid") else os.getpid()
    return (
        Path(tempfile.gettempdir())
        / f"{_ACTIVE_RUNTIME_DIRECTORY}-{uid}"
        / _ACTIVE_RUNTIME_NAME
    )


def _read_owner_only_json(path: Path) -> dict[str, object]:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ValueError("managed client runtime must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(expanded, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("managed client runtime must be a regular file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("managed client runtime must be owner-only")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise ValueError("managed client runtime must belong to this user")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            encoded = handle.read(_MAX_RUNTIME_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(encoded) > _MAX_RUNTIME_BYTES:
        raise ValueError("managed client runtime exceeds 16 KiB")
    try:
        rendered = encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "managed client runtime is not valid UTF-8"
        ) from exc
    try:
        payload = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "managed client runtime is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("managed client runtime has an unsupported shape")
    return payload


def _ensure_private_runtime_parent(parent: Path) -> None:
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink():
        raise ValueError("managed client runtime parent must not be a symlink")
    metadata = parent.stat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("managed client runtime parent must be a directory")
    if os.name != "nt":
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError(
                "managed client runtime parent must be owner-only"
            )
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise ValueError(
                "managed client runtime parent must belong to this user"
            )


def publish_active_managed_client_runtime(
    source_runtime: Path,
    *,
    destination: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Atomically publish a reduced agent-only handoff at the stable path."""

    loaded = load_managed_client_runtime(
        source_runtime,
        environ=environ,
    )
    selected = (
        active_managed_client_runtime_path(environ=environ)
        if destination is None
        else destination.expanduser()
    )
    if not selected.is_absolute():
        raise ValueError("managed client runtime destination must be absolute")
    parent = selected.parent
    _ensure_private_runtime_parent(parent)
    agent_token = loaded.environment[loaded.settings.agent_token_env]
    encoded = (
        json.dumps(
            {
                "schema_version": 1,
                "harness_config": str(loaded.harness_config),
                "agent_token_env": loaded.settings.agent_token_env,
                "agent_token": agent_token,
            },
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > _MAX_RUNTIME_BYTES:
        raise ValueError("managed client runtime exceeds 16 KiB")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{selected.name}.",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, selected)
        if os.name != "nt":
            selected.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    load_managed_client_runtime(
        selected,
        expected_harness_config=loaded.harness_config,
        environ=environ,
    )
    return selected


def load_managed_client_runtime(
    path: Path,
    *,
    expected_harness_config: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> LoadedManagedClientRuntime:
    """Load only the agent capability from an owner-only runtime handoff."""

    payload = _read_owner_only_json(path)
    if set(payload) != {
        "schema_version",
        "harness_config",
        "agent_token_env",
        "agent_token",
    }:
        raise ValueError("managed client runtime has an unsupported shape")
    if payload["schema_version"] != 1:
        raise ValueError("managed client runtime schema is unsupported")
    raw_config = payload["harness_config"]
    if not isinstance(raw_config, str) or not Path(raw_config).is_absolute():
        raise ValueError(
            "managed client runtime harness config must be absolute"
        )
    harness_config = Path(raw_config).resolve()
    if (
        expected_harness_config is not None
        and harness_config
        != expected_harness_config.expanduser().resolve()
    ):
        raise ValueError(
            "managed client runtime belongs to another harness config"
        )
    settings = load_harness_settings(harness_config)
    if payload["agent_token_env"] != settings.agent_token_env:
        raise ValueError(
            "managed client runtime agent scope does not match config"
        )
    token = payload["agent_token"]
    if not isinstance(token, str):
        raise ValueError("managed client runtime agent token is invalid")
    environment = dict(os.environ if environ is None else environ)
    environment[settings.agent_token_env] = token
    settings.agent_token(
        validate_distinct=False,
        environ=environment,
    )
    return LoadedManagedClientRuntime(
        harness_config=harness_config,
        settings=settings,
        environment=environment,
    )
