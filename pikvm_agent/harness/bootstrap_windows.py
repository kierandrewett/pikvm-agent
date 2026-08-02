"""Provision the benchmark observer on a disposable Windows VNC target.

The native binary is downloaded from a temporary HTTPS receiver. Only one
short, inspectable PowerShell command is typed through RFB; executable bytes
and Base64 are never typed as HID input.
"""

from __future__ import annotations

import argparse
import io
import re
import time
from urllib.parse import quote, urlsplit

from pikvm_agent.harness.vnc_target_lease import (
    VncTargetLease,
    normalize_vnc_endpoint,
)


def _require_https(url: str, *, label: str) -> str:
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{label} must use a valid HTTPS URL")
    return value


def validate_observer_file_path(file_path: str) -> str:
    """Return the canonical lab-workspace path or reject it before VNC opens."""

    normalized_path = file_path.replace("\\", "/")
    if not re.fullmatch(
        r"C:/PiKVM-Harness/workspace/[A-Za-z0-9][A-Za-z0-9._-]{0,95}",
        normalized_path,
    ):
        raise ValueError(
            "observer file must be a safe absolute path inside "
            "C:/PiKVM-Harness/workspace"
        )
    return normalized_path


def build_bootstrap_commands(
    *,
    artifact_url: str | None = None,
    callback_url: str | None = None,
    token: str | None = None,
    public_base_url: str | None = None,
    file_path: str | None = None,
    reuse_installed: bool = False,
    visible: bool = True,
) -> list[str]:
    """Build short provisioning commands without coupling artifact and oracle.

    ``public_base_url`` preserves the authenticated HTTPS callback mode.  The
    screenshot-only visual mode supplies only ``artifact_url`` and starts the
    observer without any data-egress endpoint.
    """
    if public_base_url is not None:
        base = _require_https(public_base_url.rstrip("/"), label="observer base")
        if not token:
            raise ValueError("observer token must not be empty")
        artifact_url = f"{base}/observer.exe?token={quote(token, safe='')}"
        callback_url = f"{base}/ingest"
    if artifact_url is None and not reuse_installed:
        raise ValueError("observer artifact URL is required")
    if artifact_url is not None:
        artifact_url = _require_https(artifact_url, label="observer artifact")
    arguments = ""
    if callback_url is not None:
        callback_url = _require_https(callback_url, label="observer callback")
        if not token:
            raise ValueError("observer token must not be empty")
        arguments = f" --callback {callback_url} --token {token}"
    if file_path is not None:
        normalized_path = validate_observer_file_path(file_path)
        # The validated workspace grammar contains no whitespace. Keeping this
        # argument unquoted also avoids the US/UK keyboard-layout ambiguity
        # where the physical double-quote key produces "@".
        arguments += f" --file {normalized_path}"
    cleanup = [
        f"taskkill /IM {image} /F"
        for image in (
            "observer.exe",
            "observer-v4.exe",
            "observer-v3.exe",
            "observer-v2compact.exe",
            "observer-v2.exe",
            "pikvm-accuracy-observer.exe",
        )
    ]
    commands = [*cleanup, "mkdir C:/PiKVM-Harness/workspace -fo"]
    if reuse_installed:
        commands.append(
            "if(!(Test-Path C:/PiKVM-Harness/observer.exe)){throw 2}"
        )
    else:
        commands.append(
            f"irm {artifact_url} -OutFile C:/PiKVM-Harness/observer.exe"
        )
    if visible:
        commands.append(f"& C:/PiKVM-Harness/observer.exe{arguments}")
    else:
        hidden_arguments = arguments.split()
        argument_list = (
            " -ArgumentList "
            + ",".join(
                "'" + argument.replace("'", "''") + "'"
                for argument in hidden_arguments
            )
            if hidden_arguments
            else ""
        )
        commands.extend(
            [
                "start C:/PiKVM-Harness/observer.exe" + argument_list,
                "exit",
            ]
        )
    return commands


def build_bootstrap_command(
    *,
    artifact_url: str | None = None,
    callback_url: str | None = None,
    token: str | None = None,
    public_base_url: str | None = None,
    file_path: str | None = None,
    reuse_installed: bool = False,
    visible: bool = True,
) -> str:
    """Return the auditable display form; deployment submits each line alone."""
    return ";".join(
        build_bootstrap_commands(
            artifact_url=artifact_url,
            callback_url=callback_url,
            token=token,
            public_base_url=public_base_url,
            file_path=file_path,
            reuse_installed=reuse_installed,
            visible=visible,
        )
    )


