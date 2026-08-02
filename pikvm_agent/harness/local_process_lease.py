"""Small cross-process leases for local single-writer operations."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Self


_LEASE_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class LocalProcessLeaseAlreadyHeld(RuntimeError):
    """Another process already owns the selected local operation."""


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
class LocalProcessLease:
    """A held advisory file lock; release is explicit and idempotent."""

    path: Path
    _fd: int | None

    @classmethod
    def acquire(
        cls,
        path: Path,
        *,
        kind: str,
        already_held_error: type[LocalProcessLeaseAlreadyHeld] = (
            LocalProcessLeaseAlreadyHeld
        ),
    ) -> Self:
        if not _LEASE_KIND_RE.fullmatch(kind):
            raise ValueError("local process lease kind is invalid")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise RuntimeError("local process lease directory is not safe")
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
                raise already_held_error(
                    "local operation is already owned by another process"
                ) from exc
            payload = (
                f"pikvm-agent-{kind}-v1\n"
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

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        self.release()
