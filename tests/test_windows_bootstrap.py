import pytest

from pikvm_agent.harness.bootstrap_windows import (
    build_bootstrap_command,
    build_bootstrap_commands,
)


def test_bootstrap_command_downloads_and_starts_observer_without_base64() -> None:
    command = build_bootstrap_command(
        public_base_url="https://observer.lab.example",
        token="0123456789abcdef",
    )

    assert (
        "irm https://observer.lab.example/observer.exe"
        "?token=0123456789abcdef" in command
    )
    assert "--callback" in command
    assert "https://observer.lab.example/ingest" in command
    assert "--token" in command
    assert "FromBase64String" not in command
    assert "YWJj" not in command


def test_bootstrap_command_rejects_non_https_callback() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        build_bootstrap_command(
            public_base_url="http://example.test",
            token="test-token",
        )


def test_bootstrap_command_is_a_single_powershell_statement() -> None:
    command = build_bootstrap_command(
        public_base_url="https://temporary.example",
        token="runtime-token",
    )

    assert "\n" not in command
    assert "\r" not in command
    # Display-only composition; deployment submits every semicolon-delimited
    # command independently and each remains below the stricter 180-char cap.
    assert len(command) < 512


def test_deployment_uses_short_quote_free_commands() -> None:
    commands = build_bootstrap_commands(
        public_base_url="https://temporary.example",
        token="runtime-token",
    )

    assert len(commands) == 9
    assert commands[:6] == [
        "taskkill /IM observer.exe /F",
        "taskkill /IM observer-v4.exe /F",
        "taskkill /IM observer-v3.exe /F",
        "taskkill /IM observer-v2compact.exe /F",
        "taskkill /IM observer-v2.exe /F",
        "taskkill /IM pikvm-accuracy-observer.exe /F",
    ]
    assert max(map(len, commands)) < 180
    assert all("'" not in command and '"' not in command for command in commands)


def test_visual_mode_download_has_no_callback_or_token() -> None:
    commands = build_bootstrap_commands(
        artifact_url="https://artifacts.example/observer-v1.exe",
    )

    assert commands[-2].startswith("irm https://artifacts.example/")
    assert commands[-1] == "& C:/PiKVM-Harness/observer.exe"
    assert "--callback" not in ";".join(commands)
    assert "--token" not in ";".join(commands)


def test_visual_mode_rejects_non_https_artifact() -> None:
    with pytest.raises(ValueError, match="artifact.*HTTPS"):
        build_bootstrap_commands(
            artifact_url="http://artifacts.example/observer.exe",
        )


def test_bootstrap_can_select_a_runtime_only_workspace_artifact() -> None:
    commands = build_bootstrap_commands(
        artifact_url="https://artifacts.example/observer.exe",
        file_path=(
            r"C:\PiKVM-Harness\workspace\quarterly-earnings.xlsx"
        ),
    )

    assert commands[-1].endswith(
        ' --file "C:/PiKVM-Harness/workspace/quarterly-earnings.xlsx"'
    )
    assert len(commands[-1]) < 180


def test_reuse_installed_observer_restarts_it_with_the_fresh_artifact_path() -> None:
    commands = build_bootstrap_commands(
        reuse_installed=True,
        file_path=(
            r"C:\PiKVM-Harness\workspace\quarterly-earnings-a1b2c3d4e5f60718.xlsx"
        ),
    )

    joined = ";".join(commands)
    assert "irm " not in joined
    assert "http://" not in joined
    assert "https://" not in joined
    assert "FromBase64String" not in joined
    assert (
        "if(!(Test-Path C:/PiKVM-Harness/observer.exe)){throw 2}"
        in commands
    )
    assert commands[-1] == (
        "& C:/PiKVM-Harness/observer.exe --file "
        '"C:/PiKVM-Harness/workspace/'
        'quarterly-earnings-a1b2c3d4e5f60718.xlsx"'
    )
    assert max(map(len, commands)) < 180


def test_hidden_observer_launch_returns_from_powershell_and_closes_the_host() -> None:
    commands = build_bootstrap_commands(
        reuse_installed=True,
        file_path=(
            r"C:\PiKVM-Harness\workspace\shakespeare-essay-a1b2c3d4e5f60718.docx"
        ),
        visible=False,
    )

    assert commands[-2] == (
        "start C:/PiKVM-Harness/observer.exe "
        '"--file C:/PiKVM-Harness/workspace/'
        'shakespeare-essay-a1b2c3d4e5f60718.docx" '
        "-WindowStyle Hidden"
    )
    assert commands[-1] == "exit"
    assert "& C:/PiKVM-Harness/observer.exe" not in ";".join(commands)


@pytest.mark.parametrize(
    "path",
    (
        "C:/Users/test/private.docx",
        "C:/PiKVM-Harness/workspace/../private.docx",
        'C:/PiKVM-Harness/workspace/bad"name.docx',
    ),
)
def test_bootstrap_refuses_artifact_paths_outside_the_lab_workspace(
    path: str,
) -> None:
    with pytest.raises(ValueError, match="safe absolute path"):
        build_bootstrap_commands(
            artifact_url="https://artifacts.example/observer.exe",
            file_path=path,
        )
