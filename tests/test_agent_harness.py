from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

from pikvm_agent.harness.agent import (
    _CONTROLLER_SYSTEM,
    _REASONER_SYSTEM,
    AgentHarness,
    _calculator_fast_path,
    _calculator_task_controller,
    _durable_last_verified_action,
    _is_read_only_settings_request,
    _locally_verified_notepad_artifact_action,
    _normalize_plan_safety_constraints,
    _normalize_sequential_key_actions,
    _normalize_windows_run_launch,
    _notepad_exact_text_controller,
    _notepad_exact_text_segments,
    _notepad_fast_path,
    _notepad_new_document_controller,
    _verification_confirms_standard_calculator,
)
from pikvm_agent.harness.agent_models import (
    ArtifactAcceptance,
    ArtifactAcceptanceState,
    ComputerObservation,
    ComputerSessionMissingError,
    ControllerDecision,
    HarnessConfig,
    ModelRequest,
    ModelResponse,
    PendingAction,
    PlanDecision,
    RunModelRoute,
    RunSnapshot,
    RunStatus,
    VerificationDecision,
)
from pikvm_agent.harness.agent_store import InMemoryRunStore, SqliteRunStore
from pikvm_agent.harness.model_budget import (
    ModelBudgetPolicy,
    ProviderCostTerms,
)
from pikvm_agent.harness.model_pool import ModelPool, RoleRoute


def test_default_harness_burst_budget_supports_full_local_workflows() -> None:
    assert HarnessConfig().max_actions_per_burst == 20


def test_modifier_free_key_sequence_is_expanded_before_hid() -> None:
    actions = [
        {"type": "key", "keys": ["3", "7", "*", "1", "9", "ENTER"]},
        {"type": "wait_for_change", "timeout_ms": 2_000},
        {"type": "wait_for_stable_screen", "stable_ms": 400},
    ]

    normalized, added, overflow = _normalize_sequential_key_actions(
        actions,
        max_actions=8,
    )

    assert overflow is False
    assert added == 5
    assert normalized[:6] == [
        {"type": "key", "keys": ["Digit3"]},
        {"type": "key", "keys": ["Digit7"]},
        {"type": "key", "keys": ["NumpadMultiply"]},
        {"type": "key", "keys": ["Digit1"]},
        {"type": "key", "keys": ["Digit9"]},
        {"type": "key", "keys": ["Enter"]},
    ]
    assert normalized[6:] == actions[1:]


def test_modifier_chord_is_not_expanded() -> None:
    actions = [{"type": "key", "keys": ["CTRL", "SHIFT", "P"]}]

    normalized, added, overflow = _normalize_sequential_key_actions(
        actions,
        max_actions=8,
    )

    assert normalized == actions
    assert added == 0
    assert overflow is False


def test_sequential_key_expansion_fails_closed_at_action_limit() -> None:
    actions = [{"type": "key", "keys": ["1", "2", "3"]}]

    normalized, added, overflow = _normalize_sequential_key_actions(
        actions,
        max_actions=2,
    )

    assert normalized == actions
    assert added == 0
    assert overflow is True


def test_literal_calculator_task_prepares_one_bounded_key_sequence() -> None:
    run = RunSnapshot(
        run_id="calculator-run",
        task=(
            "Use Windows Calculator to compute 37 multiplied by 19. "
            "Leave the exact result visible and report it."
        ),
        status=RunStatus.PAUSED,
    )
    launch = PendingAction(
        index=0,
        intent="Launch Calculator.",
        actions=[
            {"type": "key", "keys": ["WIN", "R"]},
            {"type": "type_text", "text": "calc"},
            {"type": "key", "keys": ["ENTER"]},
        ],
        based_on_world_version=1,
        based_on_control_epoch=0,
        idempotency_key="calculator-launch",
    )

    controller = _calculator_task_controller(
        run,
        launch,
        max_actions=8,
    )

    assert controller is not None
    assert controller.expects_task_completion is True
    assert [
        action.model_dump(mode="json", exclude_none=True)
        for action in controller.actions[:6]
    ] == [
        {"type": "key", "keys": ["Digit3"]},
        {"type": "key", "keys": ["Digit7"]},
        {"type": "key", "keys": ["NumpadMultiply"]},
        {"type": "key", "keys": ["Digit1"]},
        {"type": "key", "keys": ["Digit9"]},
        {"type": "key", "keys": ["Enter"]},
    ]
    assert controller.expected_evidence == [
        "Calculator's main display visibly reads exactly 703."
    ]


def test_calculator_mixed_expression_is_prepared_without_model_replanning() -> None:
    run = RunSnapshot(
        run_id="calculator-mixed-expression",
        task=(
            "Use Windows Calculator to compute 144 divided by 12, then add 7. "
            "Leave the exact result visible and report it."
        ),
        status=RunStatus.PAUSED,
    )
    launch = PendingAction(
        index=0,
        intent="Launch Calculator.",
        actions=[
            {"type": "key", "keys": ["WIN", "R"]},
            {"type": "type_text", "text": "calc"},
            {"type": "key", "keys": ["ENTER"]},
        ],
        based_on_world_version=1,
        based_on_control_epoch=0,
        idempotency_key="calculator-mixed-launch",
    )

    controller = _calculator_task_controller(
        run,
        launch,
        max_actions=20,
    )

    assert controller is not None
    assert [
        action.model_dump(mode="json", exclude_none=True)
        for action in controller.actions
    ] == [
        {"type": "key", "keys": ["Digit1"]},
        {"type": "key", "keys": ["Digit4"]},
        {"type": "key", "keys": ["Digit4"]},
        {"type": "key", "keys": ["NumpadDivide"]},
        {"type": "key", "keys": ["Digit1"]},
        {"type": "key", "keys": ["Digit2"]},
        {"type": "key", "keys": ["NumpadAdd"]},
        {"type": "key", "keys": ["Digit7"]},
        {"type": "key", "keys": ["Enter"]},
        {"type": "wait_for_change", "timeout_ms": 2_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 400,
            "timeout_ms": 3_000,
        },
    ]
    assert controller.expected_evidence == [
        "Calculator's main display visibly reads exactly 19."
    ]


def test_calculator_square_root_is_prepared_without_model_replanning() -> None:
    run = RunSnapshot(
        run_id="calculator-square-root",
        task=(
            "Use Windows Calculator to find the square root of 2025. "
            "Leave the exact result visible and report it."
        ),
        status=RunStatus.PAUSED,
    )
    launch = PendingAction(
        index=0,
        intent="Launch Calculator.",
        actions=[
            {"type": "key", "keys": ["WIN", "R"]},
            {"type": "type_text", "text": "calc"},
            {"type": "key", "keys": ["ENTER"]},
        ],
        based_on_world_version=1,
        based_on_control_epoch=0,
        idempotency_key="calculator-square-root-launch",
    )

    controller = _calculator_task_controller(
        run,
        launch,
        max_actions=20,
    )

    assert controller is not None
    assert [
        action.model_dump(mode="json", exclude_none=True)
        for action in controller.actions
    ] == [
        {"type": "key", "keys": ["Digit2"]},
        {"type": "key", "keys": ["Digit0"]},
        {"type": "key", "keys": ["Digit2"]},
        {"type": "key", "keys": ["Digit5"]},
        {"type": "wait_for_change", "timeout_ms": 2_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 400,
            "timeout_ms": 3_000,
        },
    ]
    assert controller.expects_task_completion is False
    assert controller.expected_evidence == [
        "Calculator's main display visibly reads exactly 2,025 and the "
        "square-root control is visible."
    ]


def test_calculator_percentage_is_prepared_without_model_replanning() -> None:
    run = RunSnapshot(
        run_id="calculator-percentage",
        task=(
            "Use Windows Calculator to calculate 17.5 percent of 864. "
            "Leave the exact result visible and report it."
        ),
        status=RunStatus.PAUSED,
    )
    launch = PendingAction(
        index=0,
        intent="Launch Calculator.",
        actions=[
            {"type": "key", "keys": ["WIN", "R"]},
            {"type": "type_text", "text": "calc"},
            {"type": "key", "keys": ["ENTER"]},
        ],
        based_on_world_version=1,
        based_on_control_epoch=0,
        idempotency_key="calculator-percentage-launch",
    )

    controller = _calculator_task_controller(
        run,
        launch,
        max_actions=20,
    )

    assert controller is not None
    assert [
        action.model_dump(mode="json", exclude_none=True)
        for action in controller.actions
    ] == [
        {"type": "key", "keys": ["Digit8"]},
        {"type": "key", "keys": ["Digit6"]},
        {"type": "key", "keys": ["Digit4"]},
        {"type": "key", "keys": ["NumpadMultiply"]},
        {"type": "key", "keys": ["Digit1"]},
        {"type": "key", "keys": ["Digit7"]},
        {"type": "key", "keys": ["NumpadDecimal"]},
        {"type": "key", "keys": ["Digit5"]},
        {"type": "key", "keys": ["NumpadDivide"]},
        {"type": "key", "keys": ["Digit1"]},
        {"type": "key", "keys": ["Digit0"]},
        {"type": "key", "keys": ["Digit0"]},
        {"type": "key", "keys": ["Enter"]},
        {"type": "wait_for_change", "timeout_ms": 2_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 400,
            "timeout_ms": 3_000,
        },
    ]
    assert controller.expected_evidence == [
        "Calculator's main display visibly reads exactly 151.2."
    ]


@pytest.mark.parametrize(
    ("task", "expected_keys", "expected_result"),
    [
        (
            "Use Windows Calculator to compute 13 to the power of 4. "
            "Leave the exact result visible and report it.",
            [
                "Digit1",
                "Digit3",
                "NumpadMultiply",
                "Digit1",
                "Digit3",
                "NumpadMultiply",
                "Digit1",
                "Digit3",
                "NumpadMultiply",
                "Digit1",
                "Digit3",
                "Enter",
            ],
            "28561",
        ),
        (
            "Use Windows Calculator to compute 1000.25 minus 378.49. "
            "Leave the exact result visible and report it.",
            [
                "Digit1",
                "Digit0",
                "Digit0",
                "Digit0",
                "NumpadDecimal",
                "Digit2",
                "Digit5",
                "NumpadSubtract",
                "Digit3",
                "Digit7",
                "Digit8",
                "NumpadDecimal",
                "Digit4",
                "Digit9",
                "Enter",
            ],
            "621.76",
        ),
        (
            "Use Windows Calculator to compute 88 plus 12, multiply that "
            "result by 4, then divide by 5. Leave the exact result visible "
            "and report it.",
            [
                "Digit8",
                "Digit8",
                "NumpadAdd",
                "Digit1",
                "Digit2",
                "NumpadMultiply",
                "Digit4",
                "NumpadDivide",
                "Digit5",
                "Enter",
            ],
            "80",
        ),
    ],
)
def test_more_literal_calculator_tasks_avoid_model_replanning(
    task: str,
    expected_keys: list[str],
    expected_result: str,
) -> None:
    run = RunSnapshot(
        run_id="calculator-more-literals",
        task=task,
        status=RunStatus.PAUSED,
    )
    launch = PendingAction(
        index=0,
        intent="Launch Calculator.",
        actions=[
            {"type": "key", "keys": ["WIN", "R"]},
            {"type": "type_text", "text": "calc"},
            {"type": "key", "keys": ["ENTER"]},
        ],
        based_on_world_version=1,
        based_on_control_epoch=0,
        idempotency_key="calculator-more-literals-launch",
    )

    controller = _calculator_task_controller(
        run,
        launch,
        max_actions=20,
    )

    assert controller is not None
    assert [
        action.model_dump(mode="json", exclude_none=True)
        for action in controller.actions
    ] == [
        *[{"type": "key", "keys": [key]} for key in expected_keys],
        {"type": "wait_for_change", "timeout_ms": 2_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 400,
            "timeout_ms": 3_000,
        },
    ]
    assert controller.expects_task_completion is True
    assert controller.expected_evidence == [
        "Calculator's main display visibly reads exactly "
        f"{expected_result}."
    ]


def test_calculator_reciprocal_prepares_operand_for_visual_click() -> None:
    run = RunSnapshot(
        run_id="calculator-reciprocal",
        task=(
            "Use Windows Calculator to compute the reciprocal of 64. "
            "Leave the exact result visible and report it."
        ),
        status=RunStatus.PAUSED,
    )
    launch = PendingAction(
        index=0,
        intent="Launch Calculator.",
        actions=[
            {"type": "key", "keys": ["WIN", "R"]},
            {"type": "type_text", "text": "calc"},
            {"type": "key", "keys": ["ENTER"]},
        ],
        based_on_world_version=1,
        based_on_control_epoch=0,
        idempotency_key="calculator-reciprocal-launch",
    )

    controller = _calculator_task_controller(
        run,
        launch,
        max_actions=20,
    )

    assert controller is not None
    assert [
        action.model_dump(mode="json", exclude_none=True)
        for action in controller.actions
    ] == [
        {"type": "key", "keys": ["Digit6"]},
        {"type": "key", "keys": ["Digit4"]},
        {"type": "wait_for_change", "timeout_ms": 2_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 400,
            "timeout_ms": 3_000,
        },
    ]
    assert controller.expects_task_completion is False
    assert controller.expected_evidence == [
        "Calculator's main display visibly reads exactly 64 and the "
        "reciprocal control is visible."
    ]


@pytest.mark.parametrize(
    "task",
    [
        "Use Windows Calculator to compute 144 divided by 0, then add 7.",
        (
            "Use Windows Calculator to compute 88 plus 12, multiply that "
            "result by 4, then divide by 0."
        ),
        "Use Windows Calculator to compute the reciprocal of 0.",
    ],
)
def test_calculator_zero_division_is_left_to_the_grounded_controller(
    task: str,
) -> None:
    run = RunSnapshot(
        run_id="calculator-zero-division",
        task=task,
        status=RunStatus.PAUSED,
    )
    launch = PendingAction(
        index=0,
        intent="Launch Calculator.",
        actions=[{"type": "type_text", "text": "calc"}],
        based_on_world_version=1,
        based_on_control_epoch=0,
        idempotency_key="calculator-zero-division-launch",
    )

    assert (
        _calculator_task_controller(run, launch, max_actions=20)
        is None
    )


@pytest.mark.parametrize(
    "task",
    [
        (
            "Use Windows Calculator's unit conversion to convert 42 "
            "kilometres to miles. Leave the converted value visible and "
            "report it."
        ),
        (
            "Use Windows Calculator's temperature converter to convert 23 "
            "degrees Celsius to Fahrenheit. Leave the converted value visible "
            "and report it."
        ),
    ],
)
def test_calculator_converter_skips_planning_without_guessing_navigation(
    task: str,
) -> None:
    run = RunSnapshot(
        run_id="calculator-converter",
        task=task,
        status=RunStatus.PAUSED,
    )
    fast_path = _calculator_fast_path(run, max_actions=20)

    assert fast_path is not None
    plan, controller = fast_path
    actions = [
        action.model_dump(mode="json", exclude_none=True)
        for action in controller.actions
    ]
    assert plan.summary == (
        "Open Windows Calculator and perform the requested conversion."
    )
    assert controller.expects_task_completion is False
    assert controller.expected_evidence == [
        "Windows Calculator is visibly open."
    ]
    assert not any(
        action.get("keys") == ["ControlLeft", "KeyU"]
        for action in actions
    )


def test_calculator_fast_path_requires_verified_standard_mode_for_arithmetic() -> None:
    run = RunSnapshot(
        run_id="calculator-fast-path-mode",
        task=(
            "Use Windows Calculator to compute 13 to the power of 4. "
            "Leave the exact result visible and report it."
        ),
        status=RunStatus.PAUSED,
    )

    fast_path = _calculator_fast_path(run, max_actions=20)

    assert fast_path is not None
    _, controller = fast_path
    assert controller.expected_evidence == [
        "Windows Calculator is visibly open."
    ]
    launch = PendingAction(
        index=0,
        intent="Launch Calculator.",
        actions=[{"type": "type_text", "text": "calc"}],
        based_on_world_version=1,
        based_on_control_epoch=0,
        idempotency_key="calculator-fast-path-mode-launch",
    )
    expression = _calculator_task_controller(
        run,
        launch,
        max_actions=20,
    )
    assert expression is not None
    assert [
        action.model_dump(mode="json", exclude_none=True)
        for action in expression.actions[:2]
    ] == [
        {"type": "key", "keys": ["Digit1"]},
        {"type": "key", "keys": ["Digit3"]},
    ]
    run.last_verification = VerificationDecision(
        verdict="verified",
        summary="Windows Calculator is visibly open in Temperature mode.",
        evidence=["The mode heading reads Temperature."],
    )
    assert not _verification_confirms_standard_calculator(run)
    run.last_verification = VerificationDecision(
        verdict="verified",
        summary="Windows Calculator is visibly open in Standard mode.",
        evidence=["The mode heading reads Standard."],
    )
    assert _verification_confirms_standard_calculator(run)


def test_calculator_converter_launch_accepts_any_persisted_mode() -> None:
    run = RunSnapshot(
        run_id="calculator-fast-path-converter-mode",
        task=(
            "Use Windows Calculator's temperature converter to convert 23 "
            "degrees Celsius to Fahrenheit. Leave the converted value visible "
            "and report it."
        ),
        status=RunStatus.PAUSED,
    )

    fast_path = _calculator_fast_path(run, max_actions=20)

    assert fast_path is not None
    _, controller = fast_path
    assert controller.expected_evidence == [
        "Windows Calculator is visibly open."
    ]


def test_exact_notepad_task_prepares_new_document_and_exact_text() -> None:
    run = RunSnapshot(
        run_id="notepad-fast-path",
        task=(
            "In Notepad, type exactly `Reliable automation starts with "
            "observable evidence.` and save it as "
            "C:\\PiKVM-Harness\\workspace\\codex-50\\text-01.txt. "
            "Reopen the file and verify the sentence is exact."
        ),
        status=RunStatus.PAUSED,
    )

    fast_path = _notepad_fast_path(run, max_actions=20)

    assert fast_path is not None
    plan, launch_controller = fast_path
    assert plan.summary == (
        "Create and verify the requested exact text file in Notepad."
    )
    assert [
        action.model_dump(mode="json", exclude_none=True)
        for action in launch_controller.actions
    ] == [
        {"type": "key", "keys": ["WIN", "R"]},
        {"type": "wait", "ms": 300},
        {
            "type": "type_text",
            "text": "notepad",
            "code": False,
            "secret": False,
            "context": "field",
            "verification": "exact",
        },
        {"type": "key", "keys": ["ENTER"]},
        {"type": "wait", "ms": 1_000},
        {"type": "wait_for_change", "timeout_ms": 30_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 500,
            "timeout_ms": 5_000,
        },
    ]
    assert launch_controller.expected_evidence == [
        "Windows Notepad is visibly open."
    ]
    launch = PendingAction(
        index=0,
        intent=launch_controller.intent,
        actions=[
            action.model_dump(mode="json", exclude_none=True)
            for action in launch_controller.actions
        ],
        based_on_world_version=1,
        based_on_control_epoch=0,
        idempotency_key="notepad-launch",
    )
    new_document = _notepad_new_document_controller(
        run,
        launch,
        max_actions=20,
    )
    assert new_document is not None
    assert [
        action.model_dump(mode="json", exclude_none=True)
        for action in new_document.actions
    ] == [
        {"type": "key", "keys": ["ControlLeft", "KeyN"]},
        {"type": "wait_for_change", "timeout_ms": 3_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 400,
            "timeout_ms": 3_000,
        },
    ]
    document = PendingAction(
        index=1,
        intent=new_document.intent,
        actions=[
            action.model_dump(mode="json", exclude_none=True)
            for action in new_document.actions
        ],
        based_on_world_version=2,
        based_on_control_epoch=0,
        idempotency_key="notepad-new-document",
    )
    exact_text = _notepad_exact_text_controller(
        run,
        document,
        max_actions=20,
    )
    assert exact_text is not None
    assert [
        action.model_dump(mode="json", exclude_none=True)
        for action in exact_text.actions
    ] == [
        {
            "type": "type_text",
            "text": (
                "Reliable automation starts with observable evidence."
                ),
                "code": False,
                "secret": False,
                "context": "editor",
            "verification": "exact",
        },
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 400,
            "timeout_ms": 3_000,
        },
    ]


def test_campaign_code_task_does_not_create_document_before_launch_commit() -> None:
    run = RunSnapshot(
        run_id="notepad-campaign-code",
        task=(
            "For this text/code acceptance, create a new blank document and "
            "type every requested content character during this run. Do not "
            "treat restored or pre-existing document content as task "
            "completion. Task: In Notepad, write a Python function."
        ),
        status=RunStatus.PAUSED,
    )
    launch = PendingAction(
        index=0,
        intent="Launch Notepad.",
        actions=[
            {
                "type": "type_text",
                "text": "notepad",
                "context": "field",
                "verification": "exact",
            }
        ],
        based_on_world_version=1,
        based_on_control_epoch=0,
        idempotency_key="notepad-campaign-launch",
    )

    decision = _notepad_new_document_controller(
        run,
        launch,
        max_actions=20,
    )

    assert decision is None


def test_campaign_code_task_creates_document_after_committed_launch() -> None:
    run = RunSnapshot(
        run_id="notepad-campaign-code-committed",
        task=(
            "For this text/code acceptance, create a new blank document and "
            "type every requested content character during this run. Do not "
            "treat restored or pre-existing document content as task "
            "completion. Task: In Notepad, write a Python function."
        ),
        status=RunStatus.PAUSED,
    )
    launch = PendingAction(
        index=0,
        intent="Launch Notepad.",
        actions=[
            {"type": "key", "keys": ["WIN", "R"]},
            {
                "type": "type_text",
                "text": "notepad",
                "context": "field",
                "verification": "exact",
            },
            {"type": "key", "keys": ["ENTER"]},
        ],
        based_on_world_version=1,
        based_on_control_epoch=0,
        idempotency_key="notepad-campaign-launch-committed",
    )

    decision = _notepad_new_document_controller(
        run,
        launch,
        max_actions=20,
    )

    assert decision is not None
    assert decision.intent == "Create a fresh blank Notepad document."
    assert decision.actions[0].model_dump(mode="json", exclude_none=True) == {
        "type": "key",
        "keys": ["ControlLeft", "KeyN"],
    }


def test_exact_notepad_paragraphs_use_separate_verified_text_and_line_breaks() -> None:
    first = (
        "A useful computer agent works quickly, but speed without evidence "
        "is guesswork."
    )
    second = (
        "Every action should be visible, attributable, and independently "
        "checkable."
    )
    run = RunSnapshot(
        run_id="notepad-two-paragraph-fast-path",
        task=(
            "In Notepad, create "
            "C:\\PiKVM-Harness\\workspace\\codex-50\\text-03.txt with exactly "
            f"two paragraphs. First paragraph: `{first}` Second paragraph: "
            f"`{second}` Put one blank line between them, reopen the file, "
            "and verify it."
        ),
        status=RunStatus.PAUSED,
    )

    fast_path = _notepad_fast_path(run, max_actions=20)

    assert fast_path is not None
    plan, launch_controller = fast_path
    assert plan.success_criteria == [
        (
            "The requested file exists inside the permitted lab workspace."
        ),
        (
            "The reopened file visibly contains exactly the requested two "
            "paragraphs with one blank line between them."
        ),
    ]
    launch = PendingAction(
        index=0,
        intent=launch_controller.intent,
        actions=[
            action.model_dump(mode="json", exclude_none=True)
            for action in launch_controller.actions
        ],
        based_on_world_version=1,
        based_on_control_epoch=0,
        idempotency_key="notepad-two-paragraph-launch",
    )
    new_document = _notepad_new_document_controller(
        run,
        launch,
        max_actions=20,
    )
    assert new_document is not None
    document = PendingAction(
        index=1,
        intent=new_document.intent,
        actions=[
            action.model_dump(mode="json", exclude_none=True)
            for action in new_document.actions
        ],
        based_on_world_version=2,
        based_on_control_epoch=0,
        idempotency_key="notepad-two-paragraph-new-document",
    )

    first_paragraph = _notepad_exact_text_controller(
        run,
        document,
        max_actions=20,
    )

    assert first_paragraph is not None
    assert [
        action.model_dump(mode="json", exclude_none=True)
        for action in first_paragraph.actions
    ] == [
        {
            "type": "type_text",
            "text": first,
            "code": False,
            "secret": False,
            "context": "editor",
            "verification": "exact",
        },
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 400,
            "timeout_ms": 3_000,
        },
    ]
    first_action = PendingAction(
        index=2,
        intent=first_paragraph.intent,
        actions=[
            action.model_dump(mode="json", exclude_none=True)
            for action in first_paragraph.actions
        ],
        based_on_world_version=3,
        based_on_control_epoch=0,
        idempotency_key="notepad-two-paragraph-first",
    )

    blank_line = _notepad_exact_text_controller(
        run,
        first_action,
        max_actions=20,
    )

    assert blank_line is not None
    assert [
        action.model_dump(mode="json", exclude_none=True)
        for action in blank_line.actions
    ] == [
        {"type": "key", "keys": ["SHIFT", "ENTER"]},
        {"type": "key", "keys": ["SHIFT", "ENTER"]},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 400,
            "timeout_ms": 3_000,
        },
    ]
    line_break_action = PendingAction(
        index=3,
        intent=blank_line.intent,
        actions=[
            action.model_dump(mode="json", exclude_none=True)
            for action in blank_line.actions
        ],
        based_on_world_version=4,
        based_on_control_epoch=0,
        idempotency_key="notepad-two-paragraph-line-break",
    )

    second_paragraph = _notepad_exact_text_controller(
        run,
        line_break_action,
        max_actions=20,
    )

    assert second_paragraph is not None
    assert [
        action.model_dump(mode="json", exclude_none=True)
        for action in second_paragraph.actions
    ] == [
        {
            "type": "type_text",
            "text": second,
            "code": False,
            "secret": False,
            "context": "editor",
            "verification": "exact",
        },
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 400,
            "timeout_ms": 3_000,
        },
    ]


