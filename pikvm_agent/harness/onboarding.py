"""Secret-free provider onboarding for the managed operator harness."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable

import yaml

from pikvm_agent.harness.config import HarnessSettings

ExecutableLookup = Callable[[str], str | None]
EnvironmentLookup = Callable[[str], str | None]


def _ordered_present(
    preferred: tuple[str, ...],
    providers: dict[str, dict[str, object]],
) -> list[str]:
    return [name for name in preferred if name in providers]


def build_initial_harness_settings(
    *,
    oauth_clis: str = "auto",
    executable_lookup: ExecutableLookup = shutil.which,
    environment_lookup: EnvironmentLookup = os.environ.get,
    gemini_cli_home_env: str = "PIKVM_GEMINI_CLI_HOME",
    listen: str = "127.0.0.1:47616",
    openai_model: str | None = None,
    openai_base_url: str | None = None,
    openai_api_key_env: str = "OPENAI_API_KEY",
    azure_model: str | None = None,
    azure_base_url: str | None = None,
    azure_auth: str = "api-key",
    azure_credential_env: str | None = None,
    anthropic_model: str | None = None,
    anthropic_api_key_env: str = "ANTHROPIC_API_KEY",
    gemini_model: str | None = None,
    gemini_api_key_env: str = "GEMINI_API_KEY",
    vertex_model: str | None = None,
    vertex_base_url: str | None = None,
    vertex_auth: str = "gcloud",
    vertex_credential_env: str | None = None,
    compatible_model: str | None = None,
    compatible_base_url: str | None = None,
    compatible_api_key_env: str = "MODEL_GATEWAY_KEY",
) -> HarnessSettings:
    """Build validated routes without reading or persisting credential values."""

    normalized_oauth = oauth_clis.strip().lower()
    supported_oauth = {"codex", "claude", "gemini"}
    if normalized_oauth == "auto":
        selected_oauth = {
            name
            for name in supported_oauth
            if executable_lookup(name) is not None
            and (
                name != "gemini"
                or bool(environment_lookup(gemini_cli_home_env))
            )
        }
    elif normalized_oauth == "both":
        # Preserve the original two-provider alias.
        selected_oauth = {"codex", "claude"}
    elif normalized_oauth == "all":
        selected_oauth = set(supported_oauth)
    elif normalized_oauth == "none":
        selected_oauth = set()
    else:
        selected_oauth = {
            item.strip()
            for item in normalized_oauth.split(",")
            if item.strip()
        }
        unknown = selected_oauth - supported_oauth
        if not selected_oauth or unknown:
            raise ValueError(
                "oauth_clis must be auto, all, both, none, or a comma-separated "
                "selection of codex, claude, and gemini"
            )
    providers: dict[str, dict[str, object]] = {}
    include_codex = "codex" in selected_oauth
    include_claude = "claude" in selected_oauth
    include_gemini = "gemini" in selected_oauth
    if include_codex:
        providers["codex-account"] = {
            "kind": "codex_cli",
            "model": "account-default",
        }
    if include_claude:
        providers["claude-account"] = {
            "kind": "claude_cli",
            "model": "opus",
        }
    if include_gemini:
        providers["gemini-account"] = {
            "kind": "gemini_cli",
            "model": "account-default",
            "profile_home_env": gemini_cli_home_env,
        }
    if openai_model:
        provider: dict[str, object] = {
            "kind": "openai_responses",
            "model": openai_model,
            "api_key_env": openai_api_key_env,
            "reasoning_effort": "low",
        }
        if openai_base_url:
            provider["base_url"] = openai_base_url
        providers["openai-api"] = provider
    if azure_model or azure_base_url:
        if not azure_model or not azure_base_url:
            raise ValueError(
                "azure_model and azure_base_url must be set together"
            )
        normalized_azure_auth = azure_auth.strip().lower()
        if normalized_azure_auth not in {
            "api-key",
            "entra-env",
            "azure-cli",
        }:
            raise ValueError(
                "azure_auth must be api-key, entra-env, or azure-cli"
            )
        azure_provider: dict[str, object] = {
            "kind": "azure_openai_responses",
            "model": azure_model,
            "base_url": azure_base_url,
            "reasoning_effort": "low",
        }
        if normalized_azure_auth == "azure-cli":
            azure_provider.update(
                {
                    "auth_mode": "bearer_command",
                    "credential_argv": [
                        "az",
                        "account",
                        "get-access-token",
                        "--resource",
                        "https://cognitiveservices.azure.com",
                        "--query",
                        "accessToken",
                        "-o",
                        "tsv",
                    ],
                    "inherited_env": [
                        "PATH",
                        "HOME",
                        "AZURE_CONFIG_DIR",
                    ],
                }
            )
        else:
            azure_provider.update(
                {
                    "auth_mode": (
                        "api_key"
                        if normalized_azure_auth == "api-key"
                        else "bearer_env"
                    ),
                    "credential_env": azure_credential_env
                    or (
                        "AZURE_OPENAI_API_KEY"
                        if normalized_azure_auth == "api-key"
                        else "AZURE_OPENAI_AUTH_TOKEN"
                    ),
                }
            )
        providers["azure-openai"] = azure_provider
    if anthropic_model:
        providers["anthropic-api"] = {
            "kind": "anthropic_api",
            "model": anthropic_model,
            "api_key_env": anthropic_api_key_env,
        }
    if gemini_model:
        providers["gemini-api"] = {
            "kind": "gemini_api",
            "model": gemini_model,
            "api_key_env": gemini_api_key_env,
        }
    if vertex_model or vertex_base_url:
        if not vertex_model or not vertex_base_url:
            raise ValueError(
                "vertex_model and vertex_base_url must be set together"
            )
        normalized_vertex_auth = vertex_auth.strip().lower()
        if normalized_vertex_auth not in {"gcloud", "token-env"}:
            raise ValueError(
                "vertex_auth must be gcloud or token-env"
            )
        vertex_provider: dict[str, object] = {
            "kind": "vertex_gemini",
            "model": vertex_model,
            "base_url": vertex_base_url,
        }
        if normalized_vertex_auth == "gcloud":
            vertex_provider.update(
                {
                    "auth_mode": "bearer_command",
                    "credential_argv": [
                        "gcloud",
                        "auth",
                        "print-access-token",
                    ],
                    "inherited_env": [
                        "PATH",
                        "HOME",
                        "CLOUDSDK_CONFIG",
                    ],
                }
            )
        else:
            vertex_provider.update(
                {
                    "auth_mode": "bearer_env",
                    "credential_env": vertex_credential_env
                    or "GOOGLE_CLOUD_ACCESS_TOKEN",
                }
            )
        providers["vertex-gemini"] = vertex_provider
    if compatible_model or compatible_base_url:
        if not compatible_model or not compatible_base_url:
            raise ValueError(
                "compatible_model and compatible_base_url must be set together"
            )
        providers["model-gateway"] = {
            "kind": "openai_compatible",
            "model": compatible_model,
            "base_url": compatible_base_url,
            "api_key_env": compatible_api_key_env,
        }
    if not providers:
        raise ValueError(
            "no providers selected or detected; choose --oauth-clis or supply "
            "an API model option"
        )

    reasoner = _ordered_present(
        (
            "claude-account",
            "anthropic-api",
            "openai-api",
            "azure-openai",
            "codex-account",
            "gemini-account",
            "gemini-api",
            "vertex-gemini",
            "model-gateway",
        ),
        providers,
    )
    controller = _ordered_present(
        (
            "model-gateway",
            "gemini-api",
            "vertex-gemini",
            "openai-api",
            "azure-openai",
            "gemini-account",
            "claude-account",
            "codex-account",
            "anthropic-api",
        ),
        providers,
    )
    verifier = _ordered_present(
        (
            "gemini-api",
            "vertex-gemini",
            "openai-api",
            "azure-openai",
            "anthropic-api",
            "gemini-account",
            "claude-account",
            "codex-account",
            "model-gateway",
        ),
        providers,
    )
    return HarnessSettings.model_validate(
        {
            "listen": listen,
            "daemon_url_env": "PIKVM_AGENT_DAEMON",
            "access_token_env": "PIKVM_HARNESS_TOKEN",
            "agent_token_env": "PIKVM_HARNESS_AGENT_TOKEN",
            "observer_token_env": "PIKVM_HARNESS_OBSERVER_TOKEN",
            "state_path": ".pikvm-harness/state.sqlite3",
            "artifact_dir": ".pikvm-harness/artifacts",
            "providers": providers,
            "routes": {
                "reasoner": reasoner,
                "controller": controller,
                "verifier": verifier,
            },
        }
    )


def render_initial_harness_config(settings: HarnessSettings) -> str:
    raw = settings.model_dump(mode="json", exclude_none=True)
    raw["providers"] = {
        name: spec.model_dump(
            mode="json",
            exclude_none=True,
            exclude_defaults=True,
        )
        for name, spec in settings.providers.items()
    }
    return yaml.safe_dump(raw, sort_keys=False)
