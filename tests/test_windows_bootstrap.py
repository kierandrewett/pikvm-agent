import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from pikvm_agent.harness import bootstrap_windows
from pikvm_agent.harness.bootstrap_windows import (
    build_bootstrap_command,
    build_bootstrap_commands,
    deploy,
)
from pikvm_agent.harness.vnc_target_lease import (
    VncTargetAlreadyLeased,
    VncTargetLease,
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


def test_bootstrap_opens_powershell_without_assuming_taskbar_alignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pressed: list[str] = []
    typed: list[str] = []
    client = SimpleNamespace(keyPress=pressed.append)
    monkeypatch.setattr(bootstrap_windows.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(
        bootstrap_windows,
        "_type_paced",
        lambda _client, text, *, delay_s: typed.append(text),
    )

    bootstrap_windows._open_powershell(client, character_delay_s=0.05)

    assert pressed == [
        "esc",
        "super-r",
        "enter",
        "super-r",
        "enter",
    ]
    assert typed == [
        "taskkill /IM observer.exe /F",
        "powershell",
    ]


def test_deployment_does_not_steal_focus_from_the_new_powershell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pressed: list[str] = []
    typed: list[str] = []
    pointer_actions: list[tuple[object, ...]] = []
    client = SimpleNamespace(
        factory=SimpleNamespace(force_caps=False),
        protocol=SimpleNamespace(screen=SimpleNamespace(size=(1280, 800))),
        captureScreen=lambda *_args, **_kwargs: None,
        keyPress=pressed.append,
        mouseMove=lambda *args: pointer_actions.append(("move", *args)),
        mousePress=lambda *args: pointer_actions.append(("press", *args)),
        disconnect=lambda: None,
    )
    package = ModuleType("vncdotool")
    package.api = SimpleNamespace(  # type: ignore[attr-defined]
        connect=lambda *_args, **_kwargs: client,
        shutdown=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "vncdotool", package)
    monkeypatch.setenv("PIKVM_LAB_TARGET_LEASE_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap_windows.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(
        bootstrap_windows,
        "_type_paced",
        lambda _client, text, *, delay_s: typed.append(text),
    )

    deploy(
        endpoint="disposable.invalid:5900",
        artifact_url=None,
        password=None,
        username=None,
        reuse_installed=True,
    )

    assert pointer_actions == []
    assert pressed[:5] == [
        "esc",
        "super-r",
        "enter",
        "super-r",
        "enter",
    ]
    assert typed[:2] == [
        "taskkill /IM observer.exe /F",
        "powershell",
    ]
    assert "taskkill /IM observer.exe /F" in typed


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
        " --file C:/PiKVM-Harness/workspace/quarterly-earnings.xlsx"
    )
    assert '"' not in commands[-1]
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
        "C:/PiKVM-Harness/workspace/"
        "quarterly-earnings-a1b2c3d4e5f60718.xlsx"
    )
    assert max(map(len, commands)) < 180


def test_background_observer_launch_keeps_hotkey_windows_visible() -> None:
    commands = build_bootstrap_commands(
        reuse_installed=True,
        file_path=(
            r"C:\PiKVM-Harness\workspace\shakespeare-essay-a1b2c3d4e5f60718.docx"
        ),
        visible=False,
    )

    assert commands[-2] == (
        "start C:/PiKVM-Harness/observer.exe -ArgumentList "
        "'--file','C:/PiKVM-Harness/workspace/"
        "shakespeare-essay-a1b2c3d4e5f60718.docx'"
    )
    assert "-WindowStyle Hidden" not in commands[-2]
    assert '"' not in commands[-2]
    assert commands[-1] == "exit"
    assert "& C:/PiKVM-Harness/observer.exe" not in ";".join(commands)


def test_deployment_refuses_a_leased_target_before_vnc_connect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PIKVM_LAB_TARGET_LEASE_DIR", str(tmp_path))

    def unexpected_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("deployment must acquire the target lease first")

    package = ModuleType("vncdotool")
    package.api = SimpleNamespace(connect=unexpected_connect)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "vncdotool", package)

    lease = VncTargetLease.acquire("leased.invalid:5900")
    try:
        with pytest.raises(
            VncTargetAlreadyLeased,
            match="already controlled by another local lab",
        ):
            deploy(
                endpoint="LEASED.invalid::5900",
                artifact_url=None,
                password=None,
                username=None,
                reuse_installed=True,
            )
    finally:
        lease.release()


def test_bootstrap_cli_can_restart_the_installed_observer_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        bootstrap_windows,
        "deploy",
        lambda **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap_windows.py",
            "--vnc",
            "disposable.invalid:5900",
            "--file",
            (
                "C:/PiKVM-Harness/workspace/"
                "shakespeare-essay-0123456789abcdef.docx"
            ),
            "--reuse-installed",
            "--hidden",
        ],
    )

    bootstrap_windows.main()

    assert captured["endpoint"] == "disposable.invalid:5900"
    assert captured["reuse_installed"] is True
    assert captured["visible"] is False


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
