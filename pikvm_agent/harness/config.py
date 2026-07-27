"""Configuration and provider factory for the standalone operator harness."""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
import shutil
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pikvm_agent.config import require_daemon_url
from pikvm_agent.harness.model_budget import (
    ModelBudgetPolicy,
    ProviderCostTerms,
)
from pikvm_agent.harness.model_pool import ModelPool, RoleRoute
from pikvm_agent.harness.provider_support import (
    ProviderKind,
    provider_support,
)
from pikvm_agent.harness.providers import (
    AnthropicApiProvider,
    ClaudeCodeProvider,
    CommandBearerAuth,
    EnvironmentHeaderAuth,
    GeminiApiProvider,
    GeminiCliProvider,
    CodexExecProvider,
    OpenAIResponsesProvider,
    OpenAICompatibleProvider,
    SubprocessJsonProvider,
)

HARNESS_ACCESS_TOKEN_MIN_LENGTH = 32


def _usd_to_microusd(value: Decimal) -> int:
    return int(
        (value * Decimal(1_000_000)).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


class ProviderBillingSpec(BaseModel):
    """Customer-owned billing classification; the harness invents no prices."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["subscription", "metered"]
    reservation_usd: Decimal | None = Field(default=None, gt=0)
    usage_usd_per_million: dict[str, Decimal] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_mode(self) -> "ProviderBillingSpec":
        if self.mode == "subscription":
            if self.reservation_usd is not None or self.usage_usd_per_million:
                raise ValueError(
                    "subscription billing cannot define reservations or usage prices"
                )
            return self
        if self.reservation_usd is None:
            raise ValueError("metered billing requires reservation_usd")
        if not self.usage_usd_per_million:
            raise ValueError(
                "metered billing requires usage_usd_per_million"
            )
        if any(price < 0 for price in self.usage_usd_per_million.values()):
            raise ValueError("metered usage prices cannot be negative")
        return self

    def cost_terms(self) -> ProviderCostTerms:
        if self.mode == "subscription":
            return ProviderCostTerms.subscription()
        assert self.reservation_usd is not None
        return ProviderCostTerms.metered(
            reservation_microusd=_usd_to_microusd(self.reservation_usd),
            usage_usd_per_million=self.usage_usd_per_million,
        )


class ProviderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ProviderKind
    model: str
    argv: list[str] = Field(default_factory=list)
    executable: str | None = None
    profile_home_env: str | None = None
    response_path: str = ""
    inherited_env: list[str] | None = None
    cwd: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    auth_mode: Literal[
        "api_key", "bearer_env", "bearer_command"
    ] | None = None
    credential_env: str | None = None
    credential_argv: list[str] = Field(default_factory=list)
    timeout_s: float = Field(default=90.0, ge=1, le=900)
    max_tokens: int = Field(default=4096, ge=256, le=128_000)
    reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] | None = None
    failure_cooldown_s: float = Field(default=15.0, ge=0, le=900)
    headers: dict[str, str] = Field(default_factory=dict)
    billing: ProviderBillingSpec | None = None

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "ProviderSpec":
        if self.kind == "subprocess_json" and not self.argv:
            raise ValueError("subprocess_json provider requires argv")
        if self.kind == "gemini_cli" and not self.profile_home_env:
            raise ValueError("gemini_cli provider requires profile_home_env")
        if self.kind != "gemini_cli" and self.profile_home_env is not None:
            raise ValueError(
                "profile_home_env is only supported by gemini_cli"
            )
        if self.kind in {"azure_openai_responses", "vertex_gemini"}:
            if not self.base_url:
                raise ValueError(
                    f"{self.kind} provider requires base_url"
                )
            if self.auth_mode is None:
                raise ValueError(
                    f"{self.kind} provider requires auth_mode"
                )
            if self.kind == "vertex_gemini" and self.auth_mode == "api_key":
                raise ValueError(
                    "vertex_gemini supports bearer_env or bearer_command"
                )
            if self.api_key_env is not None:
                raise ValueError(
                    f"{self.kind} uses credential_env, not api_key_env"
                )
            if self.auth_mode in {"api_key", "bearer_env"}:
                if not self.credential_env:
                    raise ValueError(
                        f"{self.auth_mode} requires credential_env"
                    )
                if self.credential_argv:
                    raise ValueError(
                        f"{self.auth_mode} cannot define credential_argv"
                    )
            elif not self.credential_argv:
                raise ValueError(
                    "bearer_command requires credential_argv"
                )
            if self.auth_mode == "bearer_command" and self.credential_env:
                raise ValueError(
                    "bearer_command cannot define credential_env"
                )
        elif (
            self.auth_mode is not None
            or self.credential_env is not None
            or self.credential_argv
        ):
            raise ValueError(
                "auth_mode and credential sources are only supported by "
                "azure_openai_responses or vertex_gemini"
            )
        if (
            self.kind
            not in {
                "subprocess_json",
                "codex_cli",
                "claude_cli",
                "gemini_cli",
                "azure_openai_responses",
                "vertex_gemini",
            }
            and not self.api_key_env
        ):
            raise ValueError(f"{self.kind} provider requires api_key_env")
        if self.kind == "openai_compatible" and not self.base_url:
            raise ValueError("openai_compatible provider requires base_url")
        sensitive_headers = {
            "authorization",
            "cookie",
            "proxy-authorization",
            "set-cookie",
            "x-api-key",
            "x-goog-api-key",
        }
        inline_secrets = sensitive_headers.intersection(
            name.casefold() for name in self.headers
        )
        if inline_secrets:
            raise ValueError(
                "provider config must not contain a secret-bearing header: "
                + ", ".join(sorted(inline_secrets))
            )
        return self


class RoleRoutes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reasoner: list[str] = Field(min_length=1)
    controller: list[str] = Field(min_length=1)
    verifier: list[str] = Field(min_length=1)


class ModelBudgetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_provider_attempts_per_run: int = Field(
        default=500,
        ge=1,
        le=100_000,
    )
    max_cost_usd_per_run: Decimal | None = Field(default=None, gt=0)
    pricing_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
    )

    @model_validator(mode="after")
    def require_version_for_cost_cap(self) -> "ModelBudgetSettings":
        if self.max_cost_usd_per_run is not None and not self.pricing_version:
            raise ValueError(
                "model cost budget requires an explicit pricing_version"
            )
        return self


class McpToolServerSpec(BaseModel):
    """One non-PiKVM MCP server available to normal assistant turns."""

    model_config = ConfigDict(extra="forbid")

    transport: Literal["stdio", "streamable_http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    cwd: Path | None = None
    inherited_env: list[str] = Field(default_factory=lambda: ["PATH"])
    url: str | None = None
    header_env: dict[str, str] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(min_length=1)
    read_only_tools: list[str] = Field(default_factory=list)
    timeout_s: float = Field(default=30.0, ge=1, le=300)

    @model_validator(mode="after")
    def validate_transport_fields(self) -> "McpToolServerSpec":
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio MCP tool server requires command")
            if self.url is not None or self.header_env:
                raise ValueError(
                    "stdio MCP tool server cannot define URL or headers"
                )
        else:
            if not self.url:
                raise ValueError(
                    "streamable_http MCP tool server requires url"
                )
            if self.command is not None or self.args or self.cwd is not None:
                raise ValueError(
                    "streamable_http MCP tool server cannot define a command"
                )
            if self.inherited_env != ["PATH"]:
                raise ValueError(
                    "streamable_http MCP tool server uses header_env, not inherited_env"
                )
        if set(self.read_only_tools) - set(self.allowed_tools):
            raise ValueError(
                "read_only_tools must be included in allowed_tools"
            )
        return self


class HarnessSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    listen: str = "127.0.0.1:47616"
    allow_remote_bind: bool = False
    daemon_url_env: str = "PIKVM_AGENT_DAEMON"
    access_token_env: str = "PIKVM_HARNESS_TOKEN"
    agent_token_env: str = "PIKVM_HARNESS_AGENT_TOKEN"
    observer_token_env: str = "PIKVM_HARNESS_OBSERVER_TOKEN"
    managed_mcp_name: str = Field(
        default="Managed PiKVM MCP",
        min_length=1,
        max_length=100,
    )
    computer_name: str = Field(
        default="Managed computer",
        min_length=1,
        max_length=200,
    )
    state_path: Path = Path(".pikvm-harness/state.sqlite3")
    artifact_dir: Path = Path(".pikvm-harness/artifacts")
    provider_conformance_path: Path = Path(
        ".pikvm-harness/provider-conformance.json"
    )
    allowed_origins: list[str] = Field(default_factory=list)
    providers: dict[str, ProviderSpec]
    routes: RoleRoutes
    assistant_tools: dict[str, McpToolServerSpec] = Field(
        default_factory=dict
    )
    model_budget: ModelBudgetSettings = Field(default_factory=ModelBudgetSettings)
    max_actions_per_advance: int = Field(default=4, ge=1, le=32)
    max_autonomous_resumes: int = Field(default=64, ge=1, le=10_000)
    max_actions_per_burst: int = Field(default=8, ge=1, le=32)
    max_total_actions: int = Field(default=100, ge=1)
    max_ungrounded_navigation_replans: int = Field(default=3, ge=1, le=16)

    @model_validator(mode="after")
    def validate_routes(self) -> "HarnessSettings":
        invalid_tool_servers = sorted(
            name
            for name in self.assistant_tools
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
        )
        if invalid_tool_servers:
            raise ValueError(
                "assistant tool server names use letters, numbers, _ or -: "
                + ", ".join(invalid_tool_servers)
            )
        configured = set(self.providers)
        for role in ("reasoner", "controller", "verifier"):
            missing = set(getattr(self.routes, role)) - configured
            if missing:
                raise ValueError(
                    f"{role} route references unknown providers: "
                    + ", ".join(sorted(missing))
                )
        if self.model_budget.max_cost_usd_per_run is not None:
            routed = {
                name
                for role in ("reasoner", "controller", "verifier")
                for name in getattr(self.routes, role)
            }
            unclassified = sorted(
                name for name in routed if self.providers[name].billing is None
            )
            if unclassified:
                raise ValueError(
                    "cost-capped routes require a billing classification: "
                    + ", ".join(unclassified)
                )
        self.host_port()
        return self

    def host_port(self) -> tuple[str, int]:
        try:
            host, port_text = self.listen.rsplit(":", 1)
            port = int(port_text)
        except (ValueError, AttributeError) as exc:
            raise ValueError("listen must be host:port") from exc
        if not host or not 1 <= port <= 65535:
            raise ValueError("listen must contain a valid host and port")
        return host.strip("[]"), port

    def daemon_url(self) -> str:
        return require_daemon_url(env_name=self.daemon_url_env)

    def optional_daemon_url(self) -> str | None:
        """Return the explicitly selected computer endpoint, if configured."""

        if not os.environ.get(self.daemon_url_env, "").strip():
            return None
        return self.daemon_url()

    def access_token(self) -> str:
        value = os.environ.get(self.access_token_env, "")
        if len(value) < HARNESS_ACCESS_TOKEN_MIN_LENGTH:
            raise ValueError(
                f"{self.access_token_env} must contain at least "
                f"{HARNESS_ACCESS_TOKEN_MIN_LENGTH} characters"
            )
        return value

    def observer_token(self, *, validate_distinct: bool = True) -> str:
        """Return the model-side telemetry credential.

        This credential is deliberately separate from the operator token. It
        can register direct MCP calls, but it cannot inspect runs or exercise
        any operator control.
        """

        value = os.environ.get(self.observer_token_env, "")
        if len(value) < HARNESS_ACCESS_TOKEN_MIN_LENGTH:
            raise ValueError(
                f"{self.observer_token_env} must contain at least "
                f"{HARNESS_ACCESS_TOKEN_MIN_LENGTH} characters"
            )
        if validate_distinct and secrets.compare_digest(
            value, self.access_token()
        ):
            raise ValueError(
                f"{self.observer_token_env} must differ from "
                f"{self.access_token_env}"
            )
        if validate_distinct and secrets.compare_digest(
            value, self.agent_token()
        ):
            raise ValueError(
                f"{self.observer_token_env} must differ from "
                f"{self.agent_token_env}"
            )
        return value

    def agent_token(self, *, validate_distinct: bool = True) -> str:
        """Return the high-level model credential without approval authority."""

        value = os.environ.get(self.agent_token_env, "")
        if len(value) < HARNESS_ACCESS_TOKEN_MIN_LENGTH:
            raise ValueError(
                f"{self.agent_token_env} must contain at least "
                f"{HARNESS_ACCESS_TOKEN_MIN_LENGTH} characters"
            )
        if validate_distinct and secrets.compare_digest(
            value, self.access_token()
        ):
            raise ValueError(
                f"{self.agent_token_env} must differ from "
                f"{self.access_token_env}"
            )
        return value

    def resolved_origins(self) -> set[str]:
        if self.allowed_origins:
            return set(self.allowed_origins)
        host, port = self.host_port()
        if host in {"127.0.0.1", "localhost", "::1"}:
            return {
                f"http://127.0.0.1:{port}",
                f"http://localhost:{port}",
                f"http://[::1]:{port}",
            }
        return set()


def load_harness_settings(path: Path) -> HarnessSettings:
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError("harness config root must be a mapping")
    base = path.resolve().parent
    for key in (
        "state_path",
        "artifact_dir",
        "provider_conformance_path",
    ):
        if key in raw:
            candidate = Path(str(raw[key]))
            if not candidate.is_absolute():
                raw[key] = str(base / candidate)
    if "provider_conformance_path" not in raw:
        raw["provider_conformance_path"] = str(
            base / ".pikvm-harness/provider-conformance.json"
        )
    assistant_tools = raw.get("assistant_tools")
    if isinstance(assistant_tools, dict):
        for value in assistant_tools.values():
            if not isinstance(value, dict) or "cwd" not in value:
                continue
            candidate = Path(str(value["cwd"]))
            if not candidate.is_absolute():
                value["cwd"] = str(base / candidate)
    return HarnessSettings.model_validate(raw)


def build_model_budget_policy(settings: HarnessSettings) -> ModelBudgetPolicy:
    maximum = settings.model_budget.max_cost_usd_per_run
    return ModelBudgetPolicy(
        max_provider_attempts=(
            settings.model_budget.max_provider_attempts_per_run
        ),
        max_cost_microusd=(
            _usd_to_microusd(maximum) if maximum is not None else None
        ),
        pricing_version=settings.model_budget.pricing_version,
        provider_costs={
            name: spec.billing.cost_terms()
            for name, spec in settings.providers.items()
            if spec.billing is not None
        },
    )


def ensure_safe_bind(settings: HarnessSettings) -> None:
    host, _ = settings.host_port()
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback and not settings.allow_remote_bind:
        raise ValueError(
            "refusing non-loopback harness bind; put the harness behind an "
            "authenticated TLS edge and set allow_remote_bind only for that deployment"
        )
    if not settings.resolved_origins():
        raise ValueError("remote binds require explicit allowed_origins")


def _provider_auth_metadata(spec: ProviderSpec) -> dict[str, str]:
    if spec.kind in {"azure_openai_responses", "vertex_gemini"}:
        mode = (
            "api_key_env"
            if spec.auth_mode == "api_key"
            else str(spec.auth_mode)
        )
        source = (
            spec.credential_argv[0]
            if spec.auth_mode == "bearer_command"
            else ""
        )
        return {"auth_mode": mode, "credential_source": source}
    if spec.kind == "codex_cli":
        return {
            "auth_mode": "saved_cli_login",
            "credential_source": spec.executable or "codex",
        }
    if spec.kind == "claude_cli":
        return {
            "auth_mode": "saved_cli_login",
            "credential_source": spec.executable or "claude",
        }
    if spec.kind == "gemini_cli":
        return {
            "auth_mode": "saved_cli_login",
            "credential_source": spec.executable or "gemini",
        }
    if spec.kind == "subprocess_json":
        return {
            "auth_mode": "external_or_none",
            "credential_source": spec.argv[0],
        }
    return {"auth_mode": "api_key_env", "credential_source": ""}


def check_provider_prerequisites(
    settings: HarnessSettings,
) -> dict[str, dict[str, object]]:
    """Report provider readiness without reading or returning credentials."""

    statuses: dict[str, dict[str, object]] = {}
    for name, spec in sorted(settings.providers.items()):
        auth_metadata = _provider_auth_metadata(spec)
        support = provider_support(spec.kind)
        status: dict[str, object] = {
            "kind": spec.kind,
            "ready": True,
            "billing_mode": (
                spec.billing.mode
                if spec.billing is not None
                else "unclassified"
            ),
            **support.readiness_metadata(auth_metadata["auth_mode"]),
            **auth_metadata,
        }
        if spec.kind in {"azure_openai_responses", "vertex_gemini"}:
            if spec.auth_mode == "bearer_command":
                executable = spec.credential_argv[0]
                status["credential"] = "owned-by-cli"
                status["executable"] = executable
                if shutil.which(executable) is None:
                    status["ready"] = False
                    status["error"] = "executable-not-found"
            else:
                env_name = str(spec.credential_env)
                present = bool(os.environ.get(env_name))
                status["credential"] = (
                    "env-present" if present else "env-missing"
                )
                status["credential_env"] = env_name
                if not present:
                    status["ready"] = False
                    status["error"] = "credential-env-missing"
        elif spec.kind in {
            "codex_cli",
            "claude_cli",
            "gemini_cli",
            "subprocess_json",
        }:
            if spec.kind == "codex_cli":
                executable = spec.executable or "codex"
                status["credential"] = "owned-by-cli"
            elif spec.kind == "claude_cli":
                executable = spec.executable or "claude"
                status["credential"] = "owned-by-cli"
            elif spec.kind == "gemini_cli":
                executable = spec.executable or "gemini"
                status["credential"] = "owned-by-cli"
                env_name = str(spec.profile_home_env)
                status["profile_home_env"] = env_name
            else:
                executable = spec.argv[0]
                status["credential"] = "external-or-none"
            status["executable"] = executable
            if shutil.which(executable) is None:
                status["ready"] = False
                status["error"] = "executable-not-found"
            elif spec.kind == "gemini_cli":
                profile_value = os.environ.get(env_name)
                if not profile_value:
                    status["ready"] = False
                    status["error"] = "profile-home-env-missing"
                elif not (
                    Path(profile_value).is_absolute()
                    and Path(profile_value).is_dir()
                ):
                    status["ready"] = False
                    status["error"] = "profile-home-unavailable"
                elif (
                    Path(profile_value).resolve()
                    == Path.home().resolve()
                ):
                    status["ready"] = False
                    status["error"] = "profile-home-not-dedicated"
        else:
            env_name = str(spec.api_key_env)
            present = bool(os.environ.get(env_name))
            status["credential"] = "env-present" if present else "env-missing"
            status["credential_env"] = env_name
            if not present:
                status["ready"] = False
                status["error"] = "credential-env-missing"
        statuses[name] = status
    return statuses


def ensure_provider_prerequisites(settings: HarnessSettings) -> None:
    """Fail closed unless every role route has at least one ready provider."""

    statuses = check_provider_prerequisites(settings)
    uncovered_roles = sorted(
        role
        for role in ("reasoner", "controller", "verifier")
        if not any(
            statuses[name]["ready"] for name in getattr(settings.routes, role)
        )
    )
    if uncovered_roles:
        raise ValueError(
            "no ready provider for role routes: "
            + ", ".join(uncovered_roles)
        )


def build_model_pool(settings: HarnessSettings) -> ModelPool:
    providers = {}
    for name, spec in settings.providers.items():
        common = {
            "name": name,
            "model": spec.model,
            "timeout_s": spec.timeout_s,
        }
        if spec.kind == "subprocess_json":
            providers[name] = SubprocessJsonProvider(
                **common,
                argv=spec.argv,
                response_path=spec.response_path,
                inherited_env=spec.inherited_env,
                cwd=spec.cwd,
            )
        elif spec.kind == "codex_cli":
            providers[name] = CodexExecProvider(
                **common,
                executable=spec.executable or "codex",
                inherited_env=spec.inherited_env,
            )
        elif spec.kind == "claude_cli":
            providers[name] = ClaudeCodeProvider(
                **common,
                executable=spec.executable or "claude",
                inherited_env=spec.inherited_env,
            )
        elif spec.kind == "gemini_cli":
            providers[name] = GeminiCliProvider(
                **common,
                executable=spec.executable or "gemini",
                profile_home_env=str(spec.profile_home_env),
                inherited_env=spec.inherited_env,
            )
        elif spec.kind == "openai_compatible":
            providers[name] = OpenAICompatibleProvider(
                **common,
                base_url=str(spec.base_url),
                api_key_env=str(spec.api_key_env),
                headers=spec.headers,
            )
        elif spec.kind == "openai_responses":
            providers[name] = OpenAIResponsesProvider(
                **common,
                base_url=spec.base_url or "https://api.openai.com/v1",
                api_key_env=str(spec.api_key_env),
                reasoning_effort=spec.reasoning_effort,
                max_output_tokens=spec.max_tokens,
            )
        elif spec.kind == "azure_openai_responses":
            if spec.auth_mode == "api_key":
                auth = EnvironmentHeaderAuth(
                    env_name=str(spec.credential_env),
                    header="api-key",
                )
            elif spec.auth_mode == "bearer_env":
                auth = EnvironmentHeaderAuth(
                    env_name=str(spec.credential_env),
                    scheme="Bearer ",
                )
            else:
                auth = CommandBearerAuth(
                    name=f"{name}-credential",
                    argv=spec.credential_argv,
                    inherited_env=spec.inherited_env
                    or ["PATH", "HOME", "AZURE_CONFIG_DIR"],
                    timeout_s=min(spec.timeout_s, 30.0),
                )
            providers[name] = OpenAIResponsesProvider(
                **common,
                base_url=str(spec.base_url),
                auth=auth,
                reasoning_effort=spec.reasoning_effort,
                max_output_tokens=spec.max_tokens,
            )
        elif spec.kind == "vertex_gemini":
            if spec.auth_mode == "bearer_env":
                auth = EnvironmentHeaderAuth(
                    env_name=str(spec.credential_env),
                    scheme="Bearer ",
                )
            else:
                auth = CommandBearerAuth(
                    name=f"{name}-credential",
                    argv=spec.credential_argv,
                    inherited_env=spec.inherited_env
                    or ["PATH", "HOME", "CLOUDSDK_CONFIG"],
                    timeout_s=min(spec.timeout_s, 30.0),
                )
            providers[name] = GeminiApiProvider(
                **common,
                base_url=str(spec.base_url),
                auth=auth,
            )
        elif spec.kind == "anthropic_api":
            args = {
                **common,
                "api_key_env": str(spec.api_key_env),
                "max_tokens": spec.max_tokens,
            }
            if spec.base_url:
                args["base_url"] = spec.base_url
            providers[name] = AnthropicApiProvider(**args)
        else:
            args = {**common, "api_key_env": str(spec.api_key_env)}
            if spec.base_url:
                args["base_url"] = spec.base_url
            providers[name] = GeminiApiProvider(**args)
    metadata = check_provider_prerequisites(settings)
    for name in providers:
        metadata[name]["routes"] = []
        metadata[name]["configured_model"] = settings.providers[name].model
    for role in ("reasoner", "controller", "verifier"):
        for position, name in enumerate(
            getattr(settings.routes, role),
            start=1,
        ):
            metadata[name]["routes"].append(
                {"role": role, "position": position}
            )
    return ModelPool(
        providers=providers,
        routes={
            "reasoner": RoleRoute(providers=settings.routes.reasoner),
            "controller": RoleRoute(providers=settings.routes.controller),
            "verifier": RoleRoute(providers=settings.routes.verifier),
        },
        provider_metadata=metadata,
        provider_conformance_path=settings.provider_conformance_path,
        failure_cooldowns={
            name: spec.failure_cooldown_s
            for name, spec in settings.providers.items()
        },
    )
