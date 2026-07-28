from __future__ import annotations

from pathlib import Path

import pytest

from pikvm_agent.harness.config import (
    HarnessSettings,
    McpToolServerSpec,
    build_model_budget_policy,
    build_model_pool,
    check_provider_prerequisites,
    ensure_safe_bind,
    ensure_provider_prerequisites,
    load_harness_settings,
)
from pikvm_agent.harness.model_budget import ProviderCostTerms
from pikvm_agent.harness.providers import (
    AnthropicApiProvider,
    CommandBearerAuth,
    ClaudeCodeProvider,
    EnvironmentHeaderAuth,
    GeminiApiProvider,
    GeminiCliProvider,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    SubprocessJsonProvider,
)


def test_gemini_cli_factory_requires_and_reports_a_dedicated_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "gemini-profile"
    profile.mkdir()
    monkeypatch.setenv("TEST_GEMINI_PROFILE", str(profile))
    monkeypatch.setattr(
        "pikvm_agent.harness.config.shutil.which",
        lambda value: "/usr/bin/gemini" if value == "gemini" else None,
    )
    settings = HarnessSettings(
        providers={
            "gemini-account": {
                "kind": "gemini_cli",
                "model": "account-default",
                "profile_home_env": "TEST_GEMINI_PROFILE",
            }
        },
        routes={
            "reasoner": ["gemini-account"],
            "controller": ["gemini-account"],
            "verifier": ["gemini-account"],
        },
    )

    pool = build_model_pool(settings)
    status = check_provider_prerequisites(settings)["gemini-account"]

    assert isinstance(pool.providers["gemini-account"], GeminiCliProvider)
    assert status["ready"] is True
    assert status["credential"] == "owned-by-cli"
    assert status["auth_mode"] == "saved_cli_login"
    assert status["credential_source"] == "gemini"
    assert status["profile_home_env"] == "TEST_GEMINI_PROFILE"
    assert status["interface"] == "Gemini headless mode"
    assert status["structured_output"] == "Harness-validated JSON"
    assert str(profile) not in str(status)


def test_gemini_cli_prerequisites_fail_closed_without_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_GEMINI_PROFILE", raising=False)
    monkeypatch.setattr(
        "pikvm_agent.harness.config.shutil.which",
        lambda value: "/usr/bin/gemini" if value == "gemini" else None,
    )
    settings = HarnessSettings(
        providers={
            "gemini-account": {
                "kind": "gemini_cli",
                "model": "account-default",
                "profile_home_env": "TEST_GEMINI_PROFILE",
            }
        },
        routes={
            "reasoner": ["gemini-account"],
            "controller": ["gemini-account"],
            "verifier": ["gemini-account"],
        },
    )

    status = check_provider_prerequisites(settings)["gemini-account"]

    assert status["ready"] is False
    assert status["error"] == "profile-home-env-missing"


def test_gemini_cli_prerequisites_reject_the_normal_user_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_GEMINI_PROFILE", str(Path.home()))
    monkeypatch.setattr(
        "pikvm_agent.harness.config.shutil.which",
        lambda value: "/usr/bin/gemini" if value == "gemini" else None,
    )
    settings = HarnessSettings(
        providers={
            "gemini-account": {
                "kind": "gemini_cli",
                "model": "account-default",
                "profile_home_env": "TEST_GEMINI_PROFILE",
            }
        },
        routes={
            "reasoner": ["gemini-account"],
            "controller": ["gemini-account"],
            "verifier": ["gemini-account"],
        },
    )

    status = check_provider_prerequisites(settings)["gemini-account"]

    assert status["ready"] is False
    assert status["error"] == "profile-home-not-dedicated"


def test_gemini_cli_config_rejects_missing_profile_environment_name() -> None:
    with pytest.raises(ValueError, match="requires profile_home_env"):
        HarnessSettings(
            providers={
                "gemini-account": {
                    "kind": "gemini_cli",
                    "model": "account-default",
                }
            },
            routes={
                "reasoner": ["gemini-account"],
                "controller": ["gemini-account"],
                "verifier": ["gemini-account"],
            },
        )


