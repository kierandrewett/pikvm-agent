"""Acceptance checks for the installable local-operator wheel."""

from __future__ import annotations

import base64
import configparser
import csv
import hashlib
import io
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

REQUIRED_OPERATOR_FILES = {
    "pikvm_agent/cli.py",
    "pikvm_agent/harness/agent.py",
    "pikvm_agent/harness/client_acceptance.py",
    "pikvm_agent/harness/client_config_audit.py",
    "pikvm_agent/harness/managed_client_launcher.py",
    "pikvm_agent/harness/smoke_lab.py",
    "pikvm_agent/harness/model_budget.py",
    "pikvm_agent/harness/stdio_transport.py",
    "pikvm_agent/harness/office_acceptance.py",
    "pikvm_agent/harness/office_runner.py",
    "pikvm_agent/harness/provider_conformance.py",
    "pikvm_agent/harness/server.py",
    "pikvm_agent/harness/scorecard.py",
    "pikvm_agent/harness/support_bundle.py",
    "pikvm_agent/harness_mcp_server.py",
    "pikvm_agent/harness_ui/app.js",
    "pikvm_agent/harness_ui/index.html",
    "pikvm_agent/harness_ui/styles.css",
}
OPERATOR_ASSETS = (
    "pikvm_agent/harness_ui/app.js",
    "pikvm_agent/harness_ui/index.html",
    "pikvm_agent/harness_ui/styles.css",
)
MAX_WHEEL_MEMBERS = 2_000
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024


class WheelAcceptanceError(ValueError):
    pass


def _forbidden_member(name: str) -> bool:
    path = PurePosixPath(name)
    lowered = tuple(part.casefold() for part in path.parts)
    leaf = lowered[-1]
    return (
        leaf in {".env", "config.local.yaml"}
        or leaf.endswith((".db", ".pyc", ".sqlite", ".sqlite3"))
        or "__pycache__" in lowered
        or ".pikvm-harness" in lowered
        or lowered[0] in {"bench", "tests"}
    )


def _safe_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in archive.infolist():
        name = info.filename
        path = PurePosixPath(name)
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or ".." in path.parts
        ):
            raise WheelAcceptanceError("wheel contains an unsafe member path")
        if name in members:
            raise WheelAcceptanceError("wheel contains a duplicate member")
        if info.is_dir():
            continue
        if _forbidden_member(name):
            raise WheelAcceptanceError("wheel contains a forbidden runtime or secret file")
        if info.file_size > MAX_MEMBER_BYTES:
            raise WheelAcceptanceError("wheel member exceeds the acceptance size limit")
        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise WheelAcceptanceError("wheel exceeds the uncompressed size limit")
        members[name] = info
    if len(members) > MAX_WHEEL_MEMBERS:
        raise WheelAcceptanceError("wheel contains too many members")
    return members


def _dist_info_file(members: set[str], filename: str) -> str:
    matches = sorted(
        name
        for name in members
        if name.count("/") == 1
        and name.split("/", 1)[0].endswith(".dist-info")
        and name.endswith("/" + filename)
    )
    if len(matches) != 1:
        raise WheelAcceptanceError(
            f"wheel must contain exactly one dist-info/{filename}"
        )
    return matches[0]


def _verify_record(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    record_path: str,
) -> int:
    try:
        rows = list(
            csv.reader(
                io.StringIO(archive.read(record_path).decode("utf-8"))
            )
        )
    except (UnicodeDecodeError, csv.Error) as exc:
        raise WheelAcceptanceError("wheel RECORD is invalid") from exc
    records = {row[0]: row[1:] for row in rows if len(row) == 3}
    if len(records) != len(rows) or set(records) != set(members):
        raise WheelAcceptanceError("wheel RECORD does not cover every member exactly")
    verified = 0
    for name, info in members.items():
        digest_and_size = records[name]
        if name == record_path:
            if digest_and_size != ["", ""]:
                raise WheelAcceptanceError("wheel RECORD must not hash itself")
            continue
        digest_text, size_text = digest_and_size
        if not digest_text.startswith("sha256="):
            raise WheelAcceptanceError("wheel member is not protected by SHA-256")
        try:
            expected_size = int(size_text)
        except ValueError as exc:
            raise WheelAcceptanceError("wheel RECORD contains an invalid size") from exc
        data = archive.read(info)
        encoded = digest_text.removeprefix("sha256=")
        padding = "=" * (-len(encoded) % 4)
        try:
            expected_digest = base64.urlsafe_b64decode(encoded + padding)
        except ValueError as exc:
            raise WheelAcceptanceError("wheel RECORD contains an invalid digest") from exc
        if expected_size != len(data) or hashlib.sha256(data).digest() != expected_digest:
            raise WheelAcceptanceError("wheel RECORD integrity verification failed")
        verified += 1
    return verified


def inspect_operator_wheel(path: Path) -> dict[str, Any]:
    """Validate a built wheel and return only artifact-level diagnostics."""

    if not path.is_file():
        raise WheelAcceptanceError("wheel file does not exist")
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise WheelAcceptanceError("wheel is not a valid ZIP archive") from exc
    with archive:
        members = _safe_members(archive)
        names = set(members)
        missing = sorted(REQUIRED_OPERATOR_FILES - names)
        if missing:
            raise WheelAcceptanceError(
                "wheel is missing required operator files: " + ", ".join(missing)
            )
        metadata_path = _dist_info_file(names, "METADATA")
        entry_points_path = _dist_info_file(names, "entry_points.txt")
        record_path = _dist_info_file(names, "RECORD")
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
        if metadata.get("Name") != "pikvm-agent" or not metadata.get("Version"):
            raise WheelAcceptanceError("wheel metadata has the wrong package identity")
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(archive.read(entry_points_path).decode("utf-8"))
            console_script = parser["console_scripts"]["pikvm-agent"].strip()
        except (UnicodeDecodeError, KeyError, configparser.Error) as exc:
            raise WheelAcceptanceError(
                "wheel has no pikvm-agent console entry point"
            ) from exc
        if console_script != "pikvm_agent.cli:app":
            raise WheelAcceptanceError("wheel console entry point targets the wrong object")
        verified = _verify_record(archive, members, record_path)
        return {
            "valid": True,
            "package": {
                "name": metadata["Name"],
                "version": metadata["Version"],
            },
            "console_script": console_script,
            "wheel_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "members": len(members),
            "uncompressed_bytes": sum(info.file_size for info in members.values()),
            "record_entries_verified": verified,
            "operator_assets": {
                name: members[name].file_size for name in OPERATOR_ASSETS
            },
        }
