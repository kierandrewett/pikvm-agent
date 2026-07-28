from __future__ import annotations

import asyncio
import io
import json
import socket
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.agent_models import RunStatus
from pikvm_agent.harness.in_guest_transport import InGuestComputerTransport
from pikvm_agent.harness.osworld_runner import (
    DockerCommandError,
    _docker,
    _run_bounded_harness,
    _start_osworld_container,
    docker_guest_endpoint,
    apply_official_postconfig,
    apply_official_setup,
    docker_run_args,
    evaluate_official_exact_match,
    is_docker_publish_failure,
    run_osworld_case,
    update_stagnant_cycle_count,
    validate_osworld_task_compatibility,
)
from pikvm_agent.harness.operator_console import (
    OperatorConsoleServer,
    operator_console_url,
    wait_for_operator_approval,
    write_operator_console_descriptor,
)
from pikvm_agent.harness.public_desktop_suites import discover_desktop_suite
from pikvm_agent.harness.public_desktop_suites import verify_checkout_revision


def _png(width: int = 800, height: int = 600) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (24, 32, 48)).save(output, "PNG")
    return output.getvalue()


def test_discovers_osworld_and_windows_agent_arena_from_official_layouts(
    tmp_path,
) -> None:
    osworld = tmp_path / "osworld"
    os_eval = osworld / "evaluation_examples"
    (os_eval / "examples" / "writer").mkdir(parents=True)
    (os_eval / "test_all.json").write_text(
        json.dumps({"writer": ["task-a"]}),
        encoding="utf-8",
    )
    (os_eval / "examples" / "writer" / "task-a.json").write_text(
        json.dumps({"id": "task-a", "instruction": "Write a line."}),
        encoding="utf-8",
    )

    winarena = tmp_path / "win-arena"
    win_eval = (
        winarena
        / "src"
        / "win-arena-container"
        / "client"
        / "evaluation_examples_windows"
    )
    (win_eval / "examples" / "notepad").mkdir(parents=True)
    (win_eval / "test_all.json").write_text(
        json.dumps({"notepad": ["task-b-WOS"]}),
        encoding="utf-8",
    )
    (win_eval / "examples" / "notepad" / "task-b-WOS.json").write_text(
        json.dumps({"id": "task-b-wos", "instruction": "Type a line."}),
        encoding="utf-8",
    )

    os_inventory = discover_desktop_suite(
        "osworld-verified",
        osworld,
        revision="os-revision",
    )
    win_inventory = discover_desktop_suite(
        "windows-agent-arena",
        winarena,
        revision="win-revision",
    )

    assert os_inventory.tasks_discovered == 1
    assert os_inventory.domains == {"writer": 1}
    assert os_inventory.tasks[0].instruction == "Write a line."
    assert win_inventory.tasks_discovered == 1
    assert win_inventory.domains == {"notepad": 1}
    assert win_inventory.tasks[0].task_id == "task-b-WOS"
    assert win_inventory.tasks[0].declared_id == "task-b-wos"
    assert len(win_inventory.integrity_warnings) == 1


def test_discovery_fails_closed_when_an_official_task_config_is_missing(
    tmp_path,
) -> None:
    evaluation = tmp_path / "evaluation_examples"
    evaluation.mkdir()
    (evaluation / "test_all.json").write_text(
        json.dumps({"writer": ["missing-task"]}),
        encoding="utf-8",
    )

    try:
        discover_desktop_suite(
            "osworld-verified",
            tmp_path,
            revision="revision",
        )
    except FileNotFoundError as exc:
        assert "missing-task" in str(exc)
    else:  # pragma: no cover - documents the fail-closed contract
        raise AssertionError("missing benchmark config was accepted")


def test_revision_evidence_fails_closed_on_checkout_mismatch(
    tmp_path, monkeypatch
) -> None:
    class Completed:
        stdout = "actual-sha\n"

    monkeypatch.setattr(
        "pikvm_agent.harness.public_desktop_suites.subprocess.run",
        lambda *args, **kwargs: Completed(),
    )

    try:
        verify_checkout_revision(tmp_path, "claimed-sha")
    except ValueError as exc:
        assert "actual-sha" in str(exc)
        assert "claimed-sha" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("mislabeled checkout was accepted")


