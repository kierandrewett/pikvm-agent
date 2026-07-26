from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess

import pytest

from pikvm_agent.pikvm.msd_media import (
    MAX_MEDIA_FILES,
    MAX_MEDIA_FILE_BYTES,
    MAX_MEDIA_TOTAL_BYTES,
    MediaFile,
    ReadOnlyIsoBuilder,
)


@pytest.mark.skipif(
    shutil.which("genisoimage") is None or shutil.which("7z") is None,
    reason="read-only ISO build/extraction tools are unavailable",
)
@pytest.mark.asyncio
async def test_read_only_iso_builder_preserves_exact_file_bytes(
    tmp_path: Path,
) -> None:
    files = [
        MediaFile("essay.txt", "A measured Shakespeare essay.\n".encode()),
        MediaFile("earnings.csv", b"Quarter,Revenue\nQ1,125000\n"),
    ]
    output = tmp_path / "artifact.iso"

    receipt = await ReadOnlyIsoBuilder().build(files, output)

    assert receipt.image_path == output
    assert receipt.image_sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert [(item.name, item.size, item.sha256) for item in receipt.files] == [
        (
            item.name,
            len(item.data),
            hashlib.sha256(item.data).hexdigest(),
        )
        for item in files
    ]
    for item in files:
        extracted = subprocess.run(
            ["7z", "e", "-so", str(output), item.name],
            check=True,
            capture_output=True,
        ).stdout
        assert extracted == item.data
    manifest = subprocess.run(
        ["7z", "e", "-so", str(output), "PIKVM-MANIFEST.JSON"],
        check=True,
        capture_output=True,
    ).stdout
    assert receipt.manifest_sha256 == hashlib.sha256(manifest).hexdigest()
    assert json.loads(manifest) == {
        "schema_version": 1,
        "files": [
            {
                "name": item.name,
                "size": len(item.data),
                "sha256": hashlib.sha256(item.data).hexdigest(),
            }
            for item in files
        ],
    }


@pytest.mark.parametrize(
    "files",
    [
        [MediaFile("../escape.txt", b"x")],
        [MediaFile("folder/file.txt", b"x")],
        [MediaFile("PIKVM-MANIFEST.JSON", b"x")],
        [MediaFile("Report.txt", b"a"), MediaFile("report.TXT", b"b")],
        [MediaFile("CON", b"x")],
        [MediaFile("trailing.", b"x")],
    ],
)
@pytest.mark.asyncio
async def test_iso_builder_refuses_unsafe_or_ambiguous_guest_names_before_tooling(
    tmp_path: Path,
    files: list[MediaFile],
) -> None:
    with pytest.raises(ValueError, match="media file"):
        await ReadOnlyIsoBuilder(executable="must-not-run").build(
            files,
            tmp_path / "artifact.iso",
        )

    assert not (tmp_path / "artifact.iso").exists()


@pytest.mark.asyncio
async def test_iso_builder_enforces_file_count_and_byte_budgets_before_tooling(
    tmp_path: Path,
) -> None:
    builder = ReadOnlyIsoBuilder(
        executable="must-not-run",
        max_files=2,
        max_file_bytes=4,
        max_total_bytes=5,
    )

    with pytest.raises(ValueError, match="file count"):
        await builder.build(
            [
                MediaFile("a.txt", b"a"),
                MediaFile("b.txt", b"b"),
                MediaFile("c.txt", b"c"),
            ],
            tmp_path / "count.iso",
        )
    with pytest.raises(ValueError, match="per-file byte budget"):
        await builder.build(
            [MediaFile("large.txt", b"12345")],
            tmp_path / "large.iso",
        )
    with pytest.raises(ValueError, match="total byte budget"):
        await builder.build(
            [
                MediaFile("a.txt", b"123"),
                MediaFile("b.txt", b"456"),
            ],
            tmp_path / "total.iso",
        )


@pytest.mark.asyncio
async def test_iso_builder_never_overwrites_an_existing_image(
    tmp_path: Path,
) -> None:
    output = tmp_path / "artifact.iso"
    output.write_bytes(b"keep-existing-image")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        await ReadOnlyIsoBuilder(executable="must-not-run").build(
            [MediaFile("report.txt", b"new")],
            output,
        )

    assert output.read_bytes() == b"keep-existing-image"


@pytest.mark.asyncio
async def test_iso_builder_cancellation_terminates_the_child_and_leaves_no_image(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "slow-builder"
    pid_file = tmp_path / "child.pid"
    executable.write_text(
        "#!/usr/bin/python3\n"
        "import os\n"
        "from pathlib import Path\n"
        "import time\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    executable.chmod(0o700)
    output = tmp_path / "artifact.iso"
    task = asyncio.create_task(
        ReadOnlyIsoBuilder(executable=str(executable)).build(
            [MediaFile("report.txt", b"content")],
            output,
        )
    )
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    pid = int(pid_file.read_text())

    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    assert not output.exists()


def test_public_media_builder_report_matches_runtime_limits() -> None:
    report = json.loads(
        (
            Path(__file__).parents[1]
            / "bench"
            / "results"
            / "2026-07-25"
            / "safety"
            / "msd-media-builder-2026-07-26.json"
        ).read_text()
    )

    assert report["contracts"] == {"passed": 10, "total": 10}
    assert report["target_contacted"] is False
    assert report["resource_envelope"] == {
        "max_files": MAX_MEDIA_FILES,
        "max_file_bytes": MAX_MEDIA_FILE_BYTES,
        "max_total_bytes": MAX_MEDIA_TOTAL_BYTES,
    }
