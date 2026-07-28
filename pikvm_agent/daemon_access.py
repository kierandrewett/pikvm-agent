"""Capability boundary for the local machine-control daemon."""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

DAEMON_TOKEN_ENV = "PIKVM_AGENT_DAEMON_TOKEN"
HARNESS_TOKEN_ENV = "PIKVM_AGENT_HARNESS_TOKEN"
DAEMON_CAPABILITY_ENV_NAMES = frozenset(
    {
        DAEMON_TOKEN_ENV,
        HARNESS_TOKEN_ENV,
        "PIKVM_HARNESS_TOKEN",
        "PIKVM_HARNESS_AGENT_TOKEN",
        "PIKVM_HARNESS_OBSERVER_TOKEN",
    }
)
MIN_DAEMON_TOKEN_LENGTH = 32
MAX_DAEMON_TOKEN_LENGTH = 512


class DaemonAccessError(ValueError):
    """The daemon capability configuration is unsafe or incomplete."""


def _validated_token(value: str, *, env_name: str) -> str:
    if not value:
        raise DaemonAccessError(f"{env_name} is required")
    if len(value) < MIN_DAEMON_TOKEN_LENGTH:
        raise DaemonAccessError(
            f"{env_name} must contain at least "
            f"{MIN_DAEMON_TOKEN_LENGTH} characters"
        )
    if len(value) > MAX_DAEMON_TOKEN_LENGTH:
        raise DaemonAccessError(
            f"{env_name} exceeds the accepted length"
        )
    return value


@dataclass(frozen=True)
class DaemonAccess:
    """Two non-interchangeable bearer capabilities.

    The action capability is sufficient for observed direct MCP calls. The
    harness capability is accepted for ordinary work and is the only
    capability that may relay an approval already made in the operator UI.
    """

    action_token: str
    harness_token: str

    def __post_init__(self) -> None:
        _validated_token(self.action_token, env_name=DAEMON_TOKEN_ENV)
        _validated_token(self.harness_token, env_name=HARNESS_TOKEN_ENV)
        if secrets.compare_digest(self.action_token, self.harness_token):
            raise DaemonAccessError(
                f"{HARNESS_TOKEN_ENV} must differ from {DAEMON_TOKEN_ENV}"
            )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "DaemonAccess":
        source = os.environ if environ is None else environ
        return cls(
            action_token=source.get(DAEMON_TOKEN_ENV, ""),
            harness_token=source.get(HARNESS_TOKEN_ENV, ""),
        )

    def authorizes(
        self,
        bearer_token: str,
        *,
        harness_only: bool = False,
    ) -> bool:
        if not bearer_token:
            return False
        if secrets.compare_digest(bearer_token, self.harness_token):
            return True
        return not harness_only and secrets.compare_digest(
            bearer_token,
            self.action_token,
        )


def bearer_from_header(value: str) -> str:
    scheme, separator, credential = value.partition(" ")
    if separator != " " or scheme.lower() != "bearer":
        return ""
    return credential.strip()


def action_token_from_environment(
    environ: Mapping[str, str] | None = None,
) -> str:
    source = os.environ if environ is None else environ
    return _validated_token(
        source.get(DAEMON_TOKEN_ENV, ""),
        env_name=DAEMON_TOKEN_ENV,
    )


def harness_token_from_environment(
    environ: Mapping[str, str] | None = None,
) -> str:
    source = os.environ if environ is None else environ
    return _validated_token(
        source.get(HARNESS_TOKEN_ENV, ""),
        env_name=HARNESS_TOKEN_ENV,
    )
