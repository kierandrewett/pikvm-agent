"""Additive, secret-reference-only model-provider connections."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from pikvm_agent.harness.config import (
    HarnessSettings,
    ProviderSpec,
    build_model_pool,
)
from pikvm_agent.harness.model_pool import ModelPool

ConnectableProviderKind = Literal[
    "codex_cli",
    "claude_cli",
    "gemini_cli",
    "openai_responses",
    "anthropic_api",
    "gemini_api",
    "openai_compatible",
]

_API_KINDS = {
    "openai_responses",
    "anthropic_api",
    "gemini_api",
    "openai_compatible",
}
_CLI_KINDS = {"codex_cli", "claude_cli", "gemini_cli"}
_CREDENTIAL_PREFIXES = (
    "aiza",
    "bearer ",
    "ghp_",
    "github_pat_",
    "sk-",
    "sk_",
    "xoxb-",
    "xoxp-",
)
_JWT_PATTERN = re.compile(
    r"^[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}$"
)


def _looks_like_credential_value(value: str) -> bool:
    normalized = value.strip().casefold()
    return normalized.startswith(_CREDENTIAL_PREFIXES) or bool(
        _JWT_PATTERN.fullmatch(value.strip())
    )


class ProviderConnectionConflict(RuntimeError):
    """A connection tried to replace an existing provider alias."""


class ProviderConnectionPolicyConflict(RuntimeError):
    """Browser setup cannot safely satisfy the active budget policy."""


class ProviderConnectionRequest(BaseModel):
    """A public connection request that can contain references, never secrets."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    )
    kind: ConnectableProviderKind
    model: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$",
    )
    base_url: str | None = Field(default=None, max_length=500)
    credential_env: str | None = Field(
        default=None,
        min_length=2,
        max_length=128,
        pattern=r"^[A-Z][A-Z0-9_]{1,127}$",
    )
    profile_home_env: str | None = Field(
        default=None,
        min_length=2,
        max_length=128,
        pattern=r"^[A-Z][A-Z0-9_]{1,127}$",
    )

    @field_validator("alias", "model")
    @classmethod
    def reject_credential_values(cls, value: str) -> str:
        if _looks_like_credential_value(value):
            raise ValueError(
                "credential values are not accepted; provide a reference"
            )
        return value

    @field_validator("base_url")
    @classmethod
    def safe_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "base_url must not contain credentials, query, or fragment"
            )
        loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and loopback
        ):
            raise ValueError("base_url must use HTTPS or loopback HTTP")
        if any(
            _looks_like_credential_value(segment)
            for segment in parsed.path.split("/")
            if segment
        ):
            raise ValueError(
                "base_url must not contain a credential-like path value"
            )
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "ProviderConnectionRequest":
        if self.kind in _CLI_KINDS:
            if self.credential_env is not None or self.base_url is not None:
                raise ValueError(
                    "CLI providers use provider-owned sign-in, not API fields"
                )
            if self.kind == "gemini_cli":
                self.profile_home_env = (
                    self.profile_home_env or "PIKVM_GEMINI_CLI_HOME"
                )
            elif self.profile_home_env is not None:
                raise ValueError(
                    "profile_home_env is only supported by gemini_cli"
                )
            return self
        if self.credential_env is None:
            raise ValueError("API providers require a credential_env name")
        if self.profile_home_env is not None:
            raise ValueError("API providers cannot use profile_home_env")
        if self.kind == "openai_compatible" and self.base_url is None:
            raise ValueError("openai_compatible requires base_url")
        return self

    def provider_spec(self) -> ProviderSpec:
        if self.kind in _CLI_KINDS:
            values: dict[str, object] = {
                "kind": self.kind,
                "model": self.model,
            }
            if self.kind == "gemini_cli":
                values["profile_home_env"] = self.profile_home_env
            return ProviderSpec.model_validate(values)
        return ProviderSpec.model_validate(
            {
                "kind": self.kind,
                "model": self.model,
                "base_url": self.base_url,
                "api_key_env": self.credential_env,
                "reasoning_effort": (
                    "low" if self.kind == "openai_responses" else None
                ),
            }
        )


class ProviderConnectionResult(BaseModel):
    """Secret-free provider state returned to the operator UI."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    configured_model: str
    kind: str
    ready: bool
    credential_owner: str
    readiness_error: str | None = None
    configured_not_routed: bool = True
    secret_received: Literal[False] = False


class ProviderConnectionManager:
    """Persist and activate additive provider configuration."""

    def __init__(
        self,
        *,
        settings: HarnessSettings,
        settings_path: Path,
        models: ModelPool,
    ) -> None:
        self._settings = settings
        self._settings_path = settings_path.expanduser().resolve()
        self._models = models
        self._lock = asyncio.Lock()

    async def connect(
        self,
        request: ProviderConnectionRequest,
    ) -> ProviderConnectionResult:
        async with self._lock:
            if self._settings.model_budget.max_cost_usd_per_run is not None:
                raise ProviderConnectionPolicyConflict(
                    "browser setup is disabled while a cost cap is active; "
                    "add reviewed billing terms in the harness config"
                )
            if (
                request.alias in self._settings.providers
                or request.alias in self._models.providers
            ):
                raise ProviderConnectionConflict(
                    f"provider alias already configured: {request.alias}"
                )
            spec = request.provider_spec()
            candidate_raw = self._settings.model_dump(mode="python")
            candidate_raw["providers"] = {
                **self._settings.providers,
                request.alias: spec,
            }
            candidate = HarnessSettings.model_validate(candidate_raw)
            isolated_raw = candidate.model_dump(mode="python")
            isolated_raw["providers"] = {request.alias: spec}
            isolated_raw["routes"] = {
                role: [request.alias]
                for role in ("reasoner", "controller", "verifier")
            }
            staged = build_model_pool(
                HarnessSettings.model_validate(isolated_raw)
            )
            try:
                self._write_config(candidate)
                self._models.adopt_unrouted_provider(staged, request.alias)
            except Exception:
                await self._close_pool(staged)
                raise
            self._settings = candidate
            health = self._models.health()[request.alias]
            return ProviderConnectionResult(
                provider=request.alias,
                configured_model=request.model,
                kind=request.kind,
                ready=bool(health.get("ready", True)),
                credential_owner=str(
                    health.get("credential_owner") or "unknown"
                ),
                readiness_error=(
                    str(health["readiness_error"])
                    if health.get("readiness_error")
                    else None
                ),
            )

    def _write_config(self, settings: HarnessSettings) -> None:
        destination = self._settings_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        rendered = yaml.safe_dump(
            settings.model_dump(
                mode="json",
                exclude_none=True,
                exclude_defaults=True,
            ),
            sort_keys=False,
        )
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                os.fchmod(temporary.fileno(), 0o600)
                temporary.write(rendered)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    async def _close_pool(pool: ModelPool) -> None:
        for provider in pool.providers.values():
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()