def test_optional_daemon_url_distinguishes_chat_only_from_selected_computer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = HarnessSettings(
        daemon_url_env="TEST_OPTIONAL_DAEMON",
        providers={
            "fixture": {
                "kind": "subprocess_json",
                "model": "fixture-model",
                "argv": ["fixture-provider"],
            }
        },
        routes={
            "reasoner": ["fixture"],
            "controller": ["fixture"],
            "verifier": ["fixture"],
        },
    )
    monkeypatch.delenv("TEST_OPTIONAL_DAEMON", raising=False)

    assert settings.optional_daemon_url() is None

    monkeypatch.setenv(
        "TEST_OPTIONAL_DAEMON",
        "http://127.0.0.1:48123/",
    )

    assert settings.optional_daemon_url() == "http://127.0.0.1:48123"


def test_loads_provider_routes_without_secrets_or_machine_endpoint_in_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "harness.yaml"
    config.write_text(
        """
listen: "127.0.0.1:47616"
daemon_url_env: "TEST_DAEMON_URL"
access_token_env: "TEST_HARNESS_TOKEN"
agent_token_env: "TEST_AGENT_TOKEN"
observer_token_env: "TEST_OBSERVER_TOKEN"
state_path: "./state.sqlite3"
artifact_dir: "./artifacts"
provider_conformance_path: "./provider-conformance.json"
providers:
  claude-oauth:
    kind: claude_cli
    model: "subscription-default"
  gateway-fast:
    kind: openai_compatible
    model: "fast-model"
    base_url: "https://gateway.example/v1"
    api_key_env: "TEST_GATEWAY_KEY"
  openai-native:
    kind: openai_responses
    model: "gpt-model"
    api_key_env: "TEST_OPENAI_KEY"
    reasoning_effort: "low"
  anthropic-api:
    kind: anthropic_api
    model: "claude-model"
    api_key_env: "TEST_ANTHROPIC_KEY"
  gemini-api:
    kind: gemini_api
    model: "gemini-model"
    api_key_env: "TEST_GEMINI_KEY"
routes:
  reasoner: ["claude-oauth", "anthropic-api"]
  controller: ["gateway-fast", "gemini-api"]
  verifier: ["gemini-api", "anthropic-api"]
"""
    )
    monkeypatch.setenv("TEST_DAEMON_URL", "http://127.0.0.1:47641")
    monkeypatch.setenv("TEST_HARNESS_TOKEN", "x" * 32)
    monkeypatch.setenv("TEST_AGENT_TOKEN", "z" * 32)
    monkeypatch.setenv("TEST_OBSERVER_TOKEN", "y" * 32)

    settings = load_harness_settings(config)
    pool = build_model_pool(settings)

    assert settings.daemon_url() == "http://127.0.0.1:47641"
    assert settings.access_token() == "x" * 32
    assert settings.agent_token() == "z" * 32
    assert settings.observer_token() == "y" * 32
    assert settings.provider_conformance_path == (
        tmp_path / "provider-conformance.json"
    )
    assert isinstance(pool.providers["claude-oauth"], ClaudeCodeProvider)
    assert isinstance(pool.providers["gateway-fast"], OpenAICompatibleProvider)
    assert isinstance(
        pool.providers["openai-native"], OpenAIResponsesProvider
    )
    assert isinstance(pool.providers["anthropic-api"], AnthropicApiProvider)
    assert isinstance(pool.providers["gemini-api"], GeminiApiProvider)
    assert pool.route_names("reasoner") == ["claude-oauth", "anthropic-api"]
    assert (
        pool.health()["claude-oauth"]["configured_model"]
        == "subscription-default"
    )
    assert (
        pool.health()["claude-oauth"]["conformance_status"]
        == "not-run"
    )


def test_assistant_mcp_tools_require_an_explicit_allow_list() -> None:
    with pytest.raises(ValueError, match="allowed_tools"):
        McpToolServerSpec(
            command="example-mcp",
        )

    with pytest.raises(ValueError, match="read_only_tools"):
        McpToolServerSpec(
            command="example-mcp",
            allowed_tools=["search"],
            read_only_tools=["send"],
        )

    configured = McpToolServerSpec(
        command="example-mcp",
        allowed_tools=["search", "send"],
        read_only_tools=["search"],
    )

    assert configured.allowed_tools == ["search", "send"]
    assert configured.read_only_tools == ["search"]


