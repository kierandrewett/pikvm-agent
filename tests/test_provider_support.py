from __future__ import annotations

from pathlib import Path
from typing import get_args

from pikvm_agent.harness.config import (
    HarnessSettings,
    ProviderSpec,
    check_provider_prerequisites,
)
from pikvm_agent.harness.provider_support import (
    PROVIDER_SUPPORT,
    ProviderSupport,
    provider_support,
)


EXPECTED_SUPPORT_TIERS = {
    "subprocess_json": "bridge",
    "codex_cli": "stable",
    "claude_cli": "stable",
    "gemini_cli": "beta",
    "openai_compatible": "bridge",
    "openai_responses": "stable",
    "azure_openai_responses": "beta",
    "anthropic_api": "stable",
    "gemini_api": "stable",
    "vertex_gemini": "beta",
}


def test_every_configurable_provider_has_one_canonical_support_contract() -> None:
    configured_kinds = set(get_args(ProviderSpec.model_fields["kind"].annotation))

    assert configured_kinds == set(PROVIDER_SUPPORT)
    assert {
        kind: support.support_tier
        for kind, support in PROVIDER_SUPPORT.items()
    } == EXPECTED_SUPPORT_TIERS
    assert all(
        isinstance(support, ProviderSupport)
        for support in PROVIDER_SUPPORT.values()
    )


def test_provider_support_contracts_define_auth_ownership_without_secrets() -> None:
    assert provider_support("codex_cli").auth_owner(
        "saved_cli_login"
    ) == "provider_cli"
    assert provider_support("openai_responses").auth_owner(
        "api_key_env"
    ) == "harness_environment"
    assert provider_support("azure_openai_responses").auth_owner(
        "bearer_command"
    ) == "provider_cli"
    assert provider_support("subprocess_json").auth_owner(
        "external_or_none"
    ) == "external_bridge"

    serialized = repr(PROVIDER_SUPPORT)
    assert "api_key_env" in serialized
    assert "API_KEY" not in serialized
    assert "token" not in serialized.lower()


def test_provider_readiness_metadata_comes_from_the_support_contract(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "pikvm_agent.harness.config.shutil.which",
        lambda value: f"/usr/bin/{value}",
    )
    settings = HarnessSettings(
        providers={
            "codex": {
                "kind": "codex_cli",
                "model": "account-default",
            }
        },
        routes={
            "reasoner": ["codex"],
            "controller": ["codex"],
            "verifier": ["codex"],
        },
    )

    metadata = check_provider_prerequisites(settings)["codex"]
    contract = provider_support("codex_cli")

    assert metadata["support_tier"] == contract.support_tier
    assert metadata["implementation_contract"] == contract.implementation_contract
    assert metadata["credential_owner"] == "provider_cli"
    assert metadata["interface"] == contract.interface
    assert metadata["pixel_input"] == contract.pixel_input
    assert metadata["structured_output"] == contract.structured_output


def test_public_provider_policy_covers_the_canonical_catalog() -> None:
    policy = (
        Path(__file__).parents[1] / "docs" / "PROVIDER_SUPPORT.md"
    ).read_text()

    for kind, support in PROVIDER_SUPPORT.items():
        assert f"`{kind}`" in policy
        assert f"`{support.support_tier}`" in policy
    assert "Readiness is not compatibility" in policy
    assert "never reads or copies a saved CLI credential" in policy
    assert "one minor release" in policy


def test_operator_ui_explains_support_contract_without_overclaiming() -> None:
    model_picker = (
        Path(__file__).parents[1]
        / "frontend"
        / "src"
        / "components"
        / "workspace"
        / "model-picker.tsx"
    ).read_text()
    javascript = (
        Path(__file__).parents[1]
        / "pikvm_agent"
        / "harness_ui"
        / "app.js"
    ).read_text()

    assert "health.support_tier" in model_picker
    assert "health.credential_owner" in model_picker
    assert "Tier ≠ live-tested" in model_picker
    assert "Tier ≠ live-tested" in javascript
