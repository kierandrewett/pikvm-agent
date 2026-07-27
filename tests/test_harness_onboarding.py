from __future__ import annotations

import yaml
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.onboarding import (
    build_initial_harness_settings,
    render_initial_harness_config,
)


def test_auto_onboarding_combines_available_oauth_and_selected_api_routes() -> None:
    settings = build_initial_harness_settings(
        oauth_clis="auto",
        executable_lookup=lambda name: (
            "/usr/bin/codex" if name == "codex" else None
        ),
        openai_model="gpt-example",
        anthropic_model="claude-example",
    )

    assert list(settings.providers) == [
        "codex-account",
        "openai-api",
        "anthropic-api",
    ]
    assert settings.providers["codex-account"].kind == "codex_cli"
    assert settings.providers["codex-account"].billing is None
    assert settings.model_budget.max_provider_attempts_per_run == 500
    assert settings.providers["openai-api"].api_key_env == "OPENAI_API_KEY"
    assert list(settings.assistant_tools) == ["web"]
    assert settings.assistant_tools["web"].command == "ddgs"
    assert settings.assistant_tools["web"].read_only_tools == [
        "search_text",
        "search_news",
        "extract_content",
    ]
    assert settings.routes.controller[0] == "openai-api"
    assert settings.routes.reasoner[0] == "anthropic-api"
    assert settings.routes.verifier[0] == "openai-api"
    assert all(
        set(getattr(settings.routes, role)) == set(settings.providers)
        for role in ("reasoner", "controller", "verifier")
    )


def test_rendered_onboarding_config_contains_names_not_secret_values() -> None:
    settings = build_initial_harness_settings(
        oauth_clis="both",
        executable_lookup=lambda _name: None,
        gemini_model="gemini-example",
    )
    rendered = render_initial_harness_config(settings)
    parsed = yaml.safe_load(rendered)

    assert parsed["agent_token_env"] == "PIKVM_HARNESS_AGENT_TOKEN"
    assert parsed["providers"]["gemini-api"]["api_key_env"] == "GEMINI_API_KEY"
    assert "secret-value" not in rendered
    assert "api_key" not in parsed["providers"]["gemini-api"]
    assert parsed["routes"]["controller"][0] == "gemini-api"


def test_auto_onboarding_adds_gemini_only_with_a_dedicated_profile() -> None:
    settings = build_initial_harness_settings(
        oauth_clis="auto",
        executable_lookup=lambda name: (
            f"/usr/bin/{name}" if name in {"codex", "gemini"} else None
        ),
        environment_lookup=lambda name: (
            "/srv/pikvm/gemini-profile"
            if name == "PIKVM_GEMINI_CLI_HOME"
            else None
        ),
    )

    assert list(settings.providers) == [
        "codex-account",
        "gemini-account",
    ]
    assert settings.providers["gemini-account"].kind == "gemini_cli"
    assert (
        settings.providers["gemini-account"].profile_home_env
        == "PIKVM_GEMINI_CLI_HOME"
    )
    assert "gemini-account" in settings.routes.controller
    assert "gemini-account" in settings.routes.verifier


def test_explicit_gemini_onboarding_is_secret_free_before_profile_exists() -> None:
    settings = build_initial_harness_settings(
        oauth_clis="gemini",
        executable_lookup=lambda _name: None,
        environment_lookup=lambda _name: None,
        gemini_cli_home_env="TEST_GEMINI_PROFILE",
    )
    rendered = render_initial_harness_config(settings)
    parsed = yaml.safe_load(rendered)

    assert parsed["providers"]["gemini-account"] == {
        "kind": "gemini_cli",
        "model": "account-default",
        "profile_home_env": "TEST_GEMINI_PROFILE",
    }
    assert "/srv/private/profile" not in rendered
    assert settings.routes.reasoner == ["gemini-account"]
    assert settings.routes.controller == ["gemini-account"]
    assert settings.routes.verifier == ["gemini-account"]


