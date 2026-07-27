from pathlib import Path

import httpx
import pytest
import yaml

from pikvm_agent.harness.agent_store import InMemoryRunStore
from pikvm_agent.harness.api import create_harness_app
from pikvm_agent.harness.config import (
    HarnessSettings,
    build_model_pool,
)
from pikvm_agent.harness.provider_connections import (
    ProviderConnectionConflict,
    ProviderConnectionManager,
    ProviderConnectionPolicyConflict,
    ProviderConnectionRequest,
)


def _settings(tmp_path: Path) -> HarnessSettings:
    return HarnessSettings.model_validate(
        {
            "listen": "127.0.0.1:47616",
            "state_path": str(tmp_path / "state.sqlite3"),
            "artifact_dir": str(tmp_path / "artifacts"),
            "providers": {
                "codex-account": {
                    "kind": "codex_cli",
                    "model": "account-default",
                }
            },
            "routes": {
                "reasoner": ["codex-account"],
                "controller": ["codex-account"],
                "verifier": ["codex-account"],
            },
        }
    )


def _write_config(path: Path, settings: HarnessSettings) -> None:
    path.write_text(
        yaml.safe_dump(
            settings.model_dump(
                mode="json",
                exclude_none=True,
                exclude_defaults=True,
            ),
            sort_keys=False,
        )
    )


@pytest.mark.asyncio
async def test_adds_api_provider_without_accepting_or_persisting_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    config_path = tmp_path / "harness.yaml"
    _write_config(config_path, settings)
    monkeypatch.setenv("TEST_OPENAI_KEY", "provider-secret-must-not-persist")
    pool = build_model_pool(settings)
    manager = ProviderConnectionManager(
        settings=settings,
        settings_path=config_path,
        models=pool,
    )

    result = await manager.connect(
        ProviderConnectionRequest(
            alias="openai-fast",
            kind="openai_responses",
            model="gpt-5-mini",
            credential_env="TEST_OPENAI_KEY",
        )
    )

    assert result.provider == "openai-fast"
    assert result.ready is True
    assert result.credential_owner == "harness_environment"
    assert result.secret_received is False
    assert "openai-fast" in pool.providers
    assert pool.health()["openai-fast"]["ready"] is True
    rendered = config_path.read_text()
    assert "TEST_OPENAI_KEY" in rendered
    assert "provider-secret-must-not-persist" not in rendered
    assert config_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_adds_missing_environment_provider_as_setup_needed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    config_path = tmp_path / "harness.yaml"
    _write_config(config_path, settings)
    monkeypatch.delenv("ANTHROPIC_TEST_KEY", raising=False)
    pool = build_model_pool(settings)
    manager = ProviderConnectionManager(
        settings=settings,
        settings_path=config_path,
        models=pool,
    )

    result = await manager.connect(
        ProviderConnectionRequest(
            alias="anthropic-review",
            kind="anthropic_api",
            model="claude-opus-4-8",
            credential_env="ANTHROPIC_TEST_KEY",
        )
    )

    assert result.ready is False
    assert result.readiness_error == "credential-env-missing"
    assert "anthropic-review" in pool.health()
    assert pool.health()["anthropic-review"]["ready"] is False


@pytest.mark.asyncio
async def test_refuses_overwrite_and_leaves_config_unchanged(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    config_path = tmp_path / "harness.yaml"
    _write_config(config_path, settings)
    before = config_path.read_bytes()
    manager = ProviderConnectionManager(
        settings=settings,
        settings_path=config_path,
        models=build_model_pool(settings),
    )

    with pytest.raises(ProviderConnectionConflict):
        await manager.connect(
            ProviderConnectionRequest(
                alias="codex-account",
                kind="codex_cli",
                model="account-default",
            )
        )

    assert config_path.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("credential_env", "sk-live-secret-value"),
        ("model", "sk-live-secret-value"),
        ("model", "model with spaces"),
        ("base_url", "https://user:password@example.test/v1"),
        ("base_url", "https://api.example.test/v1/sk-live-secret-value"),
    ],
)
def test_request_shape_refuses_secret_like_or_ambiguous_configuration(
    field: str,
    value: str,
) -> None:
    values = {
        "alias": "safe-provider",
        "kind": "openai_responses",
        "model": "gpt-5-mini",
        "credential_env": "OPENAI_API_KEY",
    }
    values[field] = value

    with pytest.raises(ValueError):
        ProviderConnectionRequest.model_validate(values)


def test_cli_connection_uses_provider_owned_login_without_secret_fields() -> None:
    request = ProviderConnectionRequest(
        alias="claude-secondary",
        kind="claude_cli",
        model="sonnet",
    )

    spec = request.provider_spec()

    assert spec.kind == "claude_cli"
    assert spec.api_key_env is None
    assert spec.credential_env is None
    assert spec.headers == {}