def test_exact_notepad_lines_use_one_verified_break_between_each_line() -> None:
    lines = ("1. Observe", "2. Act", "3. Verify", "4. Record")
    run = RunSnapshot(
        run_id="notepad-exact-lines-fast-path",
        task=(
            "In Notepad, create "
            "C:\\PiKVM-Harness\\workspace\\codex-50\\text-04.txt with these "
            "exact lines: `1. Observe` then `2. Act` then `3. Verify` then "
            "`4. Record`. Reopen it and verify all four lines."
        ),
        status=RunStatus.PAUSED,
    )
    assert _notepad_exact_text_segments(run) == lines

    prior = PendingAction(
        index=1,
        intent="Create a fresh blank Notepad document.",
        actions=[{"type": "key", "keys": ["ControlLeft", "KeyN"]}],
        based_on_world_version=2,
        based_on_control_epoch=0,
        idempotency_key="notepad-lines-new-document",
    )
    for index, expected in enumerate(lines):
        text_decision = _notepad_exact_text_controller(
            run,
            prior,
            max_actions=20,
        )
        assert text_decision is not None
        typed = [
            action.model_dump(mode="json", exclude_none=True)
            for action in text_decision.actions
        ]
        assert typed[0] == {
            "type": "type_text",
            "text": expected,
            "code": False,
            "secret": False,
            "context": "editor",
            "verification": "exact",
        }
        prior = PendingAction(
            index=2 + index * 2,
            intent=text_decision.intent,
            actions=typed,
            based_on_world_version=3 + index * 2,
            based_on_control_epoch=0,
            idempotency_key=f"notepad-lines-text-{index}",
        )
        if index == len(lines) - 1:
            assert (
                _notepad_exact_text_controller(
                    run,
                    prior,
                    max_actions=20,
                )
                is None
            )
            break
        line_break = _notepad_exact_text_controller(
            run,
            prior,
            max_actions=20,
        )
        assert line_break is not None
        break_actions = [
            action.model_dump(mode="json", exclude_none=True)
            for action in line_break.actions
        ]
        assert break_actions[:1] == [
            {"type": "key", "keys": ["SHIFT", "ENTER"]}
        ]
        assert sum(
            action.get("type") == "key" for action in break_actions
        ) == 1
        prior = PendingAction(
            index=3 + index * 2,
            intent=line_break.intent,
            actions=break_actions,
            based_on_world_version=4 + index * 2,
            based_on_control_epoch=0,
            idempotency_key=f"notepad-lines-break-{index}",
        )


def test_generated_code_plan_carries_one_durable_exact_artifact() -> None:
    content = (
        "def fizzbuzz(limit):\n"
        "    results = []\n"
        "    for number in range(1, limit + 1):\n"
        "        if number % 15 == 0:\n"
        '            results.append("FizzBuzz")\n'
        "    return results"
    )

    plan = PlanDecision(
        summary="Write and verify the requested Python artifact.",
        steps=["Open Notepad", "Enter the exact artifact", "Save and reopen it"],
        success_criteria=["The reopened file contains valid Python code."],
        artifact_content=content,
        artifact_content_kind="code",
    )

    assert plan.artifact_content == content
    assert plan.artifact_content_kind == "code"


def test_generated_code_plan_rejects_tabs_in_exact_artifact() -> None:
    with pytest.raises(ValidationError, match="spaces instead of tabs"):
        PlanDecision(
            summary="Write code.",
            steps=["Enter it"],
            success_criteria=["The code is visible."],
            artifact_content="def example():\n\treturn 1",
            artifact_content_kind="code",
        )


def test_generated_code_plan_accepts_a_conventional_trailing_newline() -> None:
    plan = PlanDecision(
        summary="Write code.",
        steps=["Enter it"],
        success_criteria=["The code is visible."],
        artifact_content="def example():\n    return 1\n",
        artifact_content_kind="code",
    )
    run = RunSnapshot(
        run_id="notepad-trailing-newline",
        task="In Notepad, write Python code.",
        status=RunStatus.PAUSED,
        plan=plan,
    )

    assert plan.artifact_content.endswith("\n")
    assert _notepad_exact_text_segments(run) == (
        "def example():",
        "    return 1",
    )


def test_generated_artifact_does_not_activate_notepad_path_for_vscode() -> None:
    run = RunSnapshot(
        run_id="vscode-generated-code",
        task="In Visual Studio Code, create a new file containing Python code.",
        status=RunStatus.PAUSED,
        plan=PlanDecision(
            summary="Write the requested Python artifact.",
            steps=["Create a file", "Enter the artifact"],
            success_criteria=["The code is visible."],
            artifact_content="def example():\n    return 1",
            artifact_content_kind="code",
        ),
    )
    prior = PendingAction(
        index=1,
        intent="Create a new file in Visual Studio Code.",
        actions=[{"type": "key", "keys": ["ControlLeft", "KeyN"]}],
        based_on_world_version=2,
        based_on_control_epoch=0,
        idempotency_key="vscode-new-file",
    )

    assert _notepad_exact_text_segments(run) == ()
    assert (
        _notepad_exact_text_controller(run, prior, max_actions=20)
        is None
    )


def test_generated_code_uses_indexed_exact_segments_without_tab_actions() -> None:
    content = (
        "def fizzbuzz(limit):\n"
        "    results = []\n"
        "    for number in range(1, limit + 1):\n"
        "        if number % 15 == 0:\n"
        '            results.append("FizzBuzz")\n'
        "    return results"
    )
    run = RunSnapshot(
        run_id="notepad-generated-code",
        task=(
            "For this text/code acceptance, create a new blank document and "
            "type every requested content character during this run. Task: In "
            "Notepad, write a valid Python fizzbuzz function."
        ),
        status=RunStatus.PAUSED,
        plan=PlanDecision(
            summary="Write and verify the requested Python artifact.",
            steps=[
                "Open Notepad",
                "Enter the exact artifact",
                "Save and reopen it",
            ],
            success_criteria=["The reopened file contains valid Python code."],
            artifact_content=content,
            artifact_content_kind="code",
        ),
    )
    assert _notepad_exact_text_segments(run) == tuple(content.splitlines())

    prior = PendingAction(
        index=1,
        intent="Create a fresh blank Notepad document.",
        actions=[{"type": "key", "keys": ["ControlLeft", "KeyN"]}],
        based_on_world_version=2,
        based_on_control_epoch=0,
        idempotency_key="notepad-code-new-document",
    )
    for index, expected in enumerate(content.splitlines()):
        text_decision = _notepad_exact_text_controller(
            run,
            prior,
            max_actions=20,
        )
        assert text_decision is not None
        assert text_decision.intent == (
            f"Enter exact segment {index + 1} of {len(content.splitlines())} "
            "in the fresh Notepad document."
        )
        text_actions = [
            action.model_dump(mode="json", exclude_none=True)
            for action in text_decision.actions
        ]
        assert text_actions[0] == {
            "type": "type_text",
            "text": expected,
            "code": True,
            "secret": False,
            "context": "editor",
            "verification": "exact",
        }
        assert all(action.get("keys") != ["TAB"] for action in text_actions)
        prior = PendingAction(
            index=2 + index * 2,
            intent=text_decision.intent,
            actions=text_actions,
            based_on_world_version=3 + index * 2,
            based_on_control_epoch=0,
            idempotency_key=f"notepad-code-text-{index}",
        )
        if index == len(content.splitlines()) - 1:
            assert (
                _notepad_exact_text_controller(run, prior, max_actions=20)
                is None
            )
            break
        line_break = _notepad_exact_text_controller(
            run,
            prior,
            max_actions=20,
        )
        assert line_break is not None
        break_actions = [
            action.model_dump(mode="json", exclude_none=True)
            for action in line_break.actions
        ]
        assert break_actions[:1] == [
            {"type": "key", "keys": ["SHIFT", "ENTER"]}
        ]
        prior = PendingAction(
            index=3 + index * 2,
            intent=line_break.intent,
            actions=break_actions,
            based_on_world_version=4 + index * 2,
            based_on_control_epoch=0,
            idempotency_key=f"notepad-code-break-{index}",
        )


def test_generated_code_exact_visual_receipt_skips_duplicate_model_proof() -> None:
    content = "def answer():\n    return 42"
    run = RunSnapshot(
        run_id="notepad-local-exact-proof",
        task="In Notepad, write a Python function.",
        status=RunStatus.RUNNING,
        plan=PlanDecision(
            summary="Write the code.",
            steps=["Enter the exact artifact"],
            success_criteria=["The exact code is visible."],
            artifact_content=content,
            artifact_content_kind="code",
        ),
    )
    action = PendingAction(
        index=2,
        intent="Enter exact segment 1 of 2 in the fresh Notepad document.",
        actions=[
            {
                "type": "type_text",
                "text": "def answer():",
                "code": True,
                "context": "editor",
                "verification": "exact",
            },
            {
                "type": "wait_for_stable_screen",
                "stable_ms": 400,
                "timeout_ms": 3_000,
            },
        ],
        expected_evidence=["The exact first line is visible."],
        based_on_world_version=2,
        based_on_control_epoch=0,
        idempotency_key="notepad-local-exact-proof",
    )
    receipt = {
        "index": 0,
        "status": "verified_exact",
        "verdict": "match",
        "focus_evidence": "read_back_verified",
        "proof_state": "exact_visual_readback",
        "exact_readback_sha256_match": True,
        "emitted_exactly_once": True,
        "correction_count": 0,
        "delivery_retries": 0,
    }

    verdict = _locally_verified_notepad_artifact_action(
        run,
        action,
        [receipt],
        after=ComputerObservation(
            session_id="session",
            status="completed",
            image_sha256="b" * 64,
            screen_hash="b2",
        ),
    )

    assert verdict is not None
    assert verdict.verdict == "verified"
    assert verdict.action_criteria[0].satisfied is True
    assert "exact hash" in verdict.evidence[0]


def test_generated_code_local_proof_rejects_sender_only_receipt() -> None:
    run = RunSnapshot(
        run_id="notepad-local-weak-proof",
        task="In Notepad, write code.",
        status=RunStatus.RUNNING,
        plan=PlanDecision(
            summary="Write the code.",
            steps=["Enter it"],
            success_criteria=["The code is visible."],
            artifact_content="return 42",
            artifact_content_kind="code",
        ),
    )
    action = PendingAction(
        index=2,
        intent="Enter the requested exact text in the fresh Notepad document.",
        actions=[
            {
                "type": "type_text",
                "text": "return 42",
                "code": True,
                "context": "editor",
                "verification": "exact",
            }
        ],
        expected_evidence=["The exact text is visible."],
        based_on_world_version=2,
        based_on_control_epoch=0,
        idempotency_key="notepad-local-weak-proof",
    )

    verdict = _locally_verified_notepad_artifact_action(
        run,
        action,
        [
            {
                "index": 0,
                "status": "delivered_unverified",
                "verdict": "unverified",
                "proof_state": "issued_only",
            }
        ],
        after=ComputerObservation(session_id="session", status="completed"),
    )

    assert verdict is None


def test_generated_code_recovers_next_segment_across_call_boundary() -> None:
    content = "def answer():\n    return 42"
    run = RunSnapshot(
        run_id="notepad-durable-segment-boundary",
        task="In Notepad, write a Python function.",
        status=RunStatus.PAUSED,
        plan=PlanDecision(
            summary="Write the code.",
            steps=["Enter it"],
            success_criteria=["The code is visible."],
            artifact_content=content,
            artifact_content_kind="code",
        ),
    )
    run.record(
        "action.checkpointed",
        index=3,
        idempotency_key="notepad-line-break",
        intent=(
            "Insert the requested line break after exact segment "
            "1 of 2 in Notepad."
        ),
        actions=[
            {"type": "key", "keys": ["SHIFT", "ENTER"]},
            {
                "type": "wait_for_stable_screen",
                "stable_ms": 400,
                "timeout_ms": 3_000,
            },
        ],
        expected_evidence=["The caret is visibly on line 2."],
    )
    run.record("action.completed", index=3, status="completed")
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="The line break is visible.",
    )
    run.record("run.paused", reason="per-call action budget reached")

    prior = _durable_last_verified_action(run)
    decision = _notepad_exact_text_controller(
        run,
        prior,
        max_actions=20,
    )

    assert prior is not None
    assert prior.index == 3
    assert decision is not None
    assert decision.intent == (
        "Enter exact segment 2 of 2 in the fresh Notepad document."
    )
    assert decision.actions[0].model_dump(
        mode="json",
        exclude_none=True,
    ) == {
        "type": "type_text",
        "text": "    return 42",
        "code": True,
        "secret": False,
        "context": "editor",
        "verification": "exact",
    }


def test_durable_action_recovery_rejects_a_newer_unverified_checkpoint() -> None:
    run = RunSnapshot(
        run_id="notepad-stale-durable-action",
        task="In Notepad, write a Python function.",
        status=RunStatus.PAUSED,
    )
    for index in (1, 2):
        run.record(
            "action.checkpointed",
            index=index,
            idempotency_key=f"action-{index}",
            intent=f"Action {index}",
            actions=[{"type": "key", "keys": ["ENTER"]}],
            expected_evidence=["The action is visible."],
        )
        run.record("action.completed", index=index, status="completed")
        if index == 1:
            run.record(
                "model.completed",
                role="verifier",
                verdict="verified",
                summary="Action 1 is visible.",
            )

    assert _durable_last_verified_action(run) is None


def test_generated_code_local_proof_rejects_active_key_prefix() -> None:
    run = RunSnapshot(
        run_id="notepad-local-active-prefix",
        task="In Notepad, write two lines of code.",
        status=RunStatus.RUNNING,
        plan=PlanDecision(
            summary="Write the code.",
            steps=["Enter it"],
            success_criteria=["The code is visible."],
            artifact_content="first()\nsecond()",
            artifact_content_kind="code",
        ),
    )
    action = PendingAction(
        index=2,
        intent="Enter exact segment 2 of 2 in the fresh Notepad document.",
        actions=[
            {"type": "key", "keys": ["SHIFT", "ENTER"]},
            {
                "type": "type_text",
                "text": "second()",
                "code": True,
                "context": "editor",
                "verification": "exact",
            },
        ],
        expected_evidence=["The second line is visible."],
        based_on_world_version=2,
        based_on_control_epoch=0,
        idempotency_key="notepad-local-active-prefix",
    )
    receipt = {
        "index": 1,
        "status": "verified_exact",
        "verdict": "match",
        "focus_evidence": "read_back_verified",
        "proof_state": "exact_visual_readback",
        "exact_readback_sha256_match": True,
        "emitted_exactly_once": True,
        "correction_count": 0,
        "delivery_retries": 0,
    }

    assert (
        _locally_verified_notepad_artifact_action(
            run,
            action,
            [receipt],
            after=ComputerObservation(
                session_id="session",
                status="completed",
            ),
        )
        is None
    )


def test_generated_code_line_break_still_requires_model_verification() -> None:
    run = RunSnapshot(
        run_id="notepad-local-line-break",
        task="In Notepad, write two lines of code.",
        status=RunStatus.RUNNING,
        plan=PlanDecision(
            summary="Write the code.",
            steps=["Enter it"],
            success_criteria=["The code is visible."],
            artifact_content="first()\nsecond()",
            artifact_content_kind="code",
        ),
    )
    action = PendingAction(
        index=2,
        intent=(
            "Insert the requested line break after exact segment "
            "1 of 2 in Notepad."
        ),
        actions=[{"type": "key", "keys": ["SHIFT", "ENTER"]}],
        expected_evidence=["The caret is on the next line."],
        based_on_world_version=2,
        based_on_control_epoch=0,
        idempotency_key="notepad-local-line-break",
    )

    assert (
        _locally_verified_notepad_artifact_action(
            run,
            action,
            [],
            after=ComputerObservation(
                session_id="session",
                status="completed",
                image_sha256="b" * 64,
                screen_hash="b2",
            ),
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interrupt_reason", "expected_accepted"),
    [("deadline", True), ("control_changed", False)],
)
async def test_exact_artifact_receipt_survives_only_passive_deadline(
    interrupt_reason: str,
    expected_accepted: bool,
) -> None:
    provider = ScriptedProvider()
    harness = build_harness(provider, FakeComputer())
    content = (
        "def fizzbuzz(limit):\n"
        "    result = []\n"
        "    for number in range(1, limit + 1):\n"
        "        if number % 15 == 0:\n"
        '            result.append("FizzBuzz")'
    )
    action = PendingAction(
        index=8,
        intent="Enter exact segment 4 of 5 in the fresh Notepad document.",
        actions=[
            {
                "type": "type_text",
                "text": "        if number % 15 == 0:",
                "code": True,
                "context": "editor",
                "verification": "exact",
            },
            {
                "type": "wait_for_stable_screen",
                "stable_ms": 400,
                "timeout_ms": 3_000,
            },
        ],
        expected_evidence=["The exact fourth line is visible."],
        based_on_world_version=2,
        based_on_control_epoch=0,
        idempotency_key="notepad-interrupted-passive-wait",
    )
    run = RunSnapshot(
        run_id="notepad-interrupted-passive-wait",
        task="In Notepad, write a valid Python fizzbuzz function.",
        status=RunStatus.RUNNING,
        session_id="session",
        plan=PlanDecision(
            summary="Write the code.",
            steps=["Enter it"],
            success_criteria=["The code is visible."],
            artifact_content=content,
            artifact_content_kind="code",
        ),
        pending_action=action,
        observation=ComputerObservation(
            session_id="session",
            status="paused",
            frame_id=8,
            world_version=2,
            control_epoch=0,
            image_sha256="a" * 64,
            screen_hash="a1",
        ),
    )
    await harness.store.save(run)
    digest = (
        "f161df95e5fbdcf496893ac38cf6b9c0a1f171e9db6e0869c9a465c2f4a901b4"
    )
    observation = ComputerObservation(
        session_id="session",
        status="interrupted",
        frame_id=9,
        world_version=2,
        control_epoch=0,
        image_sha256="b" * 64,
        screen_hash="b2",
        raw={
            "reason": interrupt_reason,
            "action_receipts": [
                {
                    "index": 0,
                    "type": "type_text",
                    "status": "verified_exact",
                    "verdict": "match",
                    "focus_evidence": "read_back_verified",
                    "requested_characters": 28,
                    "delivery_characters": 28,
                    "issued_characters": 28,
                    "correction_count": 0,
                    "delivery_retries": 0,
                    "requested_sha256": digest,
                    "delivery_sha256": digest,
                    "issued_prefix_sha256": digest,
                    "readback_sha256": digest,
                    "readback_frame_sha256": "b" * 64,
                    "exact_readback_sha256_match": True,
                    "emitted_exactly_once": True,
                    "observed_text": "        if number % 15 == 0:",
                }
            ]
        },
    )

    accepted = await harness._accept_action_observation(
        run,
        observation,
        before=run.observation,
        tool="pikvm_run_burst",
        call_id="interrupted-passive-wait:attempt:1",
        latency_ms=78_210,
        parallel_next_control=True,
    )

    assert accepted is expected_accepted
    assert run.pending_action is None
    if expected_accepted:
        assert run.status is RunStatus.RUNNING
        assert run.next_action_index == 9
        assert run.last_verification is not None
        assert run.last_verification.verdict == "verified"
        assert any(
            event.kind == "action.completed"
            and event.data["outer_status"] == "interrupted"
            for event in run.events
        )
        assert not any(event.kind == "action.failed" for event in run.events)
        assert run.run_id in harness._prefetched_controllers
    else:
        assert run.status is RunStatus.FAILED
        assert run.last_verification is None
        assert any(event.kind == "action.failed" for event in run.events)
        assert run.run_id not in harness._prefetched_controllers


def test_controller_can_launch_a_standard_app_in_one_safe_burst() -> None:
    assert "one narrow app-launch exception" in _CONTROLLER_SYSTEM
    assert "type only the app's executable name" in _CONTROLLER_SYSTEM
    assert "context ``field`` and verification ``exact``" in _CONTROLLER_SYSTEM
    assert "Keep Win+R, the exact text, and Enter in this same" in (
        _CONTROLLER_SYSTEM
    )
    assert "never issue Win+R as a standalone action" in _CONTROLLER_SYSTEM
    assert "shell, terminal, URL, file path, command arguments" in (
        _CONTROLLER_SYSTEM
    )
    assert "native ``ms-settings:`` URI" in _CONTROLLER_SYSTEM
    assert "full absolute path into the File name field" in (
        _CONTROLLER_SYSTEM
    )
    assert "File name field is normally pre-populated" in (
        _CONTROLLER_SYSTEM
    )
    assert "Ctrl+A immediately before the exact basename" in (
        _CONTROLLER_SYSTEM
    )
    assert "default selection is still active" in (
        _CONTROLLER_SYSTEM
    )
    assert "Do not generalise this exception to web URLs" in _CONTROLLER_SYSTEM
    assert "the verifier's job, not a remaining computer" in _CONTROLLER_SYSTEM
    assert "set expects_task_completion true" in _CONTROLLER_SYSTEM


def test_controller_uses_the_safe_explorer_location_shortcut() -> None:
    assert "In File Explorer, use Ctrl+L" in _CONTROLLER_SYSTEM
    assert "preserve the selection created by Ctrl+L" in _CONTROLLER_SYSTEM
    assert "do not click, refocus, move the" in _CONTROLLER_SYSTEM
    assert "as the very next active input" in _CONTROLLER_SYSTEM
    assert "repeat Ctrl+L before typing" in _CONTROLLER_SYSTEM
    assert 'draft exactly ``This PC``' in _CONTROLLER_SYSTEM
    assert "never a ``shell:`` URI" in _CONTROLLER_SYSTEM


def test_native_settings_launch_waits_through_splash_and_page_render() -> None:
    actions = [
        {"type": "key", "keys": ["META", "R"]},
        {"type": "wait", "ms": 500},
        {
            "type": "type_text",
            "text": "ms-settings:about",
            "context": "field",
            "verification": "exact",
        },
        {"type": "key", "keys": ["ENTER"]},
        {"type": "wait_for_change", "timeout_ms": 5_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 1_000,
            "timeout_ms": 8_000,
        },
    ]

    normalized, added, settled = _normalize_windows_run_launch(
        actions,
        max_actions=8,
    )

    assert added == 1
    assert settled is True
    assert normalized[1] == {
        "type": "wait_for_change",
        "timeout_ms": 5_000,
    }
    assert normalized[2] == {
        "type": "wait_for_stable_screen",
        "stable_ms": 300,
        "timeout_ms": 3_000,
    }
    assert normalized[5:8] == [
        {"type": "wait_for_change", "timeout_ms": 5_000},
        {"type": "wait", "ms": 5_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 1_000,
            "timeout_ms": 8_000,
        },
    ]


def test_native_settings_launch_normalization_is_idempotent() -> None:
    actions = [
        {"type": "key", "keys": ["META", "R"]},
        {
            "type": "type_text",
            "text": "ms-settings:display",
            "context": "field",
            "verification": "exact",
        },
        {"type": "key", "keys": ["ENTER"]},
        {"type": "wait_for_change", "timeout_ms": 5_000},
        {"type": "wait_for_change", "timeout_ms": 10_000},
    ]

    normalized, added, settled = _normalize_windows_run_launch(
        actions,
        max_actions=8,
    )

    assert added == 0
    assert settled is True
    assert normalized[1:3] == [
        {"type": "wait_for_change", "timeout_ms": 5_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 300,
            "timeout_ms": 3_000,
        },
    ]
    assert normalized[3:] == [
        actions[1],
        actions[2],
        actions[3],
        {"type": "wait", "ms": 5_000},
    ]


def test_standard_app_launch_waits_for_run_close_and_app_open() -> None:
    actions = [
        {"type": "key", "keys": ["META", "R"]},
        {
            "type": "type_text",
            "text": "notepad",
            "context": "field",
            "verification": "exact",
        },
        {"type": "key", "keys": ["ENTER"]},
        {"type": "wait_for_change", "timeout_ms": 5_000},
    ]

    normalized, added, settled = _normalize_windows_run_launch(
        actions,
        max_actions=8,
    )

    assert added == 2
    assert settled is True
    assert normalized == [
        actions[0],
        {"type": "wait_for_change", "timeout_ms": 5_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 300,
            "timeout_ms": 3_000,
        },
        actions[1],
        actions[2],
        actions[3],
        {"type": "wait_for_change", "timeout_ms": 10_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 500,
            "timeout_ms": 3_000,
        },
    ]


def test_standard_app_launch_keeps_one_long_post_submit_change_wait() -> None:
    actions = [
        {"type": "key", "keys": ["META", "R"]},
        {
            "type": "type_text",
            "text": "notepad",
            "context": "field",
            "verification": "exact",
        },
        {"type": "key", "keys": ["ENTER"]},
        {"type": "wait", "ms": 1_000},
        {"type": "wait_for_change", "timeout_ms": 30_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 500,
            "timeout_ms": 5_000,
        },
    ]

    normalized, added, settled = _normalize_windows_run_launch(
        actions,
        max_actions=20,
    )

    submit_index = normalized.index(actions[2])
    post_submit = normalized[submit_index + 1 :]
    assert added == 0
    assert settled is True
    assert [
        action
        for action in post_submit
        if action["type"] == "wait_for_change"
    ] == [{"type": "wait_for_change", "timeout_ms": 30_000}]


def test_standard_app_launch_adds_focus_preflight_when_budget_allows() -> None:
    actions = [
        {"type": "key", "keys": ["META", "R"]},
        {
            "type": "type_text",
            "text": "calc",
            "context": "field",
            "verification": "exact",
        },
        {"type": "key", "keys": ["ENTER"]},
        {"type": "wait_for_change", "timeout_ms": 5_000},
    ]

    normalized, _, settled = _normalize_windows_run_launch(
        actions,
        max_actions=20,
    )

    assert settled is True
    assert normalized[:4] == [
        {"type": "wait", "ms": 2_000},
        {"type": "key", "keys": ["Escape"]},
        {"type": "wait", "ms": 250},
        actions[0],
    ]
    renormalized, _, _ = _normalize_windows_run_launch(
        normalized,
        max_actions=20,
    )
    assert renormalized == normalized


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        (
            "Open Windows About settings and report the edition. "
            "Do not change any setting.",
            True,
        ),
        ("Open Display settings and set scale to 125%.", False),
        (
            "Inspect Display settings without changing any settings.",
            True,
        ),
    ],
)
def test_read_only_settings_request_detection(
    task: str,
    expected: bool,
) -> None:
    run = RunSnapshot(
        run_id="settings-request",
        task=task,
        status=RunStatus.PAUSED,
    )

    assert _is_read_only_settings_request(run) is expected


