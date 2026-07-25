"""CLI process for isolated PiKVM-shaped lab adapters."""

from __future__ import annotations

import argparse
import os

import uvicorn

from pikvm_agent.harness.vnc_pikvm_api import (
    VncDotoolTransport,
    create_vnc_pikvm_app,
)
from pikvm_agent.harness.in_guest_transport import InGuestComputerTransport


def _secret_from_env(name: str) -> str | None:
    return os.environ.get(name) or None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Expose an isolated computer through the PiKVM lab API."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--vnc", help="RFB endpoint: host:port")
    target.add_argument(
        "--in-guest",
        help="OSWorld-compatible in-guest HTTP endpoint supplied at runtime.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--keymap", default="en-us")
    parser.add_argument(
        "--keyboard-profile",
        choices=("generic", "windows"),
        default="generic",
        help="Runtime-only VNC keyboard compatibility profile.",
    )
    parser.add_argument("--password-env", default="PIKVM_LAB_VNC_PASSWORD")
    parser.add_argument("--username-env", default="PIKVM_LAB_VNC_USERNAME")
    args = parser.parse_args()

    transport = (
        VncDotoolTransport(
            args.vnc,
            password=_secret_from_env(args.password_env),
            username=_secret_from_env(args.username_env),
            keymap=args.keymap,
            keyboard_profile=args.keyboard_profile,
        )
        if args.vnc
        else InGuestComputerTransport(args.in_guest)
    )
    app = create_vnc_pikvm_app(transport, keymap=args.keymap)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
