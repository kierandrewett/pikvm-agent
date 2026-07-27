"""Exclusive local ownership for runtime-selected VNC lab targets.

The VNC server's client count is not a trustworthy concurrency boundary: two
independent adapter processes can both report that they are alone.  This
process-independent lease fails closed before either adapter opens RFB.

Only a versioned digest is used in the lock filename.  The endpoint itself is
never written to disk.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


_DIRECT_PORT_RE = re.compile(r"^(?P<host>[^:]+):(?P<port>\d+)$")
_NATIVE_PORT_RE = re.compile(r"^(?P<host>.+)::(?P<port>\d+)$")
_LEASE_DIR_ENV = "PIKVM_LAB_TARGET_LEASE_DIR"


class VncTargetAlreadyLeased(RuntimeError):
    """Another local adapter process owns the selected VNC target."""


def normalize_vnc_endpoint(endpoint: str) -> str:
    """Accept friendly ``host:port`` and vncdotool's ``host::port``."""

    value = endpoint.strip()
    match = _DIRECT_PORT_RE.fullmatch(value)
    if match:
        return f"{match.group('host')}::{match.group('port')}"
    return value


def canonical_vnc_target(endpoint: str) -> str:
    """Canonicalise equivalent endpoint spellings for lease identity only."""

    value = normalize_vnc_endpoint(endpoint)
    native = _NATIVE_PORT_RE.fullmatch(value)
    if native:
        host = native.group("host").rstrip(".").casefold()
        return f"{host}::{int(native.group('port'))}"
    return value.casefold()


def _lease_directory(explicit: Path | None) -> Path:
    if explicit is not None:
        directory = explicit
    elif configured := os.environ.get(_LEASE_DIR_ENV):
        directory = Path(configured)
    elif runtime_dir := os.environ.get("XDG_RUNTIME_DIR"):
        directory = Path(runtime_dir) / "pikvm-agent" / "vnc-target-leases"
    else:
        owner = str(os.getuid()) if hasattr(os, "getuid") else "current-user"
        directory = (
            Path(tempfile.gettempdir())
            / f"pikvm-agent-vnc-target-leases-{owner}"
        )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("VNC target lease directory is not a safe directory")
    return directory


def _lock_file(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows packaging
        import msvcrt

        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows packaging
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


@dataclass
class VncTargetLease:
    """A held advisory lock; release is explicit and idempotent."""

    path: Path
    _fd: int | None

    @classmethod
    def acquire(
        cls,
        endpoint: str,
        *,
        lock_dir: Path | None = None,
    ) -> VncTargetLease:
        canonical = canonical_vnc_target(endpoint)
        digest = hashlib.sha256(
            f"pikvm-agent-vnc-target-v1\0{canonical}".encode("utf-8")
        ).hexdigest()
        path = _lease_directory(lock_dir) / f"target-{digest[:32]}.lock"
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            try:
                _lock_file(fd)
            except (BlockingIOError, OSError) as exc:
                raise VncTargetAlreadyLeased(
                    "VNC target is already controlled by another local lab"
                ) from exc
            payload = (
                "pikvm-agent-vnc-target-lease-v1\n"
                f"owner-pid={os.getpid()}\n"
            ).encode("ascii")
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            os.ftruncate(fd, len(payload))
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            raise
        return cls(path=path, _fd=fd)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock_file(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> VncTargetLease:
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        self.release()
