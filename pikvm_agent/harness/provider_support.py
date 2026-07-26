"""Canonical support contracts for every model-provider adapter."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

ProviderKind = Literal[
    "subprocess_json",
    "codex_cli",
    "claude_cli",
    "gemini_cli",
    "openai_compatible",
    "openai_responses",
    "azure_openai_responses",
    "anthropic_api",
    "gemini_api",
    "vertex_gemini",
]
SupportTier = Literal["stable", "beta", "bridge"]
ImplementationContract = Literal["first_party", "external_bridge"]
CredentialOwner = Literal[
    "provider_cli",
    "harness_environment",
    "external_bridge",
]


@dataclass(frozen=True, slots=True)
class ProviderAuthContract:
    """One supported authentication mode and its credential owner."""

    mode: str
    credential_owner: CredentialOwner


@dataclass(frozen=True, slots=True)
class ProviderSupport:
    """Product contract for a provider adapter, independent of credentials."""

    kind: ProviderKind
    support_tier: SupportTier
    implementation_contract: ImplementationContract
    interface: str
    pixel_input: str
    structured_output: str
    auth: tuple[ProviderAuthContract, ...]

    def auth_owner(self, auth_mode: str) -> CredentialOwner:
        for contract in self.auth:
            if contract.mode == auth_mode:
                return contract.credential_owner
        raise ValueError(
            f"{self.kind} does not support authentication mode {auth_mode!r}"
        )

    def readiness_metadata(self, auth_mode: str) -> dict[str, str]:
        """Return public, secret-free metadata for readiness and evidence."""

        return {
            "support_tier": self.support_tier,
            "implementation_contract": self.implementation_contract,
            "credential_owner": self.auth_owner(auth_mode),
            "interface": self.interface,
            "pixel_input": self.pixel_input,
            "structured_output": self.structured_output,
        }


def _auth(
    *contracts: tuple[str, CredentialOwner],
) -> tuple[ProviderAuthContract, ...]:
    return tuple(
        ProviderAuthContract(mode=mode, credential_owner=owner)
        for mode, owner in contracts
    )


PROVIDER_SUPPORT: Mapping[str, ProviderSupport] = MappingProxyType(
    {
        "subprocess_json": ProviderSupport(
            kind="subprocess_json",
            support_tier="bridge",
            implementation_contract="external_bridge",
            interface="Custom subprocess",
            pixel_input="Bridge-defined",
            structured_output="Harness-validated JSON",
            auth=_auth(("external_or_none", "external_bridge")),
        ),
        "codex_cli": ProviderSupport(
            kind="codex_cli",
            support_tier="stable",
            implementation_contract="first_party",
            interface="Codex exec",
            pixel_input="Native image attachment",
            structured_output="Strict JSON Schema",
            auth=_auth(("saved_cli_login", "provider_cli")),
        ),
        "claude_cli": ProviderSupport(
            kind="claude_cli",
            support_tier="stable",
            implementation_contract="first_party",
            interface="Claude print mode",
            pixel_input="Isolated Read artifact",
            structured_output="Strict JSON Schema",
            auth=_auth(("saved_cli_login", "provider_cli")),
        ),
        "gemini_cli": ProviderSupport(
            kind="gemini_cli",
            support_tier="beta",
            implementation_contract="first_party",
            interface="Gemini headless mode",
            pixel_input="Isolated @ image artifact",
            structured_output="Harness-validated JSON",
            auth=_auth(("saved_cli_login", "provider_cli")),
        ),
        "openai_compatible": ProviderSupport(
            kind="openai_compatible",
            support_tier="bridge",
            implementation_contract="external_bridge",
            interface="Chat Completions API",
            pixel_input="Image data URL",
            structured_output="Strict JSON Schema",
            auth=_auth(("api_key_env", "harness_environment")),
        ),
        "openai_responses": ProviderSupport(
            kind="openai_responses",
            support_tier="stable",
            implementation_contract="first_party",
            interface="OpenAI Responses API",
            pixel_input="Native image input",
            structured_output="Strict JSON Schema",
            auth=_auth(("api_key_env", "harness_environment")),
        ),
        "azure_openai_responses": ProviderSupport(
            kind="azure_openai_responses",
            support_tier="beta",
            implementation_contract="first_party",
            interface="Azure OpenAI Responses API",
            pixel_input="Native image input",
            structured_output="Strict JSON Schema",
            auth=_auth(
                ("api_key_env", "harness_environment"),
                ("bearer_env", "harness_environment"),
                ("bearer_command", "provider_cli"),
            ),
        ),
        "anthropic_api": ProviderSupport(
            kind="anthropic_api",
            support_tier="stable",
            implementation_contract="first_party",
            interface="Anthropic Messages API",
            pixel_input="Base64 image block",
            structured_output="JSON Schema",
            auth=_auth(("api_key_env", "harness_environment")),
        ),
        "gemini_api": ProviderSupport(
            kind="gemini_api",
            support_tier="stable",
            implementation_contract="first_party",
            interface="Gemini generateContent",
            pixel_input="Inline image data",
            structured_output="JSON Schema",
            auth=_auth(("api_key_env", "harness_environment")),
        ),
        "vertex_gemini": ProviderSupport(
            kind="vertex_gemini",
            support_tier="beta",
            implementation_contract="first_party",
            interface="Vertex AI Gemini generateContent",
            pixel_input="Inline image data",
            structured_output="JSON Schema",
            auth=_auth(
                ("bearer_env", "harness_environment"),
                ("bearer_command", "provider_cli"),
            ),
        ),
    }
)


def provider_support(kind: str) -> ProviderSupport:
    """Return the canonical support contract for a configured provider kind."""

    try:
        return PROVIDER_SUPPORT[kind]
    except KeyError as exc:
        raise ValueError(f"unknown provider kind {kind!r}") from exc
