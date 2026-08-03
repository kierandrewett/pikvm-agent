"""Reviewed, atomic installation of persistent managed-client registrations.

The active desktop runtime already gives clients a path-free managed MCP
entrypoint.  This module owns the remaining mutation boundary for clients that
store MCP registrations in a JSON settings document.  Installation is a
two-step plan/apply transaction and rollback is compare-and-swap: neither path
can silently overwrite concurrent user changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pikvm_agent.harness.client_config_audit import (
    ClientConfigDocument,
    audit_client_configs,
)
from pikvm_agent.harness.client_setup import (
    render_active_managed_client_config,
)

InstallableClient = Literal["gemini"]
_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_MAX_RECEIPT_BYTES = 64 * 1024
_REVIEW_SCHEMA_VERSION = 1
_RECEIPT_SCHEMA_VERSION = 1


class ManagedClientInstallError(RuntimeError):
    """A managed registration could not be installed without data loss."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _regular_owned_file(path: Path, *, max_bytes: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ManagedClientInstallError(
            "client settings file does not exist"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ManagedClientInstallError(
            "client settings must be a regular non-symlink file"
        )
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ManagedClientInstallError(
            "client settings are owned by another user"
        )
    if metadata.st_size > max_bytes:
        raise ManagedClientInstallError("client settings file is too large")
    return metadata


def _read_owned_file(path: Path, *, max_bytes: int) -> tuple[bytes, os.stat_result]:
    metadata = _regular_owned_file(path, max_bytes=max_bytes)
    payload = path.read_bytes()
    if len(payload) > max_bytes:
        raise ManagedClientInstallError("client settings file is too large")
    return payload, metadata


def _load_object(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedClientInstallError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ManagedClientInstallError(f"{label} must contain a JSON object")
    return value


def _active_server(
    *,
    client: InstallableClient,
    executable: str,
    server_name: str,
) -> dict[str, Any]:
    rendered = render_active_managed_client_config(
        client=client,
        executable=executable,
        server_name=server_name,
    )
    parsed = _load_object(rendered.encode("utf-8"), label="generated settings")
    server = parsed.get("mcpServers", {}).get(server_name)
    if not isinstance(server, dict):  # pragma: no cover - generator invariant
        raise ManagedClientInstallError(
            "generated managed registration is invalid"
        )
    return server


def _review_digest(
    *,
    client: InstallableClient,
    server_name: str,
    before_sha256: str,
    after_sha256: str,
    server: dict[str, Any],
) -> str:
    review = json.dumps(
        {
            "schema_version": _REVIEW_SCHEMA_VERSION,
            "client": client,
            "server_name": server_name,
            "before_sha256": before_sha256,
            "after_sha256": after_sha256,
            "server": server,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(review)


@dataclass(frozen=True)
class ManagedClientInstallPlan:
    """Exact candidate plus the small, secret-free review surface."""

    client: InstallableClient
    config_path: Path
    server_name: str
    server: dict[str, Any]
    before_sha256: str
    after_sha256: str
    review_sha256: str
    changed: bool
    original: bytes
    candidate: bytes
    original_mode: int

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": _REVIEW_SCHEMA_VERSION,
            "client": self.client,
            "config_path": str(self.config_path),
            "server_name": self.server_name,
            "change": "install" if self.changed else "already_installed",
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "review_sha256": self.review_sha256,
            "registration": self.server,
        }


def plan_active_managed_install(
    *,
    client: InstallableClient,
    config_path: Path,
    executable: str,
    server_name: str = "pikvm",
) -> ManagedClientInstallPlan:
    """Plan one additive managed registration without mutating settings."""

    if client != "gemini":
        raise ManagedClientInstallError(
            "reviewed installation currently supports Gemini settings"
        )
    if not executable.strip():
        raise ManagedClientInstallError("managed launcher executable is required")
    path = config_path.expanduser().absolute()
    original, metadata = _read_owned_file(path, max_bytes=_MAX_CONFIG_BYTES)
    document = _load_object(original, label="client settings")
    raw_servers = document.get("mcpServers")
    if raw_servers is None:
        servers: dict[str, Any] = {}
        document["mcpServers"] = servers
    elif isinstance(raw_servers, dict):
        servers = raw_servers
    else:
        raise ManagedClientInstallError("mcpServers must contain a JSON object")

    server = _active_server(
        client=client,
        executable=executable,
        server_name=server_name,
    )
    existing = servers.get(server_name)
    if server_name in servers and existing != server:
        raise ManagedClientInstallError(
            "the selected MCP server name already has a different registration"
        )
    servers[server_name] = server
    candidate = (json.dumps(document, indent=2) + "\n").encode("utf-8")

    report = audit_client_configs(
        client=client,
        documents=[
            ClientConfigDocument(
                source_label="candidate",
                rendered=candidate.decode("utf-8"),
            )
        ],
    )
    if not report.safe or report.managed_count != 1:
        raise ManagedClientInstallError(
            "candidate settings do not resolve to exactly one managed PiKVM registration"
        )

    before_sha256 = _sha256(original)
    after_sha256 = _sha256(candidate)
    return ManagedClientInstallPlan(
        client=client,
        config_path=path,
        server_name=server_name,
        server=server,
        before_sha256=before_sha256,
        after_sha256=after_sha256,
        review_sha256=_review_digest(
            client=client,
            server_name=server_name,
            before_sha256=before_sha256,
            after_sha256=after_sha256,
            server=server,
        ),
        changed=original != candidate,
        original=original,
        candidate=candidate,
        original_mode=stat.S_IMODE(metadata.st_mode),
    )


def _exclusive_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _atomic_replace(path: Path, payload: bytes, *, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.pikvm-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode & 0o777)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True)
class ManagedClientInstallReceipt:
    client: InstallableClient
    config_path: Path
    backup_path: Path
    receipt_path: Path
    before_sha256: str
    after_sha256: str
    review_sha256: str
    changed: bool

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": _RECEIPT_SCHEMA_VERSION,
            "client": self.client,
            "config_path": str(self.config_path),
            "backup_path": str(self.backup_path),
            "receipt_path": str(self.receipt_path),
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "review_sha256": self.review_sha256,
            "changed": self.changed,
        }