def test_reasoner_keeps_negative_safety_guards_out_of_success_criteria() -> None:
    assert "negative safety guards" in _REASONER_SYSTEM
    assert "constraints, not success_criteria" in _REASONER_SYSTEM
    assert "do not require a later screenshot" in _REASONER_SYSTEM


def test_generic_non_mutation_success_criteria_become_constraints() -> None:
    plan = PlanDecision(
        summary="Inspect Windows About without changing anything.",
        steps=["Open About", "Read the requested values"],
        success_criteria=[
            "The Windows About page is visible.",
            "The edition and version values are legible.",
            "No settings or files were changed.",
        ],
        constraints=["Do not install software."],
    )

    normalized, moved = _normalize_plan_safety_constraints(plan)

    assert moved == 1
    assert normalized.success_criteria == [
        "The Windows About page is visible.",
        "The edition and version values are legible.",
    ]
    assert normalized.constraints == [
        "Do not install software.",
        "No settings or files were changed.",
    ]


@pytest.mark.parametrize(
    "criterion",
    [
        "Notifications are disabled.",
        "The dim-screen setting is not enabled.",
        "No matching files are present in the folder.",
    ],
)
def test_visible_negative_outcomes_remain_success_criteria(
    criterion: str,
) -> None:
    plan = PlanDecision(
        summary="Verify the requested negative state.",
        steps=["Inspect the visible state"],
        success_criteria=[criterion],
    )

    normalized, moved = _normalize_plan_safety_constraints(plan)

    assert moved == 0
    assert normalized == plan


def test_normalization_preserves_at_least_one_success_criterion() -> None:
    plan = PlanDecision(
        summary="Avoid mutation.",
        steps=["Inspect the current state"],
        success_criteria=["Do not change settings or files."],
    )

    normalized, moved = _normalize_plan_safety_constraints(plan)

    assert moved == 0
    assert normalized == plan


class ScriptedProvider:
    name = "scripted"

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.role == "reasoner":
            data = {
                "summary": "Enter the requested text and verify it.",
                "steps": ["Focus the editor", "Type the text", "Verify exact text"],
                "success_criteria": ["The editor contains exactly hello world"],
                "constraints": ["Do not submit or send anything"],
            }
        elif request.role == "controller":
            data = {
                "outcome": "act",
                "intent": "Type the requested text into the already-focused editor.",
                "actions": [{"type": "type_text", "text": "hello world"}],
                "expected_evidence": ["The focused editor shows hello world"],
            }
        else:
            data = {
                "verdict": "complete",
                "summary": "The exact requested text is visible.",
                "evidence": ["Observed hello world in the focused editor"],
                "criteria": [
                    {
                        "criterion_index": 0,
                        "satisfied": True,
                        "evidence": "The editor visibly contains exactly hello world.",
                    }
                ],
            }
        return ModelResponse(provider=self.name, model="scripted-v1", data=data)


class ParallelPostActionProvider(ScriptedProvider):
    """Block until verifier and next controller are both in flight."""

    def __init__(self, *, stale_repeat: bool = False) -> None:
        super().__init__()
        self.stale_repeat = stale_repeat
        self.controller_calls = 0
        self.verifier_calls = 0
        self.parallel_roles: set[str] = set()
        self.parallel_started = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.role == "reasoner":
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "summary": "Open the setting and confirm the result.",
                    "steps": ["Open the setting", "Confirm the requested state"],
                    "success_criteria": ["The requested setting is visibly off."],
                    "constraints": ["Preserve unrelated settings."],
                },
            )
        if request.role == "controller":
            self.controller_calls += 1
            if self.controller_calls == 1:
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "outcome": "act",
                        "intent": "Open the visibly labelled setting.",
                        "actions": [{"type": "click", "x": 320, "y": 240}],
                        "expected_evidence": [
                            "The requested setting is visible."
                        ],
                    },
                )
            if self.controller_calls == 2:
                self.parallel_roles.add("controller")
                if self.parallel_roles == {"controller", "verifier"}:
                    self.parallel_started.set()
                await asyncio.wait_for(self.parallel_started.wait(), timeout=0.5)
                if self.stale_repeat:
                    return ModelResponse(
                        provider=self.name,
                        model="scripted-v1",
                        data={
                            "outcome": "act",
                            "intent": "Stale repeat of the completed action.",
                            "actions": [{"type": "click", "x": 320, "y": 240}],
                            "expected_evidence": [
                                "The requested setting is visible."
                            ],
                        },
                    )
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "done",
                    "intent": "The requested state is now visible.",
                    "actions": [],
                    "expected_evidence": [],
                },
            )
        self.verifier_calls += 1
        if self.verifier_calls == 1:
            self.parallel_roles.add("verifier")
            if self.parallel_roles == {"controller", "verifier"}:
                self.parallel_started.set()
            await asyncio.wait_for(self.parallel_started.wait(), timeout=0.5)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "verdict": "verified",
                    "summary": "The setting is visible.",
                    "evidence": ["The labelled setting is visible."],
                    "criteria": [
                        {
                            "criterion_index": 0,
                            "satisfied": False,
                            "evidence": "The final off state is not yet confirmed.",
                        }
                    ],
                    "action_criteria": [
                        {
                            "criterion_index": 0,
                            "satisfied": True,
                            "evidence": "The requested setting is visible.",
                        }
                    ],
                },
            )
        return ModelResponse(
            provider=self.name,
            model="scripted-v1",
            data={
                "verdict": "complete",
                "summary": "The requested setting is off.",
                "evidence": ["The setting visibly shows off."],
                "criteria": [
                    {
                        "criterion_index": 0,
                        "satisfied": True,
                        "evidence": "The requested setting visibly shows off.",
                    }
                ],
                "action_criteria": [],
            },
        )


def test_artifact_acceptance_pass_requires_complete_host_evidence() -> None:
    acceptance = ArtifactAcceptance(
        kind="office_artifact",
        label="Quarterly earnings workbook",
        state=ArtifactAcceptanceState.PASSED,
        artifact_format="xlsx",
        checks_passed=24,
        checks_total=24,
        byte_count=12_345,
        sha256="a" * 64,
    )

    assert acceptance.state is ArtifactAcceptanceState.PASSED
    with pytest.raises(ValidationError, match="all declared checks"):
        ArtifactAcceptance(
            kind="office_artifact",
            label="Quarterly earnings workbook",
            state=ArtifactAcceptanceState.PASSED,
            artifact_format="xlsx",
            checks_passed=23,
            checks_total=24,
            byte_count=12_345,
            sha256="a" * 64,
        )


def test_durable_model_route_rejects_duplicates_and_ambiguous_legacy_pin() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        RunModelRoute(reasoner=["strong", "strong"])

    with pytest.raises(
        ValidationError,
        match="model_provider and model_route cannot both be selected",
    ):
        RunSnapshot(
            run_id="ambiguous-model-route",
            task="Do the task",
            status=RunStatus.RUNNING,
            model_provider="strong",
            model_route=RunModelRoute(reasoner=["strong", "backup"]),
        )


@pytest.mark.asyncio
async def test_status_and_continuation_use_the_bounded_control_snapshot() -> None:
    class TrackingStore(InMemoryRunStore):
        control_reads = 0
        full_reads = 0

        async def get_control(
            self,
            run_id: str,
            event_limit: int = 1_000,
        ) -> RunSnapshot:
            self.control_reads += 1
            return await super().get_control(run_id, event_limit)

        async def get(self, run_id: str) -> RunSnapshot:
            self.full_reads += 1
            return await super().get(run_id)

    store = TrackingStore()
    run = RunSnapshot(
        run_id="bounded-agent-read",
        task="Do not replay a complete historical timeline",
        status=RunStatus.COMPLETED,
    )
    for index in range(1_200):
        run.record("run.tick", number=index)
    await store.save(run)
    harness = AgentHarness(
        computer=object(),  # type: ignore[arg-type]
        models=object(),  # type: ignore[arg-type]
        store=store,
    )

    status = await harness.status(run.run_id)
    continued = await harness.continue_run(run.run_id)

    assert len(status.events) == 1_000
    assert status.event_cursor == 1_200
    assert continued.status is RunStatus.COMPLETED
    assert store.control_reads == 2
    assert store.full_reads == 0


class TemporarilyUnavailableProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.failures_remaining = 1

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise TimeoutError("provider temporarily unavailable")
        return await super().complete(request)


class ControllerUnavailableProvider(ScriptedProvider):
    name = "controller-unavailable"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.role == "controller":
            raise TimeoutError("controller provider unavailable")
        return await super().complete(request)


class MeteredProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        return response.model_copy(
            update={"usage": {"input_tokens": 10, "output_tokens": 5}}
        )


class InitiallyBlockedProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.controller_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            self.controller_calls += 1
            if self.controller_calls == 1:
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "outcome": "blocked",
                        "intent": "Wait for approval.",
                        "actions": [],
                        "expected_evidence": [],
                        "reason": "Approval has not been recorded.",
                    },
                )
        return await super().complete(request)


class FailingVerifierProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "verifier":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "verdict": "failed",
                    "summary": "Typed text has the wrong letter case.",
                    "evidence": ["Expected uppercase but observed lowercase."],
                },
            )
        return await super().complete(request)


class CorrectingVerifierProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.controller_calls = 0
        self.verifier_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.controller_calls += 1
            if self.controller_calls == 2:
                self.requests.append(request)
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "outcome": "act",
                        "intent": "Correct the visibly mismatched text selection.",
                        "actions": [{"type": "key", "keys": ["CTRL", "A"]}],
                        "expected_evidence": [
                            "The editor text is visibly selected for correction."
                        ],
                    },
                )
        if request.role == "verifier":
            self.verifier_calls += 1
            if self.verifier_calls == 1:
                self.requests.append(request)
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "verdict": "failed",
                        "summary": "Typed text has the wrong letter case.",
                        "evidence": ["Expected uppercase but observed lowercase."],
                    },
                )
        return await super().complete(request)


class InvalidThenRepairedControllerProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.controller_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            self.controller_calls += 1
            if self.controller_calls == 1:
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "outcome": "blocked",
                        "intent": "Contradictory model output.",
                        "actions": [{"type": "key", "keys": ["End"]}],
                        "expected_evidence": [],
                        "reason": "",
                    },
                )
        return await super().complete(request)


class StallingControllerProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "verifier":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "verdict": "verified",
                    "summary": "The action was accepted but the task needs more work.",
                    "evidence": ["The editor remains visible."],
                },
            )
        return await super().complete(request)


class RepeatedUngroundedThenKeyboardProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.controller_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            self.controller_calls += 1
            actions = (
                [{"type": "key", "keys": ["META", "M"]}]
                if self.controller_calls == 3
                else [
                    {
                        "type": "click",
                        "x": 704 if self.controller_calls == 2 else 705,
                        "y": 94,
                    }
                ]
            )
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": (
                        "Minimize the visible windows with a safe shortcut."
                        if self.controller_calls == 3
                        else "Click the visible title-bar minimize control."
                    ),
                    "actions": actions,
                    "expected_evidence": ["The obstructing windows are minimized."],
                },
            )
        return await super().complete(request)


class DistinctUngroundedThenKeyboardProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.controller_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            self.controller_calls += 1
            actions_by_call = {
                1: [{"type": "click", "x": 705, "y": 94}],
                2: [{"type": "click", "x": 620, "y": 660}],
                3: [{"type": "key", "keys": ["META", "M"]}],
            }
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": f"Bounded navigation {self.controller_calls}.",
                    "actions": actions_by_call[self.controller_calls],
                    "expected_evidence": ["Word is visible and unobstructed."],
                },
            )
        return await super().complete(request)


class KeyOnlyControllerProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": "Submit the already prepared value.",
                    "actions": [{"type": "key", "keys": ["ENTER"]}],
                    "expected_evidence": ["The prepared value is submitted."],
                },
            )
        return await super().complete(request)


class GlobalShortcutControllerProvider(ScriptedProvider):
    def __init__(self, keys: list[str] | None = None) -> None:
        super().__init__()
        self.keys = keys or ["CTRL", "ALT", "T"]

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": "Open a terminal without depending on window focus.",
                    "actions": [
                        {"type": "key", "keys": self.keys},
                        {"type": "wait_for_change", "timeout_ms": 3000},
                    ],
                    "expected_evidence": [
                        "A terminal window is visible in the foreground."
                    ],
                },
            )
        return await super().complete(request)


class GlobalShortcutSequenceControllerProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": "Exit fullscreen and open the desktop overview.",
                    "actions": [
                        {"type": "key", "keys": ["ESC"]},
                        {"type": "key", "keys": ["META"]},
                    ],
                    "expected_evidence": [
                        "The desktop overview is visible instead of fullscreen video."
                    ],
                },
            )
        return await super().complete(request)


class PointerOnlyControllerProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": "Move the pointer without making task progress.",
                    "actions": [{"type": "move", "x": 450, "y": 100}],
                    "expected_evidence": ["The pointer moved."],
                },
            )
        return await super().complete(request)


class RepeatedFailedSearchProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.controller_calls = 0
        self.verifier_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            self.controller_calls += 1
            actions = (
                [{"type": "click", "x": 120, "y": 80}]
                if self.controller_calls == 2
                else [
                    {
                        "type": "type_text",
                        "text": "dim screen when inactive",
                        "context": "field",
                    }
                ]
            )
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": f"Bounded action {self.controller_calls}.",
                    "actions": actions,
                    "expected_evidence": ["The intended intermediate state is visible."],
                },
            )
        if request.role == "verifier":
            self.requests.append(request)
            self.verifier_calls += 1
            verdict = "failed" if self.verifier_calls == 1 else "verified"
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "verdict": verdict,
                    "summary": (
                        "The search returned no results."
                        if verdict == "failed"
                        else "Focus is visibly established."
                    ),
                    "evidence": ["Visible state inspected."],
                },
            )
        return await super().complete(request)


class ToggleRetryProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.controller_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "controller":
            self.requests.append(request)
            self.controller_calls += 1
            x = 522 if self.controller_calls == 1 else 513
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "act",
                    "intent": (
                        "Enable Do Not Disturb using the visible switch."
                        if self.controller_calls == 1
                        else "Retry enabling Do Not Disturb on the same switch."
                    ),
                    "actions": [{"type": "click", "x": x, "y": 302}],
                    "expected_evidence": [
                        "The Do Not Disturb switch is visibly enabled."
                    ],
                },
            )
        if request.role == "verifier":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "verdict": "failed",
                    "summary": "The switch state is visually ambiguous.",
                    "evidence": ["The colour did not settle yet."],
                },
            )
        return await super().complete(request)


class ContradictoryCompletionProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.role == "verifier":
            self.requests.append(request)
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "verdict": "complete",
                    "summary": (
                        "This completes the current navigation action, but the "
                        "overall task has not yet been performed or verified."
                    ),
                    "evidence": ["The settings entry is now visible."],
                    "criteria": [
                        {
                            "criterion_index": 0,
                            "satisfied": False,
                            "evidence": "The final setting has not been changed.",
                        }
                    ],
                },
            )
        return await super().complete(request)


class RejectedDoneProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.role == "reasoner":
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "summary": "Disable the requested setting.",
                    "steps": ["Inspect the setting", "Disable it"],
                    "success_criteria": ["The requested setting is off."],
                    "constraints": ["Preserve unrelated settings."],
                },
            )
        if request.role == "controller":
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "done",
                    "intent": "The visible GUI has no matching control.",
                    "actions": [],
                    "expected_evidence": [],
                },
            )
        return ModelResponse(
            provider=self.name,
            model="scripted-v1",
            data={
                "verdict": "complete",
                "summary": "The requested setting is still on.",
                "evidence": ["No matching GUI control is visible."],
                "criteria": [
                    {
                        "criterion_index": 0,
                        "satisfied": False,
                        "evidence": "The setting has not been disabled.",
                    }
                ],
            },
        )


class FakeComputer:
    def __init__(self) -> None:
        self.bursts: list[dict[str, Any]] = []
        self.aborts: list[dict[str, str]] = []

    async def open(self, label: str) -> ComputerObservation:
        return ComputerObservation(
            session_id="s_1",
            status="paused",
            frame_id=1,
            world_version=7,
            control_epoch=2,
            image_path="/tmp/frame-before.jpg",
        )

    async def burst(
        self,
        *,
        session_id: str,
        actions: list[dict[str, Any]],
        based_on_world_version: int | None,
        based_on_control_epoch: int | None,
        idempotency_key: str,
    ) -> ComputerObservation:
        self.bursts.append(
            {
                "session_id": session_id,
                "actions": actions,
                "based_on_world_version": based_on_world_version,
                "based_on_control_epoch": based_on_control_epoch,
                "idempotency_key": idempotency_key,
            }
        )
        return ComputerObservation(
            session_id=session_id,
            status="completed",
            frame_id=2,
            world_version=8,
            control_epoch=2,
            image_path="/tmp/frame-after.jpg",
        )

    async def refresh(self, *, session_id: str) -> ComputerObservation:
        return ComputerObservation(
            session_id=session_id,
            status="paused",
            frame_id=3,
            world_version=9,
            control_epoch=2,
            image_path="/tmp/frame-refreshed.jpg",
        )

    async def resolve_approval(
        self, *, session_id: str, approval_id: str, decision: dict[str, Any]
    ) -> ComputerObservation:
        raise AssertionError("no approval expected")

    async def abort(self, *, session_id: str, reason: str) -> ComputerObservation:
        self.aborts.append({"session_id": session_id, "reason": reason})
        return ComputerObservation(session_id=session_id, status="aborted")


class RefreshTrackingComputer(FakeComputer):
    def __init__(self) -> None:
        super().__init__()
        self.refreshes = 0

    async def refresh(self, *, session_id: str) -> ComputerObservation:
        self.refreshes += 1
        return await super().refresh(session_id=session_id)


class MissingAbortSessionComputer(FakeComputer):
    async def abort(
        self,
        *,
        session_id: str,
        reason: str,
    ) -> ComputerObservation:
        del session_id, reason
        raise ComputerSessionMissingError("computer session no longer exists")


class ApprovalComputer(FakeComputer):
    def __init__(self) -> None:
        super().__init__()
        self.resolutions: list[dict[str, Any]] = []

    async def burst(self, **kwargs: Any) -> ComputerObservation:
        self.bursts.append(kwargs)
        return ComputerObservation(
            session_id=kwargs["session_id"],
            status="needs_approval",
            frame_id=1,
            world_version=7,
            control_epoch=2,
            approval_request={
                "approval_id": "approval_1",
                "risk": "communication_send",
                "summary": "Send a message",
            },
        )

    async def resolve_approval(
        self, *, session_id: str, approval_id: str, decision: dict[str, Any]
    ) -> ComputerObservation:
        self.resolutions.append(
            {
                "session_id": session_id,
                "approval_id": approval_id,
                "decision": decision,
            }
        )
        return ComputerObservation(
            session_id=session_id,
            status="completed" if decision["type"] == "approve" else "rejected",
            frame_id=2,
            world_version=8,
            control_epoch=2,
        )


class UngroundedNavigationComputer(FakeComputer):
    def __init__(self) -> None:
        super().__init__()
        self.resolutions: list[dict[str, Any]] = []
        self.opens = 0

    async def open(self, label: str) -> ComputerObservation:
        self.opens += 1
        observation = await super().open(label)
        return observation.model_copy(
            update={
                "session_id": f"s_{self.opens}",
                "frame_id": self.opens,
                "world_version": 6 + self.opens,
            }
        )

    async def burst(self, **kwargs: Any) -> ComputerObservation:
        self.bursts.append(kwargs)
        return ComputerObservation(
            session_id=kwargs["session_id"],
            status="needs_approval",
            frame_id=2,
            world_version=7,
            control_epoch=2,
            approval_request={
                "kind": "direct_burst",
                "approval_id": "unknown_click_1",
                "risk": "unknown",
                "reason": (
                    "coordinate click target could not be independently read"
                ),
            },
        )

    async def resolve_approval(
        self, *, session_id: str, approval_id: str, decision: dict[str, Any]
    ) -> ComputerObservation:
        self.resolutions.append(
            {
                "session_id": session_id,
                "approval_id": approval_id,
                "decision": decision,
            }
        )
        return ComputerObservation(
            session_id=session_id,
            status="blocked",
            frame_id=2,
            world_version=7,
            control_epoch=2,
        )


class UngroundedThenKeyboardComputer(UngroundedNavigationComputer):
    async def burst(self, **kwargs: Any) -> ComputerObservation:
        if kwargs["actions"] == [{"type": "key", "keys": ["META", "M"]}]:
            return await FakeComputer.burst(self, **kwargs)
        return await super().burst(**kwargs)


class StaleThenFreshComputer(FakeComputer):
    def __init__(self) -> None:
        super().__init__()
        self.refreshes = 0

    async def burst(self, **kwargs: Any) -> ComputerObservation:
        self.bursts.append(kwargs)
        if len(self.bursts) == 1:
            return ComputerObservation(
                session_id=kwargs["session_id"],
                status="stale_world",
                frame_id=2,
                world_version=8,
                control_epoch=2,
            )
        return ComputerObservation(
            session_id=kwargs["session_id"],
            status="completed",
            frame_id=4,
            world_version=10,
            control_epoch=2,
            image_path="/tmp/frame-after.jpg",
        )

    async def refresh(self, *, session_id: str) -> ComputerObservation:
        self.refreshes += 1
        return ComputerObservation(
            session_id=session_id,
            status="paused",
            frame_id=3,
            world_version=9,
            control_epoch=2,
            image_path="/tmp/frame-refreshed.jpg",
        )


class FlakyComputer(FakeComputer):
    def __init__(self) -> None:
        super().__init__()
        self.keys: list[str] = []

    async def burst(self, **kwargs: Any) -> ComputerObservation:
        self.bursts.append(kwargs)
        self.keys.append(kwargs["idempotency_key"])
        if len(self.bursts) == 1:
            raise TimeoutError("response lost after submission")
        return ComputerObservation(
            session_id=kwargs["session_id"],
            status="completed",
            frame_id=2,
            world_version=8,
            control_epoch=2,
            image_path="/tmp/frame-after.jpg",
        )


