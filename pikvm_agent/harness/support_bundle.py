"""Privacy-preserving offline support bundle for the local operator harness."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pikvm_agent import __version__
from pikvm_agent.harness.config import (
    HARNESS_ACCESS_TOKEN_MIN_LENGTH,
    HarnessSettings,
    check_provider_prerequisites,
)

SUPPORT_BUNDLE_SCHEMA_VERSION = 1
SUPPORT_BUNDLE_MAX_FILES = 10_000


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _directory_inventory(path: Path, *, max_files: int) -> dict[str, object]:
    result: dict[str, object] = {
        "exists": path.is_dir(),
        "files": 0,
        "bytes": 0,
        "truncated": False,
        "scan_limit": max_files,
    }
    if not path.is_dir():
        return result
    files = 0
    total_bytes = 0
    for root, directories, names in os.walk(path, followlinks=False):
        directories[:] = [
            name
            for name in directories
            if not (Path(root) / name).is_symlink()
        ]
        for name in names:
            candidate = Path(root) / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if files >= max_files:
                result["truncated"] = True
                result["files"] = files
                result["bytes"] = total_bytes
                return result
            try:
                total_bytes += candidate.stat().st_size
            except OSError:
                continue
            files += 1
    result["files"] = files
    result["bytes"] = total_bytes
    return result


def _file_inventory(path: Path) -> dict[str, object]:
    result: dict[str, object] = {
        "exists": path.is_file(),
        "bytes": 0,
        "parent_writable": os.access(path.parent, os.W_OK),
    }
    if path.is_file():
        try:
            result["bytes"] = path.stat().st_size
        except OSError:
            pass
    return result


def _token_health(settings: HarnessSettings) -> dict[str, object]:
    values = {
        "operator": os.environ.get(settings.access_token_env, ""),
        "agent": os.environ.get(settings.agent_token_env, ""),
        "observer": os.environ.get(settings.observer_token_env, ""),
    }
    adequate = {
        name: len(value) >= HARNESS_ACCESS_TOKEN_MIN_LENGTH
        for name, value in values.items()
    }
    present_values = [value for value in values.values() if value]
    return {
        "present": {name: bool(value) for name, value in values.items()},
        "minimum_length_met": adequate,
        "all_distinct": (
            len(present_values) == len(values)
            and all(
                not secrets.compare_digest(left, right)
                for index, left in enumerate(present_values)
                for right in present_values[index + 1 :]
            )
        ),
    }


def _target_health(settings: HarnessSettings) -> dict[str, object]:
    selected = bool(os.environ.get(settings.daemon_url_env))
    valid = False
    if selected:
        try:
            settings.daemon_url()
            valid = True
        except (ValueError, SystemExit):
            valid = False
    return {
        "selected": selected,
        "valid_url": valid,
        "endpoint_included": False,
    }


def _provider_health(settings: HarnessSettings) -> tuple[
    list[dict[str, object]], dict[str, list[str]]
]:
    prerequisites = check_provider_prerequisites(settings)
    aliases = {
        name: f"provider-{index}"
        for index, name in enumerate(sorted(settings.providers), start=1)
    }
    providers = []
    for name in sorted(settings.providers):
        spec = settings.providers[name]
        status = prerequisites[name]
        providers.append(
            {
                "id": aliases[name],
                "kind": spec.kind,
                "ready": bool(status["ready"]),
                "credential": status.get("credential", "unknown"),
                "readiness_error": status.get("error"),
                "interface": status.get("interface", "Unknown interface"),
                "pixel_input": status.get("pixel_input", "Unknown pixel input"),
                "structured_output": status.get(
                    "structured_output", "Unknown output contract"
                ),
                "billing_mode": (
                    spec.billing.mode
                    if spec.billing is not None
                    else "unclassified"
                ),
                "model_name_included": False,
                "endpoint_included": False,
            }
        )
    routes = {
        role: [
            aliases[name]
            for name in (
                getattr(settings.routes, role)
                or settings.routes.reasoner
            )
        ]
        for role in ("assistant", "reasoner", "controller", "verifier")
    }
    return providers, routes


def _static_asset_health() -> dict[str, object]:
    root = Path(__file__).resolve().parents[1] / "harness_ui"
    files = sorted(
        path
        for path in root.iterdir()
        if path.is_file() and path.suffix in {".css", ".html", ".js"}
    )
    digest = hashlib.sha256()
    total_bytes = 0
    for path in files:
        data = path.read_bytes()
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(data)
        total_bytes += len(data)
    return {
        "files": len(files),
        "bytes": total_bytes,
        "sha256": digest.hexdigest(),
    }


def build_support_bundle(
    settings: HarnessSettings,
    *,
    config_bytes: bytes,
    generated_at: datetime | None = None,
    artifact_scan_limit: int = SUPPORT_BUNDLE_MAX_FILES,
) -> dict[str, Any]:
    """Build an offline diagnostic envelope without arbitrary user strings."""

    providers, routes = _provider_health(settings)
    timestamp = (generated_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    payload: dict[str, Any] = {
        "generated_at": timestamp,
        "product": {
            "name": "pikvm-agent",
            "version": __version__,
            "support_bundle_schema": SUPPORT_BUNDLE_SCHEMA_VERSION,
        },
        "runtime": {
            "python": platform.python_version(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "configuration": {
            "sha256": _sha256_bytes(config_bytes),
            "remote_bind_enabled": settings.allow_remote_bind,
            "allowed_origin_count": len(settings.resolved_origins()),
            "provider_count": len(settings.providers),
            "autonomous_resume_limit": settings.max_autonomous_resumes,
            "model_budget": {
                "provider_attempt_limit": (
                    settings.model_budget.max_provider_attempts_per_run
                ),
                "cost_cap_enabled": (
                    settings.model_budget.max_cost_usd_per_run is not None
                ),
                "pricing_version_included": False,
                "price_values_included": False,
            },
            "endpoint_included": False,
            "secret_values_included": False,
            "provider_names_included": False,
            "model_names_included": False,
        },
        "credentials": _token_health(settings),
        "target": _target_health(settings),
        "providers": providers,
        "routes": routes,
        "storage": {
            "state": _file_inventory(settings.state_path),
            "artifacts": _directory_inventory(
                settings.artifact_dir,
                max_files=artifact_scan_limit,
            ),
            "paths_included": False,
            "artifact_names_included": False,
        },
        "operator_ui": _static_asset_health(),
        "privacy": {
            "offline_only": True,
            "network_requests": 0,
            "run_tasks_included": False,
            "run_events_included": False,
            "screenshots_included": False,
            "provider_output_included": False,
            "machine_endpoint_included": False,
            "credential_values_included": False,
        },
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "schema_version": SUPPORT_BUNDLE_SCHEMA_VERSION,
        "payload_sha256": _sha256_bytes(canonical),
        "payload": payload,
    }


def write_support_bundle(path: Path, bundle: dict[str, Any]) -> None:
    """Create a mode-0600 JSON file and refuse to overwrite existing data."""

    if not path.parent.is_dir():
        raise ValueError("support-bundle parent directory does not exist")
    data = (json.dumps(bundle, indent=2, sort_keys=True) + "\n").encode()
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ValueError("support-bundle output already exists") from exc
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