async def test_in_guest_transport_translates_only_bounded_hid_templates() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/screenshot":
            return httpx.Response(
                200,
                content=_png(),
                headers={"content-type": "image/png"},
            )
        if request.method == "POST" and request.url.path == "/run_python":
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://guest.invalid",
    )
    transport = InGuestComputerTransport(
        "http://guest.invalid",
        client=client,
    )

    await transport.connect()
    await transport.key("KeyA", True)
    await transport.key("KeyA", False)
    for offset in range(50):
        await transport.mouse_move(123 + offset, 234 + offset)
    await transport.mouse_button("left", True)
    await transport.mouse_button("left", False)
    await transport.print_text("quote ' and newline\nnever submits")
    screenshot = await transport.screenshot()
    await transport.close()

    scripts = [
        json.loads(request.content)["code"]
        for request in requests
        if request.method == "POST"
    ]
    assert transport.width == 800
    assert transport.height == 600
    assert Image.open(io.BytesIO(screenshot)).size == (800, 600)
    assert any("keyDown('a')" in script for script in scripts)
    assert any("keyUp('a')" in script for script in scripts)
    assert sum("moveTo(" in script for script in scripts) == 1
    assert any("moveTo(172, 283" in script for script in scripts)
    assert any("mouseDown(button='left')" in script for script in scripts)
    assert any("pyautogui.write(" in script for script in scripts)
    assert all("press('enter')" not in script for script in scripts)
    assert all("subprocess" not in script for script in scripts)


async def test_in_guest_transport_retries_a_corrupt_screenshot() -> None:
    screenshot_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal screenshot_calls
        if request.method == "GET" and request.url.path == "/screenshot":
            screenshot_calls += 1
            content = b"broken-png" if screenshot_calls == 1 else _png(640, 480)
            return httpx.Response(200, content=content)
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://guest.invalid",
    )
    transport = InGuestComputerTransport(
        "http://guest.invalid",
        client=client,
    )

    screenshot = await transport.screenshot()
    await transport.close()

    assert screenshot_calls == 2
    assert Image.open(io.BytesIO(screenshot)).size == (640, 480)


def test_in_guest_lab_is_a_visible_cli_surface() -> None:
    result = CliRunner().invoke(app, ["lab", "in-guest-up", "--help"])

    assert result.exit_code == 0
    assert "--endpoint" in result.stdout
    assert "PIKVM_LAB_IN_GUEST" in result.stdout


def test_suite_inventory_is_a_visible_cli_surface() -> None:
    result = CliRunner().invoke(app, ["harness", "suite-inventory", "--help"])

    assert result.exit_code == 0
    assert "--suite" in result.stdout
    assert "--revision" in result.stdout
    assert "--out" in result.stdout


def test_osworld_case_is_a_visible_cli_surface() -> None:
    result = CliRunner().invoke(
        app,
        ["harness", "osworld-case", "--help"],
        terminal_width=200,
    )

    assert result.exit_code == 0
    assert "--qcow" in result.stdout
    assert "--docker-image" in result.stdout
    assert "--task-id" in result.stdout
    assert "--interactive-approv" in result.stdout
    assert "--operator-console" in result.stdout


def test_osworld_stagnation_counter_resets_only_after_action_progress() -> None:
    count, stopped = update_stagnant_cycle_count(
        previous_action_index=10,
        current_action_index=10,
        stagnant_cycles=0,
    )
    assert (count, stopped) == (1, False)

    count, stopped = update_stagnant_cycle_count(
        previous_action_index=10,
        current_action_index=10,
        stagnant_cycles=count,
    )
    assert (count, stopped) == (2, True)

    count, stopped = update_stagnant_cycle_count(
        previous_action_index=10,
        current_action_index=11,
        stagnant_cycles=count,
    )
    assert (count, stopped) == (0, False)


