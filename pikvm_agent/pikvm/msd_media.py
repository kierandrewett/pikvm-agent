"""Build exact-byte, read-only virtual media without HID or clipboard input."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import tempfile

_SAFE_MEDIA_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}\Z")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_MANIFEST_NAME = "PIKVM-MANIFEST.JSON"
MAX_MEDIA_FILES = 32
MAX_MEDIA_FILE_BYTES = 16 * 1024 * 1024
MAX_MEDIA_TOTAL_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class MediaFile:
    name: str
    data: bytes


@dataclass(frozen=True)
class MediaFileReceipt:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ReadOnlyMediaReceipt:
    image_path: Path
    image_sha256: str
    manifest_sha256: str
    files: tuple[MediaFileReceipt, ...]


class MediaBuildError(RuntimeError):
    """The read-only media artifact could not be built exactly."""


class ReadOnlyIsoBuilder:
    def __init__(
        self,
        executable: str = "genisoimage",
        *,
        max_files: int = MAX_MEDIA_FILES,
        max_file_bytes: int = MAX_MEDIA_FILE_BYTES,
        max_total_bytes: int = MAX_MEDIA_TOTAL_BYTES,
    ) -> None:
        if min(max_files, max_file_bytes, max_total_bytes) < 1:
            raise ValueError("media build budgets must be positive")
        self.executable = executable
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes

    async def build(
        self,
        files: list[MediaFile],
        output: Path,
    ) -> ReadOnlyMediaReceipt:
        _validate_files(
            files,
            max_files=self.max_files,
            max_file_bytes=self.max_file_bytes,
            max_total_bytes=self.max_total_bytes,
        )
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise FileExistsError(f"refusing to overwrite media image: {output}")
        tool = shutil.which(self.executable)
        if tool is None:
            raise MediaBuildError(
                f"read-only media builder is unavailable: {self.executable}"
            )

        file_receipts = tuple(
            MediaFileReceipt(
                name=item.name,
                size=len(item.data),
                sha256=hashlib.sha256(item.data).hexdigest(),
            )
            for item in files
        )
        manifest = json.dumps(
            {
                "schema_version": 1,
                "files": [
                    {
                        "name": item.name,
                        "size": item.size,
                        "sha256": item.sha256,
                    }
                    for item in file_receipts
                ],
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()

        with tempfile.TemporaryDirectory(
            prefix=".pikvm-msd-build-",
            dir=output.parent,
        ) as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for item in files:
                (source / item.name).write_bytes(item.data)
            (source / _MANIFEST_NAME).write_bytes(manifest)
            temporary_image = root / "artifact.iso"
            process = await asyncio.create_subprocess_exec(
                tool,
                "-quiet",
                "-J",
                "-R",
                "-V",
                "PIKVM_AGENT",
                "-o",
                str(temporary_image),
                str(source),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env={
                    "LC_ALL": "C",
                    "SOURCE_DATE_EPOCH": "0",
                    "TZ": "UTC",
                },
            )
            try:
                _stdout, stderr = await process.communicate()
            except asyncio.CancelledError:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=1)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
                raise
            if process.returncode != 0:
                detail = stderr.decode(errors="replace").strip()[:500]
                raise MediaBuildError(
                    "read-only media builder failed"
                    + (f": {detail}" if detail else "")
                )
            if not temporary_image.is_file():
                raise MediaBuildError("read-only media builder produced no image")
            try:
                os.link(temporary_image, output)
            except FileExistsError as exc:
                raise FileExistsError(
                    f"refusing to overwrite media image: {output}"
                ) from exc
            output.chmod(0o600)

        return ReadOnlyMediaReceipt(
            image_path=output,
            image_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
            manifest_sha256=hashlib.sha256(manifest).hexdigest(),
            files=file_receipts,
        )


def _validate_files(
    files: list[MediaFile],
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> None:
    if not files:
        raise ValueError("at least one media file is required")
    if len(files) > max_files:
        raise ValueError(
            f"media file count {len(files)} exceeds the limit {max_files}"
        )
    names: set[str] = set()
    total_bytes = 0
    for item in files:
        name = item.name
        folded = name.casefold()
        stem = folded.split(".", 1)[0]
        if (
            not _SAFE_MEDIA_NAME.fullmatch(name)
            or name.endswith((" ", "."))
            or stem in _WINDOWS_RESERVED_NAMES
            or folded == _MANIFEST_NAME.casefold()
        ):
            raise ValueError(f"media file has an unsafe guest name: {name!r}")
        if folded in names:
            raise ValueError(f"media file name is ambiguous on Windows: {name!r}")
        if not isinstance(item.data, bytes):
            raise ValueError(f"media file bytes are invalid: {name!r}")
        if len(item.data) > max_file_bytes:
            raise ValueError(
                f"media file exceeds the per-file byte budget: {name!r}"
            )
        total_bytes += len(item.data)
        if total_bytes > max_total_bytes:
            raise ValueError("media files exceed the total byte budget")
        names.add(folded)