def test_assistant_mcp_tools_cannot_reintroduce_raw_machine_control() -> None:
    with pytest.raises(ValueError, match="machine-control"):
        McpToolServerSpec(
            command="raw-pikvm-mcp",
            allowed_tools=["pikvm_run_burst"],
        )

    with pytest.raises(ValueError, match="daemon capabilities"):
        McpToolServerSpec(
            command="wrapper-mcp",
            inherited_env=[
                "PATH",
                "PIKVM_AGENT_HARNESS_TOKEN",
            ],
            allowed_tools=["run_burst"],
        )

    with pytest.raises(ValueError, match="daemon capabilities"):
        McpToolServerSpec(
            transport="streamable_http",
            url="http://127.0.0.1:9999/mcp",
            header_env={
                "Authorization": "PIKVM_AGENT_DAEMON_TOKEN",
            },
            allowed_tools=["run_burst"],
        )


def test_assistant_mcp_server_namespace_cannot_collide_with_tool_separator() -> None:
    with pytest.raises(ValueError, match="server names"):
        HarnessSettings(
            providers={
                "model": {
                    "kind": "openai_responses",
                    "model": "model",
                    "api_key_env": "TEST_MODEL_KEY",
                }
            },
            routes={
                "reasoner": ["model"],
                "controller": ["model"],
                "verifier": ["model"],
            },
            assistant_tools={
                "bad.name": {
                    "command": "example-mcp",
                    "allowed_tools": ["search"],
                }
            },
        )


def test_azure_responses_factory_supports_api_key_and_cli_owned_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AZURE_OPENAI_KEY", "secret")
    monkeypatch.setenv("TEST_AZURE_OPENAI_TOKEN", "token")
    monkeypatch.setattr(
        "pikvm_agent.harness.config.shutil.which",
        lambda value: "/usr/bin/az" if value == "az" else None,
    )
    settings = HarnessSettings(
        providers={
            "azure-key": {
                "kind": "azure_openai_responses",
                "model": "controller-deployment",
                "base_url": (
                    "https://resource.openai.azure.com/openai/v1"
                ),
                "auth_mode": "api_key",
                "credential_env": "TEST_AZURE_OPENAI_KEY",
            },
            "azure-oauth": {
                "kind": "azure_openai_responses",
                "model": "reasoner-deployment",
                "base_url": (
                    "https://resource.openai.azure.com/openai/v1"
                ),
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
                "inherited_env": ["PATH", "HOME", "AZURE_CONFIG_DIR"],
            },
            "azure-token": {
                "kind": "azure_openai_responses",
                "model": "verifier-deployment",
                "base_url": (
                    "https://resource.openai.azure.com/openai/v1"
                ),
                "auth_mode": "bearer_env",
                "credential_env": "TEST_AZURE_OPENAI_TOKEN",
            },
        },
        routes={
            "reasoner": ["azure-oauth"],
            "controller": ["azure-key"],
            "verifier": ["azure-token", "azure-oauth", "azure-key"],
        },
    )

    pool = build_model_pool(settings)
    key_provider = pool.providers["azure-key"]
    oauth_provider = pool.providers["azure-oauth"]
    token_provider = pool.providers["azure-token"]
    health = pool.health()
    prerequisites = check_provider_prerequisites(settings)

    assert isinstance(key_provider, OpenAIResponsesProvider)
    assert isinstance(key_provider.auth, EnvironmentHeaderAuth)
    assert key_provider.auth.header == "api-key"
    assert isinstance(oauth_provider, OpenAIResponsesProvider)
    assert isinstance(oauth_provider.auth, CommandBearerAuth)
    assert oauth_provider.auth.argv[0] == "az"
    assert isinstance(token_provider, OpenAIResponsesProvider)
    assert isinstance(token_provider.auth, EnvironmentHeaderAuth)
    assert token_provider.auth.header == "Authorization"
    assert token_provider.auth.scheme == "Bearer "
    assert health["azure-key"]["interface"] == (
        "Azure OpenAI Responses API"
    )
    assert health["azure-key"]["credential"] == "env-present"
    assert health["azure-key"]["auth_mode"] == "api_key_env"
    assert health["azure-oauth"]["credential"] == "owned-by-cli"
    assert health["azure-oauth"]["auth_mode"] == "bearer_command"
    assert health["azure-oauth"]["credential_source"] == "az"
    assert prerequisites["azure-oauth"]["executable"] == "az"