def test_osworld_stagnation_counter_does_not_treat_provider_outage_as_stall() -> None:
    count, stopped = update_stagnant_cycle_count(
        previous_action_index=10,
        current_action_index=10,
        stagnant_cycles=1,
        operational_outage=True,
    )

    assert (count, stopped) == (1, False)


async def test_osworld_stops_after_two_consecutive_transport_outages() -> None:
    class Store:
        saved = 0

        async def save(self, run):
            self.saved += 1

    class Harness:
        def __init__(self) -> None:
            self.store = Store()
            self.continue_calls = 0
            self.run = SimpleNamespace(
                run_id="run-id",
                status=RunStatus.PAUSED,
                next_action_index=4,
                events=[],
                error=None,
                record=lambda kind, **data: self.run.events.append(
                    SimpleNamespace(kind=kind, data=data)
                ),
            )

        async def start(self, task):
            return self.run

        async def continue_run(self, run_id):
            assert run_id == "run-id"
            self.continue_calls += 1
            self.run.events.append(
                SimpleNamespace(
                    kind="action.transport_uncertain",
                    data={"index": 4},
                )
            )
            return self.run

    harness = Harness()

    run, cycles, timed_out = await _run_bounded_harness(
        harness,
        "Perform the task.",
        max_cycles=20,
        max_run_time_s=30,
    )

    assert harness.continue_calls == 2
    assert cycles == 3
    assert timed_out is False
    assert run.status is RunStatus.PAUSED
    assert run.error == (
        "benchmark stopped after two consecutive operational outages"
    )
    assert run.events[-1].kind == "run.operational_outage_stopped"
    assert harness.store.saved == 1