def test_oauth_cli_selection_accepts_combinations_and_preserves_both_alias() -> None:
    combined = build_initial_harness_settings(
        oauth_clis="codex,gemini",
        executable_lookup=lambda _name: None,
        environment_lookup=lambda _name: None,
    )
    legacy = build_initial_harness_settings(
        oauth_clis="both",
        executable_lookup=lambda _name: None,
        environment_lookup=lambda _name: None,
    )

    assert list(combined.providers) == ["codex-account", "gemini-account"]
    assert list(legacy.providers) == [
        "codex-account",
        "claude-account",
        "claude-fast",
    ]
    assert legacy.providers["claude-fast"].kind == "claude_cli"
    assert legacy.providers["claude-fast"].model == "haiku"
    assert legacy.routes.reasoner == [
        "claude-account",
        "claude-fast",
        "codex-account",
    ]
    assert legacy.routes.controller == [
        "claude-account",
        "claude-fast",
        "codex-account",
    ]
    assert legacy.routes.verifier == [
        "claude-fast",
        "claude-account",
        "codex-account",
    ]


def test_onboarding_adds_azure_cli_owned_oauth_without_a_token_value() -> None:
    settings = build_initial_harness_settings(
        oauth_clis="none",
        azure_model="azure-deployment",
        azure_base_url=(
            "https://resource.openai.azure.com/openai/v1"
        ),
        azure_auth="azure-cli",
    )
    rendered = render_initial_harness_config(settings)
    parsed = yaml.safe_load(rendered)
    provider = parsed["providers"]["azure-openai"]

    assert provider["kind"] == "azure_openai_responses"
    assert provider["auth_mode"] == "bearer_command"
    assert provider["credential_argv"] == [
        "az",
        "account",
        "get-access-token",
        "--resource",
        "https://cognitiveservices.azure.com",
        "--query",
        "accessToken",
        "-o",
        "tsv",
    ]
    assert provider["inherited_env"] == [
        "PATH",
        "HOME",
        "AZURE_CONFIG_DIR",
    ]
    assert "entra-token-secret" not in rendered
    assert settings.routes.reasoner == ["azure-openai"]
    assert settings.routes.controller == ["azure-openai"]
    assert settings.routes.verifier == ["azure-openai"]


def test_onboarding_adds_vertex_gcloud_owned_oauth() -> None:
    settings = build_initial_harness_settings(
        oauth_clis="none",
        vertex_model="gemini-model",
        vertex_base_url=(
            "https://aiplatform.googleapis.com/v1/projects/test-project/"
            "locations/global/publishers/google"
        ),
        vertex_auth="gcloud",
    )
    provider = settings.providers["vertex-gemini"]

    assert provider.kind == "vertex_gemini"
    assert provider.auth_mode == "bearer_command"
    assert provider.credential_argv == [
        "gcloud",
        "auth",
        "print-access-token",
    ]
    assert provider.inherited_env == [
        "PATH",
        "HOME",
        "CLOUDSDK_CONFIG",
    ]
    assert settings.routes.reasoner == ["vertex-gemini"]
    assert settings.routes.controller == ["vertex-gemini"]
    assert settings.routes.verifier == ["vertex-gemini"]


def test_onboarding_refuses_an_empty_provider_set() -> None:
    try:
        build_initial_harness_settings(
            oauth_clis="auto",
            executable_lookup=lambda _name: None,
        )
    except ValueError as exc:
        assert "no providers" in str(exc)
    else:
        raise AssertionError("empty provider onboarding should fail")


def test_onboarding_can_disable_packaged_web_search() -> None:
    settings = build_initial_harness_settings(
        oauth_clis="codex",
        executable_lookup=lambda _name: None,
        web_search=False,
    )

    assert settings.assistant_tools == {}


def test_harness_init_writes_secret_free_config_without_overwriting(tmp_path) -> None:
    destination = tmp_path / "harness.yaml"
    runner = CliRunner()
    first = runner.invoke(
        app,
        [
            "harness",
            "init",
            "--out",
            str(destination),
            "--oauth-clis",
            "codex",
            "--openai-model",
            "gpt-example",
        ],
    )
    second = runner.invoke(
        app,
        [
            "harness",
            "init",
            "--out",
            str(destination),
            "--oauth-clis",
            "codex",
        ],
    )

    assert first.exit_code == 0
    assert destination.is_file()
    config = yaml.safe_load(destination.read_text())
    assert list(config["providers"]) == ["codex-account", "openai-api"]
    assert "OPENAI_API_KEY" in first.stdout
    assert "PIKVM_HARNESS_TOKEN" in first.stdout
    assert "Required to start chat" in first.stdout
    assert "Required only for computer control" in first.stdout
    assert second.exit_code == 1
    assert "already exists" in second.stderr