@pytest.mark.parametrize(
    ("provider", "message"),
    [
        (
            {
                "kind": "azure_openai_responses",
                "model": "deployment",
                "auth_mode": "api_key",
                "credential_env": "AZURE_KEY",
            },
            "requires base_url",
        ),
        (
            {
                "kind": "azure_openai_responses",
                "model": "deployment",
                "base_url": (
                    "https://resource.openai.azure.com/openai/v1"
                ),
                "auth_mode": "bearer_command",
            },
            "requires credential_argv",
        ),
    ],
)
def test_azure_responses_config_fails_closed_when_auth_is_incomplete(
    provider: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        HarnessSettings(
            providers={"azure": provider},
            routes={
                "reasoner": ["azure"],
                "controller": ["azure"],
                "verifier": ["azure"],
            },
        )


def test_vertex_gemini_factory_supports_gcloud_owned_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pikvm_agent.harness.config.shutil.which",
        lambda value: "/usr/bin/gcloud" if value == "gcloud" else None,
    )
    settings = HarnessSettings(
        providers={
            "vertex": {
                "kind": "vertex_gemini",
                "model": "gemini-model",
                "base_url": (
                    "https://aiplatform.googleapis.com/v1/projects/"
                    "test-project/locations/global/publishers/google"
                ),
                "auth_mode": "bearer_command",
                "credential_argv": [
                    "gcloud",
                    "auth",
                    "print-access-token",
                ],
                "inherited_env": ["PATH", "HOME", "CLOUDSDK_CONFIG"],
            },
        },
        routes={
            "reasoner": ["vertex"],
            "controller": ["vertex"],
            "verifier": ["vertex"],
        },
    )

    pool = build_model_pool(settings)
    provider = pool.providers["vertex"]
    prerequisites = check_provider_prerequisites(settings)

    assert isinstance(provider, GeminiApiProvider)
    assert isinstance(provider.auth, CommandBearerAuth)
    assert provider.auth.argv == [
        "gcloud",
        "auth",
        "print-access-token",
    ]
    assert pool.health()["vertex"]["interface"] == (
        "Vertex AI Gemini generateContent"
    )
    assert pool.health()["vertex"]["auth_mode"] == "bearer_command"
    assert pool.health()["vertex"]["credential_source"] == "gcloud"
    assert prerequisites["vertex"]["credential"] == "owned-by-cli"
    assert prerequisites["vertex"]["executable"] == "gcloud"


@pytest.mark.parametrize("auth_mode", ["api_key", None])
def test_vertex_gemini_rejects_unsupported_or_missing_auth(
    auth_mode: str | None,
) -> None:
    provider: dict[str, object] = {
        "kind": "vertex_gemini",
        "model": "gemini-model",
        "base_url": (
            "https://aiplatform.googleapis.com/v1/projects/test-project/"
            "locations/global/publishers/google"
        ),
    }
    if auth_mode is not None:
        provider["auth_mode"] = auth_mode
        provider["credential_env"] = "VERTEX_KEY"

    with pytest.raises(ValueError, match="vertex_gemini"):
        HarnessSettings(
            providers={"vertex": provider},
            routes={
                "reasoner": ["vertex"],
                "controller": ["vertex"],
                "verifier": ["vertex"],
            },
        )


def test_explicit_versioned_prices_build_an_exact_run_cost_policy() -> None:
    settings = HarnessSettings(
        providers={
            "oauth": {
                "kind": "codex_cli",
                "model": "account-default",
                "billing": {"mode": "subscription"},
            },
            "metered": {
                "kind": "openai_responses",
                "model": "gpt-model",
                "api_key_env": "TEST_OPENAI_KEY",
                "billing": {
                    "mode": "metered",
                    "reservation_usd": "0.25",
                    "usage_usd_per_million": {
                        "input_tokens": "2.50",
                        "output_tokens": "10.00",
                    },
                },
            },
        },
        routes={
            "reasoner": ["oauth"],
            "controller": ["metered"],
            "verifier": ["metered"],
        },
        model_budget={
            "max_provider_attempts_per_run": 40,
            "max_cost_usd_per_run": "1.50",
            "pricing_version": "customer-prices-2026-07-26",
        },
    )

    policy = build_model_budget_policy(settings)

    assert policy.max_provider_attempts == 40
    assert policy.max_cost_microusd == 1_500_000
    assert policy.pricing_version == "customer-prices-2026-07-26"
    assert policy.provider_costs["oauth"] == ProviderCostTerms.subscription()
    assert policy.provider_costs["metered"] == ProviderCostTerms.metered(
        reservation_microusd=250_000,
        usage_usd_per_million={
            "input_tokens": "2.50",
            "output_tokens": "10.00",
        },
    )