def _type_paced(client: object, text: str, *, delay_s: float) -> None:
    for character in text:
        client.keyPress(character)
        time.sleep(delay_s)


def _open_powershell(client: object, *, character_delay_s: float) -> None:
    """Open PowerShell without assuming where Windows placed the Start button."""

    client.keyPress("esc")
    client.keyPress("super-r")
    time.sleep(0.75)
    _type_paced(
        client,
        "taskkill /IM observer.exe /F",
        delay_s=character_delay_s,
    )
    client.keyPress("enter")
    time.sleep(1.5)
    client.keyPress("super-r")
    time.sleep(0.75)
    _type_paced(client, "powershell", delay_s=character_delay_s)
    client.keyPress("enter")
    time.sleep(4)


def deploy(
    *,
    endpoint: str,
    artifact_url: str | None = None,
    callback_url: str | None = None,
    token: str | None = None,
    public_base_url: str | None = None,
    file_path: str | None = None,
    password: str | None,
    username: str | None,
    powershell_ready: bool = False,
    character_delay_s: float = 0.050,
    reuse_installed: bool = False,
    visible: bool = True,
) -> None:
    from vncdotool import api

    commands = build_bootstrap_commands(
        artifact_url=artifact_url,
        callback_url=callback_url,
        public_base_url=public_base_url,
        token=token,
        file_path=file_path,
        reuse_installed=reuse_installed,
        visible=visible,
    )
    with VncTargetLease.acquire(endpoint):
        client = api.connect(
            normalize_vnc_endpoint(endpoint),
            password,
            timeout=90,
            username=username,
        )
        try:
            client.factory.force_caps = True
            client.captureScreen(io.BytesIO(), format="PNG")
            if not powershell_ready:
                _open_powershell(
                    client,
                    character_delay_s=character_delay_s,
                )
            # Provisioning may resume after an interrupted experiment. Cancel any
            # PowerShell continuation prompt before clearing the current line;
            # Ctrl+A/Backspace alone only edits the latest `>>` line.
            client.keyPress("ctrl-c")
            time.sleep(0.5)
            client.keyPress("ctrl-a")
            client.keyPress("bsp")
            for index, command in enumerate(commands):
                _type_paced(client, command, delay_s=character_delay_s)
                client.keyPress("enter")
                # The download command needs more time before starting the process.
                time.sleep(8 if index == 2 else 1)
        finally:
            client.disconnect()
            api.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision the native Windows accuracy observer over VNC."
    )
    parser.add_argument("--vnc", required=True, help="RFB endpoint supplied at runtime")
    parser.add_argument("--artifact-url")
    parser.add_argument("--callback-url")
    parser.add_argument("--public-base-url")
    parser.add_argument("--token")
    parser.add_argument(
        "--file",
        dest="file_path",
        help=(
            "Runtime-only artifact path under "
            "C:/PiKVM-Harness/workspace."
        ),
    )
    parser.add_argument("--password")
    parser.add_argument("--username")
    parser.add_argument(
        "--reuse-installed",
        action="store_true",
        help="Require and restart the already-installed observer binary.",
    )
    parser.add_argument(
        "--hidden",
        action="store_true",
        help="Start the observer separately and return from PowerShell.",
    )
    parser.add_argument(
        "--powershell-ready",
        action="store_true",
        help="PowerShell is already focused; skip the Start-menu launch.",
    )
    parser.add_argument("--character-delay-ms", type=float, default=50.0)
    args = parser.parse_args()
    deploy(
        endpoint=args.vnc,
        artifact_url=args.artifact_url,
        callback_url=args.callback_url,
        public_base_url=args.public_base_url,
        token=args.token,
        file_path=args.file_path,
        password=args.password,
        username=args.username,
        powershell_ready=args.powershell_ready,
        character_delay_s=args.character_delay_ms / 1000.0,
        reuse_installed=args.reuse_installed,
        visible=not args.hidden,
    )


if __name__ == "__main__":
    main()