async def test_osworld_coordinator_keeps_setup_and_evaluator_outside_mcp() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        command = payload["command"]
        if command == "gsettings get example":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "returncode": 0,
                    "output": "false\n",
                },
            )
        return httpx.Response(
            200,
            json={"status": "success", "returncode": 0, "output": ""},
        )

    task = {
        "config": [
            {
                "type": "execute",
                "parameters": {
                    "command": [
                        "python",
                        "-c",
                        "click({SCREEN_WIDTH_HALF}, {SCREEN_HEIGHT_HALF})",
                    ],
                    "shell": False,
                },
            }
        ],
        "evaluator": {
            "func": "exact_match",
            "result": {
                "type": "vm_command_line",
                "command": "gsettings get example",
                "shell": True,
            },
            "expected": {
                "type": "rule",
                "rules": {"expected": "false\n"},
            },
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://coordinator.invalid",
    ) as client:
        await apply_official_setup(client, task, width=1920, height=1080)
        score, evaluator = await evaluate_official_exact_match(client, task)

    assert payloads[0]["command"][-1] == "click(960, 540)"
    assert score == 1.0
    assert evaluator == "exact_match(vm_command_line, rule)"


async def test_osworld_launch_setup_uses_the_official_guest_endpoint() -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, text="success")

    task = {
        "config": [
            {
                "type": "launch",
                "parameters": {
                    "command": ["google-chrome", "--new-window"],
                    "shell": False,
                },
            }
        ],
        "evaluator": {
            "func": "exact_match",
            "result": {"type": "vm_command_line", "command": "true"},
            "expected": {"type": "rule", "rules": {"expected": ""}},
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://coordinator.invalid",
    ) as client:
        await apply_official_setup(client, task, width=1920, height=1080)

    assert requests == [
        (
            "/setup/launch",
            {"command": ["google-chrome", "--new-window"], "shell": False},
        )
    ]


async def test_osworld_open_setup_uses_the_official_guest_endpoint() -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, text="success")

    task = {
        "config": [
            {
                "type": "open",
                "parameters": {"path": "/home/user/Videos/input.mp4"},
            }
        ],
        "evaluator": {
            "func": "exact_match",
            "result": {"type": "vm_command_line", "command": "true"},
            "expected": {"type": "rule", "rules": {"expected": ""}},
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://coordinator.invalid",
    ) as client:
        await apply_official_setup(client, task, width=1920, height=1080)

    assert requests == [
        (
            "/setup/open_file",
            {"path": "/home/user/Videos/input.mp4"},
        )
    ]


async def test_osworld_activate_window_setup_preserves_official_parameters() -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, text="success")

    task = {
        "config": [
            {
                "type": "activate_window",
                "parameters": {
                    "window_name": "Visual Studio Code",
                    "strict": True,
                    "by_class": False,
                },
            }
        ],
        "evaluator": {
            "func": "exact_match",
            "result": {"type": "vm_command_line", "command": "true"},
            "expected": {"type": "rule", "rules": {"expected": ""}},
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://coordinator.invalid",
    ) as client:
        await apply_official_setup(client, task, width=1920, height=1080)

    assert requests == [
        (
            "/setup/activate_window",
            {
                "window_name": "Visual Studio Code",
                "strict": True,
                "by_class": False,
            },
        )
    ]


async def test_osworld_command_setup_uses_the_execute_contract() -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={"status": "success", "returncode": 0, "output": ""},
        )

    task = {
        "config": [
            {
                "type": "command",
                "parameters": {
                    "command": ["mkdir", "-p", "/home/user/Desktop/Projects"]
                },
            }
        ],
        "evaluator": {
            "func": "exact_match",
            "result": {"type": "vm_command_line", "command": "true"},
            "expected": {"type": "rule", "rules": {"expected": ""}},
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://coordinator.invalid",
    ) as client:
        await apply_official_setup(client, task, width=1920, height=1080)

    assert requests == [
        (
            "/execute",
            {"command": ["mkdir", "-p", "/home/user/Desktop/Projects"]},
        )
    ]


async def test_osworld_evaluator_postconfig_runs_before_scoring() -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={"status": "success", "returncode": 0, "output": ""},
        )

    task = {
        "config": [],
        "evaluator": {
            "postconfig": [
                {
                    "type": "command",
                    "parameters": {
                        "command": "echo {CLIENT_PASSWORD} | sudo -S pip install pysrt",
                        "shell": True,
                    },
                }
            ],
            "func": "exact_match",
            "result": {"type": "vm_command_line", "command": "true"},
            "expected": {"type": "rule", "rules": {"expected": ""}},
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://coordinator.invalid",
    ) as client:
        await apply_official_postconfig(
            client,
            task,
            width=1920,
            height=1080,
            client_password="benchmark-password",
        )

    assert requests == [
        (
            "/execute",
            {
                "command": (
                    "echo benchmark-password | sudo -S pip install pysrt"
                ),
                "shell": True,
            },
        )
    ]


async def test_osworld_download_setup_fetches_and_uploads_outside_mcp() -> None:
    downloaded: list[str] = []
    uploaded: list[bytes] = []

    def download_handler(request: httpx.Request) -> httpx.Response:
        downloaded.append(str(request.url))
        return httpx.Response(200, content=b"official-task-file")

    def guest_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/setup/upload"
        uploaded.append(request.content)
        return httpx.Response(200, text="File Uploaded: 18 bytes")

    task = {
        "config": [
            {
                "type": "download",
                "parameters": {
                    "files": [
                        {
                            "url": "https://download.invalid/poster.webp",
                            "path": "/home/user/Desktop/poster.webp",
                        }
                    ]
                },
            }
        ],
        "evaluator": {
            "func": "exact_match",
            "result": {
                "type": "vm_command_line",
                "command": "test -f /home/user/Desktop/poster.webp",
            },
            "expected": {
                "type": "rule",
                "rules": {"expected": ""},
            },
        },
    }
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(guest_handler),
            base_url="http://coordinator.invalid",
        ) as guest,
        httpx.AsyncClient(
            transport=httpx.MockTransport(download_handler),
        ) as downloader,
    ):
        await apply_official_setup(
            guest,
            task,
            width=1920,
            height=1080,
            download_client=downloader,
        )

    assert downloaded == ["https://download.invalid/poster.webp"]
    assert b'name="file_path"' in uploaded[0]
    assert b"/home/user/Desktop/poster.webp" in uploaded[0]
    assert b"official-task-file" in uploaded[0]