def test_cost_cap_refuses_unversioned_or_unclassified_routes() -> None:
    provider = {
        "kind": "subprocess_json",
        "model": "local",
        "argv": ["model-bridge"],
    }
    routes = {
        "reasoner": ["model"],
        "controller": ["model"],
        "verifier": ["model"],
    }

    with pytest.raises(ValueError, match="pricing_version"):
        HarnessSettings(
            providers={"model": provider},
            routes=routes,
            model_budget={"max_cost_usd_per_run": "1.00"},
        )

    with pytest.raises(ValueError, match="billing classification"):
        HarnessSettings(
            providers={"model": provider},
            routes=routes,
            model_budget={
                "max_cost_usd_per_run": "1.00",
                "pricing_version": "prices-v1",
            },
        )


def test_operator_and_model_side_observer_tokens_must_be_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = HarnessSettings(
        providers={
            "model": {
                "kind": "subprocess_json",
                "model": "local",
                "argv": ["model-bridge"],
            }
        },
        routes={
            "reasoner": ["model"],
            "controller": ["model"],
            "verifier": ["model"],
        },
    )
    monkeypatch.setenv("PIKVM_HARNESS_TOKEN", "same-token-" + "x" * 32)
    monkeypatch.setenv("PIKVM_HARNESS_AGENT_TOKEN", "agent-token-" + "z" * 32)
    monkeypatch.setenv(
        "PIKVM_HARNESS_OBSERVER_TOKEN", "same-token-" + "x" * 32
    )

    with pytest.raises(ValueError, match="must differ"):
        settings.observer_token()

    monkeypatch.setenv(
        "PIKVM_HARNESS_AGENT_TOKEN", "same-token-" + "x" * 32
    )
    monkeypatch.setenv(
        "PIKVM_HARNESS_OBSERVER_TOKEN", "observer-token-" + "y" * 32
    )
    with pytest.raises(ValueError, match="must differ"):
        settings.agent_token()


def test_harness_settings_expose_a_bounded_autonomous_slice_budget() -> None:
    provider = {
        "kind": "subprocess_json",
        "model": "local",
        "argv": ["model-bridge"],
    }
    routes = {
        "reasoner": ["model"],
        "controller": ["model"],
        "verifier": ["model"],
    }

    settings = HarnessSettings(
        providers={"model": provider},
        routes=routes,
        max_autonomous_resumes=12,
    )

    assert settings.max_autonomous_resumes == 12
    with pytest.raises(ValueError):
        HarnessSettings(
            providers={"model": provider},
            routes=routes,
            max_autonomous_resumes=0,
        )


def test_remote_bind_is_refused_without_explicit_secure_deployment_flag() -> None:
    settings = HarnessSettings(
        listen="0.0.0.0:47616",
        providers={
            "model": {
                "kind": "subprocess_json",
                "model": "local",
                "argv": ["model-bridge"],
            }
        },
        routes={
            "reasoner": ["model"],
            "controller": ["model"],
            "verifier": ["model"],
        },
    )

    with pytest.raises(ValueError, match="non-loopback"):
        ensure_safe_bind(settings)


def test_provider_prerequisites_report_presence_without_exposing_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = HarnessSettings(
        providers={
            "codex": {
                "kind": "codex_cli",
                "model": "account-default",
                "executable": "codex-test",
            },
            "gemini": {
                "kind": "gemini_api",
                "model": "gemini-test",
                "api_key_env": "TEST_GEMINI_KEY",
            },
        },
        routes={
            "reasoner": ["codex"],
            "controller": ["gemini"],
            "verifier": ["gemini"],
        },
    )
    monkeypatch.setenv("TEST_GEMINI_KEY", "do-not-print-this")
    monkeypatch.setattr(
        "pikvm_agent.harness.config.shutil.which",
        lambda value: "/usr/bin/codex-test" if value == "codex-test" else None,
    )

    statuses = check_provider_prerequisites(settings)

    assert statuses == {
            "codex": {
                "kind": "codex_cli",
                "ready": True,
                "billing_mode": "unclassified",
                "support_tier": "stable",
                "implementation_contract": "first_party",
                "credential_owner": "provider_cli",
                "interface": "Codex exec",
            "pixel_input": "Native image attachment",
            "structured_output": "Strict JSON Schema",
            "auth_mode": "saved_cli_login",
            "credential_source": "codex-test",
            "executable": "codex-test",
            "credential": "owned-by-cli",
        },
            "gemini": {
                "kind": "gemini_api",
                "ready": True,
                "billing_mode": "unclassified",
                "support_tier": "stable",
                "implementation_contract": "first_party",
                "credential_owner": "harness_environment",
                "interface": "Gemini generateContent",
            "pixel_input": "Inline image data",
            "structured_output": "JSON Schema",
            "auth_mode": "api_key_env",
            "credential_source": "",
            "credential": "env-present",
            "credential_env": "TEST_GEMINI_KEY",
        },
    }
    assert "do-not-print-this" not in repr(statuses)
    ensure_provider_prerequisites(settings)