@pytest.mark.parametrize(
    ("kind", "base_url", "expected_argv"),
    [
        (
            "azure_openai_responses",
            "https://resource.openai.azure.com/openai/v1",
            [
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
        ),
        (
            "vertex_gemini",
            (
                "https://aiplatform.googleapis.com/v1/projects/test/"
                "locations/global/publishers/google"
            ),
            ["gcloud", "auth", "print-access-token"],
        ),
    ],
)
def test_cloud_oauth_connection_uses_a_fixed_provider_cli_command(
    kind: str,
    base_url: str,
    expected_argv: list[str],
) -> None:
    request = ProviderConnectionRequest(
        alias="cloud-oauth",
        kind=kind,  # type: ignore[arg-type]
        model="computer-use-model",
        base_url=base_url,
        auth_mode="bearer_command",
    )

    spec = request.provider_spec()

    assert spec.credential_argv == expected_argv
    assert spec.credential_env is None
    assert spec.api_key_env is None


def test_azure_api_key_connection_keeps_only_the_environment_reference() -> None:
    request = ProviderConnectionRequest(
        alias="azure-key",
        kind="azure_openai_responses",
        model="controller-deployment",
        base_url="https://resource.openai.azure.com/openai/v1",
        auth_mode="api_key",
        credential_env="AZURE_OPENAI_API_KEY",
    )

    spec = request.provider_spec()

    assert spec.auth_mode == "api_key"
    assert spec.credential_env == "AZURE_OPENAI_API_KEY"
    assert spec.credential_argv == []


@pytest.mark.parametrize(
    "values",
    [
        {
            "alias": "azure-oauth",
            "kind": "azure_openai_responses",
            "model": "deployment",
            "base_url": "https://resource.openai.azure.com/openai/v1",
            "auth_mode": "bearer_command",
            "credential_env": "AZURE_TOKEN",
        },
        {
            "alias": "vertex-key",
            "kind": "vertex_gemini",
            "model": "gemini-model",
            "base_url": (
                "https://aiplatform.googleapis.com/v1/projects/test/"
                "locations/global/publishers/google"
            ),
            "auth_mode": "api_key",
            "credential_env": "VERTEX_KEY",
        },
    ],
)
def test_cloud_connection_rejects_ambiguous_or_unsupported_auth(
    values: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        ProviderConnectionRequest.model_validate(values)


@pytest.mark.asyncio
async def test_cost_capped_harness_requires_reviewed_billing_configuration(
    tmp_path: Path,
) -> None:
    base = _settings(tmp_path)
    raw = base.model_dump(mode="python")
    raw["providers"]["codex-account"]["billing"] = {
        "mode": "subscription"
    }
    raw["model_budget"] = {
        "max_cost_usd_per_run": "2.00",
        "pricing_version": "2026-07-27",
    }
    settings = HarnessSettings.model_validate(raw)
    config_path = tmp_path / "harness.yaml"
    _write_config(config_path, settings)
    before = config_path.read_bytes()
    manager = ProviderConnectionManager(
        settings=settings,
        settings_path=config_path,
        models=build_model_pool(settings),
    )

    with pytest.raises(
        ProviderConnectionPolicyConflict,
        match="reviewed billing terms",
    ):
        await manager.connect(
            ProviderConnectionRequest(
                alias="openai-fast",
                kind="openai_responses",
                model="gpt-5-mini",
                credential_env="OPENAI_API_KEY",
            )
        )

    assert config_path.read_bytes() == before


@pytest.mark.asyncio
async def test_operator_api_connects_provider_and_refuses_alias_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    config_path = tmp_path / "harness.yaml"
    _write_config(config_path, settings)
    monkeypatch.setenv("OPENAI_TEST_KEY", "never-return-this-secret")
    pool = build_model_pool(settings)
    manager = ProviderConnectionManager(
        settings=settings,
        settings_path=config_path,
        models=pool,
    )
    app = create_harness_app(
        harness=object(),  # type: ignore[arg-type]
        store=InMemoryRunStore(),
        models=pool,
        access_token="operator-" + "a" * 32,
        agent_token="agent-" + "b" * 32,
        allowed_origins={"http://harness"},
        provider_connections=manager,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
        headers={"authorization": f"Bearer {'operator-' + 'a' * 32}"},
    ) as client:
        created = await client.post(
            "/api/providers",
            json={
                "alias": "openai-fast",
                "kind": "openai_responses",
                "model": "gpt-5-mini",
                "credential_env": "OPENAI_TEST_KEY",
            },
        )
        duplicate = await client.post(
            "/api/providers",
            json={
                "alias": "openai-fast",
                "kind": "openai_responses",
                "model": "gpt-5-mini",
                "credential_env": "OPENAI_TEST_KEY",
            },
        )
        health = await client.get("/api/providers")

    assert created.status_code == 201
    assert created.json()["ready"] is True
    assert created.json()["secret_received"] is False
    assert "never-return-this-secret" not in created.text
    assert duplicate.status_code == 409
    assert health.json()["openai-fast"]["ready"] is True


@pytest.mark.asyncio
async def test_model_agent_credential_cannot_configure_providers(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    config_path = tmp_path / "harness.yaml"
    _write_config(config_path, settings)
    pool = build_model_pool(settings)
    app = create_harness_app(
        harness=object(),  # type: ignore[arg-type]
        store=InMemoryRunStore(),
        models=pool,
        access_token="operator-" + "a" * 32,
        agent_token="agent-" + "b" * 32,
        allowed_origins={"http://harness"},
        provider_connections=ProviderConnectionManager(
            settings=settings,
            settings_path=config_path,
            models=pool,
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
        headers={"authorization": f"Bearer {'agent-' + 'b' * 32}"},
    ) as client:
        response = await client.post(
            "/api/providers",
            json={
                "alias": "claude-secondary",
                "kind": "claude_cli",
                "model": "sonnet",
            },
        )

    assert response.status_code == 401
    assert "claude-secondary" not in config_path.read_text()