class RestartedComputer(FakeComputer):
    def __init__(self) -> None:
        super().__init__()
        self.open_calls = 0

    async def open(self, label: str) -> ComputerObservation:
        self.open_calls += 1
        return ComputerObservation(
            session_id="s_reopened",
            status="paused",
            frame_id=1,
            world_version=1,
            control_epoch=0,
            image_path="/tmp/frame-reopened.jpg",
        )

    async def burst(self, **kwargs: Any) -> ComputerObservation:
        assert kwargs["session_id"] == "s_reopened"
        return await super().burst(**kwargs)


class FocusLostComputer(FakeComputer):
    async def burst(self, **kwargs: Any) -> ComputerObservation:
        self.bursts.append(kwargs)
        return ComputerObservation(
            session_id=kwargs["session_id"],
            status="failed",
            frame_id=2,
            world_version=7,
            control_epoch=2,
            error="typed text did not change the screen",
            raw={"reason": "type_unverified"},
        )


class UnverifiedTypingComputer(FakeComputer):
    async def burst(self, **kwargs: Any) -> ComputerObservation:
        self.bursts.append(kwargs)
        return ComputerObservation(
            session_id=kwargs["session_id"],
            status="unverified",
            frame_id=2,
            world_version=8,
            control_epoch=2,
            image_path="/tmp/frame-after.jpg",
            error="OCR could not prove the exact typed text",
            raw={"reason": "type_unverified"},
        )


class OnceOnlyUnverifiedSearchComputer(FakeComputer):
    async def burst(self, **kwargs: Any) -> ComputerObservation:
        self.bursts.append(kwargs)
        return ComputerObservation(
            session_id=kwargs["session_id"],
            status="unverified",
            frame_id=2,
            world_version=8,
            control_epoch=2,
            image_path="/tmp/frame-after.jpg",
            error="OCR could not read the search draft",
            raw={
                "reason": "type_unverified",
                "action_receipts": [
                    {
                        "index": 0,
                        "type": "type_text",
                        "status": "unverified_ambiguous",
                        "verdict": "unverified",
                        "focus_evidence": "read_back_unverified",
                        "proof_state": "issued_only",
                        "requested_characters": 8,
                        "delivery_characters": 8,
                        "issued_characters": 8,
                        "emitted_characters": 8,
                        "emitted_exactly_once": True,
                    }
                ],
            },
        )


class SearchRecoveryProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.controller_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.role == "reasoner":
            data = {
                "summary": "Find the Settings application.",
                "steps": ["Search for Settings", "Confirm the result"],
                "success_criteria": ["The Settings result is visible"],
                "constraints": ["Do not open unrelated applications"],
            }
        elif request.role == "controller":
            self.controller_calls += 1
            if self.controller_calls == 1:
                data = {
                    "outcome": "act",
                    "intent": "Search for the Settings application.",
                    "actions": [
                        {
                            "type": "type_text",
                            "text": "Settings",
                            "secret": False,
                            "code": False,
                        }
                    ],
                    "expected_evidence": ["The Settings result is visible"],
                }
            else:
                data = {
                    "outcome": "done",
                    "intent": "The Settings result is visible.",
                    "actions": [],
                    "expected_evidence": [],
                }
        else:
            data = {
                "verdict": "complete",
                "summary": "The Settings result is visible.",
                "evidence": ["The current frame shows the Settings result."],
                "criteria": [
                    {
                        "criterion_index": 0,
                        "satisfied": True,
                        "evidence": "The Settings result is visible.",
                    }
                ],
                "action_criteria": [],
            }
        return ModelResponse(provider=self.name, model="scripted-v1", data=data)


class InputReceiptComputer(FakeComputer):
    async def burst(self, **kwargs: Any) -> ComputerObservation:
        self.bursts.append(kwargs)
        return ComputerObservation(
            session_id=kwargs["session_id"],
            status="completed",
            frame_id=2,
            world_version=8,
            control_epoch=2,
            image_path="/tmp/frame-after.jpg",
            raw={
                "action_receipts": [
                    {
                        "index": 0,
                        "type": "type_text",
                        "status": "verified_exact",
                        "verdict": "match",
                        "observed_text": "hello world",
                        "observed_text_redacted": False,
                        "issued_characters": 11,
                        "requested_characters": 11,
                        "observed_characters": 11,
                        "correction_count": 1,
                        "delivery_retries": 0,
                        "used_fast_path": False,
                        "summary": "Typed and verified.",
                        "edit_distance": 0,
                        "focus_evidence": "read_back_verified",
                        "requested_sha256": "a" * 64,
                        "issued_prefix_sha256": "a" * 64,
                        "readback_sha256": "a" * 64,
                        "exact_readback_sha256_match": True,
                        "private_path": "/tmp/do-not-expose.png",
                        "unknown": {"nested": "value"},
                    },
                    {
                        "index": 99,
                        "type": "type_text",
                        "status": "verified_exact",
                        "observed_text": "not a submitted action",
                    },
                ]
            },
        )


class ImageComputer(FakeComputer):
    def __init__(self, before: Path, after: Path) -> None:
        super().__init__()
        self.before = before
        self.after = after

    async def open(self, label: str) -> ComputerObservation:
        observation = await super().open(label)
        return observation.model_copy(update={"image_path": str(self.before)})

    async def burst(self, **kwargs: Any) -> ComputerObservation:
        observation = await super().burst(**kwargs)
        return observation.model_copy(update={"image_path": str(self.after)})


class TargetSwitchComputer(FakeComputer):
    async def open(self, label: str) -> ComputerObservation:
        observation = await super().open(label)
        return observation.model_copy(
            update={
                "machine": {
                    "alias": "Machine A",
                    "fingerprint": "target:aaaaaaaaaaaaaaaa",
                }
            }
        )

    async def burst(self, **kwargs: Any) -> ComputerObservation:
        observation = await super().burst(**kwargs)
        return observation.model_copy(
            update={
                "machine": {
                    "alias": "Machine B",
                    "fingerprint": "target:bbbbbbbbbbbbbbbb",
                }
            }
        )


def build_harness(
    provider: ScriptedProvider, computer: FakeComputer
) -> AgentHarness:
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            role: RoleRoute(providers=[provider.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    return AgentHarness(
        computer=computer,
        models=pool,
        store=InMemoryRunStore(),
        config=HarnessConfig(max_actions_per_advance=1),
    )


@pytest.mark.asyncio
async def test_failed_verifier_rechecks_one_fresh_delayed_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_path = tmp_path / "before.png"
    stale_path = tmp_path / "stale.png"
    fresh_path = tmp_path / "fresh.png"
    Image.new("RGB", (64, 48), "black").save(before_path)
    Image.new("RGB", (64, 48), "navy").save(stale_path)
    Image.new("RGB", (64, 48), "green").save(fresh_path)

    class DelayedFrameProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__()
            self.verifier_calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            assert request.role == "verifier"
            self.requests.append(request)
            self.verifier_calls += 1
            verified = self.verifier_calls == 2
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "verdict": "verified" if verified else "failed",
                    "summary": (
                        "The dialog is closed in the fresh frame."
                        if verified
                        else "The stale frame still shows the dialog."
                    ),
                    "evidence": ["The visible dialog state was inspected."],
                    "action_criteria": [
                        {
                            "criterion_index": 0,
                            "satisfied": verified,
                            "evidence": (
                                "The dialog is visibly closed."
                                if verified
                                else "The dialog is still visible."
                            ),
                        }
                    ],
                },
            )

    class DelayedFrameComputer(FakeComputer):
        def __init__(self) -> None:
            super().__init__()
            self.refreshes = 0

        async def refresh(self, *, session_id: str) -> ComputerObservation:
            self.refreshes += 1
            return ComputerObservation(
                session_id=session_id,
                status="paused",
                frame_id=3,
                world_version=8,
                control_epoch=2,
                image_path=str(fresh_path),
                image_sha256="c" * 64,
                screen_hash="d" * 512,
            )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    provider = DelayedFrameProvider()
    computer = DelayedFrameComputer()
    harness = build_harness(provider, computer)
    before = ComputerObservation(
        session_id="s_1",
        status="paused",
        frame_id=1,
        world_version=7,
        control_epoch=2,
        image_path=str(before_path),
        image_sha256="a" * 64,
        screen_hash="b" * 512,
    )
    run = RunSnapshot(
        run_id="delayed-frame-recheck",
        task="Save the file.",
        status=RunStatus.RUNNING,
        session_id="s_1",
        observation=ComputerObservation(
            session_id="s_1",
            status="completed",
            frame_id=2,
            world_version=8,
            control_epoch=2,
            image_path=str(stale_path),
            image_sha256="b" * 64,
            screen_hash="c" * 512,
        ),
    )
    action = PendingAction(
        index=1,
        intent="Save the verified file.",
        actions=[{"type": "click", "x": 50, "y": 40}],
        expected_evidence=["The dialog closes."],
        based_on_world_version=7,
        based_on_control_epoch=2,
        idempotency_key="delayed-frame-save",
    )

    await harness._verify(run, action=action, before=before)

    assert provider.verifier_calls == 2
    assert computer.refreshes == 1
    assert run.status is RunStatus.RUNNING
    assert run.last_verification is not None
    assert run.last_verification.verdict == "verified"
    assert any(
        event.kind == "verification.delayed_frame_observed"
        for event in run.events
    )


@pytest.mark.asyncio
async def test_failed_verifier_does_not_recall_model_for_unchanged_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame_path = tmp_path / "unchanged.png"
    Image.new("RGB", (64, 48), "navy").save(frame_path)

    class FailedProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__()
            self.verifier_calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            assert request.role == "verifier"
            self.verifier_calls += 1
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "verdict": "failed",
                    "summary": "The dialog remains open.",
                    "evidence": ["The visible frame is unchanged."],
                    "action_criteria": [
                        {
                            "criterion_index": 0,
                            "satisfied": False,
                            "evidence": "The dialog is still visible.",
                        }
                    ],
                },
            )

    class UnchangedComputer(FakeComputer):
        def __init__(self) -> None:
            super().__init__()
            self.refreshes = 0

        async def refresh(self, *, session_id: str) -> ComputerObservation:
            self.refreshes += 1
            return ComputerObservation(
                session_id=session_id,
                status="paused",
                frame_id=2 + self.refreshes,
                world_version=8,
                control_epoch=2,
                image_path=str(frame_path),
                image_sha256="b" * 64,
                screen_hash="c" * 512,
            )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    provider = FailedProvider()
    computer = UnchangedComputer()
    harness = build_harness(provider, computer)
    observation = ComputerObservation(
        session_id="s_1",
        status="completed",
        frame_id=2,
        world_version=8,
        control_epoch=2,
        image_path=str(frame_path),
        image_sha256="b" * 64,
        screen_hash="c" * 512,
    )
    run = RunSnapshot(
        run_id="unchanged-frame-no-recheck",
        task="Save the file.",
        status=RunStatus.RUNNING,
        session_id="s_1",
        observation=observation,
    )
    action = PendingAction(
        index=1,
        intent="Save the verified file.",
        actions=[{"type": "click", "x": 50, "y": 40}],
        expected_evidence=["The dialog closes."],
        based_on_world_version=7,
        based_on_control_epoch=2,
        idempotency_key="unchanged-frame-save",
    )

    await harness._verify(run, action=action, before=observation)

    assert provider.verifier_calls == 1
    assert computer.refreshes == 3
    assert run.status is RunStatus.PAUSED
    assert any(
        event.kind == "verification.delayed_frame_unchanged"
        for event in run.events
    )


@pytest.mark.asyncio
async def test_delayed_verification_frame_discards_speculative_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_path = tmp_path / "before.png"
    stale_path = tmp_path / "stale.png"
    fresh_path = tmp_path / "fresh.png"
    Image.new("RGB", (64, 48), "black").save(before_path)
    Image.new("RGB", (64, 48), "navy").save(stale_path)
    Image.new("RGB", (64, 48), "green").save(fresh_path)

    class DelayedParallelProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__()
            self.verifier_calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            if request.role == "controller":
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "outcome": "act",
                        "intent": "Click the stale-frame follow-up.",
                        "actions": [{"type": "click", "x": 30, "y": 20}],
                        "expected_evidence": ["The stale control changes."],
                    },
                )
            assert request.role == "verifier"
            self.verifier_calls += 1
            verified = self.verifier_calls == 2
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "verdict": "verified" if verified else "failed",
                    "summary": (
                        "The fresh frame proves the prior action."
                        if verified
                        else "The stale frame does not prove the prior action."
                    ),
                    "evidence": ["The visible frame was inspected."],
                    "criteria": [
                        {
                            "criterion_index": 0,
                            "satisfied": False,
                            "evidence": "The overall task is not complete.",
                        }
                    ],
                    "action_criteria": [
                        {
                            "criterion_index": 0,
                            "satisfied": verified,
                            "evidence": (
                                "The fresh frame proves the transition."
                                if verified
                                else "The stale frame does not prove it."
                            ),
                        }
                    ],
                },
            )

    class DelayedParallelComputer(FakeComputer):
        async def refresh(self, *, session_id: str) -> ComputerObservation:
            return ComputerObservation(
                session_id=session_id,
                status="paused",
                frame_id=3,
                world_version=8,
                control_epoch=2,
                image_path=str(fresh_path),
                image_sha256="c" * 64,
                screen_hash="d" * 512,
            )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    provider = DelayedParallelProvider()
    computer = DelayedParallelComputer()
    harness = build_harness(provider, computer)
    before = ComputerObservation(
        session_id="s_1",
        status="paused",
        frame_id=1,
        world_version=7,
        control_epoch=2,
        image_path=str(before_path),
        image_sha256="a" * 64,
        screen_hash="b" * 512,
    )
    run = RunSnapshot(
        run_id="delayed-frame-prefetch-discard",
        task="Save the file and continue.",
        status=RunStatus.RUNNING,
        session_id="s_1",
        plan=PlanDecision(
            summary="Save the file and continue.",
            steps=["Save the file", "Continue from the fresh state"],
            success_criteria=["The file is saved."],
        ),
        observation=ComputerObservation(
            session_id="s_1",
            status="completed",
            frame_id=2,
            world_version=8,
            control_epoch=2,
            image_path=str(stale_path),
            image_sha256="b" * 64,
            screen_hash="c" * 512,
        ),
    )
    action = PendingAction(
        index=1,
        intent="Save the verified file.",
        actions=[{"type": "click", "x": 50, "y": 40}],
        expected_evidence=["The dialog closes."],
        based_on_world_version=7,
        based_on_control_epoch=2,
        idempotency_key="delayed-frame-parallel-save",
    )

    await harness._verify_and_prefetch_control(
        run,
        action=action,
        before=before,
    )

    assert provider.verifier_calls == 2
    assert run.status is RunStatus.RUNNING
    assert run.run_id not in harness._prefetched_controllers
    discarded = [
        event
        for event in run.events
        if event.kind == "controller.parallel_discarded"
    ]
    assert discarded
    assert (
        discarded[-1].data["reason"]
        == "delayed verification frame invalidated the speculative controller"
    )


@pytest.mark.asyncio
async def test_calculator_task_skips_reasoner_and_controller_round_trips() -> None:
    class CalculatorProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__()
            self.controller_calls = 0
            self.verifier_calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            if request.role == "reasoner":
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "summary": "Open Calculator and evaluate the expression.",
                        "steps": ["Open Calculator", "Evaluate 37 times 19"],
                        "success_criteria": [
                            "Calculator visibly displays exactly 703."
                        ],
                        "constraints": [],
                    },
                )
            if request.role == "controller":
                self.controller_calls += 1
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "outcome": "act",
                        "intent": "Launch Calculator.",
                        "actions": [
                            {"type": "key", "keys": ["WIN", "R"]},
                            {"type": "wait", "ms": 300},
                            {
                                "type": "type_text",
                                "text": "calc",
                                "context": "field",
                                "verification": "exact",
                            },
                            {"type": "key", "keys": ["ENTER"]},
                        ],
                        "expected_evidence": [
                            "Windows Calculator is visibly open."
                        ],
                    },
                )
            self.verifier_calls += 1
            if self.verifier_calls == 1:
                summary = (
                    "Windows Calculator is visibly open in Standard mode."
                )
                data = {
                    "verdict": "verified",
                    "summary": summary,
                    "evidence": [summary],
                    "action_criteria": [
                        {
                            "criterion_index": 0,
                            "satisfied": True,
                            "evidence": summary,
                        }
                    ],
                }
            else:
                data = {
                    "verdict": "complete",
                    "summary": "Calculator visibly displays exactly 703.",
                    "evidence": ["The main display reads 703."],
                    "criteria": [
                        {
                            "criterion_index": 0,
                            "satisfied": True,
                            "evidence": "The main display reads 703.",
                        }
                    ],
                }
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data=data,
            )

    provider = CalculatorProvider()
    computer = FakeComputer()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            role: RoleRoute(providers=[provider.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    harness = AgentHarness(
        computer=computer,
        models=pool,
        store=InMemoryRunStore(),
        config=HarnessConfig(
            max_actions_per_advance=2,
            max_actions_per_burst=8,
        ),
    )

    completed = await harness.start(
        "Use Windows Calculator to compute 37 multiplied by 19. "
        "Leave the exact result visible and report it."
    )

    assert completed.status is RunStatus.COMPLETED
    assert [
        request.role for request in provider.requests
    ] == ["verifier", "verifier"]
    assert provider.controller_calls == 0
    assert provider.verifier_calls == 2
    assert len(computer.bursts) == 2
    assert computer.bursts[1]["actions"] == [
        {"type": "key", "keys": ["Digit3"]},
        {"type": "key", "keys": ["Digit7"]},
        {"type": "key", "keys": ["NumpadMultiply"]},
        {"type": "key", "keys": ["Digit1"]},
        {"type": "key", "keys": ["Digit9"]},
        {"type": "key", "keys": ["Enter"]},
        {"type": "wait_for_change", "timeout_ms": 2_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 400,
            "timeout_ms": 3_000,
        },
    ]
    assert any(
        event.kind == "controller.calculator_expression_prepared"
        for event in completed.events
    )
    assert any(
        event.kind == "plan.calculator_fast_path"
        for event in completed.events
    )


@pytest.mark.asyncio
async def test_controller_replan_refreshes_the_screen_before_autonomous_resume() -> None:
    class ReplanProvider(ScriptedProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            if request.role == "reasoner":
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "summary": "Inspect the current screen.",
                        "steps": ["Inspect the current screen."],
                        "success_criteria": ["The visible state is known."],
                        "constraints": ["Do not guess from a blank frame."],
                    },
                )
            return ModelResponse(
                provider=self.name,
                model="scripted-v1",
                data={
                    "outcome": "replan",
                    "intent": "Refresh a transient blank frame.",
                    "actions": [],
                    "expected_evidence": [],
                    "reason": "The supplied frame is transiently blank.",
                },
            )

    provider = ReplanProvider()
    computer = RefreshTrackingComputer()
    result = await build_harness(provider, computer).start(
        "Open Calculator from the visible Windows desktop."
    )

    assert result.status is RunStatus.PAUSED
    assert computer.refreshes == 1
    assert result.observation is not None
    assert result.observation.frame_id == 3
    assert result.events[-1].kind == "computer.refreshed_after_replan"


def test_controller_action_schema_rejects_unknown_hid_and_verification_bypass() -> None:
    with pytest.raises(ValidationError):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Run an invented action",
                "actions": [{"type": "shell", "command": "whoami"}],
                "expected_evidence": [],
                "reason": "",
            }
        )
    with pytest.raises(ValidationError, match="no_verify"):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Bypass read-back",
                "actions": [
                    {
                        "type": "type_text",
                        "text": "hello",
                        "no_verify": True,
                    }
                ],
                "expected_evidence": [],
                "reason": "",
            }
        )


def test_controller_action_schema_rejects_duplicate_pointer_moves() -> None:
    with pytest.raises(ValidationError, match="duplicate consecutive pointer move"):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Preserve focus before typing.",
                "actions": [
                    {"type": "move", "x": 1132, "y": 539},
                    {"type": "move", "x": 1132, "y": 539},
                    {"type": "move", "x": 1132, "y": 539},
                ],
                "expected_evidence": ["Focus remains unchanged."],
            }
        )


def test_controller_action_schema_rejects_pointer_only_wiggle() -> None:
    with pytest.raises(ValidationError, match="multiple pointer-only moves"):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Preserve the current terminal state.",
                "actions": [
                    {"type": "move", "x": 353, "y": 245},
                    {"type": "move", "x": 349, "y": 248},
                    {"type": "move", "x": 353, "y": 245},
                    {"type": "move", "x": 349, "y": 248},
                ],
                "expected_evidence": ["The terminal remains unchanged."],
            }
        )


def test_controller_can_request_one_bounded_spreadsheet_grid() -> None:
    decision = ControllerDecision.model_validate(
        {
            "outcome": "act",
            "intent": "Enter the quarterly table from the verified active cell.",
            "actions": [
                {
                    "type": "spreadsheet_grid",
                    "rows": [["Q1", "124.8"], ["Q2", "132.1"]],
                }
            ],
            "expected_evidence": ["The two spreadsheet rows are visible."],
        }
    )

    assert decision.actions[0].model_dump(mode="json") == {
        "type": "spreadsheet_grid",
        "rows": [["Q1", "124.8"], ["Q2", "132.1"]],
    }


async def test_controller_prompt_limits_grid_entry_to_a_verified_spreadsheet_cell() -> None:
    provider = ScriptedProvider()
    harness = build_harness(provider, FakeComputer())

    await harness.start("Enter a small quarterly table in the workbook.")

    prompt = next(
        request.prompt
        for request in provider.requests
        if request.role == "controller"
    )
    assert "spreadsheet_grid" in prompt
    assert "verified active spreadsheet cell" in prompt
    assert "Never use it in messaging" in prompt
    assert "one reviewed local-file action" in prompt
    assert "Treat recent_verified_actions as durable evidence" in prompt


async def test_controller_prompt_prefers_a_stable_legible_end_state() -> None:
    provider = ScriptedProvider()
    harness = build_harness(provider, FakeComputer())

    await harness.start("Open Calculator and calculate 37 × 19.")

    prompt = next(
        request.prompt
        for request in provider.requests
        if request.role == "controller"
    )
    normalized = " ".join(prompt.split())
    assert "stable, directly legible local end state" in normalized
    assert "complete reversible local operation, not one mouse click" in normalized
    assert "group the full sequence of reversible local inputs" in normalized
    assert "one controller/verifier round trip on each digit" in normalized
    assert "complete expression including the equals key" in normalized
    assert "tiny expression-history text" in normalized
    assert "consequential commit actions" in normalized
    assert "click the visibly labelled Save button" in normalized
    assert "Never use bare Enter to commit a Save As dialog" in normalized
    assert "modern Notepad restores an old tab" in normalized
    assert "use Ctrl+N to create a new blank document" in normalized
    assert "Do not click into or overwrite restored content" in normalized
    assert "never put newline control characters inside type_text" in normalized
    assert "two separate Shift+Enter key actions" in normalized
    assert "non-submitting blank-line action" in normalized
    assert "including generated prose" in normalized
    assert "Never propose bare Enter for an editor line break" in normalized
    assert "Never send indentation as a whitespace-only editor type_text" in normalized
    assert "never use Tab to create code indentation" in normalized
    assert "natural word boundary within the 240-character limit" in normalized
    assert "Never concatenate two words or omit their separator" in normalized
    assert "set code true for that format-sensitive text segment" in normalized
    assert "accidental repeated spaces in ordinary prose remain blocked" in normalized
    assert "use Ctrl+O to open the native Open dialog" in normalized
    assert "do not refocus the editor first" in normalized
    assert "Ctrl+Shift+S and Save As are not reopen actions" in normalized
    assert "completed Open-dialog action" in normalized


async def test_reasoner_prompt_avoids_duplicate_pre_and_post_save_audits() -> None:
    provider = ScriptedProvider()
    harness = build_harness(provider, FakeComputer())

    await harness.start(
        "Create the workbook, save it, reopen it, and verify every required value."
    )

    prompt = next(
        request.prompt
        for request in provider.requests
        if request.role == "reasoner"
    )
    assert "do not plan a complete content audit both before and after saving" in prompt
    assert "perform the requested detailed audit once, after reopening" in prompt
    assert "simultaneously legible in one frame" in prompt
    assert "do not cancel an already-open Save As dialog solely to resume an audit" in prompt
    assert "Treat recent_verified_actions as durable evidence" in prompt


async def test_reasoner_can_plan_a_short_visible_terminal_fallback() -> None:
    provider = ScriptedProvider()
    harness = build_harness(provider, FakeComputer())

    await harness.start("Disable the local dim-screen setting.")

    prompt = next(
        request.prompt
        for request in provider.requests
        if request.role == "reasoner"
    )
    normalized = " ".join(prompt.split())
    assert "on-screen terminal" in normalized
    assert "short, inspectable command" in normalized
    assert "exact GUI control is absent" in normalized
    assert "not a hidden side channel" in normalized
    assert "Never use this fallback for a long script" in normalized
    assert "Do not invent a GUI-only or no-terminal constraint" in normalized
    assert "missing from the visible GUI, replan to that fallback" in normalized
    assert "maximize or widen the terminal" in normalized
    assert "increase its text size" in normalized
    assert "never append a guessed suffix" in normalized
    assert "cancel the draft with Ctrl+C" in normalized