def test_provider_prerequisites_fail_closed_for_missing_binary_and_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = HarnessSettings(
        providers={
            "bridge": {
                "kind": "subprocess_json",
                "model": "local",
                "argv": ["missing-bridge"],
            },
            "anthropic": {
                "kind": "anthropic_api",
                "model": "claude-test",
                "api_key_env": "MISSING_ANTHROPIC_KEY",
            },
        },
        routes={
            "reasoner": ["bridge"],
            "controller": ["anthropic"],
            "verifier": ["anthropic"],
        },
    )
    monkeypatch.delenv("MISSING_ANTHROPIC_KEY", raising=False)
    monkeypatch.setattr(
        "pikvm_agent.harness.config.shutil.which", lambda _value: None
    )

    statuses = check_provider_prerequisites(settings)

    assert statuses["bridge"]["ready"] is False
    assert statuses["bridge"]["error"] == "executable-not-found"
    assert statuses["anthropic"]["ready"] is False
    assert statuses["anthropic"]["credential"] == "env-missing"
    with pytest.raises(
        ValueError,
        match="no ready provider for role routes: controller, reasoner, verifier",
    ):
        ensure_provider_prerequisites(settings)


def test_provider_prerequisites_allow_unavailable_fallbacks_when_each_role_is_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = HarnessSettings(
        providers={
            "ready": {
                "kind": "codex_cli",
                "model": "account-default",
            },
            "optional-api": {
                "kind": "gemini_api",
                "model": "gemini-test",
                "api_key_env": "OPTIONAL_GEMINI_KEY",
            },
        },
        routes={
            "reasoner": ["optional-api", "ready"],
            "controller": ["ready", "optional-api"],
            "verifier": ["optional-api", "ready"],
        },
    )
    monkeypatch.delenv("OPTIONAL_GEMINI_KEY", raising=False)
    monkeypatch.setattr(
        "pikvm_agent.harness.config.shutil.which",
        lambda value: "/usr/bin/codex" if value == "codex" else None,
    )

    ensure_provider_prerequisites(settings)
    health = build_model_pool(settings).health()

    assert health["optional-api"]["ready"] is False
    assert health["optional-api"]["readiness_error"] == (
        "credential-env-missing"
    )
    assert health["optional-api"]["interface"] == "Gemini generateContent"
    assert health["optional-api"]["pixel_input"] == "Inline image data"
    assert health["optional-api"]["support_tier"] == "stable"
    assert health["optional-api"]["credential_owner"] == "harness_environment"
    assert health["optional-api"]["routes"] == [
        {"role": "reasoner", "position": 1},
        {"role": "controller", "position": 2},
        {"role": "verifier", "position": 1},
    ]
    assert health["ready"]["ready"] is True
    assert health["ready"]["interface"] == "Codex exec"
    assert health["ready"]["credential"] == "owned-by-cli"
    assert health["ready"]["credential_owner"] == "provider_cli"


def test_sensitive_provider_headers_are_rejected() -> None:
    with pytest.raises(ValueError, match="secret-bearing header"):
        HarnessSettings(
            providers={
                "gateway": {
                    "kind": "openai_compatible",
                    "model": "model",
                    "base_url": "https://gateway.example/v1",
                    "api_key_env": "GATEWAY_KEY",
                    "headers": {"Authorization": "Bearer inline-secret"},
                }
            },
            routes={
                "reasoner": ["gateway"],
                "controller": ["gateway"],
                "verifier": ["gateway"],
            },
        )
