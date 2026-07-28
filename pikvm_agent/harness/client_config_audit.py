"""Fail-closed audit of PiKVM registrations visible to an MCP client.

The audit deliberately returns only classifications and caller-supplied source
labels. Parsed commands, arguments, environment values, and raw configuration
never cross the module boundary in the report.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pikvm_agent.harness.client_setup import (
    ClientKind,
    valid_active_managed_mcp_arguments,
)

RegistrationClass = Literal["managed", "direct", "raw", "ambiguous"]
AuditFailure = Literal[
    "missing_managed",
    "competing_raw_or_direct",
    "ambiguous_pikvm_registration",
    "duplicate_managed",
    "invalid_config",
]
_CODEX_INVENTORY_ENV = (
    "HOME",
    "PATH",
    "CODEX_HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
)


@dataclass(frozen=True)
class ClientConfigDocument:
    """One explicitly selected client-config scope."""

    source_label: str
    rendered: str

    def __post_init__(self) -> None:
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}",
            self.source_label,
        ):
            raise ValueError(
                "source_label must be a short non-path label"
            )


class ClientConfigFinding(BaseModel):
    """Secret-free classification of one PiKVM-related MCP registration."""

    model_config = ConfigDict(frozen=True)

    source_label: str
    server_name: str
    classification: RegistrationClass


class ClientConfigAuditReport(BaseModel):
    """Aggregate isolation verdict safe to retain as benchmark evidence."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    client: ClientKind
    safe: bool
    managed_count: int
    findings: tuple[ClientConfigFinding, ...]
    failures: tuple[AuditFailure, ...]


def read_codex_effective_inventory(
    *,
    executable: str = "codex",
    project_dir: Path | None = None,
    timeout_s: float = 10.0,
    environ: Mapping[str, str] | None = None,
    config_overrides: Sequence[str] = (),
) -> ClientConfigDocument:
    """Read Codex's resolved MCP inventory without launching an MCP server."""

    override_tokens = tuple(config_overrides)
    if len(override_tokens) % 2 or any(
        override_tokens[index] != "-c"
        for index in range(0, len(override_tokens), 2)
    ):
        raise ValueError("Codex inventory overrides must be -c key=value pairs")
    if (
        any("\x00" in token for token in override_tokens)
        or sum(len(token) for token in override_tokens) > 256 * 1024
    ):
        raise ValueError("Codex inventory overrides exceed the safe input bound")
    source_environment = os.environ if environ is None else environ
    child_environment = {
        name: source_environment[name]
        for name in _CODEX_INVENTORY_ENV
        if source_environment.get(name)
    }
    completed = subprocess.run(
        [executable, *override_tokens, "mcp", "list", "--json"],
        cwd=project_dir,
        env=child_environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_s,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Codex inventory command failed")
    if len(completed.stdout) > 1024 * 1024:
        raise ValueError("Codex inventory output exceeds 1 MiB")
    try:
        rendered = completed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Codex inventory output is not UTF-8") from exc
    return ClientConfigDocument(
        source_label="native-inventory",
        rendered=rendered,
    )


def write_client_config_audit_report(
    path: str | os.PathLike[str],
    report: ClientConfigAuditReport,
) -> None:
    """Create an owner-only audit report without following overwrite paths."""

    destination = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(destination))
    if not os.path.isdir(parent):
        raise ValueError("client audit output parent does not exist")
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ValueError("client audit output already exists") from exc
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(
                (report.model_dump_json(indent=2) + "\n").encode()
            )
    except Exception:
        try:
            os.unlink(destination)
        except OSError:
            pass
        raise


def _launch_tokens(server: object) -> tuple[str, ...] | None:
    if not isinstance(server, dict):
        return None
    command = server.get("command")
    args = server.get("args", [])
    if isinstance(command, list):
        if command and all(isinstance(token, str) for token in command):
            return tuple(command)
        return None
    if not isinstance(command, str) or not command:
        return None
    if not isinstance(args, list) or not all(
        isinstance(arg, str) for arg in args
    ):
        return None
    return (command, *args)


def _classification(
    server_name: str,
    server: object,
) -> RegistrationClass | None:
    tokens = _launch_tokens(server)
    searchable = " ".join((server_name, *(tokens or ()))).lower()
    if not any(
        marker in searchable
        for marker in ("pikvm", "pikvm_agent", "pikvm-agent")
    ):
        return None
    if tokens is None:
        return "ambiguous"
    command_name = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    python_command = bool(
        re.fullmatch(r"python(?:\d+(?:\.\d+)?)?(?:\.exe)?", command_name)
    )
    console_command = command_name in {"pikvm-agent", "pikvm-agent.exe"}
    subcommand = ""
    arguments: tuple[str, ...] = ()
    if (
        python_command
        and tokens[1:4] == ("-m", "pikvm_agent.cli", "harness")
        and len(tokens) >= 5
    ):
        subcommand = tokens[4]
        arguments = tokens[5:]
    elif console_command and tokens[1:2] == ("harness",) and len(tokens) >= 3:
        subcommand = tokens[2]
        arguments = tokens[3:]

    def has_single_value(option: str) -> bool:
        indexes = [
            index for index, value in enumerate(arguments) if value == option
        ]
        return (
            len(indexes) == 1
            and indexes[0] + 1 < len(arguments)
            and bool(arguments[indexes[0] + 1])
            and not arguments[indexes[0] + 1].startswith("-")
        )

    managed_shape = subcommand == "managed-mcp" or (
        subcommand == "managed-runtime-mcp"
        and has_single_value("--runtime")
    ) or (
        subcommand == "active-managed-mcp"
        and valid_active_managed_mcp_arguments(arguments)
    )
    direct_shape = subcommand == "direct-mcp"
    raw_shape = (
        python_command
        and (
            tokens[1:3] == ("-m", "pikvm_agent.mcp_server")
            or tokens[1:4] == ("-m", "pikvm_agent.cli", "mcp")
        )
    ) or (console_command and tokens[1:2] == ("mcp",))
    if managed_shape:
        return "managed"
    if direct_shape:
        return "direct"
    if raw_shape:
        return "raw"
    return "ambiguous"