async def test_controller_handles_an_unverified_terminal_draft_without_guessing() -> None:
    provider = ScriptedProvider()
    harness = build_harness(provider, FakeComputer())

    await harness.start("Disable the local dim-screen setting.")

    prompt = next(
        request.prompt
        for request in provider.requests
        if request.role == "controller"
    )
    normalized = " ".join(prompt.split())
    assert "never append guessed missing characters" in normalized
    assert "do not press Enter" in normalized
    assert "cancel the draft with Ctrl+C" in normalized
    assert "visibly clean prompt" in normalized
    assert "long exact terminal draft" in normalized
    assert "separate verified width action" in normalized
    assert "separate verified text-size increase" in normalized
    assert "request a replan instead of blocking" in normalized
    assert "model-invented GUI-only or no-terminal constraint" in normalized


def test_controller_separates_spreadsheet_focus_from_grid_entry() -> None:
    with pytest.raises(
        ValidationError,
        match="separate verified focus action",
    ):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Click the first cell and enter the table.",
                "actions": [
                    {"type": "click", "x": 160, "y": 240},
                    {
                        "type": "spreadsheet_grid",
                        "rows": [["Q1", "124.8"]],
                    },
                ],
                "expected_evidence": ["The row is visible."],
            }
        )


def test_controller_action_schema_rejects_duplicate_click_within_burst() -> None:
    with pytest.raises(ValidationError, match="duplicate pointer activation"):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Focus the terminal once.",
                "actions": [
                    {"type": "move", "x": 300, "y": 90},
                    {"type": "click", "x": 300, "y": 90},
                    {"type": "move", "x": 300, "y": 90},
                    {"type": "click", "x": 300, "y": 90},
                ],
                "expected_evidence": ["The terminal has keyboard focus."],
            }
        )


@pytest.mark.parametrize("text", ["echo ready\n", "echo ready\r", "left\tright"])
def test_controller_action_schema_rejects_control_characters_in_text(
    text: str,
) -> None:
    with pytest.raises(ValidationError, match="control characters"):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Type without committing.",
                "actions": [
                    {
                        "type": "type_text",
                        "text": text,
                        "context": "terminal",
                    }
                ],
                "expected_evidence": ["The exact text is visible at the prompt."],
            }
        )


@pytest.mark.parametrize("text", ["", " ", "    "])
@pytest.mark.parametrize("context", ["", "editor", "field", "terminal"])
def test_controller_action_schema_rejects_invisible_text(
    text: str,
    context: str,
) -> None:
    with pytest.raises(ValidationError, match="whitespace-only type_text"):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Indent the next editor line.",
                "actions": [
                    {
                        "type": "type_text",
                        "text": text,
                        "context": context,
                        "verification": "exact",
                    }
                ],
                "expected_evidence": ["The editor line is indented."],
            }
        )


@pytest.mark.parametrize(
    "follow_up",
    [
        {"type": "key", "keys": ["ENTER"]},
        {"type": "click", "x": 500, "y": 400},
        {"type": "scroll", "direction": "down", "amount": 2},
    ],
)
def test_controller_action_schema_separates_text_from_active_follow_up(
    follow_up: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="active follow-up"):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Prepare text without committing it.",
                "actions": [
                    {
                        "type": "type_text",
                        "text": "find video.mp4",
                        "context": "terminal",
                    },
                    follow_up,
                ],
                "expected_evidence": ["The exact text is visible."],
            }
        )


def test_controller_action_schema_allows_passive_evidence_after_text() -> None:
    decision = ControllerDecision.model_validate(
        {
            "outcome": "act",
            "intent": "Prepare text and wait for settled pixels.",
            "actions": [
                {
                    "type": "type_text",
                    "text": "find video.mp4",
                    "context": "terminal",
                },
                {
                    "type": "wait_for_stable_screen",
                    "stable_ms": 300,
                    "timeout_ms": 1500,
                },
            ],
            "expected_evidence": ["The exact text is visible."],
        }
    )

    assert [action.type for action in decision.actions] == [
        "type_text",
        "wait_for_stable_screen",
    ]


@pytest.mark.parametrize(
    "text",
    ["ms-settings:about", "notepad", "explorer.exe"],
)
def test_controller_action_schema_allows_verified_windows_run_launch(
    text: str,
) -> None:
    decision = ControllerDecision.model_validate(
        {
            "outcome": "act",
            "intent": "Open one safe local Windows surface.",
            "actions": [
                {"type": "key", "keys": ["META", "R"]},
                {"type": "wait", "ms": 150},
                {
                    "type": "wait_for_stable_screen",
                    "stable_ms": 150,
                    "timeout_ms": 1500,
                },
                {
                    "type": "type_text",
                    "text": text,
                    "context": "field",
                    "verification": "exact",
                },
                {"type": "key", "keys": ["ENTER"]},
                {
                    "type": "wait_for_stable_screen",
                    "stable_ms": 300,
                    "timeout_ms": 3000,
                },
            ],
            "expected_evidence": ["The requested local surface is visible."],
        }
    )

    assert [action.type for action in decision.actions] == [
        "key",
        "wait",
        "wait_for_stable_screen",
        "type_text",
        "key",
        "wait_for_stable_screen",
    ]


def test_controller_action_schema_rejects_standalone_windows_run() -> None:
    with pytest.raises(
        ValueError,
        match="same atomic launch burst",
    ):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Open Run before deciding what to type.",
                "actions": [{"type": "key", "keys": ["WIN", "R"]}],
                "expected_evidence": ["The Run dialog is visible."],
            }
        )


@pytest.mark.parametrize(
    ("prefix", "text_action"),
    [
        (
            [],
            {
                "type": "type_text",
                "text": "ms-settings:about",
                "context": "field",
                "verification": "exact",
            },
        ),
        (
            [{"type": "click", "x": 400, "y": 300}],
            {
                "type": "type_text",
                "text": "ms-settings:about",
                "context": "field",
                "verification": "exact",
            },
        ),
        (
            [{"type": "key", "keys": ["META", "R"]}],
            {
                "type": "type_text",
                "text": "https://example.com",
                "context": "field",
                "verification": "exact",
            },
        ),
        (
            [{"type": "key", "keys": ["META", "R"]}],
            {
                "type": "type_text",
                "text": "ms-settings:about & cmd",
                "context": "field",
                "verification": "exact",
            },
        ),
        (
            [{"type": "key", "keys": ["META", "R"]}],
            {
                "type": "type_text",
                "text": "explorer.exe shell:MyComputerFolder",
                "context": "field",
                "verification": "exact",
            },
        ),
        (
            [{"type": "key", "keys": ["META", "R"]}],
            {
                "type": "type_text",
                "text": "ms-settings:about",
                "context": "terminal",
                "verification": "exact",
            },
        ),
        (
            [{"type": "key", "keys": ["META", "R"]}],
            {
                "type": "type_text",
                "text": "ms-settings:about",
                "context": "field",
            },
        ),
    ],
)
def test_controller_action_schema_rejects_unsafe_windows_run_near_misses(
    prefix: list[dict[str, object]],
    text_action: dict[str, object],
) -> None:
    with pytest.raises(
        ValidationError,
        match="active follow-up|same atomic launch burst",
    ):
        ControllerDecision.model_validate(
            {
                "outcome": "act",
                "intent": "Try an unsafe launch shape.",
                "actions": [
                    *prefix,
                    text_action,
                    {"type": "key", "keys": ["ENTER"]},
                ],
                "expected_evidence": ["A surface is visible."],
            }
        )


