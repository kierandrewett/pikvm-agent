from types import SimpleNamespace

import pytest

from pikvm_agent.harness.live_benchmark import (
    _editor_activation_target,
    _record_trial,
    _run_editor_trial,
    _timed_trial,
    evaluate_report,
    run_benchmark,
)


def _score(**overrides):
    return {
        "exact_match": True,
        "ocr_normalized_exact_match": True,
        "dangerous_commit_count": 0,
        "file_exact_match": True,
        **overrides,
    }


def _environment(**overrides):
    return {
        "focused_control_class": "Edit",
        "focus_in_foreground": True,
        "guest_fingerprint": "guest:0123456789abcdef",
        "input_desktop": "Default",
        **overrides,
    }


def test_report_gate_accepts_exact_safe_run() -> None:
    report = {
        "observer_environment_identity_required": True,
        "trials": {
            "long_prose": {
                "score": _score(),
                "burst_statuses": ["completed"],
                "environment": _environment(),
            },
            "code": {
                "score": _score(),
                "burst_statuses": ["completed"],
                "environment": _environment(),
            },
            "duplicate_retry": {
                "idempotent_replay": True,
                "score": _score(),
            },
            "ocr_grounded_click": {"status": "completed", "score": _score()},
            "dangerous_send_guard": {
                "status": "needs_approval",
                "risk": "communication_send",
                "score": _score(),
            },
            "editor_vscode": {
                "status": "completed",
                "typing_statuses": ["completed"],
                "save_prompted": True,
                "score": _score(),
                "environment": _environment(
                    focused_control_class="Chrome_RenderWidgetHostHWND"
                ),
            },
        }
    }

    assert evaluate_report(report) == []


def test_report_gate_fails_closed_on_wrong_guest_or_focus() -> None:
    report = {
        "observer_environment_identity_required": True,
        "trials": {
            "long_prose": {
                "score": _score(),
                "burst_statuses": ["completed"],
                "environment": _environment(
                    guest_fingerprint=None,
                    focus_in_foreground=False,
                ),
            },
            "code": {
                "score": _score(),
                "burst_statuses": ["completed"],
                "environment": _environment(
                    focused_control_class="",
                    input_desktop="",
                ),
            },
            "duplicate_retry": {
                "idempotent_replay": True,
                "score": _score(),
            },
            "ocr_grounded_click": {"status": "completed", "score": _score()},
            "dangerous_send_guard": {
                "status": "needs_approval",
                "risk": "communication_send",
                "score": _score(),
            },
        },
    }

    failures = evaluate_report(report)

    assert "long_prose: observer guest identity was missing" in failures
    assert "long_prose: focus was not inside the foreground window" in failures
    assert "code: observer input desktop was missing" in failures
    assert "code: focused control identity was missing" in failures


def test_report_gate_explains_accuracy_and_safety_failures() -> None:
    report = {
        "trials": {
            "long_prose": {
                "score": _score(exact_match=False),
                "burst_statuses": ["failed"],
            },
            "code": {"score": _score(), "burst_statuses": ["completed"]},
            "duplicate_retry": {
                "idempotent_replay": False,
                "score": _score(),
            },
            "ocr_grounded_click": {"status": "failed_grounding"},
            "dangerous_send_guard": {
                "status": "completed",
                "risk": "navigation",
                "score": _score(dangerous_commit_count=1),
            },
        }
    }

    failures = evaluate_report(report)

    assert any("observer text was not exact" in item for item in failures)
    assert any("send did not pause" in item for item in failures)
    assert any("dangerous control committed" in item for item in failures)


def test_editor_activation_grounds_the_fixture_title_near_screen_top() -> None:
    target = _editor_activation_target(
        [
            {"text": "actual.txt body copy", "bbox": [100, 400, 300, 420]},
            {"text": "actual.tet - Notepad", "bbox": [8, 2, 180, 18]},
        ],
        expected_title="notepad",
        screen_height=800,
    )

    assert target == {
        "text": "actual.tet - Notepad",
        "x": 94,
        "y": 10,
    }