def test_osworld_preflight_rejects_unknown_setup_before_target_start() -> None:
    task = {
        "config": [{"type": "unsupported", "parameters": {}}],
        "evaluator": {
            "func": "exact_match",
            "result": {"type": "vm_command_line", "command": "true"},
            "expected": {"type": "rule", "rules": {"expected": ""}},
        },
    }

    with pytest.raises(ValueError, match="unsupported OSWorld setup type"):
        validate_osworld_task_compatibility(task)


async def test_osworld_official_or_evaluator_scores_any_exact_match() -> None:
    commands: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        command = " ".join(payload["command"])
        commands.append(command)
        output = "uint32 300\n" if "idle-delay" in command else "false\n"
        return httpx.Response(
            200,
            json={"status": "success", "returncode": 0, "output": output},
        )

    task = {
        "evaluator": {
            "func": ["exact_match", "exact_match"],
            "conj": "or",
            "result": [
                {
                    "type": "vm_command_line",
                    "command": ["gsettings", "get", "idle-delay"],
                },
                {
                    "type": "vm_command_line",
                    "command": ["gsettings", "get", "idle-dim"],
                },
            ],
            "expected": [
                {
                    "type": "rule",
                    "rules": {"expected": "uint32 0\n"},
                },
                {
                    "type": "rule",
                    "rules": {"expected": "false\n"},
                },
            ],
        }
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://coordinator.invalid",
    ) as client:
        score, evaluator = await evaluate_official_exact_match(client, task)

    assert commands == [
        "gsettings get idle-delay",
        "gsettings get idle-dim",
    ]
    assert score == 1.0
    assert evaluator == "or(2 x exact_match(vm_command_line, rule))"


async def test_osworld_exact_match_list_defaults_to_official_and_semantics() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        output = "first\n" if payload["command"] == ["read", "first"] else "wrong\n"
        return httpx.Response(
            200,
            json={"status": "success", "returncode": 0, "output": output},
        )

    task = {
        "evaluator": {
            "func": ["exact_match", "exact_match"],
            "result": [
                {"type": "vm_command_line", "command": ["read", "first"]},
                {"type": "vm_command_line", "command": ["read", "second"]},
            ],
            "expected": [
                {"type": "rule", "rules": {"expected": "first\n"}},
                {"type": "rule", "rules": {"expected": "second\n"}},
            ],
        }
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://coordinator.invalid",
    ) as client:
        score, evaluator = await evaluate_official_exact_match(client, task)

    assert score == 0.0
    assert evaluator == "and(2 x exact_match(vm_command_line, rule))"