@pytest.mark.asyncio
async def test_start_runs_a_checkpointed_reason_act_verify_slice() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            role: RoleRoute(providers=[provider.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    harness = AgentHarness(
        computer=computer,
        models=pool,
        store=InMemoryRunStore(),
        config=HarnessConfig(max_actions_per_advance=1),
    )

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
        "verifier",
    ]
    reasoner_prompt = " ".join(provider.requests[0].prompt.split())
    assert "Do not invent exact values" in reasoner_prompt
    assert "necessary to satisfy the user's literal request" in reasoner_prompt
    assert "do not invent a numeric zoom threshold" in reasoner_prompt
    assert "authenticated user/operator corrections" in reasoner_prompt
    assert "the latest entry wins" in reasoner_prompt
    controller_prompt = " ".join(provider.requests[1].prompt.split())
    assert "visibly larger terminal glyphs" in controller_prompt
    assert "do not require a numeric zoom indicator" in controller_prompt
    verifier_prompt = " ".join(provider.requests[2].prompt.split())
    assert "Return verified only when every action assessment" in verifier_prompt
    assert "is not another computer step" in verifier_prompt
    assert "return complete rather than verified" in verifier_prompt
    assert "Do not return uncertain merely because the overall task" in verifier_prompt
    assert "visibly larger terminal glyphs are sufficient" in verifier_prompt
    assert "not require a numeric zoom percentage" in verifier_prompt
    verifier_properties = provider.requests[2].output_schema["properties"]
    assert verifier_properties["criteria"]["minItems"] == 1
    assert verifier_properties["criteria"]["maxItems"] == 1
    assert verifier_properties["action_criteria"]["minItems"] == 1
    assert verifier_properties["action_criteria"]["maxItems"] == 1
    assert len(computer.bursts) == 1
    burst = computer.bursts[0]
    assert burst["actions"] == [
        {
            "type": "type_text",
            "text": "hello world",
            "code": False,
            "secret": False,
            "context": "",
        }
    ]
    assert burst["based_on_world_version"] == 7
    assert burst["based_on_control_epoch"] == 2
    assert burst["idempotency_key"].startswith(f"{result.run_id}:action:0:")
    assert result.pending_action is None
    assert result.last_verification is not None
    assert result.last_verification.verdict == "complete"
    attempted = next(
        event for event in result.events if event.kind == "action.attempted"
    )
    completed = next(
        event for event in result.events if event.kind == "action.completed"
    )
    assert attempted.data["tool"] == "pikvm_run_burst"
    assert attempted.data["call_id"].endswith(":attempt:1")
    assert completed.data["call_id"] == attempted.data["call_id"]
    assert completed.data["tool"] == "pikvm_run_burst"
    assert completed.data["status"] == "completed"
    assert completed.data["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_post_action_verification_and_next_control_run_in_parallel(
    tmp_path: Path,
) -> None:
    provider = ParallelPostActionProvider()
    computer = FakeComputer()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            role: RoleRoute(providers=[provider.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    harness = AgentHarness(
        computer=computer,
        models=pool,
        store=SqliteRunStore(tmp_path / "parallel-run.sqlite3"),
        config=HarnessConfig(max_actions_per_advance=2),
    )

    result = await asyncio.wait_for(
        harness.start("Turn the requested setting off."),
        timeout=1,
    )

    assert result.status is RunStatus.COMPLETED
    assert provider.parallel_roles == {"controller", "verifier"}
    assert provider.controller_calls == 2
    assert provider.verifier_calls == 2
    assert any(
        event.kind == "controller.parallel_started"
        for event in result.events
    )
    assert any(
        event.kind == "controller.parallel_adopted"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_stale_parallel_repeat_is_discarded_without_replanning(
    tmp_path: Path,
) -> None:
    provider = ParallelPostActionProvider(stale_repeat=True)
    computer = FakeComputer()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            role: RoleRoute(providers=[provider.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    harness = AgentHarness(
        computer=computer,
        models=pool,
        store=SqliteRunStore(tmp_path / "stale-repeat.sqlite3"),
        config=HarnessConfig(max_actions_per_advance=2),
    )

    result = await asyncio.wait_for(
        harness.start("Turn the requested setting off."),
        timeout=1,
    )

    assert result.status is RunStatus.COMPLETED
    assert provider.controller_calls == 3
    assert [request.role for request in provider.requests].count("reasoner") == 1
    assert any(
        event.kind == "controller.parallel_stale_repeat_discarded"
        for event in result.events
    )
    assert not any(
        event.kind == "controller.repeated_actions"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_observational_follow_up_uses_one_read_only_model_call() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start("What about now?")

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == ["verifier"]
    assert computer.bursts == []
    assert result.plan is not None
    assert result.plan.constraints == ["Do not send keyboard or pointer input."]
    assert any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_plain_screen_question_uses_one_read_only_model_call() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start("what is on the screen")

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == ["verifier"]
    assert computer.bursts == []
    assert provider.requests[0].metadata["image_detail"] == "high"
    assert len(provider.requests[0].prompt) < 1_500
    assert any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_explicit_read_only_screen_description_skips_planning_and_input() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start(
        "Read-only check: describe what is currently visible on the connected "
        "disposable Windows VM. Do not click, type, scroll, press keys, or "
        "perform any computer input."
    )

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == ["verifier"]
    assert computer.bursts == []
    assert result.plan is not None
    assert result.plan.constraints == ["Do not send keyboard or pointer input."]
    assert any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_campaign_guard_does_not_disable_observation_fast_path() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start(
        "To launch an app, always use Win+R and type its executable.\n\n"
        "Task:\nDescribe what is currently visible on the Windows desktop. "
        "Do not change anything."
    )

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == ["verifier"]
    assert computer.bursts == []


def test_read_only_prefix_does_not_hide_a_later_action_request() -> None:
    run = RunSnapshot(
        run_id="mixed-read-only-and-action",
        task="Read-only check: describe the screen, then click Save.",
        status=RunStatus.RUNNING,
    )

    assert AgentHarness._is_observation_only_request(run) is False


@pytest.mark.asyncio
async def test_read_only_prefix_does_not_hide_a_later_save_request() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start(
        "Read-only check: describe the current screen. Save the document."
    )

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
        "verifier",
    ]
    assert len(computer.bursts) == 1
    assert not any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_read_only_prefix_does_not_hide_an_afterward_click() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start(
        "Read-only check: describe the screen; after describing it, click Save."
    )

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
        "verifier",
    ]
    assert len(computer.bursts) == 1
    assert not any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_read_only_prefix_does_not_hide_a_contradictory_input_request() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start(
        "Read-only check: describe the screen, but also press Escape."
    )

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
        "verifier",
    ]
    assert len(computer.bursts) == 1
    assert not any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_read_only_prefix_does_not_hide_an_input_before_description() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start(
        "Read-only check: click the window and describe the screen."
    )

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
        "verifier",
    ]
    assert len(computer.bursts) == 1
    assert not any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_read_only_prefix_does_not_hide_a_selection_request() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start(
        "Read-only check: describe the screen and select the first row."
    )

    assert result.status is RunStatus.COMPLETED
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
        "verifier",
    ]
    assert len(computer.bursts) == 1
    assert not any(
        event.kind == "plan.observation_only"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_model_phase_is_durable_while_provider_is_still_running() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider(ScriptedProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.role == "reasoner":
                entered.set()
                await release.wait()
            return await super().complete(request)

    provider = BlockingProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)
    created = await harness.create("Type hello world in the open editor.")

    continuation = asyncio.create_task(harness.continue_run(created.run_id))
    await asyncio.wait_for(entered.wait(), timeout=0.5)
    summary = await harness.store.get_summary(created.run_id)

    assert summary.active_activity is not None
    assert summary.active_activity.kind == "model"
    assert summary.active_activity.phase == "request_sent"
    assert summary.active_activity.role == "reasoner"
    assert summary.active_activity.provider == provider.name

    release.set()
    await asyncio.wait_for(continuation, timeout=1)


@pytest.mark.asyncio
async def test_chat_workspace_previews_exact_checkpoint_before_hid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)
    harness.config = HarnessConfig(
        max_actions_per_advance=1,
        interactive_action_preview_ms=300,
    )
    delays: list[float] = []

    async def record_delay(seconds: float) -> None:
        assert computer.bursts == []
        delays.append(seconds)

    monkeypatch.setattr(
        "pikvm_agent.harness.agent.asyncio.sleep",
        record_delay,
    )
    created = await harness.create(
        "Type hello world in the open editor.",
        caller={"interface": "chat_workspace", "label": "chat-workspace"},
    )
    result = await harness.continue_run(created.run_id)

    assert delays == [0.3]
    kinds = [event.kind for event in result.events]
    assert kinds.index("action.checkpointed") < kinds.index(
        "action.preview_window_opened"
    )
    assert kinds.index("action.preview_window_opened") < kinds.index(
        "action.attempted"
    )
    assert computer.bursts


@pytest.mark.asyncio
async def test_run_uses_independent_durable_routes_for_each_model_role() -> None:
    strong = ScriptedProvider()
    strong.name = "strong-model"
    fast = ScriptedProvider()
    fast.name = "fast-model"
    pool = ModelPool(
        providers={strong.name: strong, fast.name: fast},
        routes={
            role: RoleRoute(providers=[fast.name, strong.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    harness = AgentHarness(
        computer=FakeComputer(),
        models=pool,
        store=InMemoryRunStore(),
        config=HarnessConfig(max_actions_per_advance=1),
    )
    route = RunModelRoute(
        reasoner=[strong.name, fast.name],
        controller=[fast.name, strong.name],
        verifier=[strong.name, fast.name],
    )

    created = await harness.create(
        "Type hello world in the open editor.",
        model_route=route,
    )
    result = await harness.continue_run(created.run_id)

    assert result.status is RunStatus.COMPLETED
    assert result.model_route == route
    assert [request.role for request in strong.requests] == [
        "reasoner",
        "verifier",
    ]
    assert [request.role for request in fast.requests] == ["controller"]
    started = [
        event
        for event in result.events
        if event.kind == "model.started"
    ]
    assert [event.data["candidates"] for event in started] == [
        ["strong-model", "fast-model"],
        ["fast-model", "strong-model"],
        ["strong-model", "fast-model"],
    ]


@pytest.mark.asyncio
async def test_operator_steering_is_durable_and_forces_a_fresh_plan() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    created = await harness.create("Type hello world in the open editor.")
    created.plan = PlanDecision(
        summary="Stale plan",
        steps=["Use the current editor"],
        success_criteria=["Old completion criterion"],
    )
    await harness.store.save(created)

    steered = await harness.steer(
        created.run_id,
        "Use the already-open document and preserve its heading.",
    )

    assert steered.status is RunStatus.PAUSED
    assert steered.plan is None
    assert steered.operator_guidance == [
        "Use the already-open document and preserve its heading."
    ]
    assert steered.events[-1].kind == "run.steered"
    assert steered.active_activity is None

    completed = await harness.continue_run(created.run_id)

    assert completed.status is RunStatus.COMPLETED
    reasoner_prompt = provider.requests[0].prompt
    assert "operator_guidance" in reasoner_prompt
    assert "preserve its heading" in reasoner_prompt


@pytest.mark.asyncio
async def test_operator_steering_cannot_discard_an_unsettled_action() -> None:
    store = InMemoryRunStore()
    run = RunSnapshot(
        run_id="unsettled-action",
        task="Edit the document",
        status=RunStatus.RUNNING,
        pending_action=PendingAction(
            index=0,
            intent="Type exact text",
            actions=[{"type": "type_text", "text": "hello"}],
            based_on_world_version=1,
            based_on_control_epoch=1,
            idempotency_key="unsettled-action:action:0:digest",
        ),
    )
    await store.save(run)
    harness = AgentHarness(
        computer=object(),  # type: ignore[arg-type]
        models=object(),  # type: ignore[arg-type]
        store=store,
    )

    with pytest.raises(ValueError, match="pending action must settle"):
        await harness.steer(run.run_id, "Change direction")

    unchanged = await store.get(run.run_id)
    assert unchanged.pending_action is not None
    assert unchanged.operator_guidance == []


@pytest.mark.asyncio
async def test_provider_attempt_budget_pauses_before_any_hid() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            role: RoleRoute(providers=[provider.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    harness = AgentHarness(
        computer=computer,
        models=pool,
        store=InMemoryRunStore(),
        config=HarnessConfig(
            max_actions_per_advance=1,
            max_provider_attempts_per_run=1,
        ),
    )

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.PAUSED
    assert [request.role for request in provider.requests] == ["reasoner"]
    assert computer.bursts == []
    assert result.model_budget.provider_attempts == 1
    assert result.model_budget.provider_attempt_limit == 1
    assert result.model_budget.max_cost_microusd is None
    assert result.error == "model provider attempt budget exhausted"
    assert result.events[-1].kind == "model.budget_exhausted"


@pytest.mark.asyncio
async def test_provider_fallback_cannot_bypass_the_run_attempt_budget() -> None:
    primary = ControllerUnavailableProvider()
    fallback = ScriptedProvider()
    computer = FakeComputer()
    pool = ModelPool(
        providers={primary.name: primary, fallback.name: fallback},
        routes={
            "reasoner": RoleRoute(providers=[fallback.name]),
            "controller": RoleRoute(providers=[primary.name, fallback.name]),
            "verifier": RoleRoute(providers=[fallback.name]),
        },
        failure_cooldowns={primary.name: 0.0},
    )
    harness = AgentHarness(
        computer=computer,
        models=pool,
        store=InMemoryRunStore(),
        config=HarnessConfig(
            max_actions_per_advance=1,
            max_provider_attempts_per_run=2,
        ),
    )

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.PAUSED
    assert [request.role for request in fallback.requests] == ["reasoner"]
    assert [request.role for request in primary.requests] == ["controller"]
    assert result.model_budget.provider_attempts == 2
    assert computer.bursts == []
    assert result.events[-1].kind == "model.budget_exhausted"


@pytest.mark.asyncio
async def test_schema_repair_cannot_bypass_the_run_attempt_budget() -> None:
    provider = InvalidThenRepairedControllerProvider()
    computer = FakeComputer()
    harness = AgentHarness(
        computer=computer,
        models=ModelPool(
            providers={provider.name: provider},
            routes={
                role: RoleRoute(providers=[provider.name])
                for role in ("reasoner", "controller", "verifier")
            },
        ),
        store=InMemoryRunStore(),
        config=HarnessConfig(
            max_actions_per_advance=1,
            max_provider_attempts_per_run=2,
        ),
    )

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.PAUSED
    assert provider.controller_calls == 1
    assert result.model_budget.provider_attempts == 2
    assert computer.bursts == []
    assert result.events[-1].kind == "model.budget_exhausted"


@pytest.mark.asyncio
async def test_metered_cost_budget_blocks_the_next_model_before_hid() -> None:
    provider = MeteredProvider()
    computer = FakeComputer()
    harness = AgentHarness(
        computer=computer,
        models=ModelPool(
            providers={provider.name: provider},
            routes={
                role: RoleRoute(providers=[provider.name])
                for role in ("reasoner", "controller", "verifier")
            },
        ),
        store=InMemoryRunStore(),
        config=HarnessConfig(max_actions_per_advance=1),
        budget_policy=ModelBudgetPolicy(
            max_provider_attempts=100,
            max_cost_microusd=100,
            pricing_version="test-prices-v1",
            provider_costs={
                provider.name: ProviderCostTerms.metered(
                    reservation_microusd=60,
                    usage_usd_per_million={
                        "input_tokens": "2.00",
                        "output_tokens": "8.00",
                    },
                )
            },
        ),
    )

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.PAUSED
    assert [request.role for request in provider.requests] == ["reasoner"]
    assert result.model_budget.provider_attempts == 1
    assert result.model_budget.provider_attempt_limit == 100
    assert result.model_budget.committed_cost_microusd == 60
    assert result.model_budget.max_cost_microusd == 100
    assert result.model_budget.pricing_version == "test-prices-v1"
    assert result.model_budget.outstanding_cost_microusd == 0
    assert result.error == "model cost budget exhausted"
    assert computer.bursts == []


@pytest.mark.asyncio
async def test_metered_provider_without_usage_pauses_before_hid() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = AgentHarness(
        computer=computer,
        models=ModelPool(
            providers={provider.name: provider},
            routes={
                role: RoleRoute(providers=[provider.name])
                for role in ("reasoner", "controller", "verifier")
            },
        ),
        store=InMemoryRunStore(),
        config=HarnessConfig(max_actions_per_advance=1),
        budget_policy=ModelBudgetPolicy(
            max_provider_attempts=100,
            max_cost_microusd=1_000,
            provider_costs={
                provider.name: ProviderCostTerms.metered(
                    reservation_microusd=60,
                    usage_usd_per_million={"input_tokens": "2.00"},
                )
            },
        ),
    )

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.PAUSED
    assert [request.role for request in provider.requests] == ["reasoner"]
    assert result.model_budget.committed_cost_microusd == 60
    assert result.model_budget.outstanding_cost_microusd == 0
    assert result.error == "model usage report missing for metered provider"
    assert computer.bursts == []


@pytest.mark.asyncio
async def test_actual_cost_over_reservation_pauses_before_hid() -> None:
    provider = MeteredProvider()
    computer = FakeComputer()
    harness = AgentHarness(
        computer=computer,
        models=ModelPool(
            providers={provider.name: provider},
            routes={
                role: RoleRoute(providers=[provider.name])
                for role in ("reasoner", "controller", "verifier")
            },
        ),
        store=InMemoryRunStore(),
        config=HarnessConfig(max_actions_per_advance=1),
        budget_policy=ModelBudgetPolicy(
            max_provider_attempts=100,
            max_cost_microusd=50,
            provider_costs={
                provider.name: ProviderCostTerms.metered(
                    reservation_microusd=40,
                    usage_usd_per_million={
                        "input_tokens": "2.00",
                        "output_tokens": "8.00",
                    },
                )
            },
        ),
    )

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.PAUSED
    assert result.model_budget.committed_cost_microusd == 60
    assert result.error == "model cost budget exhausted after provider settlement"
    assert computer.bursts == []


@pytest.mark.asyncio
async def test_pointer_only_noop_is_rejected_before_hid() -> None:
    provider = PointerOnlyControllerProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    result = await harness.start("Inspect the terminal and continue the task.")

    assert result.status is RunStatus.PAUSED
    assert result.pending_action is None
    assert computer.bursts == []
    assert result.events[-1].kind == "controller.pointer_noop_rejected"


@pytest.mark.asyncio
async def test_managed_harness_blocks_if_target_identity_changes() -> None:
    provider = ScriptedProvider()
    harness = build_harness(provider, TargetSwitchComputer())

    result = await harness.start("Type hello world in the open editor.")
    continued = await harness.continue_run(result.run_id)

    assert result.status is RunStatus.BLOCKED
    assert result.error == "target identity changed during computer action"
    assert result.events[-1].kind == "target.identity_changed"
    assert result.events[-1].data["previous_fingerprint"] == (
        "target:aaaaaaaaaaaaaaaa"
    )
    assert result.events[-1].data["current_fingerprint"] == (
        "target:bbbbbbbbbbbbbbbb"
    )
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
    ]
    assert continued.status is RunStatus.BLOCKED


@pytest.mark.asyncio
async def test_approval_escapes_the_model_loop_and_only_exact_human_resume_executes() -> None:
    provider = ScriptedProvider()
    computer = ApprovalComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Draft and send a short message.")

    assert paused.status is RunStatus.NEEDS_APPROVAL
    assert paused.pending_approval is not None
    assert paused.pending_approval["approval_id"] == "approval_1"
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
    ]
    assert computer.resolutions == []
    with pytest.raises(ValueError, match="does not match"):
        await harness.resolve_approval(
            paused.run_id, "wrong_id", {"type": "approve"}
        )

    completed = await harness.resolve_approval(
        paused.run_id, "approval_1", {"type": "approve"}
    )

    assert completed.status is RunStatus.COMPLETED
    assert computer.resolutions == [
        {
            "session_id": "s_1",
            "approval_id": "approval_1",
            "decision": {"type": "approve"},
        }
    ]
    assert [request.role for request in provider.requests][-1] == "verifier"
    assert any(
        event.kind == "verification.evidence_refreshed"
        for event in completed.events
    )
    assert completed.observation is not None
    assert completed.observation.image_path == "/tmp/frame-refreshed.jpg"


@pytest.mark.asyncio
async def test_ungrounded_navigation_is_rejected_and_replanned_not_approved() -> None:
    provider = ScriptedProvider()
    computer = UngroundedNavigationComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Focus the visible search field.")

    assert paused.status is RunStatus.PAUSED
    assert paused.pending_action is None
    assert paused.pending_approval is None
    assert paused.observation is not None
    assert paused.session_id == "s_2"
    assert paused.observation.frame_id == 2
    assert computer.opens == 2
    assert computer.resolutions == [
        {
            "session_id": "s_1",
            "approval_id": "unknown_click_1",
            "decision": {
                "type": "reject",
                "reason": (
                    "managed harness rejected an ungrounded navigation "
                    "proposal"
                ),
            },
        }
    ]
    assert paused.events[-1].kind == "action.ungrounded_refreshed"
    assert not any(event.kind == "approval.required" for event in paused.events)
    assert harness._trajectory_signals(paused)[
        "ungrounded_navigation_replans"
    ] == 1


@pytest.mark.asyncio
async def test_repeated_ungrounded_click_is_repaired_before_more_hid() -> None:
    provider = RepeatedUngroundedThenKeyboardProvider()
    computer = UngroundedThenKeyboardComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Minimize the visible obstructing windows.")
    completed = await harness.continue_run(paused.run_id)

    assert paused.status is RunStatus.PAUSED
    assert completed.status is RunStatus.COMPLETED
    assert provider.controller_calls == 3
    assert [burst["actions"] for burst in computer.bursts] == [
        [{"type": "click", "x": 705, "y": 94, "button": "left"}],
        [{"type": "key", "keys": ["META", "M"]}],
    ]
    controller_prompts = [
        request.prompt
        for request in provider.requests
        if request.role == "controller"
    ]
    assert '"last_ungrounded_navigation": {' in controller_prompts[1]
    assert '"x": 705' in controller_prompts[1]
    assert '"controller_feedback": {' in controller_prompts[2]
    assert "was already rejected before HID" in controller_prompts[2]
    assert any(
        event.kind == "controller.ungrounded_repeat_rejected"
        for event in completed.events
    )


@pytest.mark.asyncio
async def test_distinct_ungrounded_targets_use_bounded_replan_budget() -> None:
    provider = DistinctUngroundedThenKeyboardProvider()
    computer = UngroundedThenKeyboardComputer()
    harness = build_harness(provider, computer)

    first = await harness.start("Reveal and focus Microsoft Word.")
    second = await harness.continue_run(first.run_id)
    completed = await harness.continue_run(second.run_id)

    assert first.status is RunStatus.PAUSED
    assert second.status is RunStatus.PAUSED
    assert completed.status is RunStatus.COMPLETED
    assert computer.opens == 3
    assert [burst["actions"] for burst in computer.bursts] == [
        [{"type": "click", "x": 705, "y": 94, "button": "left"}],
        [{"type": "click", "x": 620, "y": 660, "button": "left"}],
        [{"type": "key", "keys": ["META", "M"]}],
    ]
    controller_prompts = [
        request.prompt
        for request in provider.requests
        if request.role == "controller"
    ]
    assert '"ungrounded_navigation_history": [' in controller_prompts[2]
    assert '"x": 705' in controller_prompts[2]
    assert '"x": 620' in controller_prompts[2]
    assert sum(
        event.kind == "action.ungrounded_refreshed"
        for event in completed.events
    ) == 2


@pytest.mark.asyncio
async def test_ungrounded_replan_budget_exhaustion_stays_fail_closed() -> None:
    provider = DistinctUngroundedThenKeyboardProvider()
    computer = UngroundedThenKeyboardComputer()
    harness = build_harness(provider, computer)
    harness.config = HarnessConfig(
        max_actions_per_advance=1,
        max_ungrounded_navigation_replans=1,
    )

    first = await harness.start("Reveal and focus Microsoft Word.")
    blocked = await harness.continue_run(first.run_id)

    assert first.status is RunStatus.PAUSED
    assert blocked.status is RunStatus.BLOCKED
    assert blocked.error == (
        "click targets could not be independently grounded after the "
        "bounded navigation replan budget"
    )
    assert computer.opens == 2
    assert blocked.events[-1].kind == "action.ungrounded_budget_exhausted"
    assert blocked.events[-1].data["recovery_limit"] == 1


@pytest.mark.asyncio
async def test_rejecting_approval_closes_the_underlying_computer_session() -> None:
    provider = ScriptedProvider()
    computer = ApprovalComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Draft and send a short message.")
    rejected = await harness.resolve_approval(
        paused.run_id,
        "approval_1",
        {"type": "reject", "reason": "Do not send"},
    )

    assert rejected.status is RunStatus.REJECTED
    assert rejected.pending_action is None
    assert rejected.pending_approval is None
    assert rejected.observation is not None
    assert rejected.observation.status == "aborted"
    assert computer.aborts == [
        {
            "session_id": "s_1",
            "reason": "approval rejected by operator",
        }
    ]
    assert any(
        event.kind == "computer.aborted_after_rejection"
        for event in rejected.events
    )


@pytest.mark.asyncio
async def test_ambiguous_transport_retry_reuses_checkpointed_action_and_key() -> None:
    provider = ScriptedProvider()
    computer = FlakyComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Type hello world in the open editor.")

    assert paused.status is RunStatus.PAUSED
    assert paused.pending_action is not None
    checkpointed_key = paused.pending_action.idempotency_key
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
    ]

    completed = await harness.continue_run(paused.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert computer.keys == [checkpointed_key, checkpointed_key]
    assert [request.role for request in provider.requests] == [
        "reasoner",
        "controller",
        "verifier",
    ]
    attempts = [
        event for event in completed.events if event.kind == "action.attempted"
    ]
    outcomes = [
        event
        for event in completed.events
        if event.kind in {"action.transport_uncertain", "action.completed"}
    ]
    assert [event.data["call_id"] for event in attempts] == [
        f"{checkpointed_key}:attempt:1",
        f"{checkpointed_key}:attempt:2",
    ]
    assert [event.data["call_id"] for event in outcomes] == [
        event.data["call_id"] for event in attempts
    ]
    assert all(event.data["tool"] == "pikvm_run_burst" for event in outcomes)
    assert all(event.data["latency_ms"] >= 0 for event in outcomes)


@pytest.mark.asyncio
async def test_process_restart_reopens_session_and_recontrols_before_input() -> None:
    provider = ScriptedProvider()
    computer = RestartedComputer()
    harness = build_harness(provider, computer)
    interrupted = RunSnapshot(
        run_id="interrupted-run",
        task="Type hello world in the open editor.",
        status=RunStatus.PAUSED,
        session_id="s_expired",
        observation=ComputerObservation(
            session_id="s_expired",
            status="paused",
            frame_id=7,
            world_version=9,
            control_epoch=2,
            image_path="/tmp/frame-before-restart.jpg",
        ),
        plan=PlanDecision(
            summary="Enter the requested text.",
            steps=["Type the text", "Verify it"],
            success_criteria=["The requested text is visible."],
            constraints=[],
        ),
        pending_action=PendingAction(
            index=0,
            intent="Type into the old frame.",
            actions=[{"type": "type_text", "text": "stale action"}],
            based_on_world_version=9,
            based_on_control_epoch=2,
            idempotency_key="stale-action-key",
        ),
    )
    interrupted.record(
        "run.process_interrupted",
        pending_action=True,
        activity_kind="tool",
    )
    await harness.store.save(interrupted)

    completed = await harness.continue_run(interrupted.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert computer.open_calls == 1
    assert [burst["session_id"] for burst in computer.bursts] == [
        "s_reopened"
    ]
    assert [burst["idempotency_key"] for burst in computer.bursts] != [
        "stale-action-key"
    ]
    reopened = next(
        event
        for event in completed.events
        if event.kind == "computer.reopened_after_process_restart"
    )
    assert reopened.data["plan_preserved"] is True
    assert [request.role for request in provider.requests] == [
        "controller",
        "verifier",
    ]


@pytest.mark.asyncio
async def test_abort_quiesces_a_durable_run_when_daemon_session_is_gone() -> None:
    harness = build_harness(
        ScriptedProvider(),
        MissingAbortSessionComputer(),
    )
    interrupted = RunSnapshot(
        run_id="abort-after-daemon-restart",
        task="Retain the failed campaign attempt.",
        status=RunStatus.PAUSED,
        session_id="s_expired",
        error=(
            "local harness process restarted; explicit operator resume is "
            "required"
        ),
        pending_action=PendingAction(
            index=0,
            intent="Do not replay this action.",
            actions=[{"type": "type_text", "text": "stale draft"}],
            based_on_world_version=9,
            based_on_control_epoch=2,
            idempotency_key="stale-action-key",
        ),
    )
    interrupted.record(
        "run.process_interrupted",
        pending_action=True,
        resume_required=True,
    )
    await harness.store.save(interrupted)

    aborted = await harness.abort(
        interrupted.run_id,
        "campaign task concluded before mandatory reboot",
    )

    assert aborted.status is RunStatus.ABORTED
    assert aborted.pending_action is None
    assert aborted.pending_approval is None
    assert [
        event.kind for event in aborted.events[-2:]
    ] == [
        "computer.abort_session_already_absent",
        "run.aborted",
    ]


@pytest.mark.asyncio
async def test_pause_retains_a_checkpointed_action_for_idempotent_resume() -> None:
    provider = ScriptedProvider()
    computer = FlakyComputer()
    harness = build_harness(provider, computer)

    paused_after_ambiguity = await harness.start(
        "Type hello world in the open editor."
    )
    checkpointed = paused_after_ambiguity.pending_action
    assert checkpointed is not None

    paused_by_operator = await harness.pause(
        paused_after_ambiguity.run_id, "operator requested pause"
    )

    assert paused_by_operator.status is RunStatus.PAUSED
    assert paused_by_operator.pending_action == checkpointed
    assert paused_by_operator.events[-1].kind == "run.paused"
    assert paused_by_operator.events[-1].data["source"] == "operator"


@pytest.mark.asyncio
async def test_all_provider_failure_pauses_before_hid_and_can_resume() -> None:
    provider = TemporarilyUnavailableProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Type hello world in the open editor.")

    assert paused.status is RunStatus.PAUSED
    assert paused.events[-1].kind == "model.failed"
    assert computer.bursts == []

    completed = await harness.continue_run(paused.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert len(computer.bursts) == 1


@pytest.mark.asyncio
async def test_model_blocked_run_can_replan_and_resume_without_hid_replay() -> None:
    provider = InitiallyBlockedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    blocked = await harness.start("Type hello world in the open editor.")

    assert blocked.status is RunStatus.BLOCKED
    assert computer.bursts == []

    completed = await harness.continue_run(blocked.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert len(computer.bursts) == 1
    controller_prompts = [
        item.prompt for item in provider.requests if item.role == "controller"
    ]
    normalized_prompt = " ".join(controller_prompts[-1].split())
    assert "Do not wait for human approval" in normalized_prompt


@pytest.mark.asyncio
async def test_definitive_typing_failure_pauses_for_replan_with_new_action_index() -> None:
    provider = ScriptedProvider()
    computer = FocusLostComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Type hello world in the open editor.")

    assert paused.status is RunStatus.PAUSED
    assert paused.plan is None
    assert paused.pending_action is None
    assert paused.next_action_index == 1
    assert paused.events[-1].kind == "action.recoverable_failure"


@pytest.mark.asyncio
async def test_daemon_unverified_typing_cannot_be_overridden_by_model_verifier() -> None:
    provider = ScriptedProvider()
    computer = UnverifiedTypingComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Type hello world in the open editor.")

    assert paused.status is RunStatus.PAUSED
    assert paused.plan is None
    assert paused.pending_action is None
    assert paused.next_action_index == 1
    assert any(
        event.kind == "action.completed_unverified"
        for event in paused.events
    )
    assert not any(request.role == "verifier" for request in provider.requests)


@pytest.mark.asyncio
async def test_once_only_navigation_text_preserves_plan_without_replaying() -> None:
    provider = SearchRecoveryProvider()
    computer = OnceOnlyUnverifiedSearchComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Find the Settings application.")

    assert paused.status is RunStatus.PAUSED
    assert paused.plan is not None
    assert paused.pending_action is None
    assert paused.events[-1].kind == "action.completed_unverified"
    assert paused.events[-1].data["plan_preserved"] is True

    completed = await harness.continue_run(paused.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert len(computer.bursts) == 1
    assert [request.role for request in provider.requests].count("reasoner") == 1
    assert provider.controller_calls == 2


@pytest.mark.parametrize(
    "actions,receipt",
    [
        (
            [{"type": "type_text", "text": "password", "secret": True}],
            {
                "type": "type_text",
                "requested_characters": 8,
                "issued_characters": 8,
                "emitted_characters": 8,
                "emitted_exactly_once": True,
            },
        ),
        (
            [
                {
                    "type": "type_text",
                    "text": "echo hello",
                    "code": True,
                    "context": "terminal",
                }
            ],
            {
                "type": "type_text",
                "requested_characters": 10,
                "issued_characters": 10,
                "emitted_characters": 10,
                "emitted_exactly_once": True,
            },
        ),
        (
            [
                {"type": "type_text", "text": "Settings"},
                {"type": "key", "key": "ENTER"},
            ],
            {
                "type": "type_text",
                "requested_characters": 8,
                "issued_characters": 8,
                "emitted_characters": 8,
                "emitted_exactly_once": True,
            },
        ),
    ],
)
def test_uncertain_secret_code_and_committed_text_discard_plan(
    actions: list[dict[str, Any]],
    receipt: dict[str, Any],
) -> None:
    pending = PendingAction(
        index=0,
        intent="Attempt uncertain text.",
        actions=actions,
        expected_evidence=[],
        based_on_world_version=None,
        based_on_control_epoch=None,
        idempotency_key="run:action:0",
    )

    assert not AgentHarness._can_preserve_plan_after_unverified_navigation(
        pending,
        [receipt],
        reason="type_unverified",
    )


@pytest.mark.asyncio
async def test_action_event_exposes_only_bounded_input_readback_receipts() -> None:
    provider = ScriptedProvider()
    harness = build_harness(provider, InputReceiptComputer())

    completed = await harness.start("Type hello world in the open editor.")

    event = next(
        event for event in completed.events if event.kind == "action.completed"
    )
    assert event.data["input_receipts"] == [
        {
            "index": 0,
            "type": "type_text",
            "status": "verified_exact",
            "verdict": "match",
            "observed_text": "hello world",
            "observed_text_redacted": False,
            "requested_characters": 11,
            "issued_characters": 11,
            "observed_characters": 11,
            "correction_count": 1,
            "delivery_retries": 0,
            "used_fast_path": False,
            "summary": "Typed and verified.",
            "edit_distance": 0,
            "focus_evidence": "read_back_verified",
            "requested_sha256": "a" * 64,
            "issued_prefix_sha256": "a" * 64,
            "readback_sha256": "a" * 64,
            "exact_readback_sha256_match": True,
            "proof_state": "exact_ocr_readback",
        }
    ]
    assert "private_path" not in repr(event.data["input_receipts"])
    assert "unknown" not in repr(event.data["input_receipts"])
    assert harness._recent_input_delivery(completed) == [
        {
            "action_index": 0,
            "input_index": 0,
            "status": "verified_exact",
            "issued_characters": 11,
            "requested_characters": 11,
            "sender_finished": True,
            "readback_exact": True,
            "readback_available": True,
        }
    ]


def test_recent_input_delivery_distinguishes_transport_from_screen_proof() -> None:
    run = RunSnapshot(
        run_id="invisible-whitespace-receipt",
        task="Replace two spaces with one",
        status=RunStatus.PAUSED,
    )
    run.record(
        "action.completed_unverified",
        index=4,
        input_receipts=[
            {
                "index": 0,
                "status": "unverified_ambiguous",
                "issued_characters": 2,
                "requested_characters": 2,
                "requested_sha256": "a" * 64,
                "issued_prefix_sha256": "a" * 64,
                "readback_sha256": "b" * 64,
                "exact_readback_sha256_match": False,
                "observed_text": "",
            }
        ],
    )

    assert AgentHarness._recent_input_delivery(run) == [
        {
            "action_index": 4,
            "input_index": 0,
            "status": "unverified_ambiguous",
            "issued_characters": 2,
            "requested_characters": 2,
            "sender_finished": True,
            "readback_exact": False,
            "readback_available": False,
        }
    ]


def test_unverified_terminal_draft_blocks_suffixes_and_execution_until_cancelled() -> None:
    run = RunSnapshot(
        run_id="unverified-terminal-draft",
        task="Disable the dim-screen setting",
        status=RunStatus.PAUSED,
    )
    run.record(
        "action.checkpointed",
        index=5,
        actions=[
            {
                "type": "type_text",
                "text": (
                    "gsettings set "
                    "org.gnome.settings-daemon.plugins.power idle-dim false"
                ),
                "code": True,
                "context": "terminal",
            }
        ],
    )
    run.record(
        "action.completed_unverified",
        index=5,
        input_receipts=[
            {
                "index": 0,
                "status": "unverified_ambiguous",
                "issued_characters": 68,
                "requested_characters": 68,
                "requested_sha256": "a" * 64,
                "issued_prefix_sha256": "a" * 64,
                "exact_readback_sha256_match": False,
                "observed_text": "",
            }
        ],
    )

    assert AgentHarness._unsafe_unverified_input_followup(
        run,
        [{"type": "type_text", "text": "se", "code": True, "context": "terminal"}],
    )
    assert AgentHarness._unsafe_unverified_input_followup(
        run,
        [{"type": "key", "keys": ["ENTER"]}],
    )
    assert not AgentHarness._unsafe_unverified_input_followup(
        run,
        [{"type": "key", "keys": ["CTRL", "C"]}],
    )
    assert not AgentHarness._unsafe_unverified_input_followup(
        run,
        [{"type": "key", "keys": ["META", "ARROWUP"]}],
    )

    run.record(
        "action.checkpointed",
        index=6,
        actions=[{"type": "key", "keys": ["ctrl+c"]}],
    )
    run.record("action.completed", index=6)

    assert not AgentHarness._unsafe_unverified_input_followup(
        run,
        [
            {
                "type": "type_text",
                "text": (
                    "gsettings set "
                    "org.gnome.settings-daemon.plugins.power idle-dim false"
                ),
                "code": True,
                "context": "terminal",
            }
        ],
    )


def test_unverified_exact_field_blocks_enter_until_dismissed() -> None:
    run = RunSnapshot(
        run_id="unverified-exact-field",
        task="Open Windows Display settings",
        status=RunStatus.PAUSED,
    )
    run.record(
        "action.checkpointed",
        index=2,
        actions=[
            {
                "type": "type_text",
                "text": "ms-settings:display",
                "context": "field",
                "verification": "exact",
            }
        ],
    )
    run.record(
        "action.completed_unverified",
        index=2,
        input_receipts=[
            {
                "index": 0,
                "issued_characters": 19,
                "requested_characters": 19,
                "requested_sha256": "b" * 64,
                "issued_prefix_sha256": "b" * 64,
                "exact_readback_sha256_match": False,
            }
        ],
    )

    assert AgentHarness._unsafe_unverified_input_followup(
        run,
        [{"type": "key", "keys": ["ENTER"]}],
    )
    assert AgentHarness._unsafe_unverified_input_followup(
        run,
        [{"type": "click", "x": 104, "y": 743, "button": "left"}],
    )
    assert AgentHarness._unsafe_unverified_input_followup(
        run,
        [
            {
                "type": "type_text",
                "text": "ms-settings:display",
                "context": "field",
                "verification": "exact",
            }
        ],
    )
    assert not AgentHarness._unsafe_unverified_input_followup(
        run,
        [{"type": "key", "keys": ["ESC"]}],
    )

    run.record(
        "action.checkpointed",
        index=3,
        actions=[{"type": "key", "keys": ["ESC"]}],
    )
    run.record("action.completed", index=3)

    assert not AgentHarness._unsafe_unverified_input_followup(
        run,
        [{"type": "key", "keys": ["ENTER"]}],
    )


@pytest.mark.parametrize(
    ("issued_characters", "issued_prefix_sha256"),
    [(6, "d" * 64), (3, "e" * 64)],
)
def test_unverified_exact_editor_is_not_dismissed_by_escape(
    issued_characters: int,
    issued_prefix_sha256: str,
) -> None:
    run = RunSnapshot(
        run_id="unverified-exact-editor",
        task="Type an exact Notepad line",
        status=RunStatus.PAUSED,
    )
    run.record(
        "action.checkpointed",
        index=2,
        actions=[
            {
                "type": "type_text",
                "text": "2. Act",
                "context": "editor",
                "verification": "exact",
            }
        ],
    )
    run.record(
        "action.completed_unverified",
        index=2,
        input_receipts=[
            {
                "index": 0,
                "issued_characters": issued_characters,
                "requested_characters": 6,
                "requested_sha256": "d" * 64,
                "issued_prefix_sha256": issued_prefix_sha256,
                "exact_readback_sha256_match": False,
            }
        ],
    )
    run.record(
        "action.checkpointed",
        index=3,
        actions=[{"type": "key", "keys": ["ESC"]}],
    )
    run.record("action.completed", index=3)

    assert AgentHarness._unsafe_unverified_input_followup(
        run,
        [
            {
                "type": "type_text",
                "text": "3. Verify",
                "context": "editor",
                "verification": "exact",
            }
        ],
    )
    assert AgentHarness._unsafe_unverified_input_followup(
        run,
        [{"type": "click", "x": 100, "y": 100, "button": "left"}],
    )
    assert AgentHarness._unsafe_unverified_input_followup(
        run,
        [{"type": "key", "keys": ["CTRL", "SHIFT", "S"]}],
    )
    assert AgentHarness._unsafe_unverified_input_followup(
        run,
        [{"type": "key", "keys": ["BACKSPACE"]}],
    )
    assert AgentHarness._unsafe_unverified_input_followup(
        run,
        [{"type": "key", "keys": ["CTRL", "Z"]}],
    )
    assert not AgentHarness._unsafe_unverified_input_followup(
        run,
        [{"type": "key", "keys": ["ESC"]}],
    )


def test_recoverable_exact_field_failure_blocks_pointer_commit() -> None:
    run = RunSnapshot(
        run_id="recoverable-unverified-exact-field",
        task="Open Windows Calculator",
        status=RunStatus.PAUSED,
    )
    run.record(
        "action.checkpointed",
        index=0,
        actions=[
            {
                "type": "type_text",
                "text": "calc",
                "context": "field",
                "verification": "exact",
            }
        ],
    )
    run.record(
        "action.recoverable_failure",
        index=0,
        reason="type_unverified",
        input_receipts=[
            {
                "index": 0,
                "issued_characters": 4,
                "requested_characters": 4,
                "requested_sha256": "c" * 64,
                "issued_prefix_sha256": "c" * 64,
                "exact_readback_sha256_match": False,
            }
        ],
    )

    assert AgentHarness._unsafe_unverified_input_followup(
        run,
        [{"type": "click", "x": 104, "y": 743, "button": "left"}],
    )
    assert not AgentHarness._unsafe_unverified_input_followup(
        run,
        [{"type": "key", "keys": ["ESC"]}],
    )


def test_long_terminal_draft_requires_a_verified_legibility_step() -> None:
    run = RunSnapshot(
        run_id="long-terminal-legibility",
        task="Disable the dim-screen setting",
        status=RunStatus.PAUSED,
    )
    proposed = [
        {
            "type": "type_text",
            "text": (
                "gsettings set "
                "org.gnome.settings-daemon.plugins.power idle-dim false"
            ),
            "code": True,
            "context": "terminal",
        }
    ]

    assert AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )
    assert not AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        [
            {
                "type": "type_text",
                "text": "gsettings list-schemas",
                "code": True,
                "context": "terminal",
            }
        ],
    )

    run.record(
        "action.checkpointed",
        index=4,
        intent="Maximize the terminal before entering the exact command.",
        actions=[{"type": "key", "keys": ["META", "UP"]}],
    )
    run.record("action.completed", index=4, status="completed")
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="The terminal is visibly maximized and the clean prompt is legible.",
    )

    assert AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )

    run.record(
        "action.checkpointed",
        index=5,
        intent="Increase the terminal text size before entering the exact command.",
        actions=[{"type": "key", "keys": ["CTRL", "SHIFT", "EQUAL"]}],
    )
    run.record("action.completed", index=5, status="completed")
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="The terminal text is visibly zoomed in and larger.",
    )

    assert not AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )

    run.record(
        "action.checkpointed",
        index=6,
        intent=(
            "Open the terminal's hamburger menu to find the zoom-in control."
        ),
        actions=[{"type": "click", "x": 1179, "y": 33}],
    )
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary=(
            "The terminal menu opened and shows the zoom controls."
        ),
    )

    assert not AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )

    run.record(
        "action.checkpointed",
        index=7,
        intent="Type the exact command for visual verification.",
        actions=proposed,
    )
    run.record(
        "action.completed_unverified",
        index=7,
        status="unverified",
        input_receipts=[
            {
                "index": 0,
                "status": "unverified_ambiguous",
                "exact_readback_sha256_match": False,
            }
        ],
    )

    assert AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )

    run.record(
        "action.checkpointed",
        index=8,
        intent="Cancel the unread terminal draft with Ctrl+C.",
        actions=[{"type": "key", "keys": ["CTRL", "C"]}],
    )

    assert AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )

    run.record("action.completed", index=8)

    assert AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )

    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="Ctrl+C cleared the draft and a clean empty prompt is visible.",
    )

    assert not AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )

    run.record(
        "action.checkpointed",
        index=9,
        intent="Increase the terminal text size after the unreadable draft.",
        actions=[{"type": "key", "keys": ["CTRL", "SHIFT", "EQUAL"]}],
    )
    run.record("action.completed", index=9, status="completed")
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="The terminal is zoomed in and the clean prompt remains visible.",
    )

    assert not AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )

    for index in range(10, 19):
        run.record(
            "action.checkpointed",
            index=index,
            intent=f"Perform unrelated verified recovery step {index}.",
            actions=[{"type": "wait", "ms": 100}],
        )
        run.record("action.completed", index=index, status="completed")
        run.record(
            "model.completed",
            role="verifier",
            verdict="verified",
            summary=f"Recovery step {index} completed.",
        )

    assert not AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )

    run.record(
        "action.checkpointed",
        index=19,
        intent="Open a new terminal window.",
        actions=[{"type": "key", "keys": ["CTRL", "ALT", "T"]}],
    )
    run.record("action.completed", index=19, status="completed")
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="A new terminal opened at its default narrow width.",
    )

    assert AgentHarness._long_terminal_draft_needs_legibility_step(
        run,
        proposed,
    )