def test_editor_activation_fails_closed_without_a_grounded_title() -> None:
    assert (
        _editor_activation_target(
            [{"text": "observer body", "bbox": [10, 200, 300, 220]}],
            expected_title="notepad",
            screen_height=800,
        )
        is None
    )


def test_editor_activation_requires_app_specific_marker_when_configured() -> None:
    lines = [
        {"text": "actual.txt - Notepad", "bbox": [8, 2, 180, 18]},
        {"text": "actual.tt", "bbox": [300, 210, 430, 230]},
        {"text": "File Edit View", "bbox": [8, 25, 180, 42]},
    ]

    assert (
        _editor_activation_target(
            lines,
            expected_title="visual studio code",
            screen_height=800,
            required_markers=["restricted mode", "spaces utf-8"],
        )
        is None
    )

    lines.append(
        {"text": "Restricted Mode", "bbox": [100, 760, 220, 780]}
    )
    target = _editor_activation_target(
        lines,
        expected_title="visual studio code",
        screen_height=800,
        required_markers=["restricted mode", "spaces utf-8"],
    )
    assert target is not None
    assert target["text"] == "actual.tt"


async def test_https_oracle_cannot_skip_token_bound_provisioning() -> None:
    with pytest.raises(ValueError, match="only by screenshot visual mode"):
        await run_benchmark(
            endpoint="unused:5900",
            artifact=None,
            artifact_url=None,
            observer_public_base_url=None,
            receiver_bind_host="127.0.0.1",
            receiver_port=47642,
            keymap="en-us",
            password=None,
            username=None,
            editors=[],
            observer_mode="https",
            skip_provision=True,
        )


async def test_timed_trial_preserves_a_structured_failure_record() -> None:
    async def fail():
        raise RuntimeError("visual page checksum failed")

    result = await _timed_trial("visual", fail())

    assert result["status"] == "harness_error"
    assert result["error_type"] == "RuntimeError"
    assert result["error"] == "visual page checksum failed"
    assert result["elapsed_ms"] >= 0


async def test_record_trial_stops_sequence_after_harness_error() -> None:
    report = {"trials": {}}

    async def fail():
        raise RuntimeError("observer transport failed")

    should_continue = await _record_trial(report, "code", fail())

    assert should_continue is False
    assert report["trials"]["code"]["status"] == "harness_error"
    assert report["aborted_after"] == "code"


async def test_editor_trial_never_saves_after_unverified_typing() -> None:
    class Driver:
        session_id = "session"
        width = 1280
        height = 800

        def __init__(self) -> None:
            self.saved = False

        async def playbook(self, *args, **kwargs):
            return {"status": "completed"}

        async def call(self, *args, **kwargs):
            return {
                "text": "Visual Studio Code",
                "lines": [
                    {
                        "text": "actual.txt - Visual Studio Code",
                        "bbox": [20, 10, 300, 30],
                    },
                    {"text": "Restricted Mode", "bbox": [10, 700, 200, 720]},
                ],
            }

        async def burst(self, actions, **kwargs):
            if actions == [{"type": "key", "keys": ["CTRL", "S"]}]:
                self.saved = True
            return {"status": "completed"}

        async def type_chunks(self, *args, **kwargs):
            return [{"status": "failed", "reason": "type_unverified"}]

    class Oracle:
        async def reset(self, *args, **kwargs):
            return "trial"

        async def seal(self, *args, **kwargs):
            return (
                {"exact_match": False},
                SimpleNamespace(
                    foreground_executable="Code.exe",
                    foreground_title="actual.txt",
                ),
            )

    driver = Driver()
    result = await _run_editor_trial(
        driver,
        Oracle(),
        name="vscode",
        definition={
            "command": "code fixture.txt",
            "content": "exact code",
            "expected_title": "visual studio code",
            "expected_executable": "code.exe",
            "activation_markers": ["restricted mode"],
        },
    )

    assert result["status"] == "typing_failed"
    assert result["typing_statuses"] == ["failed"]
    assert driver.saved is False