async def test_osworld_official_utc_evaluator_matches_upstream_semantics() -> None:
    timedatectl = """\
               Local time: Sat 2026-07-25 06:30:00 GMT
           Universal time: Sat 2026-07-25 06:30:00 UTC
                 RTC time: Sat 2026-07-25 06:30:00
                Time zone: Etc/GMT (GMT, +0000)
System clock synchronized: yes
              NTP service: active
          RTC in local TZ: no
"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["command"] == "timedatectl status"
        return httpx.Response(
            200,
            json={"status": "success", "returncode": 0, "output": timedatectl},
        )

    task = {
        "evaluator": {
            "func": "is_utc_0",
            "result": {
                "type": "vm_command_line",
                "command": "timedatectl status",
                "shell": True,
            },
        }
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://coordinator.invalid",
    ) as client:
        score, evaluator = await evaluate_official_exact_match(client, task)

    assert score == 1.0
    assert evaluator == "is_utc_0(vm_command_line)"


def test_osworld_software_emulation_forwards_the_guest_api_explicitly(
    tmp_path,
) -> None:
    arguments = docker_run_args(
        container_name="case",
        qcow=tmp_path / "Ubuntu.qcow2",
        docker_image="image@sha256:digest",
        kvm_available=False,
    )

    assert "USER_PORTS=5000" in arguments
    assert "KVM=N" in arguments
    assert "/dev/kvm" not in arguments
    assert arguments[-1] == "image@sha256:digest"


def test_osworld_direct_bridge_fallback_avoids_docker_port_publishing(
    tmp_path,
) -> None:
    arguments = docker_run_args(
        container_name="case",
        qcow=tmp_path / "Ubuntu.qcow2",
        docker_image="image@sha256:digest",
        kvm_available=True,
        publish_guest_port=False,
    )

    assert "USER_PORTS=5000" in arguments
    assert "-p" not in arguments
    assert "--device" in arguments
    assert "/dev/kvm" in arguments


def test_osworld_recognizes_only_docker_publish_network_failures() -> None:
    publish_failure = DockerCommandError(
        ("run", "-p", "127.0.0.1::5000", "image"),
        returncode=125,
        stdout="",
        stderr=(
            "failed to set up container networking: Unable to enable DNAT "
            "rule: iptables: No chain/target/match by that name"
        ),
    )
    unrelated_failure = DockerCommandError(
        ("run", "image"),
        returncode=125,
        stdout="",
        stderr="invalid mount config for type bind",
    )

    assert is_docker_publish_failure(publish_failure) is True
    assert is_docker_publish_failure(unrelated_failure) is False
    assert "Unable to enable DNAT rule" in str(publish_failure)


def test_osworld_resolves_direct_bridge_guest_endpoint(monkeypatch) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_docker(*args: str, timeout: float = 600) -> str:
        commands.append(args)
        return "172.17.0.9"

    monkeypatch.setattr(
        "pikvm_agent.harness.osworld_runner._docker",
        fake_docker,
    )

    endpoint = docker_guest_endpoint(
        "container-id",
        access="direct_bridge",
    )

    assert endpoint == "http://172.17.0.9:5000"
    assert commands == [
        (
            "inspect",
            "container-id",
            "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
        )
    ]


def test_osworld_retries_publish_failure_on_private_bridge(
    monkeypatch,
    tmp_path,
) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_docker(*args: str, timeout: float = 600) -> str:
        commands.append(args)
        if len(commands) == 1:
            raise DockerCommandError(
                args,
                returncode=125,
                stdout="",
                stderr=(
                    "failed to set up container networking: Unable to enable "
                    "DNAT rule: iptables: No chain/target/match by that name"
                ),
            )
        return "container-id"

    monkeypatch.setattr(
        "pikvm_agent.harness.osworld_runner._docker",
        fake_docker,
    )

    container_id, access, reason = _start_osworld_container(
        container_name="case",
        qcow=tmp_path / "Ubuntu.qcow2",
        docker_image="image@sha256:digest",
        kvm_available=True,
        timeout_s=30,
    )

    assert container_id == "container-id"
    assert access == "direct_bridge"
    assert reason is not None and "Unable to enable DNAT rule" in reason
    assert "-p" in commands[0]
    assert "-p" not in commands[1]


def test_osworld_docker_error_preserves_stderr(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(
            125,
            args[0],
            output="",
            stderr="precise Docker failure",
        )

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(DockerCommandError) as captured:
        _docker("run", "image")

    assert captured.value.returncode == 125
    assert captured.value.stderr == "precise Docker failure"
    assert "precise Docker failure" in str(captured.value)


async def test_osworld_model_wall_budget_aborts_durable_run() -> None:
    continue_started = asyncio.Event()

    class Harness:
        aborted: list[tuple[str, str]] = []

        async def start(self, task):
            return SimpleNamespace(
                run_id="run-id",
                status=RunStatus.PAUSED,
                next_action_index=1,
            )

        async def continue_run(self, run_id):
            continue_started.set()
            await asyncio.Event().wait()

        async def abort(self, run_id, reason):
            self.aborted.append((run_id, reason))
            return SimpleNamespace(
                run_id=run_id,
                status=RunStatus.ABORTED,
                next_action_index=1,
            )

    harness = Harness()

    run, cycles, timed_out = await _run_bounded_harness(
        harness,
        "Perform the task.",
        max_cycles=20,
        max_run_time_s=0.01,
    )

    assert continue_started.is_set()
    assert run.status is RunStatus.ABORTED
    assert cycles == 1
    assert timed_out is True
    assert harness.aborted == [
        ("run-id", "OSWorld model wall-time budget reached")
    ]


async def test_osworld_interactive_approval_resumes_the_same_run() -> None:
    decisions: list[dict[str, str]] = []

    class Harness:
        async def start(self, task):
            return SimpleNamespace(
                run_id="run-id",
                status=RunStatus.NEEDS_APPROVAL,
                next_action_index=4,
                pending_approval={
                    "approval_id": "approval-id",
                    "risk": "terminal_mutating",
                },
                events=[],
            )

        async def resolve_approval(self, run_id, approval_id, decision):
            assert run_id == "run-id"
            assert approval_id == "approval-id"
            decisions.append(decision)
            return SimpleNamespace(
                run_id=run_id,
                status=RunStatus.COMPLETED,
                next_action_index=5,
                pending_approval=None,
                events=[],
            )

    async def approve(run):
        assert run.pending_approval["risk"] == "terminal_mutating"
        return {"type": "approve", "reason": "approved in isolated benchmark"}

    run, cycles, timed_out = await _run_bounded_harness(
        Harness(),
        "Perform the task.",
        max_cycles=20,
        max_run_time_s=30,
        approval_resolver=approve,
    )

    assert run.status is RunStatus.COMPLETED
    assert cycles == 2
    assert timed_out is False
    assert decisions == [
        {"type": "approve", "reason": "approved in isolated benchmark"}
    ]


async def test_osworld_without_resolver_leaves_approval_pending() -> None:
    class Harness:
        async def start(self, task):
            return SimpleNamespace(
                run_id="run-id",
                status=RunStatus.NEEDS_APPROVAL,
                next_action_index=4,
                pending_approval={"approval_id": "approval-id"},
                events=[],
            )

    run, cycles, timed_out = await _run_bounded_harness(
        Harness(),
        "Perform the task.",
        max_cycles=20,
        max_run_time_s=30,
    )

    assert run.status is RunStatus.NEEDS_APPROVAL
    assert cycles == 1
    assert timed_out is False


async def test_osworld_external_operator_console_can_resolve_approval() -> None:
    waiter_calls: list[str] = []

    class Harness:
        async def start(self, task):
            return SimpleNamespace(
                run_id="run-id",
                status=RunStatus.NEEDS_APPROVAL,
                next_action_index=4,
                pending_approval={"approval_id": "approval-id"},
                events=[],
            )

    async def wait_for_console(run):
        waiter_calls.append(run.pending_approval["approval_id"])
        return SimpleNamespace(
            run_id=run.run_id,
            status=RunStatus.COMPLETED,
            next_action_index=5,
            pending_approval=None,
            events=[],
        )

    run, cycles, timed_out = await _run_bounded_harness(
        Harness(),
        "Perform the task.",
        max_cycles=20,
        max_run_time_s=30,
        approval_waiter=wait_for_console,
    )

    assert run.status is RunStatus.COMPLETED
    assert cycles == 2
    assert timed_out is False
    assert waiter_calls == ["approval-id"]


async def test_osworld_prepared_console_run_uses_the_shared_execution_lock() -> None:
    prepared = SimpleNamespace(
        run_id="run-id",
        status=RunStatus.RUNNING,
        next_action_index=0,
        pending_approval=None,
        events=[],
    )
    locks: dict[str, asyncio.Lock] = {}

    class Harness:
        async def start(self, task):
            raise AssertionError("prepared console run must not be opened twice")

        async def continue_run(self, run_id):
            assert run_id == "run-id"
            assert locks[run_id].locked()
            return SimpleNamespace(
                run_id=run_id,
                status=RunStatus.COMPLETED,
                next_action_index=1,
                pending_approval=None,
                events=[],
            )

    run, cycles, timed_out = await _run_bounded_harness(
        Harness(),
        "Perform the task.",
        max_cycles=20,
        max_run_time_s=30,
        initial_run=prepared,
        run_locks=locks,
    )

    assert run.status is RunStatus.COMPLETED
    assert cycles == 1
    assert timed_out is False


async def test_operator_console_waiter_tracks_the_exact_pending_approval() -> None:
    pending = SimpleNamespace(
        run_id="run-id",
        status=RunStatus.NEEDS_APPROVAL,
        pending_approval={"approval_id": "approval-id"},
    )
    completed = SimpleNamespace(
        run_id="run-id",
        status=RunStatus.COMPLETED,
        pending_approval=None,
    )

    class Store:
        calls = 0

        async def get_state(self, run_id):
            assert run_id == "run-id"
            self.calls += 1
            return pending if self.calls == 1 else completed

    store = Store()
    result = await wait_for_operator_approval(
        store,
        pending,
        poll_interval_s=0,
    )

    assert result is completed
    assert store.calls == 2


def test_operator_console_descriptor_contains_discovery_not_secrets(
    tmp_path,
) -> None:
    descriptor = tmp_path / "operator-console.json"
    write_operator_console_descriptor(
        descriptor,
        url=operator_console_url("::1", 47616),
        access_token_env="TEST_HARNESS_TOKEN",
    )

    raw = descriptor.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["url"] == "http://[::1]:47616/app/"
    assert payload["access_token_env"] == "TEST_HARNESS_TOKEN"
    assert "secret-token-value" not in raw
    assert "daemon_url" not in payload
    assert "target_endpoint" not in payload


async def test_embedded_operator_console_starts_and_stops_on_existing_loop() -> None:
    try:
        probe_socket = socket.socket()
    except PermissionError:
        pytest.skip("test sandbox does not allow loopback sockets")
    with probe_socket as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    app = FastAPI()
    app.state.shutdown_requested = asyncio.Event()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    server = OperatorConsoleServer(app, host="127.0.0.1", port=port)
    await server.start()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{port}/health")
        assert response.json() == {"status": "ok"}
    finally:
        await server.close()

    assert app.state.shutdown_requested.is_set()


async def test_osworld_external_cancel_aborts_durable_run_before_reraising() -> None:
    continue_started = asyncio.Event()

    class Harness:
        aborted = False

        async def start(self, task):
            return SimpleNamespace(
                run_id="run-id",
                status=RunStatus.PAUSED,
                next_action_index=1,
            )

        async def continue_run(self, run_id):
            continue_started.set()
            await asyncio.Event().wait()

        async def abort(self, run_id, reason):
            self.aborted = True
            return SimpleNamespace(
                run_id=run_id,
                status=RunStatus.ABORTED,
                next_action_index=1,
            )

    harness = Harness()
    task = asyncio.create_task(
        _run_bounded_harness(
            harness,
            "Perform the task.",
            max_cycles=20,
            max_run_time_s=30,
        )
    )
    await continue_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert harness.aborted is True


async def test_osworld_unscored_preflight_failure_is_preserved(
    tmp_path,
    monkeypatch,
) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(
        json.dumps(
            {
                "config": [],
                "evaluator": {
                    "func": "exact_match",
                    "result": {"type": "vm_command_line", "command": "true"},
                    "expected": {
                        "type": "rule",
                        "rules": {"expected": ""},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "pikvm_agent.harness.osworld_runner.verify_checkout_revision",
        lambda *args, **kwargs: "revision",
    )
    monkeypatch.setattr(
        "pikvm_agent.harness.osworld_runner.discover_desktop_suite",
        lambda *args, **kwargs: SimpleNamespace(
            tasks=[
                SimpleNamespace(
                    task_id="task-id",
                    config_path=task_path,
                    domain="os",
                    instruction="Perform the task.",
                )
            ]
        ),
    )
    output = tmp_path / "results"

    with pytest.raises(FileNotFoundError):
        await run_osworld_case(
            repo=tmp_path,
            suite_revision="revision",
            qcow=tmp_path / "missing.qcow2",
            docker_image="official-image",
            task_id="task-id",
            harness_config=tmp_path / "harness.yaml",
            output_dir=output,
        )

    failure = json.loads((output / "failure.json").read_text())
    assert failure["status"] == "unscored"
    assert failure["stage"] == "preflight"
    assert failure["error_type"] == "FileNotFoundError"
    assert failure["official_score"] is None