def test_internal_terminal_legibility_ignores_unrequested_numeric_zoom() -> None:
    run = RunSnapshot(
        run_id="terminal-legibility-evidence",
        task="Disable the dim-screen setting",
        status=RunStatus.PAUSED,
    )
    intent = (
        "Click the terminal menu's Zoom In control once to increase text size."
    )
    actions = [
        {"type": "click", "x": 1222, "y": 77, "button": "left"},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 300,
            "timeout_ms": 3000,
        },
    ]
    expected = [
        "Zoom percentage in menu increases above 100%",
        "Prompt text appears visibly larger",
    ]

    assert AgentHarness._normalized_expected_evidence(
        run,
        intent=intent,
        actions=actions,
        expected_evidence=expected,
    ) == ["Prompt text appears visibly larger"]

    numeric_request = run.model_copy(
        update={"task": "Set the terminal zoom to 125%."}
    )
    assert AgentHarness._normalized_expected_evidence(
        numeric_request,
        intent=intent,
        actions=actions,
        expected_evidence=expected,
    ) == expected


def test_visible_terminal_legibility_overrides_only_numeric_doubt() -> None:
    run = RunSnapshot(
        run_id="terminal-legibility-verdict",
        task="Disable the dim-screen setting",
        status=RunStatus.PAUSED,
    )
    action = PendingAction(
        index=7,
        intent=(
            "Click the terminal menu's Zoom In control once to increase text "
            "size."
        ),
        actions=[
            {"type": "click", "x": 1222, "y": 77, "button": "left"},
            {
                "type": "wait_for_stable_screen",
                "stable_ms": 300,
                "timeout_ms": 3000,
            },
        ],
        expected_evidence=["Prompt text appears visibly larger"],
        based_on_world_version=7,
        based_on_control_epoch=1,
        idempotency_key="terminal-legibility:action:7:test",
    )
    verdict = VerificationDecision(
        verdict="uncertain",
        summary=(
            "The terminal prompt text is now visibly larger, but the menu "
            "closed before the zoom percentage could be confirmed."
        ),
        evidence=[
            "The prompt glyphs appear visibly larger in the after frame."
        ],
        action_criteria=[
            {
                "criterion_index": 0,
                "satisfied": True,
                "evidence": "Prompt glyphs are visibly larger than before.",
            }
        ],
    )

    normalized, reason = (
        AgentHarness._normalized_internal_legibility_verdict(
            run,
            action=action,
            verdict=verdict,
        )
    )

    assert normalized.verdict == "verified"
    assert reason is not None

    numeric_request = run.model_copy(
        update={"task": "Set the terminal zoom to 125%."}
    )
    unchanged, reason = (
        AgentHarness._normalized_internal_legibility_verdict(
            numeric_request,
            action=action,
            verdict=verdict,
        )
    )
    assert unchanged.verdict == "uncertain"
    assert reason is None


@pytest.mark.asyncio
async def test_long_terminal_draft_is_replaced_with_legibility_action_before_hid() -> None:
    class LegibilityProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__()
            self.controller_calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.role == "controller":
                self.requests.append(request)
                self.controller_calls += 1
                actions = (
                    [
                        {
                            "type": "type_text",
                            "text": (
                                "gsettings set "
                                "org.gnome.settings-daemon.plugins.power "
                                "idle-dim false"
                            ),
                            "code": True,
                            "context": "terminal",
                            "verification": "exact",
                        }
                    ]
                    if self.controller_calls == 1
                    else [
                        {
                            "type": "key",
                            "keys": ["CTRL", "SHIFT", "EQUAL"],
                        }
                    ]
                )
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "outcome": "act",
                        "intent": (
                            "Type the exact setting command."
                            if self.controller_calls == 1
                            else (
                                "Increase the terminal text size before "
                                "typing."
                            )
                        ),
                        "actions": actions,
                        "expected_evidence": [
                            "The terminal is visibly maximized and legible."
                        ],
                    },
                )
            if request.role == "verifier":
                self.requests.append(request)
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "verdict": "verified",
                        "summary": (
                            "The terminal text is visibly zoomed in and the "
                            "prompt is legible."
                        ),
                        "evidence": ["The terminal text is visibly larger."],
                    },
                )
            return await super().complete(request)

    provider = LegibilityProvider()
    computer = FakeComputer()
    store = InMemoryRunStore()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            role: RoleRoute(providers=[provider.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    harness = AgentHarness(
        computer=computer,
        models=pool,
        store=store,
        config=HarnessConfig(max_actions_per_advance=1),
    )
    run = RunSnapshot(
        run_id="guard-long-terminal-draft",
        task="Disable the dim-screen setting",
        status=RunStatus.PAUSED,
        session_id="s_1",
        observation=await computer.open("legibility-test"),
        plan=PlanDecision(
            summary="Disable the requested setting.",
            steps=["Enter the exact local setting command."],
            success_criteria=["The dim-screen setting is off."],
            constraints=["Preserve unrelated settings."],
        ),
    )
    run.record(
        "action.checkpointed",
        index=4,
        intent="Maximize the terminal before entering the exact command.",
        actions=[{"type": "key", "keys": ["META", "UP"]}],
    )
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="The terminal is visibly maximized and legible.",
    )
    run.record(
        "action.checkpointed",
        index=5,
        intent="Increase the terminal text size before entering the exact command.",
        actions=[{"type": "key", "keys": ["CTRL", "SHIFT", "EQUAL"]}],
    )
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="The terminal text is visibly zoomed in and larger.",
    )
    run.record(
        "action.checkpointed",
        index=6,
        intent="Type the exact command for visual verification.",
        actions=[
            {
                "type": "type_text",
                "text": (
                    "gsettings set "
                    "org.gnome.settings-daemon.plugins.power idle-dim false"
                ),
                "code": True,
                "context": "terminal",
                "verification": "exact",
            }
        ],
    )
    run.record(
        "action.completed_unverified",
        index=6,
        status="unverified",
        input_receipts=[
            {
                "index": 0,
                "status": "unverified_ambiguous",
                "exact_readback_sha256_match": False,
            }
        ],
    )
    await store.save(run)

    result = await harness.continue_run(run.run_id)

    assert provider.controller_calls == 2
    assert [burst["actions"] for burst in computer.bursts] == [
        [{"type": "key", "keys": ["CTRL", "SHIFT", "EQUAL"]}]
    ], [(event.kind, event.data) for event in result.events[-8:]]
    assert any(
        event.kind == "controller.long_terminal_draft_rejected"
        for event in result.events
    )
    controller_prompts = [
        request.prompt
        for request in provider.requests
        if request.role == "controller"
    ]
    assert '"controller_feedback": {' in controller_prompts[1]
    assert "Do not type any text yet" in controller_prompts[1]
    assert "increase the terminal text size" in controller_prompts[1]


@pytest.mark.asyncio
async def test_unverified_terminal_suffix_is_replaced_with_cancel_before_hid() -> None:
    class RecoveryProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__()
            self.controller_calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.role == "controller":
                self.requests.append(request)
                self.controller_calls += 1
                action = (
                    {
                        "type": "type_text",
                        "text": "se",
                        "code": True,
                        "context": "terminal",
                    }
                    if self.controller_calls == 1
                    else {"type": "key", "keys": ["CTRL", "C"]}
                )
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "outcome": "act",
                        "intent": "Recover the unread terminal draft safely.",
                        "actions": [action],
                        "expected_evidence": ["A clean terminal prompt is visible."],
                    },
                )
            if request.role == "verifier":
                self.requests.append(request)
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "verdict": "failed",
                        "summary": "The task still needs a clean command entry.",
                        "evidence": ["The draft was cancelled safely."],
                    },
                )
            return await super().complete(request)

    provider = RecoveryProvider()
    computer = FakeComputer()
    store = InMemoryRunStore()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            role: RoleRoute(providers=[provider.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    harness = AgentHarness(
        computer=computer,
        models=pool,
        store=store,
        config=HarnessConfig(max_actions_per_advance=1),
    )
    run = RunSnapshot(
        run_id="recover-unverified-terminal-draft",
        task="Disable the dim-screen setting",
        status=RunStatus.PAUSED,
        session_id="s_1",
        observation=await computer.open("recovery-test"),
        plan=PlanDecision(
            summary="Disable the requested setting.",
            steps=["Enter the exact local setting command."],
            success_criteria=["The dim-screen setting is off."],
            constraints=["Preserve unrelated settings."],
        ),
    )
    run.record(
        "action.checkpointed",
        index=5,
        actions=[
            {
                "type": "type_text",
                "text": (
                    "gsettings set "
                    "org.gnome.settings-daemon.plugins.power idle-dim false"
                ),
                "code": True,
                "context": "terminal",
            }
        ],
    )
    run.record(
        "action.completed_unverified",
        index=5,
        input_receipts=[
            {
                "index": 0,
                "issued_characters": 68,
                "requested_characters": 68,
                "requested_sha256": "a" * 64,
                "issued_prefix_sha256": "a" * 64,
                "exact_readback_sha256_match": False,
            }
        ],
    )
    await store.save(run)

    result = await harness.continue_run(run.run_id)

    assert provider.controller_calls == 2
    assert [burst["actions"] for burst in computer.bursts] == [
        [{"type": "key", "keys": ["CTRL", "C"]}]
    ]
    controller_prompts = [
        request.prompt
        for request in provider.requests
        if request.role == "controller"
    ]
    assert '"controller_feedback": {' in controller_prompts[1]
    assert "Do not append, retype, or execute the unread draft" in (
        controller_prompts[1]
    )
    assert any(
        event.kind == "controller.unverified_input_followup_rejected"
        for event in result.events
    )


@pytest.mark.asyncio
async def test_unverified_editor_input_refuses_generic_undo_before_hid() -> None:
    class RecoveryProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__()
            self.controller_calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.role == "controller":
                self.requests.append(request)
                self.controller_calls += 1
                action = (
                    {"type": "click", "x": 420, "y": 220}
                    if self.controller_calls == 1
                    else {"type": "key", "keys": ["CTRL", "Z"]}
                )
                return ModelResponse(
                    provider=self.name,
                    model="scripted-v1",
                    data={
                        "outcome": "act",
                        "intent": "Undo the unread editor input safely.",
                        "actions": [action],
                        "expected_evidence": [
                            "The editor returns to its prior clean line."
                        ],
                    },
                )
            return await super().complete(request)

    provider = RecoveryProvider()
    computer = FakeComputer()
    store = InMemoryRunStore()
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            role: RoleRoute(providers=[provider.name])
            for role in ("reasoner", "controller", "verifier")
        },
    )
    harness = AgentHarness(
        computer=computer,
        models=pool,
        store=store,
        config=HarnessConfig(max_actions_per_advance=1),
    )
    run = RunSnapshot(
        run_id="recover-unverified-editor-input",
        task="Enter a Python loop in the open editor.",
        status=RunStatus.PAUSED,
        session_id="s_1",
        observation=await computer.open("editor-recovery-test"),
        plan=PlanDecision(
            summary="Enter the requested loop.",
            steps=["Enter and verify the exact editor line."],
            success_criteria=["The exact lowercase loop is visible."],
            constraints=["Do not duplicate unread input."],
        ),
    )
    run.record(
        "action.checkpointed",
        index=5,
        actions=[
            {
                "type": "type_text",
                "text": "    for i in range(1, limit + 1):",
                "code": True,
                "context": "editor",
                "verification": "exact",
            }
        ],
    )
    run.record(
        "action.recoverable_failure",
        index=5,
        input_receipts=[
            {
                "index": 0,
                "requested_characters": 33,
                "issued_characters": 33,
                "requested_sha256": "a" * 64,
                "issued_prefix_sha256": "a" * 64,
                "exact_readback_sha256_match": False,
            }
        ],
    )
    await store.save(run)

    result = await harness.continue_run(run.run_id)

    assert provider.controller_calls >= 2
    assert computer.bursts == []
    assert any(
        event.kind == "controller.unverified_input_followup_rejected"
        for event in result.events
    )
    controller_prompts = [
        request.prompt
        for request in provider.requests
        if request.role == "controller"
    ]
    assert "editor Undo can coalesce" in controller_prompts[1]


def test_completed_editor_undo_does_not_clear_unverified_input_gate() -> None:
    run = RunSnapshot(
        run_id="completed-editor-undo",
        task="Enter a Python loop in the open editor.",
        status=RunStatus.PAUSED,
    )
    run.record(
        "action.checkpointed",
        index=5,
        actions=[
            {
                "type": "type_text",
                "text": "    for i in range(1, limit + 1):",
                "context": "editor",
                "verification": "exact",
            }
        ],
    )
    run.record(
        "action.recoverable_failure",
        index=5,
        input_receipts=[
            {
                "index": 0,
                "requested_characters": 33,
                "issued_characters": 33,
                "requested_sha256": "a" * 64,
                "issued_prefix_sha256": "a" * 64,
                "exact_readback_sha256_match": False,
            }
        ],
    )
    run.record(
        "action.checkpointed",
        index=6,
        actions=[{"type": "key", "keys": ["CTRL", "Z"]}],
    )
    run.record("action.completed", index=6, status="completed")

    assert AgentHarness._unsafe_unverified_input_followup(
        run,
        [
            {
                "type": "type_text",
                "text": "    for i in range(1, limit + 1):",
                "context": "editor",
                "verification": "exact",
            }
        ],
    )


def test_recent_verified_actions_keep_bounded_durable_task_evidence() -> None:
    run = RunSnapshot(
        run_id="durable-verification-memory",
        task="Save and reopen the workbook",
        status=RunStatus.PAUSED,
    )
    run.record(
        "action.checkpointed",
        index=3,
        intent="Select B8 and inspect its stored formula.",
        actions=[{"type": "click", "x": 100, "y": 200}],
    )
    run.record("action.completed", index=3, status="completed")
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="B8 contains =SUM(B4:B7), not a typed constant.",
    )
    run.record(
        "action.checkpointed",
        index=4,
        intent="Focus the filename field.",
        actions=[{"type": "key", "keys": ["alt+n"]}],
    )
    run.record("action.completed", index=4, status="completed")
    run.record(
        "model.completed",
        role="verifier",
        verdict="uncertain",
        summary="Focus was not visibly proven.",
    )

    assert AgentHarness._recent_verified_actions(run) == [
        {
            "action_index": 3,
            "intent": "Select B8 and inspect its stored formula.",
            "verdict": "verified",
            "summary": "B8 contains =SUM(B4:B7), not a typed constant.",
        }
    ]


def test_secret_input_receipt_is_redacted_again_at_harness_boundary() -> None:
    receipts = AgentHarness._public_input_receipts(
        {
            "action_receipts": [
                {
                    "index": 0,
                    "type": "type_text",
                    "status": "verified_exact",
                    "observed_text": "maliciously retained secret",
                    "observed_text_redacted": False,
                    "summary": "maliciously retained secret",
                    "typed_characters": 27,
                    "intended_characters": 27,
                    "intended_sha256": "b" * 64,
                    "acknowledged_prefix_sha256": "b" * 64,
                    "observed_sha256": "b" * 64,
                    "exact_sha256_match": True,
                }
            ]
        },
        [{"type": "type_text", "text": "password", "secret": True}],
    )

    assert receipts == [
        {
            "index": 0,
            "type": "type_text",
            "status": "delivered_unverified",
            "verdict": "unverified",
            "focus_evidence": "read_back_not_retained",
            "proof_state": "not_retained",
            "observed_text_redacted": True,
            "requested_characters": 27,
            "issued_characters": 27,
        }
    ]
    assert "secret" not in repr(receipts)