def test_harness_check_accepts_chat_without_a_computer_target(
    tmp_path,
    monkeypatch,
) -> None:
    destination = tmp_path / "harness.yaml"
    settings = build_initial_harness_settings(
        oauth_clis="codex",
        executable_lookup=lambda _name: "/usr/bin/codex",
        web_search=False,
    )
    destination.write_text(render_initial_harness_config(settings))
    monkeypatch.setenv(
        "PIKVM_HARNESS_TOKEN",
        "test-harness-token-0123456789abcdef",
    )
    monkeypatch.delenv("PIKVM_AGENT_DAEMON", raising=False)
    monkeypatch.delenv("PIKVM_HARNESS_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("PIKVM_HARNESS_OBSERVER_TOKEN", raising=False)
    monkeypatch.setattr(
        "pikvm_agent.harness.config.shutil.which",
        lambda value: "/usr/bin/codex" if value == "codex" else None,
    )

    result = CliRunner().invoke(
        app,
        ["harness", "check", "--config", str(destination)],
    )

    assert result.exit_code == 0
    body = yaml.safe_load(result.stdout)
    assert body["ok"] is True
    assert body["computer"] == {
        "configured": False,
        "ready": False,
    }


def test_harness_check_can_require_a_selected_computer(
    tmp_path,
    monkeypatch,
) -> None:
    destination = tmp_path / "harness.yaml"
    settings = build_initial_harness_settings(
        oauth_clis="codex",
        executable_lookup=lambda _name: "/usr/bin/codex",
        web_search=False,
    )
    destination.write_text(render_initial_harness_config(settings))
    monkeypatch.setenv(
        "PIKVM_HARNESS_TOKEN",
        "test-harness-token-0123456789abcdef",
    )
    monkeypatch.delenv("PIKVM_AGENT_DAEMON", raising=False)
    monkeypatch.setattr(
        "pikvm_agent.harness.config.shutil.which",
        lambda value: "/usr/bin/codex" if value == "codex" else None,
    )

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "check",
            "--config",
            str(destination),
            "--require-computer",
        ],
    )

    assert result.exit_code == 2
    assert "--require-computer" in result.stderr


def test_harness_init_exposes_azure_api_key_onboarding(tmp_path) -> None:
    destination = tmp_path / "harness.yaml"
    result = CliRunner().invoke(
        app,
        [
            "harness",
            "init",
            "--out",
            str(destination),
            "--oauth-clis",
            "none",
            "--azure-model",
            "azure-deployment",
            "--azure-base-url",
            "https://resource.openai.azure.com/openai/v1",
            "--azure-auth",
            "api-key",
            "--azure-credential-env",
            "CUSTOM_AZURE_KEY",
        ],
    )

    assert result.exit_code == 0
    config = yaml.safe_load(destination.read_text())
    provider = config["providers"]["azure-openai"]
    assert provider["auth_mode"] == "api_key"
    assert provider["credential_env"] == "CUSTOM_AZURE_KEY"
    assert "CUSTOM_AZURE_KEY" in result.stdout


def test_harness_init_exposes_vertex_token_environment_onboarding(
    tmp_path,
) -> None:
    destination = tmp_path / "harness.yaml"
    result = CliRunner().invoke(
        app,
        [
            "harness",
            "init",
            "--out",
            str(destination),
            "--oauth-clis",
            "none",
            "--vertex-model",
            "gemini-model",
            "--vertex-base-url",
            (
                "https://aiplatform.googleapis.com/v1/projects/"
                "test-project/locations/global/publishers/google"
            ),
            "--vertex-auth",
            "token-env",
            "--vertex-credential-env",
            "CUSTOM_GOOGLE_TOKEN",
        ],
    )

    assert result.exit_code == 0
    config = yaml.safe_load(destination.read_text())
    provider = config["providers"]["vertex-gemini"]
    assert provider["auth_mode"] == "bearer_env"
    assert provider["credential_env"] == "CUSTOM_GOOGLE_TOKEN"
    assert "CUSTOM_GOOGLE_TOKEN" in result.stdout