def install_active_managed_registration(
    *,
    plan: ManagedClientInstallPlan,
    reviewed_sha256: str,
) -> ManagedClientInstallReceipt:
    """Apply an unchanged reviewed plan and retain exact rollback material."""

    if reviewed_sha256 != plan.review_sha256:
        raise ManagedClientInstallError("review digest does not match this plan")
    current, _ = _read_owned_file(
        plan.config_path,
        max_bytes=_MAX_CONFIG_BYTES,
    )
    if _sha256(current) != plan.before_sha256:
        raise ManagedClientInstallError(
            "client settings changed after review; create a new plan"
        )

    suffix = f"{time.time_ns()}-{plan.after_sha256[:12]}"
    backup_path = plan.config_path.with_name(
        f".{plan.config_path.name}.pikvm-backup-{suffix}"
    )
    receipt_path = plan.config_path.with_name(
        f".{plan.config_path.name}.pikvm-receipt-{suffix}.json"
    )
    _exclusive_write(backup_path, plan.original)
    receipt_payload = {
        "schema_version": _RECEIPT_SCHEMA_VERSION,
        "client": plan.client,
        "config_path": str(plan.config_path),
        "backup_path": str(backup_path),
        "before_sha256": plan.before_sha256,
        "after_sha256": plan.after_sha256,
        "review_sha256": plan.review_sha256,
        "config_mode": plan.original_mode,
    }
    _exclusive_write(
        receipt_path,
        (json.dumps(receipt_payload, indent=2) + "\n").encode("utf-8"),
    )
    try:
        if plan.changed:
            _atomic_replace(
                plan.config_path,
                plan.candidate,
                mode=plan.original_mode,
            )
    except Exception as exc:
        raise ManagedClientInstallError(
            "managed registration installation failed; original settings remain in the backup"
        ) from exc
    return ManagedClientInstallReceipt(
        client=plan.client,
        config_path=plan.config_path,
        backup_path=backup_path,
        receipt_path=receipt_path,
        before_sha256=plan.before_sha256,
        after_sha256=plan.after_sha256,
        review_sha256=plan.review_sha256,
        changed=plan.changed,
    )


def rollback_active_managed_registration(
    receipt_path: Path,
) -> dict[str, Any]:
    """Restore exact prior bytes only while the installed candidate is current."""

    path = receipt_path.expanduser().absolute()
    payload, _ = _read_owned_file(path, max_bytes=_MAX_RECEIPT_BYTES)
    receipt = _load_object(payload, label="install receipt")
    if receipt.get("schema_version") != _RECEIPT_SCHEMA_VERSION:
        raise ManagedClientInstallError("unsupported install receipt version")
    if receipt.get("client") != "gemini":
        raise ManagedClientInstallError("unsupported install receipt client")
    try:
        config_path = Path(receipt["config_path"])
        backup_path = Path(receipt["backup_path"])
        before_sha256 = str(receipt["before_sha256"])
        after_sha256 = str(receipt["after_sha256"])
        mode = int(receipt["config_mode"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ManagedClientInstallError("install receipt is incomplete") from exc
    if not config_path.is_absolute() or not backup_path.is_absolute():
        raise ManagedClientInstallError("install receipt paths must be absolute")
    if backup_path.parent != config_path.parent:
        raise ManagedClientInstallError(
            "install receipt backup must be beside the settings file"
        )
    if not backup_path.name.startswith(
        f".{config_path.name}.pikvm-backup-"
    ):
        raise ManagedClientInstallError("install receipt backup name is invalid")

    current, _ = _read_owned_file(config_path, max_bytes=_MAX_CONFIG_BYTES)
    if _sha256(current) != after_sha256:
        raise ManagedClientInstallError(
            "client settings changed after installation; rollback refused"
        )
    original, _ = _read_owned_file(backup_path, max_bytes=_MAX_CONFIG_BYTES)
    if _sha256(original) != before_sha256:
        raise ManagedClientInstallError("rollback backup digest does not match")
    _atomic_replace(config_path, original, mode=mode)
    return {
        "schema_version": _RECEIPT_SCHEMA_VERSION,
        "client": "gemini",
        "config_path": str(config_path),
        "restored_sha256": before_sha256,
        "receipt_path": str(path),
        "backup_retained": True,
    }