@pytest.mark.asyncio
async def test_continue_recovers_persisted_type_unverified_failure() -> None:
    provider = ScriptedProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)
    legacy = await harness.create("Type hello world in the open editor.")
    legacy.status = RunStatus.FAILED
    legacy.error = "typed text did not change the screen"
    legacy.observation = ComputerObservation(
        session_id=legacy.session_id or "s_1",
        status="failed",
        frame_id=2,
        world_version=7,
        control_epoch=2,
        error=legacy.error,
        raw={"reason": "type_unverified"},
    )
    await harness.store.save(legacy)

    completed = await harness.continue_run(legacy.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert completed.next_action_index == 2
    assert computer.bursts[0]["idempotency_key"].startswith(
        f"{legacy.run_id}:action:1:"
    )


@pytest.mark.asyncio
async def test_verifier_failure_preserves_plan_for_a_faster_correction() -> None:
    provider = FailingVerifierProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Type hello world in the open editor.")

    assert paused.status is RunStatus.PAUSED
    assert paused.plan is not None
    assert paused.next_action_index == 1
    assert paused.last_verification is not None
    assert paused.last_verification.verdict == "failed"
    assert paused.events[-1].kind == "verification.failed"
    assert paused.events[-1].data["plan_reused"] is True
    verifier_prompt = next(
        request.prompt for request in provider.requests if request.role == "verifier"
    )
    normalized_prompt = " ".join(verifier_prompt.split())
    assert '"last_controller": {' in normalized_prompt
    assert '"intent": "Type the requested text into the already-focused editor."' in (
        normalized_prompt
    )


@pytest.mark.asyncio
async def test_verifier_failure_correction_does_not_repeat_reasoning() -> None:
    provider = CorrectingVerifierProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Type hello world in the open editor.")
    completed = await harness.continue_run(paused.run_id)

    assert paused.status is RunStatus.PAUSED
    assert completed.status is RunStatus.COMPLETED
    assert len(computer.bursts) == 2
    assert sum(request.role == "reasoner" for request in provider.requests) == 1


@pytest.mark.asyncio
async def test_invalid_structured_controller_gets_one_pre_hid_repair_attempt() -> None:
    provider = InvalidThenRepairedControllerProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    completed = await harness.start("Type hello world in the open editor.")

    assert completed.status is RunStatus.COMPLETED
    assert provider.controller_calls == 2
    assert len(computer.bursts) == 1
    controller_requests = [
        request for request in provider.requests if request.role == "controller"
    ]
    assert "YOUR PREVIOUS JSON WAS REJECTED" not in controller_requests[0].prompt
    assert "YOUR PREVIOUS JSON WAS REJECTED" in controller_requests[1].prompt
    assert '"input"' not in controller_requests[1].prompt


@pytest.mark.asyncio
async def test_whitespace_only_controller_gets_one_pre_hid_repair_attempt() -> None:
    class WhitespaceThenRepairedControllerProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__()
            self.controller_calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.role == "controller":
                self.controller_calls += 1
                if self.controller_calls == 1:
                    self.requests.append(request)
                    return ModelResponse(
                        provider=self.name,
                        model="scripted-v1",
                        data={
                            "outcome": "act",
                            "intent": "Indent the next editor line.",
                            "actions": [
                                {
                                    "type": "type_text",
                                    "text": "    ",
                                    "verification": "exact",
                                }
                            ],
                            "expected_evidence": [
                                "The editor line is indented."
                            ],
                        },
                    )
            return await super().complete(request)

    provider = WhitespaceThenRepairedControllerProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    completed = await harness.start("Type hello world in the open editor.")

    assert completed.status is RunStatus.COMPLETED
    assert provider.controller_calls == 2
    assert len(computer.bursts) == 1
    assert [
        action["text"]
        for action in computer.bursts[0]["actions"]
        if action["type"] == "type_text"
    ] == ["hello world"]
    controller_requests = [
        request for request in provider.requests if request.role == "controller"
    ]
    assert "YOUR PREVIOUS JSON WAS REJECTED" in controller_requests[1].prompt
    assert "whitespace-only type_text" in controller_requests[1].prompt


@pytest.mark.asyncio
async def test_exact_repeated_action_is_stopped_before_duplicate_hid() -> None:
    provider = StallingControllerProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    first_checkpoint = await harness.start("Type hello world in the open editor.")
    paused = await harness.continue_run(first_checkpoint.run_id)

    assert first_checkpoint.status is RunStatus.PAUSED
    assert any(
        event.kind == "verification.action_rejected"
        for event in first_checkpoint.events
    )
    assert paused.status is RunStatus.PAUSED
    assert paused.error == "controller repeated the previous action unchanged"
    assert paused.next_action_index == 1
    assert len(computer.bursts) == 1
    assert paused.events[-1].kind == "controller.repeated_actions"
    controller_prompts = [
        request.prompt for request in provider.requests if request.role == "controller"
    ]
    assert '"trajectory_signals": {' in controller_prompts[-1]
    assert '"type_text": 1' in controller_prompts[-1]
    assert "visible no results, do not repeat it" in controller_prompts[-1]


@pytest.mark.asyncio
async def test_stale_refusal_requires_fresh_controller_decision_before_retry() -> None:
    provider = ScriptedProvider()
    computer = StaleThenFreshComputer()
    harness = build_harness(provider, computer)

    stale = await harness.start("Type hello world in the open editor.")
    completed = await harness.continue_run(stale.run_id)

    assert stale.status is RunStatus.PAUSED
    assert stale.observation is not None
    assert stale.observation.world_version == 9
    assert stale.pending_action is None
    assert computer.refreshes == 1
    assert len(computer.bursts) == 2
    assert computer.bursts[1]["based_on_world_version"] == 9
    assert (
        computer.bursts[1]["idempotency_key"]
        == computer.bursts[0]["idempotency_key"]
    )
    assert completed.status is RunStatus.COMPLETED
    assert any(event.kind == "action.stale_world_refreshed" for event in stale.events)
    assert not any(
        event.kind == "action.stale_world_retry_checkpointed" for event in stale.events
    )
    assert sum(request.role == "reasoner" for request in provider.requests) == 1
    assert sum(request.role == "controller" for request in provider.requests) == 2


@pytest.mark.asyncio
async def test_stale_refusal_never_rebases_a_commit_key() -> None:
    provider = KeyOnlyControllerProvider()
    computer = StaleThenFreshComputer()
    harness = build_harness(provider, computer)

    stale = await harness.start("Submit the already prepared value.")

    assert stale.status is RunStatus.PAUSED
    assert stale.pending_action is None
    assert computer.refreshes == 1
    assert len(computer.bursts) == 1
    assert any(event.kind == "action.stale_world_refreshed" for event in stale.events)
    assert not any(
        event.kind == "action.stale_world_retry_checkpointed"
        for event in stale.events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "keys",
    [["CTRL", "ALT", "T"], ["SUPER"], ["META"], ["ESC"]],
)
async def test_stale_refusal_requires_fresh_decision_for_global_shortcut(
    keys: list[str],
) -> None:
    provider = GlobalShortcutControllerProvider(keys)
    computer = StaleThenFreshComputer()
    harness = build_harness(provider, computer)

    stale = await harness.start("Open a terminal.")
    completed = await harness.continue_run(stale.run_id)

    assert stale.status is RunStatus.PAUSED
    assert stale.pending_action is None
    assert computer.refreshes == 1
    assert len(computer.bursts) == 2
    assert computer.bursts[1]["based_on_world_version"] == 9
    assert (
        computer.bursts[1]["idempotency_key"]
        == computer.bursts[0]["idempotency_key"]
    )
    assert completed.status is RunStatus.COMPLETED
    assert any(event.kind == "action.stale_world_refreshed" for event in stale.events)
    assert not any(
        event.kind == "action.stale_world_retry_checkpointed" for event in stale.events
    )
    assert sum(request.role == "controller" for request in provider.requests) == 2


@pytest.mark.asyncio
async def test_stale_refusal_requires_fresh_decision_for_navigation_sequence() -> None:
    provider = GlobalShortcutSequenceControllerProvider()
    computer = StaleThenFreshComputer()
    harness = build_harness(provider, computer)

    stale = await harness.start("Exit fullscreen and open the desktop overview.")
    completed = await harness.continue_run(stale.run_id)

    assert stale.status is RunStatus.PAUSED
    assert stale.pending_action is None
    assert len(computer.bursts) == 2
    assert computer.bursts[1]["actions"] == [
        {"type": "key", "keys": ["ESC"]},
        {"type": "key", "keys": ["META"]},
    ]
    assert (
        computer.bursts[1]["idempotency_key"]
        == computer.bursts[0]["idempotency_key"]
    )
    assert completed.status is RunStatus.COMPLETED
    assert sum(request.role == "controller" for request in provider.requests) == 2


@pytest.mark.asyncio
async def test_stale_refusal_never_rebases_focus_dependent_shortcut() -> None:
    provider = GlobalShortcutControllerProvider(["CTRL", "L"])
    computer = StaleThenFreshComputer()
    harness = build_harness(provider, computer)

    stale = await harness.start("Focus the browser address bar.")

    assert stale.status is RunStatus.PAUSED
    assert stale.pending_action is None
    assert len(computer.bursts) == 1
    assert any(event.kind == "action.stale_world_refreshed" for event in stale.events)
    assert not any(
        event.kind == "action.stale_world_retry_checkpointed"
        for event in stale.events
    )


@pytest.mark.asyncio
async def test_failed_text_search_cannot_repeat_after_intervening_focus_action() -> None:
    provider = RepeatedFailedSearchProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    first = await harness.start("Find the requested setting.")
    focused = await harness.continue_run(first.run_id)
    stopped = await harness.continue_run(focused.run_id)

    assert first.status is RunStatus.PAUSED
    assert focused.status is RunStatus.PAUSED
    assert stopped.status is RunStatus.PAUSED
    assert stopped.error == "controller repeated text input after unsuccessful verification"
    assert len(computer.bursts) == 2
    assert stopped.events[-1].kind == "controller.repeated_unsuccessful_text"


@pytest.mark.asyncio
async def test_verifier_receives_labelled_before_after_composite(
    tmp_path: Path,
) -> None:
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    Image.new("RGB", (10, 10), "#ff0000").save(before)
    Image.new("RGB", (10, 10), "#0000ff").save(after)
    provider = ScriptedProvider()
    harness = build_harness(provider, ImageComputer(before, after))

    result = await harness.start("Type hello world in the open editor.")

    assert result.status is RunStatus.COMPLETED
    request = next(item for item in provider.requests if item.role == "verifier")
    assert request.image_path not in {str(before), str(after)}
    assert request.image_path is not None
    composite = Path(request.image_path)
    assert result.latest_verification_image_path == str(composite)
    assert result.latest_verification_image_revision == 1
    assert len(result.verification_images) == 1
    assert result.verification_images[0].revision == 1
    assert result.verification_images[0].action_index == 1
    assert result.verification_images[0].path == str(composite)
    evidence_event = next(
        event
        for event in result.events
        if event.kind == "verification.evidence_captured"
    )
    assert evidence_event.data == {
        "revision": 1,
        "action_index": 1,
        "before_frame_id": 1,
        "after_frame_id": 2,
    }
    assert composite.is_file()
    assert "before-after" in composite.name
    with Image.open(composite) as image:
        assert image.size == (20, 42)
        assert image.getpixel((5, 37))[0] > 240
        assert image.getpixel((15, 37))[2] > 240
    normalized_prompt = " ".join(request.prompt.split())
    assert "left panel is BEFORE" in normalized_prompt
    assert "right panel is AFTER" in normalized_prompt


@pytest.mark.asyncio
async def test_nearby_toggle_retry_is_stopped_before_second_hid() -> None:
    provider = ToggleRetryProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    first = await harness.start("Enable Do Not Disturb.")
    blocked = await harness.continue_run(first.run_id)

    assert first.status is RunStatus.PAUSED
    assert blocked.status is RunStatus.BLOCKED
    assert blocked.error == (
        "unsafe retry of a state-changing toggle after ambiguous verification"
    )
    assert len(computer.bursts) == 1
    assert blocked.next_action_index == 1
    assert blocked.events[-1].kind == "controller.non_idempotent_retry_stopped"


@pytest.mark.asyncio
async def test_contradictory_complete_verdict_cannot_end_the_task() -> None:
    provider = ContradictoryCompletionProvider()
    computer = FakeComputer()
    harness = build_harness(provider, computer)

    paused = await harness.start("Type hello world in the open editor.")

    assert paused.status is RunStatus.PAUSED
    assert paused.last_verification is not None
    assert paused.last_verification.verdict == "verified"
    rejected = [
        event
        for event in paused.events
        if event.kind == "verification.complete_rejected"
    ]
    assert len(rejected) == 1
    assert "criterion 0" in rejected[0].data["reason"]
    assert len(computer.bursts) == 1


def test_reopen_completion_requires_a_later_verified_reopen_action() -> None:
    run = RunSnapshot(
        run_id="reopen-transition-gate",
        task=(
            "Save the sentence as text-01.txt. Reopen the file and verify it."
        ),
        status=RunStatus.RUNNING,
        plan=PlanDecision(
            summary="Save and reopen the file.",
            steps=["Save the file", "Reopen it", "Verify the text"],
            success_criteria=["The reopened file contains the exact sentence."],
            constraints=[],
        ),
    )
    run.record(
        "action.checkpointed",
        index=9,
        intent="Save the verified filename in the permitted workspace.",
        actions=[{"type": "click", "x": 587, "y": 425}],
    )
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="The Save As dialog closed and Notepad remains open.",
    )
    verdict = VerificationDecision(
        verdict="complete",
        summary="Saved text-01.txt and reopened it.",
        evidence=["Notepad visibly shows the sentence."],
        criteria=[
            {
                "criterion_index": 0,
                "satisfied": True,
                "evidence": "The sentence is visible in Notepad.",
            }
        ],
        action_criteria=[],
    )

    rejection = AgentHarness._completion_rejection_reason(run, verdict)

    assert rejection is not None
    assert "separately verified reopen action after save" in rejection


def test_later_verified_reopen_action_satisfies_reopen_completion_gate() -> None:
    run = RunSnapshot(
        run_id="verified-reopen-transition",
        task=(
            "Save the sentence as text-01.txt. Reopen the file and verify it."
        ),
        status=RunStatus.RUNNING,
        plan=PlanDecision(
            summary="Save and reopen the file.",
            steps=["Save the file", "Reopen it", "Verify the text"],
            success_criteria=["The reopened file contains the exact sentence."],
            constraints=[],
        ),
    )
    for index, intent, summary in (
        (
            9,
            "Save the verified filename in the permitted workspace.",
            "The Save As dialog closed and the file is saved.",
        ),
        (
            10,
            "Reopen the saved text-01.txt file from the workspace.",
            "The saved file reopened and the exact sentence is visible.",
        ),
    ):
        run.record(
            "action.checkpointed",
            index=index,
            intent=intent,
            actions=[{"type": "click", "x": 587, "y": 425}],
        )
        run.record("action.completed", index=index, status="completed")
        run.record(
            "model.completed",
            role="verifier",
            verdict="verified",
            summary=summary,
        )
    verdict = VerificationDecision(
        verdict="complete",
        summary="Saved and reopened text-01.txt.",
        evidence=["The reopened file visibly contains the exact sentence."],
        criteria=[
            {
                "criterion_index": 0,
                "satisfied": True,
                "evidence": "The reopened file visibly contains the sentence.",
            }
        ],
        action_criteria=[],
    )

    assert AgentHarness._completion_rejection_reason(run, verdict) is None


@pytest.mark.parametrize(
    "reopen_intent",
    [
        "Open the verified CSV from the native Open dialog.",
        "Open the selected saved text file in the native Open dialog.",
    ],
)
def test_verified_open_dialog_commit_satisfies_reopen_completion_gate(
    reopen_intent: str,
) -> None:
    """Regress the exact reopen wording from live CSV campaign v1."""

    run = RunSnapshot(
        run_id="verified-open-dialog-commit",
        task="Save text-09.csv. Reopen it and verify all values.",
        status=RunStatus.RUNNING,
        plan=PlanDecision(
            summary="Save and reopen the CSV.",
            steps=["Save the file", "Reopen it", "Verify every value"],
            success_criteria=["The reopened CSV contains all requested values."],
            constraints=[],
        ),
    )
    for index, intent, expected_evidence, summary in (
        (
            13,
            "Commit the verified CSV basename in the visible Save As dialog.",
            ["The Save As dialog closes and the CSV remains visible."],
            "The CSV is saved under the requested basename.",
        ),
        (
            17,
            reopen_intent,
            [
                "The Open dialog closes and Notepad visibly displays the "
                "reopened CSV with its header and four data rows."
            ],
            "Created and reopened text-09.csv; all CSV values are visible.",
        ),
    ):
        run.record(
            "action.checkpointed",
            index=index,
            intent=intent,
            actions=[{"type": "click", "x": 570, "y": 408}],
            expected_evidence=expected_evidence,
        )
        run.record("action.completed", index=index, status="completed")
        run.record(
            "model.completed",
            role="verifier",
            verdict="verified",
            summary=summary,
        )
    verdict = VerificationDecision(
        verdict="complete",
        summary=(
            "Created and reopened text-09.csv in Notepad with the requested "
            "header and all four quarterly rows."
        ),
        evidence=[
            "The visible Notepad document shows the header plus Q1 through Q4."
        ],
        criteria=[
            {
                "criterion_index": 0,
                "satisfied": True,
                "evidence": "The reopened CSV contains all requested values.",
            }
        ],
        action_criteria=[],
    )

    assert AgentHarness._completion_rejection_reason(run, verdict) is None


def test_opening_native_open_dialog_does_not_satisfy_reopen_gate() -> None:
    run = RunSnapshot(
        run_id="open-dialog-is-not-reopen",
        task="Save text-09.csv. Reopen it and verify all values.",
        status=RunStatus.RUNNING,
        plan=PlanDecision(
            summary="Save and reopen the CSV.",
            steps=["Save the file", "Reopen it", "Verify every value"],
            success_criteria=["The reopened CSV contains all requested values."],
            constraints=[],
        ),
    )
    for index, intent, summary in (
        (
            13,
            "Commit the verified CSV basename in the visible Save As dialog.",
            "The CSV has been saved and remains visible in Notepad.",
        ),
        (
            14,
            "Open Notepad's native Open dialog to reopen the just-saved CSV.",
            "The native Open dialog is visible; no file has reopened yet.",
        ),
    ):
        run.record(
            "action.checkpointed",
            index=index,
            intent=intent,
            actions=[{"type": "click", "x": 570, "y": 408}],
        )
        run.record("action.completed", index=index, status="completed")
        run.record(
            "model.completed",
            role="verifier",
            verdict="verified",
            summary=summary,
        )
    verdict = VerificationDecision(
        verdict="complete",
        summary="Saved and reopened text-09.csv.",
        evidence=["The CSV values are visible."],
        criteria=[
            {
                "criterion_index": 0,
                "satisfied": True,
                "evidence": "The CSV values are visible.",
            }
        ],
        action_criteria=[],
    )

    rejection = AgentHarness._completion_rejection_reason(run, verdict)

    assert rejection is not None
    assert "separately verified reopen action after save" in rejection


def test_delayed_reopen_frame_can_verify_last_completed_action() -> None:
    run = RunSnapshot(
        run_id="delayed-verified-reopen-transition",
        task="Save text-04.txt. Reopen the file and verify it.",
        status=RunStatus.RUNNING,
        plan=PlanDecision(
            summary="Save and reopen the file.",
            steps=["Save the file", "Reopen it", "Verify the text"],
            success_criteria=["The reopened file contains the exact text."],
            constraints=[],
        ),
    )
    run.record(
        "action.checkpointed",
        index=9,
        intent="Save the verified filename in the permitted workspace.",
        actions=[{"type": "click", "x": 667, "y": 506}],
    )
    run.record("action.completed", index=9, status="completed")
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="The Save As dialog closed and the file is saved.",
    )
    run.record(
        "action.checkpointed",
        index=10,
        intent="Open the selected text-04.txt file in Notepad.",
        actions=[{"type": "click", "x": 570, "y": 408}],
    )
    run.record("action.completed", index=10, status="completed")
    run.record(
        "model.completed",
        role="verifier",
        verdict="failed",
        summary="The delayed frame still shows the Open dialog.",
    )
    run.record(
        "action.checkpointed",
        index=11,
        intent="Open the selected file using a second activation.",
        actions=[{"type": "double_click", "x": 218, "y": 203}],
    )
    run.record(
        "action.ungrounded_refreshed",
        reason="coordinate click target could not be independently read",
    )
    run.record(
        "model.completed",
        role="verifier",
        verdict="complete",
        summary="The delayed frame now shows the reopened text-04.txt.",
    )
    verdict = VerificationDecision(
        verdict="complete",
        summary="Saved and reopened text-04.txt.",
        evidence=["The reopened file visibly contains the exact text."],
        criteria=[
            {
                "criterion_index": 0,
                "satisfied": True,
                "evidence": "The reopened file contains the exact text.",
            }
        ],
        action_criteria=[],
    )

    assert AgentHarness._completion_rejection_reason(run, verdict) is None


def test_saved_file_selection_then_delayed_reopen_satisfies_gate() -> None:
    """Regress the exact durable evidence sequence from live campaign v6."""

    run = RunSnapshot(
        run_id="selected-save-delayed-reopen-transition",
        task="Save text-06.txt. Reopen the file and verify it.",
        status=RunStatus.RUNNING,
        plan=PlanDecision(
            summary="Save and reopen the file.",
            steps=["Save the file", "Reopen it", "Verify the text"],
            success_criteria=["The reopened file contains the exact text."],
            constraints=[],
        ),
    )
    run.record(
        "action.checkpointed",
        index=9,
        intent="Save the completed paragraph under the verified filename.",
        actions=[{"type": "key", "keys": ["ENTER"]}],
    )
    run.record("action.completed", index=9, status="completed")
    run.record(
        "model.completed",
        role="verifier",
        verdict="uncertain",
        summary="The Save As dialog closed but the filename is not legible.",
    )
    run.record(
        "action.checkpointed",
        index=11,
        intent=(
            "Select the visibly listed saved text-06.txt file in the native "
            "Open dialog."
        ),
        actions=[{"type": "click", "x": 218, "y": 203}],
    )
    run.record("action.completed", index=11, status="completed")
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary=(
            "The saved text-06.txt file is selected in the Open dialog and "
            "ready to open."
        ),
    )
    run.record(
        "action.checkpointed",
        index=12,
        intent="Open the selected saved text-06.txt file from the Open dialog.",
        actions=[{"type": "key", "keys": ["ENTER"]}],
    )
    run.record("action.completed", index=12, status="completed")
    run.record(
        "model.completed",
        role="verifier",
        verdict="failed",
        summary="The delayed frame still shows the Open dialog.",
    )
    run.record(
        "verification.delayed_frame_observed",
        action_index=12,
    )
    verdict = VerificationDecision(
        verdict="complete",
        summary=(
            "The saved text-06.txt file has reopened and displays the complete "
            "original text."
        ),
        evidence=["The reopened file visibly contains the exact text."],
        criteria=[
            {
                "criterion_index": 0,
                "satisfied": True,
                "evidence": "The reopened file contains the exact text.",
            }
        ],
        action_criteria=[],
    )
    current_reopen = PendingAction(
        index=12,
        intent="Open the selected saved text-06.txt file from the Open dialog.",
        actions=[{"type": "key", "keys": ["ENTER"]}],
        expected_evidence=["The reopened file contains the exact text."],
        based_on_world_version=4,
        based_on_control_epoch=0,
        idempotency_key="run:action:12",
    )

    assert (
        AgentHarness._completion_rejection_reason(
            run,
            verdict,
            action=current_reopen,
        )
        is None
    )


def test_unexecuted_reopen_intent_cannot_satisfy_completion_gate() -> None:
    run = RunSnapshot(
        run_id="unexecuted-reopen-transition",
        task="Save text-03.txt. Reopen the file and verify it.",
        status=RunStatus.RUNNING,
        plan=PlanDecision(
            summary="Save and reopen the file.",
            steps=["Save the file", "Reopen it", "Verify the text"],
            success_criteria=["The reopened file contains the exact text."],
            constraints=[],
        ),
    )
    run.record(
        "action.checkpointed",
        index=7,
        intent="Save the verified filename in the permitted workspace.",
        actions=[{"type": "click", "x": 667, "y": 506}],
    )
    run.record("action.completed", index=7, status="completed")
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="The file is saved and remains open in Notepad.",
    )
    run.record(
        "action.checkpointed",
        index=8,
        intent="Establish editor focus before reopening the saved file.",
        actions=[{"type": "click", "x": 600, "y": 300}],
    )
    run.record(
        "action.ungrounded_refreshed",
        reason="coordinate click target could not be independently read",
    )
    run.record(
        "model.completed",
        role="verifier",
        verdict="verified",
        summary="The file is still visibly open in Notepad.",
    )
    verdict = VerificationDecision(
        verdict="complete",
        summary="Saved and reopened text-03.txt.",
        evidence=["The requested text is visible."],
        criteria=[
            {
                "criterion_index": 0,
                "satisfied": True,
                "evidence": "The requested text is visible.",
            }
        ],
        action_criteria=[],
    )

    rejection = AgentHarness._completion_rejection_reason(run, verdict)

    assert rejection is not None
    assert "separately verified reopen action after save" in rejection


def test_verified_overwrite_confirmation_counts_as_save_before_reopen() -> None:
    run = RunSnapshot(
        run_id="verified-overwrite-reopen-transition",
        task=(
            "Replace text-01.txt if it exists. Reopen the file and verify it."
        ),
        status=RunStatus.RUNNING,
        plan=PlanDecision(
            summary="Replace and reopen the file.",
            steps=["Confirm replacement", "Reopen it", "Verify the text"],
            success_criteria=["The reopened file contains the exact sentence."],
            constraints=[],
        ),
    )
    for index, intent, summary in (
        (
            12,
            (
                "Confirm replacement of the existing text-01.txt in the "
                "visible Save As confirmation dialog."
            ),
            "The replacement was confirmed and Notepad shows the saved file.",
        ),
        (
            13,
            "Open the selected text-01.txt from the Open dialog to reopen it.",
            "The saved file reopened and the exact sentence is visible.",
        ),
    ):
        run.record(
            "action.checkpointed",
            index=index,
            intent=intent,
            actions=[{"type": "key", "keys": ["ENTER"]}],
        )
        run.record("action.completed", index=index, status="completed")
        run.record(
            "model.completed",
            role="verifier",
            verdict="verified",
            summary=summary,
        )
    verdict = VerificationDecision(
        verdict="complete",
        summary="Replaced and reopened text-01.txt.",
        evidence=["The reopened file visibly contains the exact sentence."],
        criteria=[
            {
                "criterion_index": 0,
                "satisfied": True,
                "evidence": "The reopened file visibly contains the sentence.",
            }
        ],
        action_criteria=[],
    )

    assert AgentHarness._completion_rejection_reason(run, verdict) is None


@pytest.mark.asyncio
async def test_rejected_done_decision_forces_a_fresh_plan() -> None:
    provider = RejectedDoneProvider()
    harness = build_harness(provider, FakeComputer())

    paused = await harness.start("Disable the requested setting.")

    assert paused.status is RunStatus.PAUSED
    assert paused.plan is None
    assert [
        event.kind for event in paused.events
    ][-3:] == [
        "verification.complete_rejected",
        "run.replanning_after_incomplete_done",
        "run.paused",
    ]

    paused_again = await harness.continue_run(paused.run_id)

    assert paused_again.status is RunStatus.PAUSED
    assert sum(
        request.role == "reasoner" for request in provider.requests
    ) == 2


def test_verification_schema_requires_per_criterion_assessments() -> None:
    schema = VerificationDecision.model_json_schema()

    assert "criteria" in schema["properties"]
    assert "action_criteria" in schema["properties"]
    assert schema["properties"]["summary"]["maxLength"] == 1_200


def test_verified_action_requires_every_expected_evidence_item() -> None:
    action = PendingAction(
        index=3,
        intent="Inspect B8's stored formula.",
        actions=[{"type": "click", "x": 75, "y": 243}],
        expected_evidence=[
            "The Name Box reads B8.",
            "The formula bar shows =SUM(B4:B7).",
        ],
        based_on_world_version=4,
        based_on_control_epoch=0,
        idempotency_key="run:action:3",
    )
    verified = VerificationDecision(
        verdict="verified",
        summary="B8 and its formula are visibly confirmed.",
        evidence=["B8 is selected and the formula bar is legible."],
        criteria=[],
        action_criteria=[
            {
                "criterion_index": 0,
                "satisfied": True,
                "evidence": "The Name Box visibly reads B8.",
            },
            {
                "criterion_index": 1,
                "satisfied": True,
                "evidence": "The formula bar visibly reads =SUM(B4:B7).",
            },
        ],
    )

    assert (
        AgentHarness._verified_action_rejection_reason(action, verified)
        is None
    )
    assert "expected indexes 0..1" in (
        AgentHarness._verified_action_rejection_reason(
            action,
            verified.model_copy(update={"action_criteria": []}),
        )
        or ""
    )
    assert "expected evidence 1" in (
        AgentHarness._verified_action_rejection_reason(
            action,
            verified.model_copy(
                update={
                    "action_criteria": [
                        verified.action_criteria[0],
                        verified.action_criteria[1].model_copy(
                            update={"satisfied": False}
                        ),
                    ]
                }
            ),
        )
        or ""
    )


def test_verification_summary_is_bounded_for_user_facing_chat() -> None:
    with pytest.raises(ValidationError):
        VerificationDecision(
            verdict="complete",
            summary="x" * 1_201,
            evidence=[],
            criteria=[],
        )