def _jsonc_comment_end(rendered: str, index: int) -> int | None:
    if index + 1 >= len(rendered) or rendered[index] != "/":
        return None
    following = rendered[index + 1]
    if following == "/":
        end = index + 2
        while end < len(rendered) and rendered[end] not in "\r\n":
            end += 1
        return end
    if following != "*":
        return None
    closing = rendered.find("*/", index + 2)
    if closing < 0:
        raise ValueError("unterminated JSONC comment")
    return closing + 2


def _next_jsonc_token(rendered: str, index: int) -> int:
    while index < len(rendered):
        if rendered[index].isspace():
            index += 1
            continue
        comment_end = _jsonc_comment_end(rendered, index)
        if comment_end is None:
            return index
        index = comment_end
    return index


def _normalize_jsonc(rendered: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(rendered):
        current = rendered[index]
        if in_string:
            output.append(current)
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == '"':
                in_string = False
            index += 1
            continue
        if current == '"':
            in_string = True
            output.append(current)
            index += 1
            continue
        comment_end = _jsonc_comment_end(rendered, index)
        if comment_end is not None:
            output.append(" ")
            index = comment_end
            continue
        if current == ",":
            lookahead = _next_jsonc_token(rendered, index + 1)
            if (
                lookahead < len(rendered)
                and rendered[lookahead] in "}]"
            ):
                index += 1
                continue
        output.append(current)
        index += 1
    return "".join(output)


def _load_json_config(rendered: str) -> object:
    return json.loads(_normalize_jsonc(rendered))


def _parse_servers(rendered: str, client: ClientKind) -> dict[str, object]:
    if client == "codex":
        try:
            parsed_toml = tomllib.loads(rendered)
        except tomllib.TOMLDecodeError:
            pass
        else:
            servers = parsed_toml.get("mcp_servers", {})
            if isinstance(servers, dict):
                return servers
            raise TypeError
    parsed_json = _load_json_config(rendered)
    if client == "codex" and isinstance(parsed_json, list):
        inventory: dict[str, object] = {}
        for item in parsed_json:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("transport"), dict)
            ):
                raise TypeError
            name = item["name"]
            if name in inventory:
                raise ValueError("duplicate Codex inventory server")
            server = dict(item["transport"])
            server["enabled"] = item.get("enabled", True)
            inventory[name] = server
        return inventory
    if not isinstance(parsed_json, dict):
        raise TypeError
    if client == "opencode":
        mcp = parsed_json.get("mcp", {})
        if not isinstance(mcp, dict):
            raise TypeError
        servers = mcp.get("servers", mcp)
    else:
        servers = parsed_json.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise TypeError
    return servers


def audit_client_configs(
    *,
    client: ClientKind,
    documents: list[ClientConfigDocument],
) -> ClientConfigAuditReport:
    """Audit effective PiKVM surfaces from low-to-high precedence documents."""

    effective_findings: dict[str, ClientConfigFinding] = {}
    failures: list[AuditFailure] = []
    for document in documents:
        try:
            servers = _parse_servers(document.rendered, client)
        except (
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            failures.append("invalid_config")
            continue
        for server_name, server in servers.items():
            if not isinstance(server_name, str):
                continue
            if isinstance(server, dict) and (
                server.get("enabled") is False
                or server.get("disabled") is True
            ):
                effective_findings.pop(server_name, None)
                continue
            classification = _classification(server_name, server)
            if classification is None:
                effective_findings.pop(server_name, None)
                continue
            effective_findings[server_name] = ClientConfigFinding(
                source_label=document.source_label,
                server_name=server_name,
                classification=classification,
            )

    findings = tuple(effective_findings.values())

    managed_count = sum(
        finding.classification == "managed" for finding in findings
    )
    if managed_count == 0:
        failures.append("missing_managed")
    elif managed_count > 1:
        failures.append("duplicate_managed")
    if any(
        finding.classification in {"raw", "direct"} for finding in findings
    ):
        failures.append("competing_raw_or_direct")
    if any(
        finding.classification == "ambiguous" for finding in findings
    ):
        failures.append("ambiguous_pikvm_registration")

    unique_failures = tuple(dict.fromkeys(failures))
    return ClientConfigAuditReport(
        client=client,
        safe=not unique_failures,
        managed_count=managed_count,
        findings=findings,
        failures=unique_failures,
    )
