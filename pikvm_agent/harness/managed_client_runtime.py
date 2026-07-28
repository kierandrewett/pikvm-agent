"""Owner-only runtime handoff for persistent managed MCP clients."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pikvm_agent.harness.config import (
    HarnessSettings,
    load_harness_settings,
)

_MAX_RUNTIME_BYTES = 16 * 1024


@dataclass(frozen=True)
class LoadedManagedClientRuntime:
    """Validated harness settings plus the agent-scoped child environment."""

    harness_config: Path
    settings: HarnessSettings
    environment: dict[str, str]


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
