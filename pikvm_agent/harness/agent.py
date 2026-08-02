"""Checkpointed provider-neutral task harness over the raw PiKVM MCP tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from decimal import Decimal
from math import isqrt
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from pikvm_agent.core.windows_launch import (
    is_verified_windows_run_launch,
    is_windows_run_focus_preflight,
    is_windows_run_key_action,
)
from pikvm_agent.executor.burst import BurstError, normalize_keys, validate_actions
from pikvm_agent.harness.agent_models import (
    TERMINAL_RUN_STATUSES,
    ComputerObservation,
    ComputerSessionMissingError,
    ControllerDecision,
    HarnessConfig,
    ModelRequest,
    ModelRole,
    PendingAction,
    PlanDecision,
    RunModelRoute,
    RunSnapshot,
    RunStatus,
    VerificationDecision,
    VerificationImageArtifact,
)
from pikvm_agent.harness.agent_store import RunStore
from pikvm_agent.harness.input_receipts import public_input_receipts
from pikvm_agent.harness.model_budget import (
    DurableRunModelBudget,
    ModelBudgetExceeded,
    ModelBudgetPolicy,
)
from pikvm_agent.harness.model_pool import ModelPool, ModelPoolError
from pikvm_agent.harness.redaction import redact_secrets


class ComputerDriver(Protocol):
    async def open(self, label: str) -> ComputerObservation: ...

    async def refresh(self, *, session_id: str) -> ComputerObservation: ...

    async def burst(
        self,
        *,
        session_id: str,
        actions: list[dict[str, Any]],
        based_on_world_version: int | None,
        based_on_control_epoch: int | None,
        idempotency_key: str,
    ) -> ComputerObservation: ...

    async def resolve_approval(
        self, *, session_id: str, approval_id: str, decision: dict[str, Any]
    ) -> ComputerObservation: ...

    async def abort(self, *, session_id: str, reason: str) -> ComputerObservation: ...


_REASONER_SYSTEM = """\
You are the deliberate planner for a physical-computer task. Produce a short,
durable plan and observable completion criteria. The target is accessible only
through screenshots and guarded keyboard/mouse actions. Never propose base64,
large scripts, heredocs, compressed payloads, clipboard APIs, SSH, or hidden
side channels. An on-screen terminal opened and operated through guarded HID is
part of the visible computer, not a hidden side channel. When an exact GUI
control is absent, a short, inspectable command may be planned if it directly
satisfies the local task and its resulting state can be independently verified.
Never use this fallback for a long script, encoded payload, command chain,
installer, package change, or unrelated system mutation. Preserve
existing/default values unless the user explicitly asked to change them.
When the task explicitly asks to inspect a read-only Windows Settings page,
prefer the page's native ``ms-settings:`` URI over manually traversing Settings.
Do not invent a GUI-only or no-terminal constraint merely because the first
strategy uses the GUI. Constraints record authenticated user requirements, not
the planner's preferred route. When an allowed short local fallback is needed
because the exact control is missing from the visible GUI, replan to that
fallback instead of declaring the task impossible.
For a command that may approach the visible line width, maximize or widen the
terminal and separately increase its text size before typing. Both the available
width and the larger text must be independently verified. If the sender issued
a complete terminal draft but the screen could not prove an exact readback,
never append a guessed suffix and never execute the draft. First cancel the
draft with Ctrl+C, increase the terminal text size again, verify a visibly clean
prompt with larger text, and only then plan one clean retype.
Terminal zoom is internal preparation for OCR, not part of the user's task:
do not invent a numeric zoom threshold or percentage success criterion unless
the user or authenticated operator guidance explicitly requested that value.
operator_guidance contains authenticated user/operator
corrections to the original task: obey it, and when entries conflict, the latest
entry wins. Never dismiss a requirement in operator_guidance merely because it
was absent from the original task string. Do not invent exact values, delays,
quantities, formats, or preferences absent from both the task and guidance.
Every success criterion must be necessary to satisfy the user's literal request
as amended by operator guidance, not a nicer or stricter task the planner made
up. Put negative safety guards such as "do not change settings or files" in
constraints, not success_criteria. The action ledger and policy enforce those
guards; do not require a later screenshot to prove that no unrelated mutation
occurred. Keep a negative statement in success_criteria only when that visible
negative state is itself the requested outcome, such as a setting being disabled
or a folder containing no matching files. When the exact label is unavailable,
a semantically equivalent visible control may be used only when its effect
satisfies the amended request and can be verified. Do not add approval-request
steps to the plan. The controller
proposes the next bounded action; the independent daemon policy decides whether
that exact action requires human approval and exits the model loop if it does.
Plan for minimum sufficient evidence. When a task requires saving and reopening,
do not plan a complete content audit both before and after saving. Before saving,
verify only enough to avoid committing an
incorrect artifact; perform the requested detailed audit once, after reopening.
Treat values that are simultaneously legible in one frame as grouped evidence.
Reserve sequential formula-bar or field readbacks for stored formulas, truncated
content, or other requirements the rendered surface cannot prove.
Once a correctly targeted Save As dialog is open, do not cancel an already-open Save As dialog solely to resume an audit that can be completed after reopening.
Cancel only when current evidence indicates the content or destination is wrong.
Treat recent_verified_actions as durable evidence. Do not repeat a completed
check unless the current screen visibly contradicts that recorded result.
Treat recent_input_delivery as sender evidence. sender_finished means the local
sender issued the whole intended payload; it is not a transport acknowledgement
and not screen proof. Do not blindly replay sender-finished text merely because
OCR could not read invisible whitespace or a wrapped field. Re-observe first,
then use a bounded application-level check. Require readback_exact or artifact
evidence before claiming exact on-screen or saved content.
When the task asks you to generate prose or code specifically in Notepad,
author the complete final artifact once in artifact_content and set its
matching artifact_content_kind. Use literal newline characters, no Markdown
fences, and no commentary around the artifact. For code, include the complete
indentation on every line using spaces only. This durable artifact is the
source of truth for deterministic input; do not defer wording, syntax, or
indentation choices to the controller. Leave both artifact fields null when
the task does not create generated text or code in Notepad."""


_NEGATIVE_MUTATION_GUARD_PREFIX = re.compile(
    r"^(?:no\b|do\s+not\b|don't\b|without\b|nothing\b|preserve\b)",
    re.IGNORECASE,
)
_NEGATIVE_MUTATION_GUARD_ACTION = re.compile(
    r"\b(?:chang(?:e|ed|ing)|modif(?:y|ied|ying)|alter(?:ed|ing)?|"
    r"mutat(?:e|ed|ing)|edit(?:ed|ing)?|writ(?:e|ten|ing)|sav(?:e|ed|ing)|"
    r"send|sent|sending|submit(?:ted|ting)?|delet(?:e|ed|ing)|"
    r"remov(?:e|ed|ing)|creat(?:e|ed|ing)|install(?:ed|ing)?)\b",
    re.IGNORECASE,
)
_NEGATIVE_MUTATION_GUARD_TARGET = re.compile(
    r"\b(?:settings?|files?|documents?|messages?|emails?|permissions?|"
    r"accounts?|data|content|configuration|preferences?|system)\b",
    re.IGNORECASE,
)
_DELAYED_VERIFICATION_REFRESH_DELAYS_S = (0.0, 0.45, 0.90)


def _normalize_plan_safety_constraints(
    plan: PlanDecision,
) -> tuple[PlanDecision, int]:
    """Move generic non-mutation guards out of pixel-verifiable outcomes."""

    movable_indices = [
        index
        for index, criterion in enumerate(plan.success_criteria)
        if _NEGATIVE_MUTATION_GUARD_PREFIX.search(criterion.strip())
        and _NEGATIVE_MUTATION_GUARD_ACTION.search(criterion)
        and (
            _NEGATIVE_MUTATION_GUARD_TARGET.search(criterion)
            or criterion.strip().lower().startswith("nothing")
        )
    ]
    if (
        not movable_indices
        or len(movable_indices) == len(plan.success_criteria)
    ):
        return plan, 0

    normalized_constraints = list(plan.constraints)
    constraint_keys = {
        " ".join(constraint.casefold().split())
        for constraint in normalized_constraints
    }
    moved_indices: set[int] = set()
    for index in movable_indices:
        criterion = plan.success_criteria[index]
        key = " ".join(criterion.casefold().split())
        if key not in constraint_keys:
            if len(normalized_constraints) >= 20:
                continue
            normalized_constraints.append(criterion)
            constraint_keys.add(key)
        moved_indices.add(index)

    if not moved_indices:
        return plan, 0
    normalized_criteria = [
        criterion
        for index, criterion in enumerate(plan.success_criteria)
        if index not in moved_indices
    ]
    return (
        plan.model_copy(
            update={
                "success_criteria": normalized_criteria,
                "constraints": normalized_constraints,
            }
        ),
        len(moved_indices),
    )


_READ_ONLY_SETTINGS_VERB = re.compile(
    r"\b(?:check|describe|find|inspect|read|report|show|tell|view|what|which)\b",
    re.IGNORECASE,
)
_MUTATING_SETTINGS_VERB = re.compile(
    r"\b(?:adjust|change|choose|configure|disable|enable|select|set|toggle|"
    r"turn\s+(?:off|on)|update)\b",
    re.IGNORECASE,
)
_NEGATED_MUTATION_CLAUSE = re.compile(
    r"\b(?:do\s+not|don't|without)\s+"
    r"(?:make\s+any\s+)?(?:adjusting|changing|choosing|configuring|disabling|"
    r"enabling|selecting|setting|toggling|turning|updating|change)\b[^.;]*",
    re.IGNORECASE,
)


def _is_read_only_settings_request(run: RunSnapshot) -> bool:
    """Recognise a literal read-only Settings inspection request."""

    request = " ".join([run.task, *run.operator_guidance])
    actionable_request = _NEGATED_MUTATION_CLAUSE.sub("", request)
    return (
        _READ_ONLY_SETTINGS_VERB.search(actionable_request) is not None
        and _MUTATING_SETTINGS_VERB.search(actionable_request) is None
    )


def _normalize_windows_run_launch(
    actions: list[dict[str, Any]],
    *,
    max_actions: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Wait for Windows Run before typing and through Settings startup.

    Windows Run first transitions to a stable Settings splash, then transitions
    again when the requested page is rendered. A single change wait can
    therefore make a fast controller verify the transient splash and pay for a
    complete recovery turn.
    """

    normalized = [dict(action) for action in actions]
    if not is_verified_windows_run_launch(normalized):
        return normalized, 0, False
    text_index = next(
        (
            index
            for index, action in enumerate(normalized)
            if action.get("type") == "type_text"
        ),
        -1,
    )
    if text_index < 0:
        return normalized, 0, False
    run_index = next(
        (
            index
            for index, action in enumerate(normalized[:text_index])
            if is_windows_run_key_action(action)
        ),
        -1,
    )
    pre_type_settle_normalized = False
    pre_type_wait_indices = [
        index
        for index, action in enumerate(
            normalized[run_index + 1 : text_index],
            start=run_index + 1,
        )
        if action.get("type") == "wait"
    ]
    pre_type_change_wait = any(
        action.get("type") == "wait_for_change"
        for action in normalized[run_index + 1 : text_index]
    )
    if pre_type_wait_indices:
        wait_index = pre_type_wait_indices[-1]
        normalized[wait_index] = {
            "type": "wait_for_change",
            "timeout_ms": 5_000,
        }
        pre_type_settle_normalized = True
    elif not pre_type_change_wait and len(normalized) < max_actions:
        normalized.insert(
            run_index + 1,
            {"type": "wait_for_change", "timeout_ms": 5_000},
        )
        text_index += 1
        pre_type_settle_normalized = True
    pre_type_stable = any(
        action.get("type") == "wait_for_stable_screen"
        for action in normalized[run_index + 1 : text_index]
    )
    if not pre_type_stable and len(normalized) < max_actions:
        normalized.insert(
            text_index,
            {
                "type": "wait_for_stable_screen",
                "stable_ms": 300,
                "timeout_ms": 3_000,
            },
        )
        text_index += 1
        pre_type_settle_normalized = True

    text = next(
        (
            str(action.get("text") or "")
            for action in normalized
            if action.get("type") == "type_text"
        ),
        "",
    )
    is_settings_launch = text.casefold().startswith("ms-settings:")

    submit_index = next(
        (
            index
            for index, action in enumerate(normalized)
            if action.get("type") == "key"
            and [
                str(key).casefold()
                for key in (action.get("keys") or [])
            ]
            in (["enter"], ["return"])
        ),
        -1,
    )
    if submit_index < 0:
        return normalized, 0, pre_type_settle_normalized

    added = 0
    post_submit_change_indices = [
        index
        for index, action in enumerate(normalized)
        if index > submit_index
        and action.get("type") == "wait_for_change"
    ]
    if not post_submit_change_indices and len(normalized) < max_actions:
        insertion_index = next(
            (
                index
                for index, action in enumerate(normalized)
                if index > submit_index
                and action.get("type") == "wait_for_stable_screen"
            ),
            submit_index + 1,
        )
        normalized.insert(
            insertion_index,
            {"type": "wait_for_change", "timeout_ms": 10_000},
        )
        post_submit_change_indices = [insertion_index]
        added += 1

    if not is_settings_launch:
        long_post_submit_change_wait = any(
            int(normalized[index].get("timeout_ms") or 0) >= 20_000
            for index in post_submit_change_indices
        )
        while (
            len(post_submit_change_indices) < 2
            and not long_post_submit_change_wait
            and len(normalized) < max_actions
        ):
            insertion_index = (
                post_submit_change_indices[-1] + 1
                if post_submit_change_indices
                else submit_index + 1
            )
            normalized.insert(
                insertion_index,
                {"type": "wait_for_change", "timeout_ms": 10_000},
            )
            post_submit_change_indices.append(insertion_index)
            added += 1
        post_submit_stable = any(
            action.get("type") == "wait_for_stable_screen"
            for action in normalized[submit_index + 1 :]
        )
        if not post_submit_stable and len(normalized) < max_actions:
            insertion_index = (
                post_submit_change_indices[-1] + 1
                if post_submit_change_indices
                else submit_index + 1
            )
            normalized.insert(
                insertion_index,
                {
                    "type": "wait_for_stable_screen",
                    "stable_ms": 500,
                    "timeout_ms": 3_000,
                },
            )
            added += 1
        return (
            _add_windows_run_focus_preflight(
                normalized,
                max_actions=max_actions,
            ),
            added,
            pre_type_settle_normalized,
        )

    if post_submit_change_indices:
        first_change_index = post_submit_change_indices[0]
        extra_change_indices = post_submit_change_indices[1:]
        if extra_change_indices:
            # The Settings splash animates enough to satisfy another raw
            # change wait while the requested page is still unavailable. Hold
            # a bounded render window after the first real launch transition.
            normalized[extra_change_indices[0]] = {
                "type": "wait",
                "ms": 5_000,
            }
        elif len(normalized) < max_actions:
            normalized.insert(
                first_change_index + 1,
                {"type": "wait", "ms": 5_000},
            )
            added += 1
    return (
        _add_windows_run_focus_preflight(
            normalized,
            max_actions=max_actions,
        ),
        added,
        pre_type_settle_normalized,
    )


def _add_windows_run_focus_preflight(
    actions: list[dict[str, Any]],
    *,
    max_actions: int,
) -> list[dict[str, Any]]:
    """Warm remote focus before Win+R without broadening the launch grammar."""

    active_actions = [
        action
        for action in actions
        if action.get("type")
        not in {"wait", "wait_for_change", "wait_for_stable_screen"}
    ]
    if (
        active_actions
        and is_windows_run_focus_preflight(active_actions[0])
    ):
        return actions
    if len(actions) + 3 > max_actions:
        return actions
    return [
        # The showcase desktop can satisfy its initial ready gate before a
        # delayed startup notification paints. Let that harmless late surface
        # settle, then dismiss it before asking Windows to focus Run. This is
        # far cheaper than emitting exact text into a moving post-login frame
        # and paying for a full recovery turn.
        {"type": "wait", "ms": 2_000},
        {"type": "key", "keys": ["Escape"]},
        {"type": "wait", "ms": 250},
        *actions,
    ]


_KEY_MODIFIER_CODES = {
    "ControlLeft",
    "ControlRight",
    "ShiftLeft",
    "ShiftRight",
    "AltLeft",
    "AltRight",
    "MetaLeft",
    "MetaRight",
}


def _normalize_sequential_key_actions(
    actions: list[dict[str, Any]],
    *,
    max_actions: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Expand modifier-free key lists into bounded sequential presses.

    Controller schemas use a list for chords, but models naturally express
    short calculator and navigation sequences as one list too. Pressing those
    keys simultaneously is never the intended input. Canonicalise and split
    only lists without a modifier; preserve real shortcuts verbatim.
    """

    normalized: list[dict[str, Any]] = []
    split_actions = 0
    for action in actions:
        copied = dict(action)
        if copied.get("type") != "key":
            normalized.append(copied)
            continue
        keys = copied.get("keys") or (
            [copied["key"]] if copied.get("key") else []
        )
        canonical = normalize_keys(keys)
        if (
            len(canonical) <= 1
            or any(key in _KEY_MODIFIER_CODES for key in canonical)
        ):
            normalized.append(copied)
            continue
        split_actions += len(canonical) - 1
        for key in canonical:
            normalized.append({"type": "key", "keys": [key]})

    if len(normalized) > max_actions:
        return [dict(action) for action in actions], 0, True
    return normalized, split_actions, False


_CALCULATOR_TASK_EXPRESSION = re.compile(
    r"\b(?P<left>\d{1,6})\s*"
    r"(?P<operator>multiplied\s+by|times|\*)\s*"
    r"(?P<right>\d{1,6})\b",
    re.IGNORECASE,
)
_CALCULATOR_MIXED_EXPRESSION = re.compile(
    r"\b(?P<dividend>\d{1,6})\s+divided\s+by\s+"
    r"(?P<divisor>\d{1,6}),?\s+then\s+add\s+"
    r"(?P<addend>\d{1,6})\b",
    re.IGNORECASE,
)
_CALCULATOR_SQUARE_ROOT_EXPRESSION = re.compile(
    r"\bsquare\s+root\s+of\s+(?P<value>\d{1,12})\b",
    re.IGNORECASE,
)
_CALCULATOR_PERCENTAGE_EXPRESSION = re.compile(
    r"\bcalculate\s+(?P<percent>\d{1,6}(?:\.\d{1,6})?)\s+"
    r"percent\s+of\s+(?P<value>\d{1,12}(?:\.\d{1,6})?)\b",
    re.IGNORECASE,
)
_CALCULATOR_POWER_EXPRESSION = re.compile(
    r"\bcompute\s+(?P<base>\d{1,6})\s+to\s+the\s+power\s+of\s+"
    r"(?P<exponent>[1-8])\b",
    re.IGNORECASE,
)
_CALCULATOR_SUBTRACTION_EXPRESSION = re.compile(
    r"\bcompute\s+(?P<left>\d{1,12}(?:\.\d{1,6})?)\s+minus\s+"
    r"(?P<right>\d{1,12}(?:\.\d{1,6})?)\b",
    re.IGNORECASE,
)
_CALCULATOR_CHAIN_EXPRESSION = re.compile(
    r"\bcompute\s+(?P<left>\d{1,6})\s+plus\s+(?P<right>\d{1,6}),?\s+"
    r"multiply\s+that\s+result\s+by\s+(?P<factor>\d{1,6}),?\s+"
    r"then\s+divide\s+by\s+(?P<divisor>\d{1,6})\b",
    re.IGNORECASE,
)
_CALCULATOR_RECIPROCAL_EXPRESSION = re.compile(
    r"\breciprocal\s+of\s+(?P<value>\d{1,12})\b",
    re.IGNORECASE,
)
_CALCULATOR_CONVERTER_TASK = re.compile(
    r"\bWindows\s+Calculator(?:'s)?\s+"
    r"(?:unit\s+conversion|temperature\s+converter)\s+to\s+convert\b",
    re.IGNORECASE,
)
_NOTEPAD_EXACT_TEXT_TASK = re.compile(
    r"\bIn\s+Notepad,\s*type\s+exactly\s+`(?P<text>[^`\r\n]{1,240})`",
    re.IGNORECASE,
)
_NOTEPAD_TWO_PARAGRAPH_TASK = re.compile(
    r"\bIn\s+Notepad,.*?\bwith\s+exactly\s+two\s+paragraphs\.\s*"
    r"First\s+paragraph:\s*`(?P<first>[^`\r\n]{1,240})`\s*"
    r"Second\s+paragraph:\s*`(?P<second>[^`\r\n]{1,240})`\s*"
    r"Put\s+one\s+blank\s+line\s+between\s+them\b",
    re.IGNORECASE,
)
_NOTEPAD_EXACT_LINES_TASK = re.compile(
    r"\bIn\s+Notepad,.*?\bwith\s+these\s+exact\s+lines:\s*"
    r"(?P<body>`[^`\r\n]{1,240}`"
    r"(?:\s+then\s+`[^`\r\n]{1,240}`){1,19})",
    re.IGNORECASE,
)


def _calculator_number_keys(value: str) -> list[str]:
    return [
        (
            "NumpadDecimal"
            if character == "."
            else f"Digit{character}"
        )
        for character in value
    ]


def _calculator_key_actions(
    keys: list[str | list[str]],
) -> list[dict[str, Any]]:
    return [
        {
            "type": "key",
            "keys": key if isinstance(key, list) else [key],
        }
        for key in keys
    ]


def _calculator_decimal_text(value: Decimal) -> str:
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result


def _launched_calculator(action: PendingAction | None) -> bool:
    if action is None:
        return False
    return any(
        item.get("type") == "type_text"
        and str(item.get("text") or "").strip().casefold()
        in {"calc", "calc.exe", "calculator"}
        for item in action.actions
    )


def _calculator_task_controller(
    run: RunSnapshot,
    action: PendingAction | None,
    *,
    max_actions: int,
) -> ControllerDecision | None:
    """Prepare a deterministic literal expression after Calculator opens."""

    if not _launched_calculator(action):
        return None

    multiplication = _CALCULATOR_TASK_EXPRESSION.search(run.task)
    mixed = _CALCULATOR_MIXED_EXPRESSION.search(run.task)
    square_root = _CALCULATOR_SQUARE_ROOT_EXPRESSION.search(run.task)
    percentage = _CALCULATOR_PERCENTAGE_EXPRESSION.search(run.task)
    power = _CALCULATOR_POWER_EXPRESSION.search(run.task)
    subtraction = _CALCULATOR_SUBTRACTION_EXPRESSION.search(run.task)
    chain = _CALCULATOR_CHAIN_EXPRESSION.search(run.task)
    reciprocal = _CALCULATOR_RECIPROCAL_EXPRESSION.search(run.task)
    expects_task_completion = True
    expected_evidence: list[str] | None = None
    if multiplication is not None:
        left = multiplication.group("left")
        right = multiplication.group("right")
        keys: list[str | list[str]] = [
            *_calculator_number_keys(left),
            "NumpadMultiply",
            *_calculator_number_keys(right),
            "Enter",
        ]
        intent = (
            "Evaluate the requested local Calculator expression "
            f"{left} × {right}."
        )
        result = str(int(left) * int(right))
    elif mixed is not None:
        dividend = mixed.group("dividend")
        divisor = mixed.group("divisor")
        addend = mixed.group("addend")
        if int(divisor) == 0 or int(dividend) % int(divisor):
            return None
        keys = [
            *_calculator_number_keys(dividend),
            "NumpadDivide",
            *_calculator_number_keys(divisor),
            "NumpadAdd",
            *_calculator_number_keys(addend),
            "Enter",
        ]
        intent = (
            "Evaluate the requested local Calculator expression "
            f"{dividend} ÷ {divisor} + {addend}."
        )
        result = str(int(dividend) // int(divisor) + int(addend))
    elif square_root is not None:
        value = square_root.group("value")
        result_value = isqrt(int(value))
        if result_value * result_value != int(value):
            return None
        intent = (
            "Evaluate the requested local Calculator expression "
            f"√{value}."
        )
        result = str(result_value)
        keys = _calculator_number_keys(value)
        expects_task_completion = False
        expected_evidence = [
            f"Calculator's main display visibly reads exactly "
            f"{int(value):,} and the square-root control is visible."
        ]
    elif percentage is not None:
        percent = percentage.group("percent")
        value = percentage.group("value")
        keys = [
            *_calculator_number_keys(value),
            "NumpadMultiply",
            *_calculator_number_keys(percent),
            "NumpadDivide",
            "Digit1",
            "Digit0",
            "Digit0",
            "Enter",
        ]
        result_value = Decimal(value) * Decimal(percent) / Decimal(100)
        result = _calculator_decimal_text(result_value)
        intent = (
            "Evaluate the requested local Calculator percentage "
            f"{percent}% of {value}."
        )
    elif power is not None:
        base = power.group("base")
        exponent = int(power.group("exponent"))
        factor_keys = _calculator_number_keys(base)
        keys = []
        for factor_index in range(exponent):
            if factor_index:
                keys.append("NumpadMultiply")
            keys.extend(factor_keys)
        keys.append("Enter")
        result = str(int(base) ** exponent)
        intent = (
            "Evaluate the requested local Calculator expression "
            f"{base}^{exponent}."
        )
    elif subtraction is not None:
        left = subtraction.group("left")
        right = subtraction.group("right")
        keys = [
            *_calculator_number_keys(left),
            "NumpadSubtract",
            *_calculator_number_keys(right),
            "Enter",
        ]
        result = _calculator_decimal_text(Decimal(left) - Decimal(right))
        intent = (
            "Evaluate the requested local Calculator expression "
            f"{left} − {right}."
        )
    elif chain is not None:
        left = chain.group("left")
        right = chain.group("right")
        factor = chain.group("factor")
        divisor = chain.group("divisor")
        if int(divisor) == 0:
            return None
        keys = [
            *_calculator_number_keys(left),
            "NumpadAdd",
            *_calculator_number_keys(right),
            "NumpadMultiply",
            *_calculator_number_keys(factor),
            "NumpadDivide",
            *_calculator_number_keys(divisor),
            "Enter",
        ]
        result_value = (
            (Decimal(left) + Decimal(right))
            * Decimal(factor)
            / Decimal(divisor)
        )
        result = _calculator_decimal_text(result_value)
        intent = (
            "Evaluate the requested local Calculator expression "
            f"({left} + {right}) × {factor} ÷ {divisor}."
        )
    elif reciprocal is not None:
        value = reciprocal.group("value")
        if int(value) == 0:
            return None
        keys = _calculator_number_keys(value)
        expects_task_completion = False
        expected_evidence = [
            f"Calculator's main display visibly reads exactly {int(value):,} "
            "and the reciprocal control is visible."
        ]
        intent = (
            "Prepare the requested local Calculator reciprocal "
            f"1/{value} for a grounded click on the visible reciprocal control."
        )
    else:
        return None

    active_actions = _calculator_key_actions(keys)
    actions = [
        *active_actions,
        {"type": "wait_for_change", "timeout_ms": 2_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 400,
            "timeout_ms": 3_000,
        },
    ]
    if len(actions) > max_actions:
        return None
    if expected_evidence is None:
        expected_evidence = [
            f"Calculator's main display visibly reads exactly {result}."
        ]
    return ControllerDecision(
        outcome="act",
        intent=intent,
        actions=actions,
        expected_evidence=expected_evidence,
        expects_task_completion=expects_task_completion,
    )


def _verification_confirms_standard_calculator(
    run: RunSnapshot,
) -> bool:
    """Require model evidence of Standard mode before arithmetic fast-path HID."""

    verification = run.last_verification
    if verification is None or verification.verdict != "verified":
        return False
    text = json.dumps(
        verification.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
    ).casefold()
    return "calculator" in text and "standard" in text


def _calculator_fast_path(
    run: RunSnapshot,
    *,
    max_actions: int,
) -> tuple[PlanDecision, ControllerDecision] | None:
    """Prepare a known local Calculator task without two model round trips."""

    launch_probe = PendingAction(
        index=0,
        intent="Launch Windows Calculator.",
        actions=[{"type": "type_text", "text": "calc"}],
        based_on_world_version=(
            run.observation.world_version
            if run.observation is not None
            else None
        ),
        based_on_control_epoch=(
            run.observation.control_epoch
            if run.observation is not None
            else None
        ),
        idempotency_key=f"{run.run_id}:calculator-fast-path-probe",
    )
    calculator_followup = _calculator_task_controller(
        run,
        launch_probe,
        max_actions=max_actions,
    )
    converter_task = _CALCULATOR_CONVERTER_TASK.search(run.task) is not None
    if calculator_followup is None and not converter_task:
        return None
    plan = PlanDecision(
        summary=(
            "Open Windows Calculator and perform the requested conversion."
            if converter_task
            else "Open Windows Calculator and evaluate the literal expression."
        ),
        steps=[
            "Open Windows Calculator using the bounded local app launcher.",
            (
                "Open the unit converter with its keyboard command, then use "
                "the visible controls to select the requested units."
                if converter_task
                else (
                    "Enter the parsed expression with inspectable HID key "
                    "events."
                )
            ),
            "Verify the exact visible result before reporting it.",
        ],
        success_criteria=[
            (
                "Windows Calculator visibly displays the exact result "
                "requested by the task."
            )
        ],
        constraints=[
            "Use only the local Windows Calculator application.",
            "Do not interact with any communication or external application.",
        ],
    )
    controller = ControllerDecision(
        outcome="act",
        intent="Launch Windows Calculator.",
        actions=[
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
        expected_evidence=[
            "Windows Calculator is visibly open."
        ],
        expects_task_completion=False,
    )
    return plan, controller


def _notepad_exact_text_payload(run: RunSnapshot) -> str | None:
    match = _NOTEPAD_EXACT_TEXT_TASK.search(run.task)
    return match.group("text") if match is not None else None


def _task_targets_notepad(task: str) -> bool:
    """Require an explicit positive Notepad target for deterministic input."""

    return bool(
        re.search(
            r"\b(?:in|using|with|open)\s+(?:windows\s+)?notepad\b",
            " ".join(task.casefold().split()),
        )
    )


def _chunk_notepad_artifact_line(
    line: str,
    *,
    kind: str,
) -> tuple[str, ...]:
    """Split one visible line without changing its reconstructed bytes."""

    chunks: list[str] = []
    remaining = line
    while len(remaining) > 240:
        split_at = 240
        if kind == "prose":
            word_boundary = remaining.rfind(" ", 1, 241)
            if word_boundary > 0:
                split_at = word_boundary
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


def _notepad_exact_text_parts(
    run: RunSnapshot,
) -> tuple[tuple[str, int], ...]:
    """Return visible text chunks and exact line breaks following each one."""

    payload = _notepad_exact_text_payload(run)
    if payload is not None:
        return ((payload, 0),)
    match = _NOTEPAD_TWO_PARAGRAPH_TASK.search(run.task)
    if match is not None:
        return ((match.group("first"), 2), (match.group("second"), 0))
    line_match = _NOTEPAD_EXACT_LINES_TASK.search(run.task)
    if line_match is not None:
        lines = tuple(
            re.findall(r"`([^`\r\n]{1,240})`", line_match.group("body"))
        )
        return tuple(
            (line, 1 if index < len(lines) - 1 else 0)
            for index, line in enumerate(lines)
        )
    if (
        run.plan is None
        or run.plan.artifact_content is None
        or not _task_targets_notepad(run.task)
    ):
        return ()
    kind = run.plan.artifact_content_kind or "prose"
    parts: list[tuple[str, int]] = []
    for line_match in re.finditer(r"([^\n]+)(\n*)", run.plan.artifact_content):
        chunks = _chunk_notepad_artifact_line(
            line_match.group(1),
            kind=kind,
        )
        for chunk_index, chunk in enumerate(chunks):
            parts.append(
                (
                    chunk,
                    (
                        len(line_match.group(2))
                        if chunk_index == len(chunks) - 1
                        else 0
                    ),
                )
            )
    return tuple(parts)


def _notepad_exact_text_segments(run: RunSnapshot) -> tuple[str, ...]:
    return tuple(text for text, _ in _notepad_exact_text_parts(run))


def _requires_fresh_notepad_document(run: RunSnapshot) -> bool:
    """Recognize the campaign contract that forbids restored Notepad tabs."""

    task = " ".join(run.task.casefold().split())
    return (
        "notepad" in task
        and "create a new blank document" in task
        and "do not treat restored or pre-existing document content" in task
    )


def _notepad_segment_break_count(run: RunSnapshot, index: int) -> int:
    parts = _notepad_exact_text_parts(run)
    return parts[index][1] if 0 <= index < len(parts) else 0


def _launched_notepad(action: PendingAction | None) -> bool:
    if action is None:
        return False
    return is_verified_windows_run_launch(action.actions)


def _created_new_notepad_document(
    action: PendingAction | None,
) -> bool:
    if action is None:
        return False
    return any(
        item.get("type") == "key"
        and {
            str(key).strip().casefold()
            for key in item.get("keys") or []
        }
        in (
            {"controlleft", "keyn"},
            {"ctrl", "n"},
        )
        for item in action.actions
    )


def _notepad_new_document_controller(
    run: RunSnapshot,
    action: PendingAction | None,
    *,
    max_actions: int,
) -> ControllerDecision | None:
    """Create a fresh focused tab after Notepad restores old session state."""

    if (
        not (
            _notepad_exact_text_segments(run)
            or _requires_fresh_notepad_document(run)
        )
        or not _launched_notepad(action)
    ):
        return None
    actions = [
        {"type": "key", "keys": ["ControlLeft", "KeyN"]},
        {"type": "wait_for_change", "timeout_ms": 3_000},
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 400,
            "timeout_ms": 3_000,
        },
    ]
    if len(actions) > max_actions:
        return None
    return ControllerDecision(
        outcome="act",
        intent="Create a fresh blank Notepad document.",
        actions=actions,
        expected_evidence=[
            "Notepad visibly shows a fresh blank editable document."
        ],
        expects_task_completion=False,
    )


def _notepad_exact_text_controller(
    run: RunSnapshot,
    action: PendingAction | None,
    *,
    max_actions: int,
) -> ControllerDecision | None:
    """Advance one exact Notepad text segment after verified local input."""

    segments = _notepad_exact_text_segments(run)
    if not segments:
        return None
    payload: str | None = None
    payload_index: int | None = None
    if _created_new_notepad_document(action):
        payload_index = 0
        payload = segments[0]
    else:
        typed_index = _typed_notepad_segment_index(action, segments)
        break_count = (
            _notepad_segment_break_count(run, typed_index)
            if typed_index is not None
            else 0
        )
        if typed_index is not None and break_count > 0:
            actions = [
                {"type": "key", "keys": ["SHIFT", "ENTER"]}
                for _ in range(break_count)
            ]
            actions.append(
                {
                    "type": "wait_for_stable_screen",
                    "stable_ms": 400,
                    "timeout_ms": 3_000,
                }
            )
            if len(actions) > max_actions:
                return None
            separator = (
                "single blank line"
                if break_count == 2
                else "line break"
            )
            return ControllerDecision(
                outcome="act",
                intent=(
                    f"Insert the requested {separator} after exact segment "
                    f"{typed_index + 1} of {len(segments)} in Notepad."
                ),
                actions=actions,
                expected_evidence=[
                    (
                        "Notepad visibly keeps the exact preceding segment "
                        f"and places the caret after the requested {separator}."
                    )
                ],
                expects_task_completion=False,
            )
        if typed_index is not None and typed_index < len(segments) - 1:
            payload_index = typed_index + 1
            payload = segments[payload_index]
        break_match = re.fullmatch(
            r"Insert the requested (?:single blank line|line break) after "
            r"exact segment (?P<index>\d+) of (?P<count>\d+) in Notepad\.",
            action.intent if action is not None else "",
        )
        if break_match is not None:
            prior_index = int(break_match.group("index")) - 1
            expected_breaks = _notepad_segment_break_count(run, prior_index)
            if (
                int(break_match.group("count")) == len(segments)
                and 0 <= prior_index < len(segments) - 1
                and _inserted_notepad_line_breaks(
                    action,
                    expected_breaks,
                )
            ):
                payload_index = prior_index + 1
                payload = segments[payload_index]
    if payload is None:
        return None
    assert payload_index is not None
    actions = [
        {
            "type": "type_text",
            "text": payload,
            "code": (
                run.plan is not None
                and run.plan.artifact_content_kind == "code"
            ),
            "context": "editor",
            "verification": "exact",
        },
        {
            "type": "wait_for_stable_screen",
            "stable_ms": 400,
            "timeout_ms": 3_000,
        },
    ]
    if len(actions) > max_actions:
        return None
    return ControllerDecision(
        outcome="act",
        intent=(
            "Enter the requested exact text in the fresh Notepad document."
            if len(segments) == 1
            else (
                f"Enter exact segment {payload_index + 1} of {len(segments)} "
                "in the fresh Notepad document."
            )
        ),
        actions=actions,
        expected_evidence=[
            f"Notepad visibly contains exactly `{payload}`."
        ],
        expects_task_completion=False,
    )


def _typed_notepad_segment_index(
    action: PendingAction | None,
    segments: tuple[str, ...],
) -> int | None:
    """Recover durable segment progress from the verified action intent."""

    if action is None:
        return None
    indexed = re.fullmatch(
        r"Enter exact segment (?P<index>\d+) of (?P<count>\d+) "
        r"in the fresh Notepad document\.",
        action.intent,
    )
    if indexed is not None and int(indexed.group("count")) == len(segments):
        index = int(indexed.group("index")) - 1
        if 0 <= index < len(segments) and _typed_exact_editor_text(
            action,
            segments[index],
        ):
            return index
        return None
    if len(segments) == 1 and _typed_exact_editor_text(action, segments[0]):
        return 0
    return next(
        (
            index
            for index, segment in enumerate(segments[:-1])
            if _typed_exact_editor_text(action, segment)
        ),
        None,
    )


def _typed_exact_editor_text(
    action: PendingAction | None,
    payload: str,
) -> bool:
    if action is None:
        return False
    return any(
        item.get("type") == "type_text"
        and str(item.get("text") or "") == payload
        and str(item.get("context") or "") == "editor"
        and str(item.get("verification") or "") == "exact"
        for item in action.actions
    )


def _inserted_notepad_line_breaks(
    action: PendingAction | None,
    count: int,
) -> bool:
    if action is None:
        return False
    line_breaks = [
        {
            str(key).strip().casefold()
            for key in item.get("keys") or []
        }
        for item in action.actions
        if item.get("type") == "key"
    ]
    return line_breaks.count({"shift", "enter"}) == count


def _durable_last_verified_action(run: RunSnapshot) -> PendingAction | None:
    """Recover the latest verified action across call and process boundaries."""

    current: dict[str, Any] | None = None
    completed = False
    latest: PendingAction | None = None
    for event in run.events:
        if event.kind == "action.checkpointed":
            data = event.data
            # A newer checkpoint supersedes every older verified action. If
            # execution stops before this action is independently verified,
            # returning the older action would resume from stale screen state.
            latest = None
            if (
                isinstance(data.get("index"), int)
                and isinstance(data.get("intent"), str)
                and isinstance(data.get("actions"), list)
            ):
                current = data
                completed = False
            else:
                current = None
                completed = False
            continue
        if event.kind == "action.completed" and current is not None:
            completed = event.data.get("index") == current.get("index")
            continue
        if event.kind in {
            "action.completed_unverified",
            "action.failed",
            "action.recoverable_failure",
        }:
            current = None
            completed = False
            latest = None
            continue
        model_verified = (
            event.kind == "model.completed"
            and event.data.get("role") == "verifier"
            and event.data.get("verdict") in {"verified", "complete"}
        )
        local_verified = (
            event.kind == "verification.local_completed"
            and event.data.get("verdict") == "verified"
        )
        if not completed or current is None or not (model_verified or local_verified):
            continue
        latest = PendingAction(
            index=int(current["index"]),
            intent=str(current["intent"]),
            actions=[
                dict(item)
                for item in current["actions"]
                if isinstance(item, dict)
            ],
            expected_evidence=[
                str(item)
                for item in current.get("expected_evidence") or []
            ],
            expects_task_completion=bool(
                current.get("expects_task_completion", False)
            ),
            based_on_world_version=None,
            based_on_control_epoch=None,
            idempotency_key=str(current.get("idempotency_key") or "recovered"),
        )
        completed = False
    return latest


def _locally_verified_notepad_artifact_action(
    run: RunSnapshot,
    action: PendingAction | None,
    input_receipts: list[dict[str, Any]],
    *,
    after: ComputerObservation | None,
) -> VerificationDecision | None:
    """Trust exact local OCR once for one inert generated-artifact segment."""

    if (
        action is None
        or run.plan is None
        or run.plan.artifact_content is None
        or after is None
    ):
        return None
    segments = _notepad_exact_text_segments(run)
    typed_index = _typed_notepad_segment_index(action, segments)
    if typed_index is None or action.expects_task_completion:
        return None
    typed_actions = [
        (index, item)
        for index, item in enumerate(action.actions)
        if item.get("type") == "type_text"
    ]
    if len(typed_actions) != 1 or any(
        item.get("type")
        not in {
            "type_text",
            "wait",
            "wait_for_change",
            "wait_for_stable_screen",
        }
        for item in action.actions
    ):
        return None
    action_index, _ = typed_actions[0]
    receipt = next(
        (
            item
            for item in input_receipts
            if item.get("index") == action_index
        ),
        None,
    )
    if receipt is None or not (
        receipt.get("status") == "verified_exact"
        and receipt.get("verdict") == "match"
        and receipt.get("focus_evidence") == "read_back_verified"
        and receipt.get("proof_state") == "exact_visual_readback"
        and receipt.get("exact_readback_sha256_match") is True
        and receipt.get("emitted_exactly_once") is True
        and receipt.get("correction_count") == 0
        and receipt.get("delivery_retries") == 0
    ):
        return None
    summary = (
        f"Exact local visual readback verified artifact segment "
        f"{typed_index + 1} of {len(segments)}."
    )
    evidence = [
        (
            "The watched sender emitted the segment exactly once and the "
            "independent local OCR readback matched its exact hash."
        )
    ]
    return VerificationDecision(
        verdict="verified",
        summary=summary,
        evidence=evidence,
        criteria=[],
        action_criteria=[
            {
                "criterion_index": index,
                "satisfied": True,
                "evidence": evidence[0],
            }
            for index in range(len(action.expected_evidence))
        ],
    )


def _notepad_fast_path(
    run: RunSnapshot,
    *,
    max_actions: int,
) -> tuple[PlanDecision, ControllerDecision] | None:
    """Prepare a literal exact-text Notepad task without model planning."""

    segments = _notepad_exact_text_segments(run)
    if not segments or max_actions < 7:
        return None
    paragraph_task = (
        _NOTEPAD_TWO_PARAGRAPH_TASK.search(run.task) is not None
    )
    exact_lines_task = (
        _NOTEPAD_EXACT_LINES_TASK.search(run.task) is not None
    )
    plan = PlanDecision(
        summary="Create and verify the requested exact text file in Notepad.",
        steps=[
            "Open Windows Notepad using the bounded local app launcher.",
            "Create a fresh blank document instead of reusing restored tabs.",
            "Enter and independently verify the exact requested text.",
            "Save inside the permitted lab workspace, reopen, and verify it.",
        ],
        success_criteria=[
            "The requested file exists inside the permitted lab workspace.",
            (
                "The reopened file visibly contains exactly the requested two "
                "paragraphs with one blank line between them."
                if paragraph_task
                else (
                    "The reopened file visibly contains exactly the requested "
                    f"{len(segments)} lines."
                    if exact_lines_task
                    else (
                        "The reopened file visibly contains exactly "
                        f"`{segments[0]}`."
                    )
                )
            ),
        ],
        constraints=[
            (
                "Keep every file mutation inside "
                "C:\\PiKVM-Harness\\workspace\\codex-50."
            ),
            "Do not interact with communications or external applications.",
        ],
    )
    controller = ControllerDecision(
        outcome="act",
        intent="Launch Windows Notepad.",
        actions=[
            {"type": "key", "keys": ["WIN", "R"]},
            {"type": "wait", "ms": 300},
            {
                "type": "type_text",
                "text": "notepad",
                "context": "field",
                "verification": "exact",
            },
            {"type": "key", "keys": ["ENTER"]},
            # Let the Run dialog finish closing before establishing the
            # baseline. Windows 11 Notepad can take 20+ seconds to cold-start
            # on the lab VM; waiting on pixels avoids an early verifier/model
            # round trip while still returning as soon as the app surfaces.
            {"type": "wait", "ms": 1_000},
            {"type": "wait_for_change", "timeout_ms": 30_000},
            {
                "type": "wait_for_stable_screen",
                "stable_ms": 500,
                "timeout_ms": 5_000,
            },
        ],
        expected_evidence=["Windows Notepad is visibly open."],
        expects_task_completion=False,
    )
    return plan, controller


_CONTROLLER_SYSTEM = """\
You are the fast controller for a physical computer. Choose one bounded logical
burst against the supplied frame and checkpointed plan. A bounded burst is a
complete reversible local operation, not one mouse click or one digit. Once the
target and focus are verified, group the full sequence of reversible local
inputs needed to reach the stable end state, up to the action limit, in one
decision. Do not spend one controller/verifier round trip on each digit, key,
calculator button, or harmless local navigation step when the grouped result
can be directly verified. Use only supported HID actions. Keep text short and
inspectable. Do not submit/send/delete/purchase/
install/change permissions in the same burst that prepares the action. Never
claim success; the independent verifier decides. If evidence is unclear, ask
for replan instead of guessing or repeating input. Do not wait for human
approval and do not claim that approval is missing: propose the bounded action,
then let the independent daemon policy create an exact approval request if the
action is consequential. Never infer keyboard focus from appearance, a window
being foreground, or a visible caret alone. If focus is not established by the
last verified action, first propose a separate bounded focus action and verify
it before typing. Once the verifier has established focus, proceed with the
requested bounded input. Local unsaved typing is not a commit; do not emit
pointer-only no-ops, repeated moves, or waits merely to preserve focus. After
any focus-lost or type-unverified result, do not repeat
the text; re-observe and establish focus first. After a verifier failure, do
not repeat the original action unchanged. Use the verifier evidence to propose
only a bounded correction, or block if the current pixels cannot support one.
Set expects_task_completion true only when this action should satisfy every
remaining success criterion. This is a scheduling hint, never a success claim;
the independent verifier still decides. Producing a user-facing report from
the resulting visible evidence is the verifier's job, not a remaining computer
step. When one read-only action should expose everything needed to answer the
user, set expects_task_completion true so the harness does not speculatively
request another controller decision.
There is one narrow app-launch exception to the separate focus-action rule.
When the current frame visibly proves a surfaced Windows desktop and the task
requires a standard local app, launch it in one bounded burst: Win+R, a short
settle, wait for a stable screen, type only the app's executable name with no
arguments using context ``field`` and verification ``exact``, press Enter, wait
for the screen to change, then wait for a stable screen. Keep Win+R, the exact text, and Enter in this same
burst; never issue Win+R as a standalone action, and do not reuse a field
focused by an earlier burst. Do not use this
exception for a shell, terminal, URL, file path, command arguments, or any
consequential operation. Verify the launched app as the burst's stable end
state.
When modern Notepad restores an old tab after launch and the task requires a
new document, use Ctrl+N to create a new blank document as the next bounded
action. Do not click into or overwrite restored content, and do not treat a
restored tab as the requested new document.
When plan.artifact_content is present, it is the complete immutable source of
truth for generated editor content. Do not rewrite, repair, extend, or improvise
it from the screenshot. The deterministic editor path enters its indexed exact
segments; after that, continue only with saving, reopening, and verification.
For multi-line content in a verified local editor, including generated prose,
never put newline control characters inside type_text. Enter each text segment
with a separate exact type_text action and verify it. Create every editor line
break with Shift+Enter in a later bounded action. Never propose bare Enter for
an editor line break: it is intentionally treated as a possible commit outside
the editor. Create one required blank line with two separate Shift+Enter key
actions and verify that non-submitting blank-line action before entering the
next exact text segment. Never send indentation as a whitespace-only editor
type_text action because pixels cannot prove invisible text. When spaces are
load-bearing, include the indentation and visible line content in one exact
segment. In plain-text code editors, every code segment must carry its full
space-based indentation and must begin from the verified new line; never use
Tab to create code indentation. Never treat Home as an absolute column-one
command or repair indentation with Home plus Shift+End because modern Notepad
Home stops at the first non-whitespace character. If exact code entry is
contradicted, request a clean-document replan instead of accumulating an
indentation repair. Never put active key actions after type_text in the same
burst.
When the user explicitly requires repeated spaces or other load-bearing
whitespace inside one editor line, set code true for that format-sensitive
text segment so it receives strict formatting delivery and exact readback.
Use this only for the explicitly requested spacing: accidental repeated spaces
in ordinary prose remain blocked before HID delivery.
When generating prose longer than one type_text payload, end every payload at
a natural word boundary within the 240-character limit. Never concatenate two
words or omit their separator to fill the limit. Begin the continuation with
the exact required whitespace so the independently verified segments reconstruct
the intended prose byte-for-byte.
For a task that explicitly asks to inspect a read-only Windows Settings page,
the same bounded launch exception may type one native ``ms-settings:`` URI
instead of an executable. It must begin exactly with ``ms-settings:``, contain
no whitespace or shell metacharacters, and open only the requested local page.
Do not generalise this exception to web URLs, file URIs, commands, or arbitrary
protocol handlers.
In a Windows Save As dialog, navigate to the destination directory separately:
use Ctrl+L, draft the exact directory path, verify it, then commit that local
navigation and enter only the short basename in the File name field. Never type
a full absolute path into the File name field; narrow horizontally scrolling
filename controls cannot provide reliable whole-path visual readback.
After the destination and basename are independently verified, click the
visibly labelled Save button as the bounded commit action. Never use bare Enter
to commit a Save As dialog: an unlabelled Enter cannot be grounded to the
intended local-file control and must remain behind the unknown-action gate.
When the task requires reopening a saved local artifact, immediately after the
verified save use Ctrl+O to open the native Open dialog; do not refocus the
editor first. Ctrl+Shift+S and Save As are not reopen actions. A reopen is
complete only after a later completed Open-dialog action selects the saved file
and the verifier sees the requested content in the reopened artifact.
The File name field is normally pre-populated. After its focus is independently
verified, use Ctrl+A immediately before the exact basename in the same
reversible input burst. Never assume the default selection is still active:
typing without Ctrl+A can append the basename to Notepad's generated title.
In File Explorer, use Ctrl+L as a separate reversible focus action instead of
guessing address-bar coordinates. After that focus is independently verified,
preserve the selection created by Ctrl+L: do not click, refocus, move the
pointer, or send another navigation key before the exact draft. Type the draft
as the very next active input; otherwise repeat Ctrl+L before typing rather than
appending to the current location. Then draft exactly ``This PC`` with context
``field`` and verification ``exact`` to
open the local drive view, never a ``shell:`` URI or another namespace alias.
Enter must remain a later action gated by the exact draft readback.
Prefer a bounded reversible burst that reaches a stable, directly legible local
end state over stopping at a low-contrast or transient intermediate state. For
short local calculations, enter the complete expression including the equals
key in one burst and verify the final displayed result; do not make legibility
of tiny expression-history text a required intermediate checkpoint unless the
user specifically asked for it. Never use this guidance to combine
consequential commit actions with their preparation.
When a required GUI control is visibly absent and the reasoner permits a short,
inspectable local terminal fallback, request a replan instead of blocking. A
model-invented GUI-only or no-terminal constraint is not authenticated operator
guidance and must not turn an available fallback into an impossible task.
Before typing a long exact terminal draft, require both a separate verified
width action (maximize or widen) and a separate verified text-size increase
(zoom in or enlarge the font). Do not combine either legibility action with the
text entry. If recent_verified_actions does not prove both properties after the
terminal was opened, propose one missing non-text legibility action first. A
prior text-size proof expires when exact terminal readback is unverified: after
cancelling that draft, require a new verified text-size increase before
retyping it. Maximizing again does not satisfy this post-failure requirement.
For this preparation, visibly larger terminal glyphs and a clean prompt are the
minimum sufficient evidence; do not require a numeric zoom indicator unless the
user explicitly requested a numeric zoom value. Do not put a numeric zoom
percentage in expected_evidence for this internal preparation unless the
authenticated user request asks for that number.
For a short rectangular table in a spreadsheet application, and only after
the verifier established a verified active spreadsheet cell, use one
spreadsheet_grid action instead of one model turn per cell. It accepts 1 to 8
rows and columns, non-empty cells of at most 80 characters, and at most 240
characters total; it types cells exactly once and navigates with Tab, Enter,
and Home. It is one reviewed local-file action whose saved artifact still
requires independent verification. Never use it in messaging, forms,
terminals, or any application where those navigation keys could submit or send
content.
Treat trajectory_signals as durable evidence. If the same exact search query
already produced visible no results, do not repeat it unless the application
or search scope visibly changed. Try one semantically equivalent visible
control or a different bounded navigation strategy, then replan or block
instead of cycling. If ungrounded_navigation_replans is nonzero, do not repeat
the same coordinate-only click: use a visibly grounded target, a safe keyboard
navigation action, or request a replan. Treat ungrounded_navigation_history as
explicitly refused pointer targets: do not revisit them or another blank/icon-
only target that cannot be independently read. Treat recent_input_delivery as
sender evidence. When sender_finished is true, the local sender finished issuing
the payload, but the guest may still have dropped it. Never blindly replay it.
Re-observe and use a bounded application-level check. Only readback_exact proves
an exact OCR read-back. For an unverified terminal draft, never append guessed
missing characters and do not press Enter; cancel the draft with Ctrl+C in a
separate non-text burst, make the terminal legible, verify a visibly clean
prompt, and only then type the complete command once from that clean state.
Treat recent_verified_actions as durable evidence. Do not repeat a completed
check unless current pixels contradict it."""

_VERIFIER_SYSTEM = """\
You are the independent verifier. Compare the plan, intended action, before
state, and after state. When an action has before/after screenshots, the
attached image is a labelled comparison: the left panel is BEFORE and the
right panel is AFTER. Compare control geometry first (knob side, checkmark,
selection, text, enabled/disabled shape, and position); never assume that a
particular accent colour is required. Return complete only when visible
evidence proves every success criterion. Return one criteria assessment for
every zero-based success-criterion index; complete requires every assessment
to be satisfied by specific visible evidence. For every verdict, return one
action_criteria assessment for every zero-based expected-evidence item on the
intended action. Return verified only when every action assessment is satisfied
by specific visible evidence and more task steps remain.
Writing the requested values in the user-facing summary is not another computer
step. If the current pixels satisfy every task criterion, return complete and
answer the request in that summary. In particular, when
action.expects_task_completion is true and all task and action assessments are
satisfied, return complete rather than verified.
Do not return uncertain merely because the overall task is not complete. Return
uncertain only when the intended action result itself is ambiguous: OCR
ambiguity, unexpected focus, stale frames, missing characters,
transition/hover styling, or an unexplained UI change. Never infer success from
the controller's claim and never call a state-changing toggle failed merely
because its colour has not settled when its geometry visibly changed to the
intended state.
When the task requires reopening a saved artifact, the application remaining
open immediately after Save is not a reopen. Return complete only after a
later, separately verified action visibly opens the saved artifact again.
For an internal terminal legibility action, visibly larger terminal glyphs are
sufficient when the before/after comparison directly proves the increase and a
clean prompt remains visible. Do not require a numeric zoom percentage unless
the user's literal task or authenticated operator guidance requested one; a
model-invented percentage is not a task requirement.

The summary is user-facing chat copy, not a verification log. For complete,
answer the user's request directly in one to three short sentences by default.
For verified, uncertain, or failed, state the current outcome in one concise
sentence. Put frame IDs, control epochs, before/after mechanics, criterion
accounting, pixel comparisons, and exhaustive evidence in evidence and criteria,
never in summary unless the user explicitly asked for those diagnostics. Do not
say "the verifier", "the comparison image", "success criteria", or "the pixels"
in summary. Detailed writing explicitly requested by the user belongs in the
target artifact; summary should still describe the result concisely."""

_OBSERVATION_VERIFIER_SYSTEM = """\
You are a fast screen reader. Answer the user's observational question using
only the attached current screenshot. Do not infer hidden state or propose
computer input. Return complete when the screenshot answers the question and
uncertain when it does not. The summary is the direct user-facing answer in one
or two concise sentences. Provide exactly one criteria assessment at index 0,
and return an empty action_criteria list."""

_OBSERVATION_ONLY_REQUESTS = frozenset(
    {
        "and now",
        "describe the screen",
        "did it work",
        "did that work",
        "how about now",
        "is it done",
        "is that done",
        "what about now",
        "what can you see",
        "what can you see rn",
        "what changed",
        "what do you see",
        "what do you see rn",
        "what is on screen",
        "what is on the screen",
        "whats on screen",
        "whats on the screen",
    }
)

_EXPLICIT_OBSERVATION_PREFIXES = (
    "describe",
    "observation only",
    "read only",
)
_OBSERVATION_TARGET_PATTERN = re.compile(
    r"\b(?:describe|report|show|tell me|what)\b"
    r".{0,120}\b(?:desktop|screen|visible|window|vm)\b"
)
_COMPUTER_INPUT_VERB_PATTERN = (
    r"(?:click|double click|drag|move|open|press|save|scroll|select|"
    r"send|submit|type)"
)
_FOLLOW_UP_INPUT_PATTERN = re.compile(
    r"\b(?:after(?: that| [a-z ]{1,80},)|afterwards?|also|and|"
    r"but(?: also)?|finally|next|then)\s+"
    rf"(?:please\s+)?{_COMPUTER_INPUT_VERB_PATTERN}\b"
)
_SENTENCE_INPUT_PATTERN = re.compile(
    r"(?:^|[.!?;:]\s*)(?:please\s+)?"
    rf"{_COMPUTER_INPUT_VERB_PATTERN}\b"
)
_DIRECT_SCREEN_TARGET_PATTERN = re.compile(
    r"\b(?:desktop|screen|visible|window|vm)\b"
)
_TASK_SECTION_PATTERN = re.compile(
    r"(?:^|\n)task:\s*\n",
    re.IGNORECASE,
)


def is_observation_only_request(request: str) -> bool:
    """Return whether a literal request needs fresh pixels but no HID input."""

    task_sections = list(_TASK_SECTION_PATTERN.finditer(request))
    if task_sections:
        request = request[task_sections[-1].end() :]
    normalized = re.sub(r"[^a-z0-9']+", " ", request.casefold()).strip()
    normalized = normalized.replace("'", "")
    if normalized in _OBSERVATION_ONLY_REQUESTS:
        return True
    explicit_mode = normalized.startswith(_EXPLICIT_OBSERVATION_PREFIXES)
    observation_target = bool(
        _OBSERVATION_TARGET_PATTERN.search(normalized)
    )
    follow_up_input = bool(
        _FOLLOW_UP_INPUT_PATTERN.search(request.casefold())
    )
    sentence_input = bool(
        _SENTENCE_INPUT_PATTERN.search(request.casefold())
    )
    return (
        explicit_mode
        and observation_target
        and not follow_up_input
        and not sentence_input
    )


def is_direct_screen_observation_request(request: str) -> bool:
    """Return whether a fresh-screen request is unambiguous without a model."""

    return (
        is_observation_only_request(request)
        and _DIRECT_SCREEN_TARGET_PATTERN.search(request.casefold())
        is not None
    )


class AgentHarness:
    """Deep module owning planning, action checkpoints, retries and approvals."""

    def __init__(
        self,
        *,
        computer: ComputerDriver,
        models: ModelPool,
        store: RunStore,
        config: HarnessConfig | None = None,
        budget_policy: ModelBudgetPolicy | None = None,
    ) -> None:
        self.computer = computer
        self.models = models
        self.store = store
        self.config = config or HarnessConfig()
        self.budget_policy = budget_policy or ModelBudgetPolicy(
            max_provider_attempts=self.config.max_provider_attempts_per_run,
        )
        # This is deliberately ephemeral. A speculative controller decision
        # never authorizes HID and may be recomputed safely after a restart.
        self._prefetched_controllers: dict[str, ControllerDecision] = {}

    async def start(self, task: str) -> RunSnapshot:
        run = await self.create(task)
        if run.status is RunStatus.FAILED:
            return run
        return await self._advance(run)

    async def create(
        self,
        task: str,
        *,
        caller: dict[str, Any] | None = None,
        model_provider: str | None = None,
        model_route: RunModelRoute | None = None,
    ) -> RunSnapshot:
        """Create/open a run without waiting for a model.

        UI/API callers use this to render the first frame immediately, then call
        ``continue_run`` in a background task while streaming checkpoints.
        """
        task = task.strip()
        if not task:
            raise ValueError("task must not be empty")
        run = RunSnapshot(
            run_id=str(uuid.uuid4()),
            task=task,
            status=RunStatus.PLANNING,
            caller=dict(caller or {}),
            model_provider=model_provider,
            model_route=model_route,
        )
        run.model_budget.provider_attempt_limit = (
            self.budget_policy.max_provider_attempts
        )
        run.model_budget.max_cost_microusd = (
            self.budget_policy.max_cost_microusd
        )
        run.model_budget.pricing_version = self.budget_policy.pricing_version
        run.record(
            "run.created",
            interface=run.caller.get("interface"),
            caller_label=run.caller.get("label"),
            model_provider=run.model_provider,
            model_route=(
                run.model_route.model_dump(mode="json", exclude_none=True)
                if run.model_route is not None
                else None
            ),
        )
        await self.store.save(run)
        try:
            observation = await self.computer.open(task)
        except Exception as exc:  # transport failed before any action
            run.status = RunStatus.FAILED
            run.error = f"computer open failed: {exc}"
            run.record("computer.open_failed", error=str(exc))
            await self.store.save(run)
            return run
        run.session_id = observation.session_id
        run.observation = observation
        run.status = RunStatus.RUNNING
        run.record(
            "computer.opened",
            session_id=observation.session_id,
            frame_id=observation.frame_id,
            world_version=observation.world_version,
        )
        await self.store.save(run)
        return run

    async def activate_computer(
        self,
        run_id: str,
        computer_task: str,
    ) -> RunSnapshot:
        """Acquire the physical computer only after an assistant requests it."""

        computer_task = computer_task.strip()
        if not computer_task:
            raise ValueError("computer task must not be empty")
        run = await self.store.get_control(run_id)
        if run.origin != "managed" or run.mode != "assistant":
            raise ValueError("only an assistant run can activate the computer")
        if run.pending_approval is not None or run.pending_action is not None:
            raise ValueError("assistant run is not at a computer hand-off checkpoint")
        run.mode = "computer"
        run.computer_task = computer_task
        run.plan = None
        run.operator_guidance = []
        run.last_controller = None
        run.last_verification = None
        run.latest_verification_image_path = None
        run.status = RunStatus.PLANNING
        run.error = None
        existing_session_id = run.session_id
        run.record(
            "assistant.computer_requested",
            task=computer_task,
            reusing_session=existing_session_id is not None,
        )
        await self.store.save(run)
        try:
            observation = (
                await self.computer.refresh(session_id=existing_session_id)
                if existing_session_id is not None
                else await self.computer.open(computer_task)
            )
        except Exception as exc:
            run.status = RunStatus.FAILED
            operation = "refresh" if existing_session_id is not None else "open"
            run.error = f"computer {operation} failed: {exc}"
            run.record(f"computer.{operation}_failed", error=str(exc))
            await self.store.save(run)
            return run
        run.session_id = observation.session_id
        run.observation = observation
        run.status = RunStatus.RUNNING
        run.record(
            (
                "computer.reused"
                if existing_session_id is not None
                else "computer.opened"
            ),
            session_id=observation.session_id,
            frame_id=observation.frame_id,
            world_version=observation.world_version,
        )
        await self.store.save(run)
        return run

    async def status(self, run_id: str) -> RunSnapshot:
        return await self.store.get_control(run_id)

    async def continue_run(self, run_id: str) -> RunSnapshot:
        # A continuation loaded from durable state must never consume an
        # ephemeral decision left behind by a cancelled prior coroutine.
        self._prefetched_controllers.pop(run_id, None)
        run = await self.store.get_control(run_id)
        if self._computer_session_was_interrupted(run):
            run = await self._reopen_after_process_restart(run)
            if run.status is RunStatus.PAUSED:
                return run
        retry_provider_cooldown = (
            run.status is RunStatus.PAUSED
            and bool(run.events)
            and run.events[-1].kind == "model.failed"
        )
        if run.status is RunStatus.FAILED and self._recoverable_failure(
            run.observation
        ):
            # Compatibility for checkpoints created before recoverable
            # computer failures were modelled as pauses. The failed MCP result
            # is definitive, so advance the action index and never replay it.
            run.next_action_index += 1
            run.plan = None
            run.status = RunStatus.RUNNING
            run.error = None
            run.record("run.recovering_computer_failure")
            await self.store.save(run)
        if (
            run.status is RunStatus.FAILED
            and run.last_verification is not None
            and run.last_verification.verdict == "failed"
        ):
            # Compatibility for older checkpoints where verifier disagreement
            # ended the run. The action result is already definitive and its
            # index advanced, so replan a correction without replaying it.
            run.plan = None
            run.status = RunStatus.RUNNING
            run.error = None
            run.record("run.recovering_verification_failure")
            await self.store.save(run)
        if run.status is RunStatus.BLOCKED:
            if (
                run.events
                and run.events[-1].kind == "target.identity_changed"
            ):
                return run
            # A model/environment block contains no accepted HID action. Replan
            # from the durable observation so an operator can retry after the
            # condition changes without creating a new run.
            run.plan = None
            run.status = RunStatus.RUNNING
            run.error = None
            run.record("run.replanning_after_block")
            await self.store.save(run)
        if run.status in TERMINAL_RUN_STATUSES:
            return run
        if run.status is RunStatus.NEEDS_APPROVAL:
            return run
        run.status = RunStatus.RUNNING
        run.error = None
        return await self._advance(
            run,
            retry_provider_cooldown=retry_provider_cooldown,
        )

    @staticmethod
    def _computer_session_was_interrupted(run: RunSnapshot) -> bool:
        for event in reversed(run.events):
            if event.kind == "computer.reopened_after_process_restart":
                return False
            if event.kind == "run.process_interrupted":
                return True
        return False

    async def _reopen_after_process_restart(
        self,
        run: RunSnapshot,
    ) -> RunSnapshot:
        """Replace process-local computer state before resuming durable work."""

        try:
            observation = await self.computer.open(
                run.computer_task or run.task
            )
        except Exception as exc:
            run.status = RunStatus.PAUSED
            run.error = f"computer reopen after process restart failed: {exc}"
            run.record(
                "computer.reopen_after_process_restart_failed",
                error=str(exc),
            )
            await self.store.save(run)
            return run
        abandoned_action = run.pending_action
        run.session_id = observation.session_id
        run.observation = observation
        run.pending_action = None
        run.pending_approval = None
        run.last_controller = None
        run.last_verification = None
        run.status = RunStatus.RUNNING
        run.error = None
        run.record(
            "computer.reopened_after_process_restart",
            session_id=observation.session_id,
            frame_id=observation.frame_id,
            world_version=observation.world_version,
            abandoned_pending_action=abandoned_action is not None,
            plan_preserved=run.plan is not None,
        )
        await self.store.save(run)
        return run

    async def pause(
        self, run_id: str, reason: str = "paused by operator"
    ) -> RunSnapshot:
        """Pause model progress without discarding a durable pending action."""

        self._prefetched_controllers.pop(run_id, None)
        run = await self.store.get_control(run_id)
        if run.status in TERMINAL_RUN_STATUSES or run.status is RunStatus.NEEDS_APPROVAL:
            return run
        run.status = RunStatus.PAUSED
        run.record("run.paused", reason=reason, source="operator")
        await self.store.save(run)
        return run

    async def steer(self, run_id: str, instruction: str) -> RunSnapshot:
        """Checkpoint operator guidance and force a fresh managed plan."""

        self._prefetched_controllers.pop(run_id, None)
        instruction = instruction.strip()
        if not instruction:
            raise ValueError("steering instruction must not be empty")
        if len(instruction) > 2_000:
            raise ValueError("steering instruction exceeds 2000 characters")
        run = await self.store.get_control(run_id)
        if run.origin != "managed":
            raise ValueError("only managed runs accept operator steering")
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.REJECTED,
            RunStatus.ABORTED,
            RunStatus.FAILED,
        }:
            raise ValueError("terminal run cannot be steered")
        if run.status is RunStatus.NEEDS_APPROVAL:
            raise ValueError(
                "pending approval must be resolved before steering"
            )
        if (
            run.status is RunStatus.BLOCKED
            and run.events
            and run.events[-1].kind == "target.identity_changed"
        ):
            raise ValueError(
                "target identity change must be resolved outside the model loop"
            )
        if run.pending_action is not None:
            raise ValueError(
                "pending action must settle or be aborted before steering"
            )
        if len(run.operator_guidance) >= 20:
            raise ValueError("operator steering history limit reached")
        run.operator_guidance.append(instruction)
        run.plan = None
        run.status = RunStatus.PAUSED
        run.error = None
        run.record(
            "run.steered",
            instruction=instruction,
            guidance_count=len(run.operator_guidance),
            source="operator",
        )
        await self.store.save(run)
        return run

    async def resolve_approval(
        self, run_id: str, approval_id: str, decision: dict[str, Any]
    ) -> RunSnapshot:
        self._prefetched_controllers.pop(run_id, None)
        run = await self.store.get_control(run_id)
        pending = run.pending_approval or {}
        if run.status is not RunStatus.NEEDS_APPROVAL:
            raise ValueError("run is not waiting for approval")
        if approval_id != pending.get("approval_id"):
            raise ValueError("approval_id does not match the pending approval")
        if not run.session_id:
            raise ValueError("run has no computer session")
        decision_type = decision.get("type")
        if decision_type not in {"approve", "reject", "take_over"}:
            raise ValueError("decision.type must be approve, reject, or take_over")
        run.record("approval.resolving", approval_id=approval_id, decision=decision_type)
        await self.store.save(run)
        observation = await self.computer.resolve_approval(
            session_id=run.session_id,
            approval_id=approval_id,
            decision=decision,
        )
        run.pending_approval = None
        if decision_type != "approve":
            run.pending_action = None
            run.observation = observation
            if decision_type == "reject":
                try:
                    run.observation = await self.computer.abort(
                        session_id=run.session_id,
                        reason="approval rejected by operator",
                    )
                    run.record(
                        "computer.aborted_after_rejection",
                        session_id=run.session_id,
                    )
                except Exception as exc:
                    run.error = (
                        "approval was rejected, but computer-session abort "
                        f"could not be confirmed: {exc}"
                    )
                    run.record(
                        "computer.abort_after_rejection_failed",
                        session_id=run.session_id,
                        error=str(exc),
                    )
            run.status = (
                RunStatus.REJECTED
                if decision_type == "reject"
                else RunStatus.ABORTED
            )
            run.record("approval.not_approved", decision=decision_type)
            await self.store.save(run)
            return run
        await self._accept_action_observation(run, observation)
        if run.status in TERMINAL_RUN_STATUSES or run.status is RunStatus.NEEDS_APPROVAL:
            return run
        return await self._advance(run)

    async def abort(self, run_id: str, reason: str = "aborted by caller") -> RunSnapshot:
        self._prefetched_controllers.pop(run_id, None)
        run = await self.store.get_control(run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            return run
        if run.session_id:
            try:
                with_abort = await self.computer.abort(
                    session_id=run.session_id, reason=reason
                )
                run.observation = with_abort
            except ComputerSessionMissingError:
                # A restarted daemon has no executable work for this durable
                # session. Treat its authoritative 404 as successful
                # quiescence so operators can abort the paused run and proceed
                # to the mandatory machine reboot. Other transport failures
                # still propagate and block reboot.
                run.record(
                    "computer.abort_session_already_absent",
                    session_id=run.session_id,
                )
        run.pending_action = None
        run.pending_approval = None
        run.status = RunStatus.ABORTED
        run.record("run.aborted", reason=reason)
        await self.store.save(run)
        return run

    async def _advance(
        self,
        run: RunSnapshot,
        *,
        retry_provider_cooldown: bool = False,
    ) -> RunSnapshot:
        actions_this_call = 0
        while actions_this_call < self.config.max_actions_per_advance:
            if run.next_action_index >= self.config.max_total_actions:
                run.status = RunStatus.BLOCKED
                run.error = "maximum total action budget reached"
                run.record("run.budget_exhausted")
                await self.store.save(run)
                return run

            if run.pending_action is not None:
                outcome = await self._execute_pending(
                    run,
                    parallel_next_control=(
                        self.config.parallel_post_action_control
                        and not run.pending_action.expects_task_completion
                        and actions_this_call + 1
                        < self.config.max_actions_per_advance
                    ),
                )
                if outcome != "continue":
                    return run
                actions_this_call += 1
                if run.status in TERMINAL_RUN_STATUSES:
                    return run
                continue

            if run.plan is None and self._is_observation_only_request(run):
                run.plan = PlanDecision(
                    summary="Answer from the current screen without input.",
                    steps=[
                        "Inspect the current visible screen evidence.",
                        "Report only what the pixels establish.",
                    ],
                    success_criteria=[
                        (
                            "The response answers the latest observational "
                            "request using current visible screen evidence."
                        )
                    ],
                    constraints=["Do not send keyboard or pointer input."],
                )
                run.record(
                    "plan.observation_only",
                    source="literal_read_only_fast_path",
                )
                await self.store.save(run)
                await self._verify(
                    run,
                    action=None,
                    before=None,
                )
                if run.status is RunStatus.RUNNING:
                    run.status = RunStatus.PAUSED
                    run.record(
                        "run.paused",
                        reason="screen evidence did not fully answer the request",
                    )
                    await self.store.save(run)
                return run

            if run.plan is None:
                calculator_fast_path = (
                    _calculator_fast_path(
                        run,
                        max_actions=self.config.max_actions_per_burst,
                    )
                    if run.next_action_index == 0
                    else None
                )
                notepad_fast_path = (
                    _notepad_fast_path(
                        run,
                        max_actions=self.config.max_actions_per_burst,
                    )
                    if (
                        run.next_action_index == 0
                        and calculator_fast_path is None
                    )
                    else None
                )
                literal_fast_path = (
                    calculator_fast_path or notepad_fast_path
                )
                if literal_fast_path is not None:
                    run.plan, launch_controller = literal_fast_path
                    self._prefetched_controllers[run.run_id] = (
                        launch_controller
                    )
                    run.record(
                        (
                            "plan.calculator_fast_path"
                            if calculator_fast_path is not None
                            else "plan.notepad_fast_path"
                        ),
                        source=(
                            "literal_task_expression"
                            if calculator_fast_path is not None
                            else "literal_exact_text_task"
                        ),
                    )
                    await self.store.save(run)
                elif not await self._plan(
                    run,
                    bypass_cooldown=retry_provider_cooldown,
                ):
                    return run
                retry_provider_cooldown = False

            previous_controller = run.last_controller
            controller = self._prefetched_controllers.pop(
                run.run_id,
                None,
            )
            if controller is not None:
                run.record(
                    "controller.parallel_adopted",
                    outcome=controller.outcome,
                    intent=controller.intent,
                )
                await self.store.save(run)
            else:
                verified_action = _durable_last_verified_action(run)
                controller = (
                    _notepad_new_document_controller(
                        run,
                        verified_action,
                        max_actions=self.config.max_actions_per_burst,
                    )
                    or _notepad_exact_text_controller(
                        run,
                        verified_action,
                        max_actions=self.config.max_actions_per_burst,
                    )
                )
                if controller is not None:
                    run.record(
                        "controller.durable_notepad_adopted",
                        outcome=controller.outcome,
                        intent=controller.intent,
                        source="verified_action_ledger",
                    )
                    await self.store.save(run)
                else:
                    controller = await self._control(
                        run,
                        bypass_cooldown=retry_provider_cooldown,
                    )
            retry_provider_cooldown = False
            if controller is None:
                return run
            if self._repeats_ungrounded_navigation(run, controller):
                rejected = self._last_ungrounded_navigation(run) or {}
                rejected_actions = rejected.get("rejected_actions") or []
                run.record(
                    "controller.ungrounded_repeat_rejected",
                    actions=self._visible_actions(rejected_actions),
                    reason=(
                        "the same or near-identical coordinate-only navigation "
                        "was already rejected before HID"
                    ),
                )
                await self.store.save(run)
                controller = await self._control(
                    run,
                    controller_feedback={
                        "reason": (
                            "This same or near-identical coordinate-only "
                            "navigation was already rejected before HID "
                            "because its target could not be independently "
                            "grounded."
                        ),
                        "rejected_actions": self._visible_actions(
                            rejected_actions
                        ),
                        "instruction": (
                            "Do not repeat or slightly perturb these "
                            "coordinates. Choose a safe keyboard navigation "
                            "action, a visibly grounded text target, or replan."
                        ),
                    },
                )
                if controller is None:
                    return run
                if self._repeats_ungrounded_navigation(run, controller):
                    run.plan = None
                    run.status = RunStatus.PAUSED
                    run.error = (
                        "controller repeated an ungrounded coordinate action "
                        "after explicit correction feedback"
                    )
                    run.record(
                        "controller.ungrounded_correction_failed",
                        actions=self._visible_actions(rejected_actions),
                    )
                    await self.store.save(run)
                    return run
            proposed_actions = [
                action.model_dump(mode="json", exclude_none=True)
                for action in controller.actions
            ]
            if self._unsafe_unverified_input_followup(
                run,
                proposed_actions,
            ):
                run.record(
                    "controller.unverified_input_followup_rejected",
                    action_types=[
                        str(action.get("type") or "")
                        for action in proposed_actions
                    ],
                    reason=(
                        "sender issued the full input draft but exact "
                        "screen readback was unavailable"
                    ),
                )
                await self.store.save(run)
                controller = await self._control(
                    run,
                    controller_feedback={
                        "reason": (
                            "The sender issued the entire input draft, but "
                            "the screen did not prove an exact readback."
                        ),
                        "instruction": (
                            "Do not append, retype, or execute the unread "
                            "draft. Cancel a terminal draft with Ctrl+C, or "
                            "dismiss a single-line field draft with Esc, in "
                            "one separate non-text action. Do not modify an "
                            "unread editor draft: editor Undo can coalesce and "
                            "remove earlier verified input. Re-enter text only "
                            "after a safe cancellation has completed and a "
                            "clean surface has been observed."
                        ),
                    },
                )
                if controller is None:
                    return run
                proposed_actions = [
                    action.model_dump(mode="json", exclude_none=True)
                    for action in controller.actions
                ]
                if self._unsafe_unverified_input_followup(
                    run,
                    proposed_actions,
                ):
                    run.plan = None
                    # The controller has already received one deterministic
                    # correction turn and still proposed an action that could
                    # mutate or execute the unread draft. This is not a
                    # retryable pause: repeating the same model loop cannot
                    # safely change machine state and only burns provider
                    # latency. Preserve the draft and stop until the operator
                    # explicitly steers or retries the blocked run.
                    run.status = RunStatus.BLOCKED
                    run.error = (
                        "controller tried to change or execute an unverified "
                        "input draft"
                    )
                    run.record(
                        "controller.unverified_input_correction_failed",
                        action_types=[
                            str(action.get("type") or "")
                            for action in proposed_actions
                        ],
                    )
                    await self.store.save(run)
                    return run
            if self._long_terminal_draft_needs_legibility_step(
                run,
                proposed_actions,
            ):
                run.record(
                    "controller.long_terminal_draft_rejected",
                    action_types=[
                        str(action.get("type") or "")
                        for action in proposed_actions
                    ],
                    reason=(
                        "long exact terminal text requires a separately "
                        "verified legibility action before HID"
                    ),
                )
                await self.store.save(run)
                controller = await self._control(
                    run,
                    controller_feedback={
                        "reason": (
                            "The proposed exact terminal draft is too long to "
                            "type before both full terminal width and increased "
                            "text size have been independently verified."
                        ),
                        "instruction": (
                            "Do not type any text yet. Propose one non-text, "
                            "reversible legibility action. If full width is not "
                            "already verifier-confirmed, maximize or widen the "
                            "terminal. Otherwise increase the terminal text size "
                            "or zoom in (for example with the OS-appropriate "
                            "Ctrl+plus shortcut), then let the verifier check it."
                        ),
                    },
                )
                if controller is None:
                    return run
                proposed_actions = [
                    action.model_dump(mode="json", exclude_none=True)
                    for action in controller.actions
                ]
                if self._long_terminal_draft_needs_legibility_step(
                    run,
                    proposed_actions,
                ):
                    run.plan = None
                    run.status = RunStatus.PAUSED
                    run.error = (
                        "controller repeated a long exact terminal draft "
                        "before a verified legibility action"
                    )
                    run.record(
                        "controller.long_terminal_legibility_correction_failed",
                        action_types=[
                            str(action.get("type") or "")
                            for action in proposed_actions
                        ],
                    )
                    await self.store.save(run)
                    return run
            if self._unsafe_non_idempotent_retry(
                previous_controller,
                controller,
                verification=run.last_verification,
            ):
                run.plan = None
                run.status = RunStatus.BLOCKED
                run.error = (
                    "unsafe retry of a state-changing toggle after ambiguous "
                    "verification"
                )
                run.record(
                    "controller.non_idempotent_retry_stopped",
                    previous_intent=(
                        previous_controller.intent
                        if previous_controller is not None
                        else None
                    ),
                    proposed_intent=controller.intent,
                    actions=self._visible_actions(
                        [
                            item.model_dump(mode="json", exclude_none=True)
                            for item in controller.actions
                        ]
                    ),
                )
                await self.store.save(run)
                return run
            (
                normalized_actions,
                added_change_waits,
                pre_type_settle_normalized,
            ) = _normalize_windows_run_launch(
                proposed_actions,
                max_actions=self.config.max_actions_per_burst,
            )
            completion_hint_added = (
                added_change_waits > 0
                and not controller.expects_task_completion
                and any(
                    action.get("type") == "type_text"
                    and str(action.get("text") or "")
                    .casefold()
                    .startswith("ms-settings:")
                    for action in normalized_actions
                )
                and _is_read_only_settings_request(run)
            )
            if (
                added_change_waits
                or pre_type_settle_normalized
                or completion_hint_added
            ):
                controller_data = controller.model_dump(
                    mode="json",
                    exclude_none=True,
                )
                controller_data["actions"] = normalized_actions
                if completion_hint_added:
                    controller_data["expects_task_completion"] = True
                controller = ControllerDecision.model_validate(controller_data)
                proposed_actions = normalized_actions
                run.record(
                    "controller.windows_run_launch_normalized",
                    added_change_waits=added_change_waits,
                    pre_type_settle_normalized=pre_type_settle_normalized,
                    completion_hint_added=completion_hint_added,
                )
            (
                normalized_actions,
                split_key_actions,
                key_sequence_overflow,
            ) = _normalize_sequential_key_actions(
                proposed_actions,
                max_actions=self.config.max_actions_per_burst,
            )
            if key_sequence_overflow:
                run.plan = None
                run.status = RunStatus.PAUSED
                run.error = (
                    "controller key sequence exceeds the bounded action limit "
                    "after safe expansion"
                )
                run.record(
                    "controller.sequential_keys_action_limit",
                    limit=self.config.max_actions_per_burst,
                )
                await self.store.save(run)
                return run
            if split_key_actions:
                controller_data = controller.model_dump(
                    mode="json",
                    exclude_none=True,
                )
                controller_data["actions"] = normalized_actions
                controller = ControllerDecision.model_validate(controller_data)
                proposed_actions = normalized_actions
                run.record(
                    "controller.sequential_keys_normalized",
                    added_actions=split_key_actions,
                )
            run.last_controller = controller
            if controller.outcome == "blocked":
                run.status = RunStatus.BLOCKED
                run.error = controller.reason or controller.intent
                run.record("controller.blocked", reason=run.error)
                await self.store.save(run)
                return run
            if controller.outcome == "replan":
                run.plan = None
                run.status = RunStatus.PAUSED
                run.record("controller.requested_replan", reason=controller.reason)
                if run.session_id:
                    try:
                        refreshed = await self.computer.refresh(
                            session_id=run.session_id,
                        )
                    except Exception as exc:
                        run.record(
                            "computer.refresh_after_replan_failed",
                            error=str(exc),
                        )
                    else:
                        run.observation = refreshed
                        run.record(
                            "computer.refreshed_after_replan",
                            session_id=refreshed.session_id,
                            frame_id=refreshed.frame_id,
                            world_version=refreshed.world_version,
                        )
                await self.store.save(run)
                return run
            if controller.outcome == "done":
                await self._verify(run, action=None, before=run.observation)
                if run.status is RunStatus.RUNNING:
                    run.plan = None
                    run.record(
                        "run.replanning_after_incomplete_done",
                        verification_summary=(
                            run.last_verification.summary
                            if run.last_verification is not None
                            else ""
                        ),
                    )
                    run.status = RunStatus.PAUSED
                    run.record("run.paused", reason="verifier requires more work")
                    await self.store.save(run)
                return run

            actions = [
                action.model_dump(mode="json", exclude_none=True)
                for action in controller.actions
            ]
            if self._same_controller_actions(
                previous_controller,
                controller,
            ):
                # Exact repeated HID is almost always an agent stall and is
                # especially dangerous for text. Refuse it before checkpoint
                # creation so a retry cannot duplicate accepted input.
                run.plan = None
                run.status = RunStatus.PAUSED
                run.error = "controller repeated the previous action unchanged"
                run.record(
                    "controller.repeated_actions",
                    actions=self._visible_actions(actions),
                )
                await self.store.save(run)
                return run
            if self._repeated_unsuccessful_text_input(run, actions):
                run.plan = None
                run.status = RunStatus.PAUSED
                run.error = (
                    "controller repeated text input after unsuccessful verification"
                )
                run.record(
                    "controller.repeated_unsuccessful_text",
                    action_types=[str(action.get("type") or "") for action in actions],
                )
                await self.store.save(run)
                return run
            try:
                validate_actions(actions)
            except BurstError as exc:
                run.plan = None
                run.status = RunStatus.PAUSED
                run.error = f"controller proposed invalid actions: {exc}"
                run.record("controller.invalid_actions", error=str(exc))
                await self.store.save(run)
                return run
            if len(actions) > self.config.max_actions_per_burst:
                run.plan = None
                run.status = RunStatus.PAUSED
                run.error = (
                    f"controller proposed {len(actions)} actions; "
                    f"limit is {self.config.max_actions_per_burst}"
                )
                run.record("controller.action_limit", count=len(actions))
                await self.store.save(run)
                return run
            if actions and all(
                action.get("type") == "move" for action in actions
            ):
                run.plan = None
                run.status = RunStatus.PAUSED
                run.error = "controller proposed a pointer-only no-op"
                run.record(
                    "controller.pointer_noop_rejected",
                    actions=self._visible_actions(actions),
                )
                await self.store.save(run)
                return run
            expected_evidence = self._normalized_expected_evidence(
                run,
                intent=controller.intent,
                actions=actions,
                expected_evidence=controller.expected_evidence,
            )
            if expected_evidence != controller.expected_evidence:
                controller = controller.model_copy(
                    update={"expected_evidence": expected_evidence}
                )
                run.last_controller = controller
                run.record(
                    "controller.expected_evidence_normalized",
                    reason=(
                        "numeric terminal zoom evidence was not requested "
                        "by the user"
                    ),
                )
            run.pending_action = self._pending_action(
                run, controller, actions
            )
            run.record(
                "action.checkpointed",
                index=run.pending_action.index,
                idempotency_key=run.pending_action.idempotency_key,
                intent=controller.intent,
                actions=self._visible_actions(actions),
                expected_evidence=controller.expected_evidence,
            )
            preview_ms = (
                self.config.interactive_action_preview_ms
                if run.caller.get("interface") == "chat_workspace"
                else 0
            )
            if preview_ms:
                run.record(
                    "action.preview_window_opened",
                    index=run.pending_action.index,
                    idempotency_key=run.pending_action.idempotency_key,
                    duration_ms=preview_ms,
                )
            # Critical ordering: the durable pending action and first-party UI
            # preview exist before HID. Headless clients keep benchmark speed;
            # the chat workspace gets one bounded local render window.
            await self.store.save(run)
            if preview_ms:
                await asyncio.sleep(preview_ms / 1_000)

        run.status = RunStatus.PAUSED
        run.record("run.paused", reason="per-call action budget reached")
        await self.store.save(run)
        return run

    @staticmethod
    def _is_observation_only_request(run: RunSnapshot) -> bool:
        request = (
            run.operator_guidance[-1]
            if run.operator_guidance
            else run.task
        )
        return is_observation_only_request(request)

    async def _plan(
        self,
        run: RunSnapshot,
        *,
        bypass_cooldown: bool = False,
    ) -> bool:
        request = self._model_request(run, "reasoner", PlanDecision, _REASONER_SYSTEM)
        run.record(
            "model.started",
            role="reasoner",
            candidates=self.models.route_names(
                "reasoner",
                preferred_provider=run.model_provider,
                provider_route=self._provider_route(run, "reasoner"),
            ),
        )
        await self.store.save(run)
        try:
            plan, response = await self.models.complete(
                request,
                PlanDecision,
                on_event=self._model_event_sink(run, "reasoner"),
                bypass_cooldown=bypass_cooldown,
                budget=self._model_budget(run),
                preferred_provider=run.model_provider,
                provider_route=self._provider_route(run, "reasoner"),
            )
        except ModelBudgetExceeded as exc:
            await self._model_budget_exhausted(run, "reasoner", exc)
            return False
        except ModelPoolError as exc:
            await self._model_failed(run, "reasoner", exc)
            return False
        plan, normalized_constraint_count = _normalize_plan_safety_constraints(
            plan
        )
        run.plan = plan
        if normalized_constraint_count:
            run.record(
                "plan.constraints_normalized",
                count=normalized_constraint_count,
                source="generic_non_mutation_guard",
            )
        run.record(
            "model.completed",
            role="reasoner",
            provider=response.provider,
            model=response.model,
            latency_ms=response.latency_ms,
            usage=response.usage,
            plan=plan.model_dump(mode="json"),
        )
        await self.store.save(run)
        return True

    async def _control(
        self,
        run: RunSnapshot,
        *,
        bypass_cooldown: bool = False,
        controller_feedback: dict[str, Any] | None = None,
    ) -> ControllerDecision | None:
        request = self._model_request(
            run,
            "controller",
            ControllerDecision,
            _CONTROLLER_SYSTEM,
            extra=(
                {"controller_feedback": controller_feedback}
                if controller_feedback is not None
                else None
            ),
        )
        run.record(
            "model.started",
            role="controller",
            candidates=self.models.route_names(
                "controller",
                preferred_provider=run.model_provider,
                provider_route=self._provider_route(run, "controller"),
            ),
        )
        await self.store.save(run)
        try:
            decision, response = await self.models.complete(
                request,
                ControllerDecision,
                on_event=self._model_event_sink(run, "controller"),
                bypass_cooldown=bypass_cooldown,
                budget=self._model_budget(run),
                preferred_provider=run.model_provider,
                provider_route=self._provider_route(run, "controller"),
            )
        except ModelBudgetExceeded as exc:
            await self._model_budget_exhausted(run, "controller", exc)
            return None
        except ModelPoolError as exc:
            await self._model_failed(run, "controller", exc)
            return None
        run.record(
            "model.completed",
            role="controller",
            provider=response.provider,
            model=response.model,
            outcome=decision.outcome,
            latency_ms=response.latency_ms,
            usage=response.usage,
            intent=decision.intent,
        )
        await self.store.save(run)
        return decision

    async def _verify(
        self,
        run: RunSnapshot,
        *,
        action: PendingAction | None,
        before: ComputerObservation | None,
        _allow_delayed_frame_retry: bool = True,
    ) -> None:
        after = run.observation
        after_image = Path(after.image_path) if after and after.image_path else None
        if after_image is None:
            try:
                refreshed = await self.computer.refresh(
                    session_id=run.session_id
                    or (after.session_id if after is not None else "")
                )
            except Exception as exc:
                run.status = RunStatus.PAUSED
                run.error = (
                    "computer action completed, but its verification image "
                    f"could not be refreshed: {exc}"
                )
                run.record(
                    "verification.evidence_unavailable",
                    error=str(exc),
                )
                await self.store.save(run)
                return
            previous_fingerprint = str(
                (after.machine if after is not None else {}).get(
                    "fingerprint"
                )
                or ""
            )
            refreshed_fingerprint = str(
                refreshed.machine.get("fingerprint") or ""
            )
            if (
                previous_fingerprint
                and refreshed_fingerprint
                and previous_fingerprint != refreshed_fingerprint
            ):
                run.observation = refreshed
                run.plan = None
                run.status = RunStatus.BLOCKED
                run.error = (
                    "target identity changed while refreshing verification "
                    "evidence"
                )
                run.record(
                    "target.identity_changed",
                    previous_fingerprint=previous_fingerprint,
                    current_fingerprint=refreshed_fingerprint,
                    source="harness_verification_refresh",
                )
                await self.store.save(run)
                return
            refreshed_image = (
                Path(refreshed.image_path)
                if refreshed.image_path
                else None
            )
            if refreshed_image is None:
                run.observation = refreshed
                run.status = RunStatus.PAUSED
                run.error = (
                    "computer action completed, but no readable verification "
                    "image was returned"
                )
                run.record("verification.evidence_unavailable")
                await self.store.save(run)
                return
            run.observation = refreshed
            run.record(
                "verification.evidence_refreshed",
                frame_id=refreshed.frame_id,
                world_version=refreshed.world_version,
            )
            await self.store.save(run)
        comparison_image = self._verification_composite(
            before=before,
            after=run.observation,
            run_id=run.run_id,
            action_index=run.next_action_index,
        )
        if comparison_image:
            run.latest_verification_image_path = comparison_image
            run.latest_verification_image_revision += 1
            evidence = VerificationImageArtifact(
                revision=run.latest_verification_image_revision,
                action_index=run.next_action_index,
                before_frame_id=before.frame_id if before else None,
                after_frame_id=(
                    run.observation.frame_id
                    if run.observation is not None
                    else None
                ),
                path=comparison_image,
            )
            run.verification_images = [
                *run.verification_images[-63:],
                evidence,
            ]
            run.record(
                "verification.evidence_captured",
                revision=evidence.revision,
                action_index=evidence.action_index,
                before_frame_id=evidence.before_frame_id,
                after_frame_id=evidence.after_frame_id,
            )
            await self.store.save(run)
        observation_only = (
            action is None and self._is_observation_only_request(run)
        )
        request = self._model_request(
            run,
            "verifier",
            VerificationDecision,
            (
                _OBSERVATION_VERIFIER_SYSTEM
                if observation_only
                else _VERIFIER_SYSTEM
            ),
            image_path=comparison_image,
            compact_observation=observation_only,
            image_detail="high" if observation_only else "original",
            extra={
                "action": action.model_dump(mode="json") if action else None,
                "before": before.model_dump(mode="json") if before else None,
                "verification_image": (
                    {
                        "layout": "left panel is BEFORE; right panel is AFTER",
                        "path": comparison_image,
                    }
                    if comparison_image
                    else {
                        "layout": "AFTER frame only; no readable BEFORE frame",
                        "path": (
                            run.observation.image_path
                            if run.observation is not None
                            else None
                        ),
                    }
                ),
            },
        )
        run.record(
            "model.started",
            role="verifier",
            candidates=self.models.route_names(
                "verifier",
                preferred_provider=run.model_provider,
                provider_route=self._provider_route(run, "verifier"),
            ),
        )
        await self.store.save(run)
        try:
            verdict, response = await self.models.complete(
                request,
                VerificationDecision,
                on_event=self._model_event_sink(run, "verifier"),
                budget=self._model_budget(run),
                preferred_provider=run.model_provider,
                provider_route=self._provider_route(run, "verifier"),
            )
        except ModelBudgetExceeded as exc:
            await self._model_budget_exhausted(run, "verifier", exc)
            return
        except ModelPoolError as exc:
            await self._model_failed(run, "verifier", exc)
            return
        reported_verdict = verdict.verdict
        verdict, legibility_normalization = (
            self._normalized_internal_legibility_verdict(
                run,
                action=action,
                verdict=verdict,
            )
        )
        completion_rejection = self._completion_rejection_reason(
            run,
            verdict,
            action=action,
        )
        action_rejection = self._verified_action_rejection_reason(
            action,
            verdict,
        )
        if completion_rejection is not None:
            verdict = verdict.model_copy(update={"verdict": "verified"})
        elif action_rejection is not None:
            verdict = verdict.model_copy(update={"verdict": "uncertain"})
        run.last_verification = verdict
        run.record(
            "model.completed",
            role="verifier",
            provider=response.provider,
            model=response.model,
            verdict=verdict.verdict,
            reported_verdict=reported_verdict,
            latency_ms=response.latency_ms,
            usage=response.usage,
            summary=verdict.summary,
            evidence=verdict.evidence,
            action_criteria=[
                item.model_dump(mode="json")
                for item in verdict.action_criteria
            ],
        )
        if (
            _allow_delayed_frame_retry
            and action is not None
            and verdict.verdict in {"failed", "uncertain"}
        ):
            refreshed = await self._delayed_verification_observation(
                run,
                baseline=run.observation,
            )
            if run.status in TERMINAL_RUN_STATUSES:
                return
            if refreshed is not None:
                await self._verify(
                    run,
                    action=action,
                    before=before,
                    _allow_delayed_frame_retry=False,
                )
                return
        if legibility_normalization is not None:
            run.record(
                "verification.internal_legibility_normalized",
                reason=legibility_normalization,
                reported_verdict=reported_verdict,
                effective_verdict=verdict.verdict,
            )
        if completion_rejection is not None:
            run.record(
                "verification.complete_rejected",
                reason=completion_rejection,
                summary=verdict.summary,
            )
        if action_rejection is not None:
            run.record(
                "verification.action_rejected",
                reason=action_rejection,
                summary=verdict.summary,
            )
        if verdict.verdict == "complete":
            run.status = RunStatus.COMPLETED
            run.error = None
            run.record("run.completed", summary=verdict.summary)
        elif verdict.verdict == "verified":
            run.status = RunStatus.RUNNING
            run.error = None
        elif verdict.verdict == "uncertain":
            run.plan = None
            run.status = RunStatus.PAUSED
            run.error = verdict.summary
            run.record("verification.uncertain", summary=verdict.summary)
        else:
            run.status = RunStatus.PAUSED
            run.error = verdict.summary
            run.record(
                "verification.failed",
                summary=verdict.summary,
                plan_reused=run.plan is not None,
            )
        await self.store.save(run)

    async def _delayed_verification_observation(
        self,
        run: RunSnapshot,
        *,
        baseline: ComputerObservation | None,
    ) -> ComputerObservation | None:
        """Poll pixels once before paying models to recover from a stale frame."""

        baseline_sha256 = (
            str(baseline.image_sha256 or "") if baseline is not None else ""
        )
        session_id = run.session_id or (
            baseline.session_id if baseline is not None else ""
        )
        if len(baseline_sha256) != 64 or not session_id:
            return None
        baseline_fingerprint = str(
            (baseline.machine if baseline is not None else {}).get(
                "fingerprint"
            )
            or ""
        )
        attempts = 0
        for delay_s in _DELAYED_VERIFICATION_REFRESH_DELAYS_S:
            if delay_s:
                await asyncio.sleep(delay_s)
            attempts += 1
            try:
                refreshed = await self.computer.refresh(
                    session_id=session_id,
                )
            except Exception as exc:
                run.record(
                    "verification.delayed_frame_refresh_failed",
                    attempt=attempts,
                    error=str(exc),
                )
                await self.store.save(run)
                return None
            refreshed_fingerprint = str(
                refreshed.machine.get("fingerprint") or ""
            )
            if (
                baseline_fingerprint
                and refreshed_fingerprint
                and baseline_fingerprint != refreshed_fingerprint
            ):
                run.observation = refreshed
                run.plan = None
                run.status = RunStatus.BLOCKED
                run.error = (
                    "target identity changed during delayed verification "
                    "refresh"
                )
                run.record(
                    "target.identity_changed",
                    previous_fingerprint=baseline_fingerprint,
                    current_fingerprint=refreshed_fingerprint,
                    source="harness_delayed_verification_refresh",
                )
                await self.store.save(run)
                return None
            refreshed_sha256 = str(refreshed.image_sha256 or "")
            if (
                len(refreshed_sha256) == 64
                and refreshed_sha256 != baseline_sha256
            ):
                run.observation = refreshed
                run.record(
                    "verification.delayed_frame_observed",
                    attempt=attempts,
                    previous_frame_id=(
                        baseline.frame_id if baseline is not None else None
                    ),
                    fresh_frame_id=refreshed.frame_id,
                    previous_image_sha256=baseline_sha256,
                    fresh_image_sha256=refreshed_sha256,
                )
                await self.store.save(run)
                return refreshed
        run.record(
            "verification.delayed_frame_unchanged",
            attempts=attempts,
            image_sha256=baseline_sha256,
        )
        await self.store.save(run)
        return None

    async def _execute_pending(
        self,
        run: RunSnapshot,
        *,
        parallel_next_control: bool = False,
    ) -> str:
        action = run.pending_action
        if action is None or not run.session_id:
            raise RuntimeError("pending action requires a computer session")
        before = run.observation
        action.attempts += 1
        tool = "pikvm_run_burst"
        call_id = (
            f"{action.idempotency_key}:attempt:{action.attempts}"
        )
        started = time.monotonic()
        run.record(
            "action.attempted",
            index=action.index,
            attempt=action.attempts,
            idempotency_key=action.idempotency_key,
            tool=tool,
            call_id=call_id,
            arguments={
                "session_id": run.session_id,
                "actions": self._visible_actions(action.actions),
                "based_on_world_version": action.based_on_world_version,
                "based_on_control_epoch": action.based_on_control_epoch,
                "idempotency_key": action.idempotency_key,
            },
        )
        await self.store.save(run)
        try:
            observation = await self.computer.burst(
                session_id=run.session_id,
                actions=action.actions,
                based_on_world_version=action.based_on_world_version,
                based_on_control_epoch=action.based_on_control_epoch,
                idempotency_key=action.idempotency_key,
            )
        except Exception as exc:
            # Ambiguous transport result: retain the exact pending action/key.
            latency_ms = round((time.monotonic() - started) * 1000)
            run.status = RunStatus.PAUSED
            run.error = f"computer transport failed; safe to retry same action: {exc}"
            run.record(
                "action.transport_uncertain",
                index=action.index,
                idempotency_key=action.idempotency_key,
                tool=tool,
                call_id=call_id,
                latency_ms=latency_ms,
                status="transport_uncertain",
                error=str(exc),
            )
            await self.store.save(run)
            return "stop"
        latency_ms = round((time.monotonic() - started) * 1000)
        accepted = await self._accept_action_observation(
            run,
            observation,
            before=before,
            tool=tool,
            call_id=call_id,
            latency_ms=latency_ms,
            parallel_next_control=parallel_next_control,
        )
        return "continue" if accepted else "stop"

    async def _accept_action_observation(
        self,
        run: RunSnapshot,
        observation: ComputerObservation,
        *,
        before: ComputerObservation | None = None,
        tool: str | None = None,
        call_id: str | None = None,
        latency_ms: int | None = None,
        parallel_next_control: bool = False,
    ) -> bool:
        action = run.pending_action
        tool_outcome = {
            key: value
            for key, value in {
                "tool": tool,
                "call_id": call_id,
                "latency_ms": latency_ms,
            }.items()
            if value is not None
        }
        input_receipts = self._public_input_receipts(
            observation.raw,
            action.actions if action is not None else [],
        )
        receipt_outcome = (
            {"input_receipts": input_receipts}
            if input_receipts
            else {}
        )
        screen_proof = {
            key: value
            for key, value in {
                "image_sha256": observation.image_sha256,
                "screen_hash": observation.screen_hash,
            }.items()
            if value
        }
        continuity_before = before or run.observation
        previous_machine = (
            continuity_before.machine
            if continuity_before is not None
            else {}
        )
        previous_fingerprint = str(
            previous_machine.get("fingerprint") or ""
        )
        current_fingerprint = str(
            observation.machine.get("fingerprint") or ""
        )
        run.observation = observation
        if (
            previous_fingerprint
            and current_fingerprint
            and previous_fingerprint != current_fingerprint
        ):
            run.pending_action = None
            run.pending_approval = None
            run.plan = None
            run.status = RunStatus.BLOCKED
            run.error = "target identity changed during computer action"
            run.record(
                "target.identity_changed",
                previous_fingerprint=previous_fingerprint,
                current_fingerprint=current_fingerprint,
                previous_alias=previous_machine.get("alias"),
                current_alias=observation.machine.get("alias"),
                source="harness",
                status=observation.status,
                **screen_proof,
                **receipt_outcome,
                **tool_outcome,
            )
            await self.store.save(run)
            return False
        if observation.status == "needs_approval":
            approval_request = observation.approval_request or {}
            if self._is_ungrounded_navigation(approval_request):
                approval_id = str(
                    approval_request.get("approval_id") or ""
                )
                try:
                    dismissed = await self.computer.resolve_approval(
                        session_id=run.session_id or observation.session_id,
                        approval_id=approval_id,
                        decision={
                            "type": "reject",
                            "reason": (
                                "managed harness rejected an ungrounded "
                                "navigation proposal"
                            ),
                        },
                    )
                except Exception:
                    # If the exact daemon hold cannot be cleared, keep the
                    # visible approval boundary rather than guessing that no
                    # input can occur.
                    pass
                else:
                    prior_recoveries = sum(
                        event.kind == "action.ungrounded_refreshed"
                        for event in run.events
                    )
                    if (
                        prior_recoveries
                        >= self.config.max_ungrounded_navigation_replans
                    ):
                        run.pending_action = None
                        run.pending_approval = None
                        run.observation = dismissed
                        run.plan = None
                        run.status = RunStatus.BLOCKED
                        run.error = (
                            "click targets could not be independently grounded "
                            "after the bounded navigation replan budget"
                        )
                        run.record(
                            "action.ungrounded_budget_exhausted",
                            approval_id=approval_id,
                            risk=approval_request.get("risk"),
                            reason=approval_request.get("reason"),
                            dismissal_status=dismissed.status,
                            recovery_count=prior_recoveries,
                            recovery_limit=(
                                self.config.max_ungrounded_navigation_replans
                            ),
                            error=run.error,
                            **tool_outcome,
                        )
                        await self.store.save(run)
                        return False
                    try:
                        reopened = await self.computer.open(run.task)
                    except Exception as exc:
                        run.pending_action = None
                        run.pending_approval = None
                        run.observation = dismissed
                        run.status = RunStatus.PAUSED
                        run.error = (
                            "ungrounded navigation was rejected, but a fresh "
                            f"managed session could not be opened: {exc}"
                        )
                        run.record(
                            "action.ungrounded_refresh_failed",
                            approval_id=approval_id,
                            risk=approval_request.get("risk"),
                            reason=approval_request.get("reason"),
                            dismissal_status=dismissed.status,
                            **tool_outcome,
                        )
                        await self.store.save(run)
                        return False
                    reopened_fingerprint = str(
                        reopened.machine.get("fingerprint") or ""
                    )
                    if (
                        previous_fingerprint
                        and reopened_fingerprint
                        and previous_fingerprint != reopened_fingerprint
                    ):
                        run.pending_action = None
                        run.pending_approval = None
                        run.plan = None
                        run.observation = reopened
                        run.status = RunStatus.BLOCKED
                        run.error = (
                            "target identity changed while rejecting "
                            "ungrounded navigation"
                        )
                        run.record(
                            "target.identity_changed",
                            previous_fingerprint=previous_fingerprint,
                            current_fingerprint=reopened_fingerprint,
                            previous_alias=previous_machine.get("alias"),
                            current_alias=reopened.machine.get("alias"),
                            source="harness_ungrounded_refresh",
                            **tool_outcome,
                        )
                        await self.store.save(run)
                        return False
                    previous_session_id = run.session_id
                    run.session_id = reopened.session_id
                    run.observation = reopened
                    run.pending_action = None
                    run.pending_approval = None
                    run.last_controller = None
                    run.status = RunStatus.PAUSED
                    run.error = None
                    run.record(
                        "action.ungrounded_refreshed",
                        approval_id=approval_id,
                        risk=approval_request.get("risk"),
                        reason=approval_request.get("reason"),
                        refused_frame_id=observation.frame_id,
                        previous_session_id=previous_session_id,
                        fresh_session_id=reopened.session_id,
                        fresh_frame_id=reopened.frame_id,
                        fresh_world_version=reopened.world_version,
                        plan_preserved=run.plan is not None,
                        recovery_count=prior_recoveries + 1,
                        recovery_limit=(
                            self.config.max_ungrounded_navigation_replans
                        ),
                        **tool_outcome,
                    )
                    await self.store.save(run)
                    return False
            run.pending_approval = approval_request
            run.status = RunStatus.NEEDS_APPROVAL
            run.record(
                "approval.required",
                approval_id=run.pending_approval.get("approval_id"),
                risk=run.pending_approval.get("risk"),
                request=run.pending_approval,
                status=observation.status,
                **tool_outcome,
            )
            await self.store.save(run)
            return False
        if observation.status in {"stale_world", "control_changed"}:
            refused_status = observation.status
            # A stale frame proves only that this specific HID proposal is no
            # longer authorized. The high-level task plan remains useful; the
            # controller will receive the fresh frame and must author a new
            # action against it. Human/concurrent control changes are different:
            # discard the plan and re-reason after authority is reacquired.
            if refused_status == "control_changed":
                run.plan = None
            run.status = RunStatus.PAUSED
            try:
                refreshed = await self.computer.refresh(
                    session_id=run.session_id or observation.session_id
                )
            except Exception as exc:
                run.pending_action = None
                run.error = (
                    f"action refused: {refused_status}; "
                    f"fresh observation failed: {exc}"
                )
                run.record(
                    "action.refused_stale",
                    status=refused_status,
                    refresh_error=str(exc),
                    **tool_outcome,
                )
                await self.store.save(run)
                return False
            refreshed_fingerprint = str(
                refreshed.machine.get("fingerprint") or ""
            )
            if (
                previous_fingerprint
                and refreshed_fingerprint
                and previous_fingerprint != refreshed_fingerprint
            ):
                run.pending_approval = None
                run.status = RunStatus.BLOCKED
                run.error = "target identity changed during stale-world refresh"
                run.record(
                    "target.identity_changed",
                    previous_fingerprint=previous_fingerprint,
                    current_fingerprint=refreshed_fingerprint,
                    previous_alias=previous_machine.get("alias"),
                    current_alias=refreshed.machine.get("alias"),
                    source="harness_stale_refresh",
                    **tool_outcome,
                )
                await self.store.save(run)
                return False
            run.observation = refreshed
            run.pending_action = None
            run.last_controller = None
            run.error = f"action refused: {refused_status}; world refreshed"
            run.record(
                "action.stale_world_refreshed",
                status=refused_status,
                refused_world_version=observation.world_version,
                fresh_world_version=refreshed.world_version,
                plan_preserved=run.plan is not None,
                fresh_controller_decision_required=True,
                **tool_outcome,
            )
            await self.store.save(run)
            return False
        if observation.status == "unverified":
            run.pending_action = None
            if action is not None:
                run.next_action_index = max(
                    run.next_action_index,
                    action.index + 1,
                )
            run.error = observation.error
            plan_preserved = self._can_preserve_plan_after_unverified_navigation(
                action,
                input_receipts,
                reason=str(observation.raw.get("reason") or ""),
            )
            if not plan_preserved:
                run.plan = None
            run.status = RunStatus.PAUSED
            run.record(
                "action.completed_unverified",
                index=action.index if action else None,
                frame_id=observation.frame_id,
                world_version=observation.world_version,
                reason=observation.raw.get("reason"),
                status=observation.status,
                plan_preserved=plan_preserved,
                **screen_proof,
                **receipt_outcome,
                **tool_outcome,
            )
            await self.store.save(run)
            return False
        local_verdict = _locally_verified_notepad_artifact_action(
            run,
            action,
            input_receipts,
            after=observation,
        )
        recovered_passive_wait_interrupt = (
            observation.status == "interrupted"
            and observation.raw.get("reason") == "deadline"
            and local_verdict is not None
        )
        if (
            observation.status not in {"completed", "paused", "done"}
            and not recovered_passive_wait_interrupt
        ):
            run.pending_action = None
            run.error = observation.error or f"computer returned {observation.status}"
            if self._recoverable_failure(observation):
                if action is not None:
                    run.next_action_index = max(
                        run.next_action_index,
                        action.index + 1,
                    )
                run.plan = None
                run.status = RunStatus.PAUSED
                run.record(
                    "action.recoverable_failure",
                    index=action.index if action else None,
                    status=observation.status,
                    reason=observation.raw.get("reason"),
                    error=run.error,
                    **screen_proof,
                    **receipt_outcome,
                    **tool_outcome,
                )
            else:
                run.status = RunStatus.FAILED
                run.record(
                    "action.failed",
                    index=action.index if action else None,
                    status=observation.status,
                    error=run.error,
                    **screen_proof,
                    **receipt_outcome,
                    **tool_outcome,
                )
            await self.store.save(run)
            return False
        run.pending_action = None
        if action is not None:
            run.next_action_index = max(run.next_action_index, action.index + 1)
        run.error = None
        run.status = RunStatus.RUNNING
        completion_outcome = (
            {
                "outer_status": observation.status,
                "recovered_from_passive_wait_interrupt": True,
            }
            if recovered_passive_wait_interrupt
            else {}
        )
        run.record(
            "action.completed",
            index=action.index if action else None,
            frame_id=observation.frame_id,
            world_version=observation.world_version,
            status=(
                "completed"
                if recovered_passive_wait_interrupt
                else observation.status
            ),
            **completion_outcome,
            **screen_proof,
            **receipt_outcome,
            **tool_outcome,
        )
        if local_verdict is not None:
            run.last_verification = local_verdict
            run.record(
                "verification.local_completed",
                verifier="exact_visual_readback",
                verdict=local_verdict.verdict,
                summary=local_verdict.summary,
                evidence=local_verdict.evidence,
                action_criteria=[
                    item.model_dump(mode="json")
                    for item in local_verdict.action_criteria
                ],
            )
            deterministic_controller = _notepad_exact_text_controller(
                run,
                action,
                max_actions=self.config.max_actions_per_burst,
            )
            if deterministic_controller is not None:
                self._prefetched_controllers[run.run_id] = (
                    deterministic_controller
                )
                run.record(
                    "controller.notepad_followup_prepared",
                    intent=deterministic_controller.intent,
                    source="durable_plan_artifact",
                )
            await self.store.save(run)
            return True
        await self.store.save(run)
        if parallel_next_control:
            await self._verify_and_prefetch_control(
                run,
                action=action,
                before=before,
            )
        else:
            await self._verify(run, action=action, before=before)
        return run.status is RunStatus.RUNNING

    async def _verify_and_prefetch_control(
        self,
        run: RunSnapshot,
        *,
        action: PendingAction | None,
        before: ComputerObservation | None,
    ) -> None:
        """Overlap independent verification with a non-authorizing decision.

        The controller may inspect the fresh post-action frame while the
        verifier checks the same transition. Its output remains ephemeral and
        cannot reach HID unless verification succeeds and the normal action
        validation/checkpoint path adopts it.
        """

        verification_frame_sha256 = str(
            (run.observation.image_sha256 if run.observation else None)
            or ""
        )
        calculator_controller = _calculator_task_controller(
            run,
            action,
            max_actions=self.config.max_actions_per_burst,
        )
        notepad_controller = (
            _notepad_new_document_controller(
                run,
                action,
                max_actions=self.config.max_actions_per_burst,
            )
            or _notepad_exact_text_controller(
                run,
                action,
                max_actions=self.config.max_actions_per_burst,
            )
        )
        deterministic_controller = (
            calculator_controller or notepad_controller
        )
        roles = (
            [
                "verifier",
                (
                    "deterministic_calculator"
                    if calculator_controller is not None
                    else "deterministic_notepad"
                ),
            ]
            if deterministic_controller is not None
            else ["verifier", "controller"]
        )
        run.record(
            "controller.parallel_started",
            action_index=action.index if action is not None else None,
            roles=roles,
        )
        if deterministic_controller is not None:
            run.record(
                (
                    "controller.calculator_expression_prepared"
                    if calculator_controller is not None
                    else "controller.notepad_followup_prepared"
                ),
                intent=deterministic_controller.intent,
                source=(
                    "literal_task_expression"
                    if calculator_controller is not None
                    else "literal_exact_text_task"
                ),
            )
        await self.store.save(run)
        verify_task = asyncio.create_task(
            self._verify(run, action=action, before=before),
            name=f"harness-verify:{run.run_id}",
        )
        if deterministic_controller is not None:
            try:
                await verify_task
            except BaseException:
                if not verify_task.done():
                    verify_task.cancel()
                await asyncio.gather(verify_task, return_exceptions=True)
                raise
            verified = (
                run.status is RunStatus.RUNNING
                and run.last_verification is not None
                and run.last_verification.verdict == "verified"
            )
            if verified and (
                calculator_controller is None
                or _verification_confirms_standard_calculator(run)
            ):
                controller = deterministic_controller
            elif verified and calculator_controller is not None:
                run.record(
                    "controller.calculator_expression_discarded",
                    reason="verifier did not confirm Standard mode",
                )
                await self.store.save(run)
                controller = await self._control(run)
            else:
                controller = deterministic_controller
        else:
            control_task = asyncio.create_task(
                self._control(run),
                name=f"harness-control:{run.run_id}",
            )
            try:
                _, controller = await asyncio.gather(
                    verify_task,
                    control_task,
                )
            except BaseException:
                for task in (verify_task, control_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(
                    verify_task,
                    control_task,
                    return_exceptions=True,
                )
                raise

        verification_frame_changed = bool(
            verification_frame_sha256
            and run.observation is not None
            and run.observation.image_sha256
            and run.observation.image_sha256
            != verification_frame_sha256
        )
        if (
            run.status is RunStatus.RUNNING
            and controller is not None
            and run.last_verification is not None
            and run.last_verification.verdict == "verified"
            and not verification_frame_changed
        ):
            if self._same_controller_actions(
                run.last_controller,
                controller,
            ):
                run.record(
                    "controller.parallel_stale_repeat_discarded",
                    outcome=controller.outcome,
                    intent=controller.intent,
                    reason=(
                        "speculative decision repeated the action that had "
                        "already completed"
                    ),
                )
            else:
                self._prefetched_controllers[run.run_id] = controller
                run.record(
                    "controller.parallel_ready",
                    outcome=controller.outcome,
                    intent=controller.intent,
                )
        else:
            run.record(
                "controller.parallel_discarded",
                reason=(
                    (
                        "delayed verification frame invalidated the "
                        "speculative controller"
                    )
                    if verification_frame_changed
                    else "verification did not authorize another action"
                    if controller is not None
                    else "controller did not return a usable decision"
                ),
                run_status=run.status.value,
                verification_verdict=(
                    run.last_verification.verdict
                    if run.last_verification is not None
                    else None
                ),
            )
        await self.store.save(run)

    @staticmethod
    def _can_preserve_plan_after_unverified_navigation(
        action: PendingAction | None,
        receipts: list[dict[str, Any]],
        *,
        reason: str,
    ) -> bool:
        """Keep a useful plan after harmless once-only navigation text.

        This does not verify the text or authorize a follow-up. The run still
        pauses and the next controller must inspect the resulting frame. The
        narrow shape excludes secrets, code, terminals, and every active key
        commit so uncertain consequential input continues to force replanning.
        """

        if action is None or reason != "type_unverified":
            return False
        text_actions = [
            item
            for item in action.actions
            if str(item.get("type") or "") == "type_text"
        ]
        if len(text_actions) != 1 or any(
            str(item.get("type") or "")
            not in {
                "click",
                "type_text",
                "wait",
                "wait_for_change",
                "wait_for_stable_screen",
            }
            for item in action.actions
        ):
            return False
        draft = text_actions[0]
        text = str(draft.get("text") or "")
        if (
            not text
            or len(text) > 64
            or draft.get("secret") is True
            or draft.get("code") is True
            or "terminal" in str(draft.get("context") or "").casefold()
        ):
            return False
        receipt = next(
            (
                item
                for item in receipts
                if item.get("type") == "type_text"
            ),
            None,
        )
        if receipt is None:
            return False
        requested = receipt.get("requested_characters")
        return (
            receipt.get("emitted_exactly_once") is True
            and isinstance(requested, int)
            and requested == len(text)
            and receipt.get("issued_characters") == requested
            and receipt.get("emitted_characters") == requested
        )

    @staticmethod
    def _same_controller_actions(
        previous: ControllerDecision | None,
        proposed: ControllerDecision,
    ) -> bool:
        if previous is None or (
            previous.outcome != "act" or proposed.outcome != "act"
        ):
            return False
        return [
            action.model_dump(mode="json", exclude_none=True)
            for action in previous.actions
        ] == [
            action.model_dump(mode="json", exclude_none=True)
            for action in proposed.actions
        ]

    @staticmethod
    def _is_ungrounded_navigation(
        approval_request: dict[str, Any],
    ) -> bool:
        """Return whether managed mode should discard and replan a click.

        The proposal is never executed. Direct mode still fails closed in the
        daemon because an external controller may not own a recovery loop.
        """

        return (
            approval_request.get("kind") == "direct_burst"
            and approval_request.get("risk") == "unknown"
            and approval_request.get("reason")
            == "coordinate click target could not be independently read"
        )

    def _pending_action(
        self,
        run: RunSnapshot,
        controller: ControllerDecision,
        actions: list[dict[str, Any]],
    ) -> PendingAction:
        canonical = json.dumps(
            actions, sort_keys=True, separators=(",", ":")
        ).encode()
        digest = hashlib.sha256(canonical).hexdigest()[:16]
        observation = run.observation
        return PendingAction(
            index=run.next_action_index,
            intent=controller.intent,
            actions=actions,
            expected_evidence=controller.expected_evidence,
            expects_task_completion=controller.expects_task_completion,
            based_on_world_version=(
                observation.world_version if observation is not None else None
            ),
            based_on_control_epoch=(
                observation.control_epoch if observation is not None else None
            ),
            idempotency_key=(
                f"{run.run_id}:action:{run.next_action_index}:{digest}"
            ),
        )

    @staticmethod
    def _is_internal_terminal_text_size_action(
        *,
        intent: str,
        actions: list[dict[str, Any]],
    ) -> bool:
        """Recognise reversible OCR preparation, never arbitrary terminal HID."""

        normalized_intent = intent.casefold()
        if "terminal" not in normalized_intent or not re.search(
            r"\b(?:zoom|text[- ]size|font[- ]size|enlarg|larger|"
            r"increas\w*\s+(?:the\s+)?(?:terminal\s+)?"
            r"(?:text|font|glyph))",
            normalized_intent,
        ):
            return False
        allowed = {
            "click",
            "key",
            "wait",
            "wait_for_change",
            "wait_for_stable_screen",
        }
        return bool(actions) and all(
            str(action.get("type") or "") in allowed
            for action in actions
        )

    @staticmethod
    def _requests_numeric_terminal_zoom(run: RunSnapshot) -> bool:
        request = " ".join(
            [run.task, *run.operator_guidance]
        ).casefold()
        return (
            "terminal" in request
            and re.search(r"\b(?:zoom|text|font)\b", request) is not None
            and (
                "%" in request
                or re.search(r"\bpercent(?:age)?\b", request) is not None
            )
        )

    @classmethod
    def _normalized_expected_evidence(
        cls,
        run: RunSnapshot,
        *,
        intent: str,
        actions: list[dict[str, Any]],
        expected_evidence: list[str],
    ) -> list[str]:
        """Remove model-invented numeric criteria from OCR preparation."""

        if (
            not cls._is_internal_terminal_text_size_action(
                intent=intent,
                actions=actions,
            )
            or cls._requests_numeric_terminal_zoom(run)
        ):
            return list(expected_evidence)
        relevant = [
            item
            for item in expected_evidence
            if re.search(
                r"\b(?:percent(?:age)?|zoom[- ]level|numeric)\b|"
                r"\b\d{2,3}\s*%",
                item.casefold(),
            )
            is None
        ]
        if relevant:
            return relevant
        return [
            "The terminal glyphs are visibly larger and a clean prompt remains "
            "readable."
        ]

    @classmethod
    def _normalized_internal_legibility_verdict(
        cls,
        run: RunSnapshot,
        *,
        action: PendingAction | None,
        verdict: VerificationDecision,
    ) -> tuple[VerificationDecision, str | None]:
        """Resolve one narrow verifier contradiction for reversible OCR prep."""

        if (
            action is None
            or verdict.verdict != "uncertain"
            or cls._requests_numeric_terminal_zoom(run)
            or not cls._is_internal_terminal_text_size_action(
                intent=action.intent,
                actions=action.actions,
            )
            or not action.expected_evidence
        ):
            return verdict, None
        assessments = {
            item.criterion_index: item
            for item in verdict.action_criteria
        }
        if set(assessments) != set(range(len(action.expected_evidence))) or any(
            not assessments[index].satisfied
            or not assessments[index].evidence.strip()
            for index in range(len(action.expected_evidence))
        ):
            return verdict, None
        claim = " ".join(
            [
                verdict.summary,
                *verdict.evidence,
                *(
                    item.evidence
                    for item in verdict.action_criteria
                ),
            ]
        ).casefold()
        visibly_larger = re.search(
            r"\b(?:visibly|noticeably)\s+(?:larger|enlarged)\b|"
            r"\b(?:larger|enlarged)\b.{0,40}\bvisible",
            claim,
        )
        irrelevant_numeric_doubt = re.search(
            r"\b(?:closed|hidden|not|cannot|could not|unable)\b"
            r".{0,120}\b(?:zoom|percent(?:age)?|100%)\b|"
            r"\b(?:zoom|percent(?:age)?|100%)\b"
            r".{0,120}\b(?:not|cannot|could not|unable|confirm)",
            claim,
        )
        if visibly_larger is None or irrelevant_numeric_doubt is None:
            return verdict, None
        return (
            verdict.model_copy(update={"verdict": "verified"}),
            (
                "all user-relevant legibility criteria were visibly satisfied; "
                "only an unrequested numeric zoom indicator was unavailable"
            ),
        )

    @staticmethod
    def _provider_route(
        run: RunSnapshot,
        role: ModelRole,
    ) -> list[str] | None:
        if run.model_route is None:
            return None
        return run.model_route.for_role(role)

    def _model_request(
        self,
        run: RunSnapshot,
        role: ModelRole,
        output_type: type[Any],
        system: str,
        *,
        extra: dict[str, Any] | None = None,
        image_path: str | None = None,
        compact_observation: bool = False,
        image_detail: str = "original",
    ) -> ModelRequest:
        if compact_observation:
            observation = run.observation
            context: dict[str, Any] = {
                "task": run.computer_task or run.task,
                "success_criteria": (
                    run.plan.success_criteria if run.plan else []
                ),
                "screen": (
                    {
                        "frame_id": observation.frame_id,
                        "width": observation.width,
                        "height": observation.height,
                    }
                    if observation is not None
                    else None
                ),
            }
        else:
            context = {
                "task": run.computer_task or run.task,
                "operator_guidance": run.operator_guidance,
                "plan": run.plan.model_dump(mode="json") if run.plan else None,
                "action_index": run.next_action_index,
                "last_controller": (
                    run.last_controller.model_dump(mode="json")
                    if run.last_controller
                    else None
                ),
                "last_verification": (
                    run.last_verification.model_dump(mode="json")
                    if run.last_verification
                    else None
                ),
                "observation": (
                    run.observation.model_dump(mode="json")
                    if run.observation
                    else None
                ),
                "recent_input_delivery": self._recent_input_delivery(run),
                "recent_verified_actions": self._recent_verified_actions(run),
                "trajectory_signals": self._trajectory_signals(run),
            }
        if extra:
            context.update(extra)
        prompt = (
            f"{system}\n\nReturn only JSON matching the supplied schema.\n\n"
            f"RUN CONTEXT:\n{json.dumps(context, ensure_ascii=False, sort_keys=True)}"
        )
        output_schema = output_type.model_json_schema()
        if role == "verifier":
            action_context = context.get("action")
            expected_evidence = (
                action_context.get("expected_evidence")
                if isinstance(action_context, dict)
                else []
            )
            output_schema = self._constrained_verification_schema(
                output_schema,
                task_criteria=(
                    len(run.plan.success_criteria)
                    if run.plan is not None
                    else 0
                ),
                action_criteria=(
                    len(expected_evidence)
                    if isinstance(expected_evidence, list)
                    else 0
                ),
            )
        return ModelRequest(
            role=role,
            prompt=prompt,
            output_schema=output_schema,
            image_path=(
                image_path
                if image_path is not None
                else run.observation.image_path if run.observation else None
            ),
            run_id=run.run_id,
            metadata={
                "action_index": run.next_action_index,
                "image_detail": image_detail,
            },
        )

    @staticmethod
    def _constrained_verification_schema(
        schema: dict[str, Any],
        *,
        task_criteria: int,
        action_criteria: int,
    ) -> dict[str, Any]:
        """Require one structured assessment for every declared criterion."""

        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return schema
        for field, count in (
            ("criteria", task_criteria),
            ("action_criteria", action_criteria),
        ):
            definition = properties.get(field)
            if not isinstance(definition, dict):
                continue
            definition["minItems"] = count
            definition["maxItems"] = count
        return schema

    @staticmethod
    def _trajectory_signals(run: RunSnapshot) -> dict[str, Any]:
        """Aggregate prior outcomes without exporting historical text or screen data."""
        action_type_counts: dict[str, int] = {}
        verifier_verdict_counts: dict[str, int] = {}
        recoverable_failures = 0
        repeated_unsuccessful_text_stops = 0
        ungrounded_navigation_replans = 0
        for event in run.events:
            data = event.data
            if event.kind == "action.checkpointed":
                actions = data.get("actions")
                if isinstance(actions, list):
                    for action in actions:
                        if not isinstance(action, dict):
                            continue
                        kind = str(action.get("type") or "unknown")
                        action_type_counts[kind] = action_type_counts.get(kind, 0) + 1
            elif (
                event.kind == "model.completed"
                and data.get("role") == "verifier"
            ):
                verdict = str(data.get("verdict") or "unknown")
                if verdict in {"verified", "complete", "uncertain", "failed"}:
                    verifier_verdict_counts[verdict] = (
                        verifier_verdict_counts.get(verdict, 0) + 1
                    )
            elif event.kind == "action.recoverable_failure":
                recoverable_failures += 1
            elif event.kind == "controller.repeated_unsuccessful_text":
                repeated_unsuccessful_text_stops += 1
            elif event.kind == "action.ungrounded_refreshed":
                ungrounded_navigation_replans += 1
        return {
            "action_type_counts": action_type_counts,
            "verifier_verdict_counts": verifier_verdict_counts,
            "recoverable_failures": recoverable_failures,
            "repeated_unsuccessful_text_stops": repeated_unsuccessful_text_stops,
            "ungrounded_navigation_replans": ungrounded_navigation_replans,
            "last_ungrounded_navigation": (
                AgentHarness._last_ungrounded_navigation(run)
            ),
            "ungrounded_navigation_history": (
                AgentHarness._ungrounded_navigation_history(run)
            ),
        }

    @staticmethod
    def _recent_input_delivery(run: RunSnapshot) -> list[dict[str, Any]]:
        """Summarise recent input receipts without replaying retained text."""

        recent: list[dict[str, Any]] = []
        for event in reversed(run.events):
            if event.kind not in {
                "action.completed",
                "action.completed_unverified",
                "action.recoverable_failure",
            }:
                continue
            receipts = event.data.get("input_receipts")
            if not isinstance(receipts, list):
                continue
            for receipt in receipts:
                if not isinstance(receipt, dict):
                    continue
                requested = receipt.get("requested_characters")
                issued = receipt.get("issued_characters")
                requested_hash = receipt.get("requested_sha256")
                issued_hash = receipt.get("issued_prefix_sha256")
                sender_finished = (
                    isinstance(requested, int)
                    and not isinstance(requested, bool)
                    and isinstance(issued, int)
                    and not isinstance(issued, bool)
                    and requested == issued
                    and isinstance(requested_hash, str)
                    and requested_hash != ""
                    and requested_hash == issued_hash
                )
                recent.append(
                    {
                        "action_index": event.data.get("index"),
                        "input_index": receipt.get("index"),
                        "status": receipt.get("status"),
                        "issued_characters": issued,
                        "requested_characters": requested,
                        "sender_finished": sender_finished,
                        "readback_exact": (
                            receipt.get("exact_readback_sha256_match") is True
                        ),
                        "readback_available": bool(
                            receipt.get("observed_text")
                        ),
                    }
                )
                if len(recent) >= 8:
                    return list(reversed(recent))
        return list(reversed(recent))

    @staticmethod
    def _recent_verified_actions(
        run: RunSnapshot,
        *,
        limit: int | None = 8,
    ) -> list[dict[str, Any]]:
        """Read durable verification work with an optional prompt-size bound."""

        current_action: dict[str, Any] | None = None
        last_completed_unverified: dict[str, Any] | None = None
        verified: list[dict[str, Any]] = []
        for event in run.events:
            if event.kind == "action.checkpointed":
                index = event.data.get("index")
                intent = event.data.get("intent")
                if isinstance(index, int) and isinstance(intent, str):
                    current_action = {
                        "action_index": index,
                        "intent": intent[:500],
                        "_completed": False,
                    }
                else:
                    current_action = None
                continue
            if (
                event.kind == "action.completed"
                and current_action is not None
                and event.data.get("index")
                == current_action.get("action_index")
            ):
                current_action["_completed"] = True
                last_completed_unverified = current_action
                continue
            if (
                event.kind
                in {
                    "action.completed_unverified",
                    "action.failed",
                    "action.recoverable_failure",
                }
                and current_action is not None
            ):
                current_action["_completed"] = False
                # These outcomes may follow partial target input. A later frame
                # cannot safely be attributed to an older completed action.
                last_completed_unverified = None
                continue
            if (
                event.kind == "action.ungrounded_refreshed"
                and current_action is not None
            ):
                # Grounding rejected the new action before HID. Preserve the
                # previous completed action so delayed remote-video
                # publication can still verify that exact transition.
                current_action["_completed"] = False
                continue
            model_verification = (
                event.kind == "model.completed"
                and event.data.get("role") == "verifier"
            )
            local_verification = event.kind == "verification.local_completed"
            if not model_verification and not local_verification:
                continue
            verification_action = (
                current_action
                if (
                    current_action is not None
                    and current_action.get("_completed") is True
                )
                else last_completed_unverified
            )
            if verification_action is None:
                continue
            verdict = str(event.data.get("verdict") or "")
            summary = event.data.get("summary")
            if verdict not in {"verified", "complete"} or not isinstance(
                summary,
                str,
            ):
                continue
            verified.append(
                {
                    "action_index": verification_action["action_index"],
                    "intent": verification_action["intent"],
                    "verdict": verdict,
                    "summary": summary[:500],
                }
            )
            verification_action["_completed"] = False
            last_completed_unverified = None
        return verified[-limit:] if limit is not None else verified

    @staticmethod
    def _ungrounded_navigation_history(
        run: RunSnapshot,
    ) -> list[dict[str, Any]]:
        last_checkpointed_actions: list[dict[str, Any]] | None = None
        history: list[dict[str, Any]] = []
        for event in run.events:
            if event.kind == "action.checkpointed":
                actions = event.data.get("actions")
                if isinstance(actions, list):
                    last_checkpointed_actions = [
                        dict(action)
                        for action in actions
                        if (
                            isinstance(action, dict)
                            and action.get("type")
                            in {
                                "click",
                                "double_click",
                                "move",
                                "scroll",
                                "wait",
                                "wait_for_stable_screen",
                                "wait_for_change",
                            }
                        )
                    ]
            elif event.kind == "action.ungrounded_refreshed":
                history.append(
                    {
                        "reason": (
                            event.data.get("reason")
                            or (
                                "coordinate click target could not be "
                                "independently read"
                            )
                        ),
                        "rejected_actions": list(
                            last_checkpointed_actions or []
                        ),
                        "refused_frame_id": event.data.get("refused_frame_id"),
                        "fresh_frame_id": event.data.get("fresh_frame_id"),
                    }
                )
        return history[-16:]

    @staticmethod
    def _last_ungrounded_navigation(
        run: RunSnapshot,
    ) -> dict[str, Any] | None:
        history = AgentHarness._ungrounded_navigation_history(run)
        return history[-1] if history else None

    @staticmethod
    def _repeats_ungrounded_navigation(
        run: RunSnapshot,
        controller: ControllerDecision,
    ) -> bool:
        if controller.outcome != "act":
            return False
        rejection_history = AgentHarness._ungrounded_navigation_history(run)
        if not rejection_history:
            return False
        proposed_actions = [
            action.model_dump(mode="json", exclude_none=True)
            for action in controller.actions
        ]
        navigation_action_types = {
            "click",
            "double_click",
            "move",
            "scroll",
            "wait",
            "wait_for_stable_screen",
            "wait_for_change",
        }
        if not proposed_actions or not all(
            str(action.get("type") or "") in navigation_action_types
            for action in proposed_actions
        ):
            return False

        def click_signature(
            actions: list[dict[str, Any]],
        ) -> list[tuple[str, int, int, str]]:
            return [
                (
                    str(action.get("type") or ""),
                    int(action.get("x") or 0),
                    int(action.get("y") or 0),
                    str(action.get("button") or "left"),
                )
                for action in actions
                if action.get("type") in {"click", "double_click"}
            ]

        proposed_clicks = click_signature(proposed_actions)
        return any(
            isinstance(rejected_actions, list)
            and bool(rejected_clicks := click_signature(rejected_actions))
            and len(rejected_clicks) == len(proposed_clicks)
            and all(
                rejected_type == proposed_type
                and rejected_button == proposed_button
                and abs(rejected_x - proposed_x) <= 4
                and abs(rejected_y - proposed_y) <= 4
                for (
                    rejected_type,
                    rejected_x,
                    rejected_y,
                    rejected_button,
                ), (
                    proposed_type,
                    proposed_x,
                    proposed_y,
                    proposed_button,
                ) in zip(rejected_clicks, proposed_clicks, strict=True)
            )
            for rejected_actions in (
                rejection.get("rejected_actions")
                for rejection in rejection_history
            )
        )

    @staticmethod
    def _repeated_unsuccessful_text_input(
        run: RunSnapshot,
        proposed_actions: list[dict[str, Any]],
    ) -> bool:
        """Stop a locally detected failed query loop without sending its text anywhere."""

        def signatures(actions: Any) -> set[tuple[str, str, bool]]:
            if not isinstance(actions, list):
                return set()
            return {
                (
                    str(action.get("text") or ""),
                    str(action.get("context") or ""),
                    bool(action.get("code")),
                )
                for action in actions
                if (
                    isinstance(action, dict)
                    and action.get("type") == "type_text"
                    and not action.get("secret")
                )
            }

        proposed = signatures(proposed_actions)
        if not proposed:
            return False
        active_match = False
        for event in run.events:
            if event.kind == "action.checkpointed":
                active_match = bool(
                    proposed.intersection(signatures(event.data.get("actions")))
                )
            elif (
                active_match
                and event.kind == "model.completed"
                and event.data.get("role") == "verifier"
                and event.data.get("verdict") in {"failed", "uncertain"}
            ):
                return True
        return False

    @staticmethod
    def _unsafe_unverified_input_followup(
        run: RunSnapshot,
        proposed_actions: list[dict[str, Any]],
    ) -> bool:
        """Require surface-safe cancellation before changing unread exact input."""

        checkpoints: dict[int, list[dict[str, Any]]] = {}
        active_unverified_surfaces: set[str] = set()
        for event in run.events:
            index = event.data.get("index")
            if not isinstance(index, int):
                continue
            if event.kind == "action.checkpointed":
                actions = event.data.get("actions")
                if isinstance(actions, list):
                    checkpoints[index] = [
                        dict(action)
                        for action in actions
                        if isinstance(action, dict)
                    ]
                continue
            checkpointed = checkpoints.get(index, [])
            if event.kind in {
                "action.completed_unverified",
                "action.recoverable_failure",
            }:
                exact_text_surfaces = {
                    action_index: (
                        "terminal"
                        if action.get("context") == "terminal"
                        else (
                            "editor"
                            if action.get("context") == "editor"
                            else "field"
                        )
                    )
                    for action_index, action in enumerate(checkpointed)
                    if (
                        action.get("type") == "type_text"
                        and (
                            action.get("context") == "terminal"
                            or action.get("verification") == "exact"
                        )
                    )
                }
                receipts = event.data.get("input_receipts")
                if not exact_text_surfaces or not isinstance(receipts, list):
                    continue
                for receipt in receipts:
                    if not isinstance(receipt, dict):
                        continue
                    requested = receipt.get("requested_characters")
                    issued = receipt.get("issued_characters")
                    issued_hash = receipt.get("issued_prefix_sha256")
                    surface = exact_text_surfaces.get(receipt.get("index"))
                    issued_prefix_is_recorded = (
                        isinstance(requested, int)
                        and isinstance(issued, int)
                        and 0 < issued <= requested
                        and isinstance(issued_hash, str)
                        and bool(issued_hash)
                    )
                    if (
                        surface is not None
                        and isinstance(requested, int)
                        and requested > 0
                        and issued_prefix_is_recorded
                        and receipt.get("exact_readback_sha256_match") is not True
                    ):
                        active_unverified_surfaces.add(surface)
                continue
            if event.kind == "action.completed":
                completed_keysets = {
                    frozenset(
                        token
                        for key in action.get("keys", [])
                        if isinstance(key, str)
                        for token in re.split(r"[+\s]+", key.upper())
                        if token
                    )
                    for action in checkpointed
                    if action.get("type") == "key"
                }
                if frozenset({"CTRL", "C"}) in completed_keysets:
                    active_unverified_surfaces.discard("terminal")
                if completed_keysets.intersection(
                    {
                        frozenset({"ESC"}),
                        frozenset({"ESCAPE"}),
                    }
                ):
                    active_unverified_surfaces.discard("field")

        if not active_unverified_surfaces:
            return False
        if "editor" in active_unverified_surfaces:
            passive_actions = {
                "wait",
                "wait_for_change",
                "wait_for_stable_screen",
            }
            for action in proposed_actions:
                action_type = str(action.get("type") or "")
                if action_type in passive_actions:
                    continue
                if action_type == "key":
                    keyset = {
                        token
                        for key in action.get("keys", [])
                        if isinstance(key, str)
                        for token in re.split(r"[+\s]+", key.upper())
                        if token
                    }
                    if keyset in ({"ESC"}, {"ESCAPE"}):
                        continue
                # Escape may dismiss an editor popup, but no editor mutation
                # is safe here: Notepad can coalesce multiple prior inputs into
                # one Undo unit. Every other input can mutate, save, submit,
                # select, or reposition unread content and must stop before HID.
                return True
        for action in proposed_actions:
            if (
                "terminal" in active_unverified_surfaces
                and (
                    (
                        action.get("type") == "type_text"
                        and action.get("context") == "terminal"
                    )
                    or (
                        action.get("type") == "key"
                        and bool(
                            {
                                str(key).upper()
                                for key in action.get("keys", [])
                                if isinstance(key, str)
                            }.intersection({"ENTER", "RETURN"})
                        )
                    )
                )
            ):
                return True
            if (
                {"field", "editor"}.intersection(
                    active_unverified_surfaces
                )
                and (
                    (
                        action.get("type") == "type_text"
                        and (
                            action.get("context") == "field"
                            or action.get("verification") == "exact"
                        )
                    )
                    or (
                        action.get("type") == "key"
                        and bool(
                            {
                                str(key).upper()
                                for key in action.get("keys", [])
                                if isinstance(key, str)
                            }.intersection({"ENTER", "RETURN"})
                        )
                    )
                    or action.get("type") in {
                        "click",
                        "double_click",
                    }
                )
            ):
                return True
        return False

    @staticmethod
    def _long_terminal_draft_needs_legibility_step(
        run: RunSnapshot,
        proposed_actions: list[dict[str, Any]],
    ) -> bool:
        """Refuse a long exact terminal draft until its surface is legible."""

        def is_long_terminal_draft(action: dict[str, Any]) -> bool:
            return (
                action.get("type") == "type_text"
                and action.get("context") == "terminal"
                and (
                    bool(action.get("code"))
                    or action.get("verification") == "exact"
                )
                and len(str(action.get("text") or "")) > 48
            )

        has_long_terminal_draft = any(
            is_long_terminal_draft(action)
            for action in proposed_actions
        )
        if not has_long_terminal_draft:
            return False

        terminal_opened_at = -1
        width_verified_at = -1
        text_size_verified_at = -1
        long_terminal_checkpoints: set[int] = set()
        unverified_terminal_at = -1
        verified_draft_cancellation_at = -1
        cancellation_checkpoints: set[int] = set()
        completed_cancellations: set[int] = set()
        current_action_index = -1
        for event in run.events:
            action_index = event.data.get("index")
            if event.kind == "action.checkpointed":
                if not isinstance(action_index, int):
                    current_action_index = -1
                    continue
                current_action_index = action_index
                actions = event.data.get("actions")
                if isinstance(actions, list) and any(
                    action.get("type") == "key"
                    and {
                        token
                        for key in action.get("keys", [])
                        if isinstance(key, str)
                        for token in re.split(r"[+\s]+", key.upper())
                        if token
                    }
                    == {"CTRL", "C"}
                    for action in actions
                    if isinstance(action, dict)
                ):
                    cancellation_checkpoints.add(action_index)
                if isinstance(actions, list) and any(
                    is_long_terminal_draft(action)
                    for action in actions
                    if isinstance(action, dict)
                ):
                    long_terminal_checkpoints.add(action_index)
                continue
            if (
                event.kind == "action.completed"
                and isinstance(action_index, int)
                and action_index in cancellation_checkpoints
            ):
                completed_cancellations.add(action_index)
                continue
            if (
                isinstance(action_index, int)
                and action_index in long_terminal_checkpoints
                and event.kind
                in {
                    "action.completed_unverified",
                    "action.failed",
                    "action.recoverable_failure",
                }
            ):
                unverified_terminal_at = max(
                    unverified_terminal_at,
                    action_index,
                )
                continue
            if (
                event.kind == "model.completed"
                and event.data.get("role") == "verifier"
                and event.data.get("verdict") in {"verified", "complete"}
                and current_action_index in completed_cancellations
            ):
                verified_draft_cancellation_at = max(
                    verified_draft_cancellation_at,
                    current_action_index,
                )

        for item in AgentHarness._recent_verified_actions(run, limit=None):
            action_index = item.get("action_index")
            if not isinstance(action_index, int):
                continue
            evidence_fields = [
                str(item.get(key) or "").casefold()
                for key in ("intent", "summary")
            ]
            evidence = " ".join(evidence_fields)
            terminal_named = "terminal" in evidence
            terminal_surface_opened = any(
                re.search(
                    r"\b(?:open(?:ed)?|launch(?:ed)?|start(?:ed)?)"
                    r"(?:\s+(?:the|a|new))?\s+terminal"
                    r"(?:\s+(?:application|app|window|shell))?"
                    r"(?!\s+menu|['’]s\b)\b",
                    field,
                )
                or re.search(
                    r"\bterminal(?:\s+(?:application|app|window|shell))?"
                    r"\s+(?:(?:is|was|has been)\s+)?"
                    r"(?:open(?:ed)?|launch(?:ed)?|start(?:ed)?)\b",
                    field,
                )
                for field in evidence_fields
            )
            if terminal_surface_opened:
                terminal_opened_at = max(terminal_opened_at, action_index)
            if terminal_named and re.search(
                r"\b(?:maximi[sz](?:e|ed)|widen(?:ed)?|full[- ]width)\b",
                evidence,
            ):
                width_verified_at = max(
                    width_verified_at,
                    action_index,
                )
            if terminal_named and re.search(
                r"\b(?:zoom(?:ed)?\s+in|enlarg(?:e|ed)|larger|"
                r"increas(?:e|ed|ing))\b",
                evidence,
            ) and re.search(
                r"\b(?:text|font|zoom)\b",
                evidence,
            ):
                text_size_verified_at = max(
                    text_size_verified_at,
                    action_index,
                )

        active_unverified_terminal_at = (
            unverified_terminal_at
            if verified_draft_cancellation_at <= unverified_terminal_at
            else -1
        )
        return (
            width_verified_at <= terminal_opened_at
            or text_size_verified_at
            <= max(terminal_opened_at, active_unverified_terminal_at)
        )

    @staticmethod
    def _verification_composite(
        *,
        before: ComputerObservation | None,
        after: ComputerObservation | None,
        run_id: str,
        action_index: int,
    ) -> str | None:
        """Persist a labelled, full-resolution visual delta for the verifier."""

        before_path = Path(before.image_path) if before and before.image_path else None
        after_path = Path(after.image_path) if after and after.image_path else None
        if (
            before_path is None
            or after_path is None
            or not before_path.is_file()
            or not after_path.is_file()
        ):
            return None
        output = after_path.with_name(
            f"{after_path.stem}.before-after-{run_id}-{action_index}.png"
        )
        try:
            with (
                Image.open(before_path) as before_source,
                Image.open(after_path) as after_source,
            ):
                before_image = ImageOps.exif_transpose(before_source).convert("RGB")
                after_image = ImageOps.exif_transpose(after_source).convert("RGB")
                panel_width = max(before_image.width, after_image.width)
                panel_height = max(before_image.height, after_image.height)
                label_height = 32
                composite = Image.new(
                    "RGB",
                    (panel_width * 2, panel_height + label_height),
                    "#202124",
                )
                composite.paste(
                    before_image,
                    (
                        (panel_width - before_image.width) // 2,
                        label_height + (panel_height - before_image.height) // 2,
                    ),
                )
                composite.paste(
                    after_image,
                    (
                        panel_width
                        + (panel_width - after_image.width) // 2,
                        label_height + (panel_height - after_image.height) // 2,
                    ),
                )
                draw = ImageDraw.Draw(composite)
                draw.text((8, 9), "BEFORE", fill="white")
                draw.text((panel_width + 8, 9), "AFTER", fill="white")
                composite.save(output, format="PNG", optimize=True)
        except (OSError, UnidentifiedImageError):
            return None
        return str(output)

    @classmethod
    def _unsafe_non_idempotent_retry(
        cls,
        previous: ControllerDecision | None,
        proposed: ControllerDecision,
        *,
        verification: VerificationDecision | None,
    ) -> bool:
        """Stop a nearby second click that could undo a successful toggle."""

        if (
            previous is None
            or previous.outcome != "act"
            or proposed.outcome != "act"
            or verification is None
            or verification.verdict not in {"failed", "uncertain"}
            or not cls._toggle_intent(previous.intent)
            or not cls._toggle_intent(proposed.intent)
            or len(previous.actions) != 1
            or len(proposed.actions) != 1
        ):
            return False
        prior = previous.actions[0]
        retry = proposed.actions[0]
        if prior.type not in {"click", "double_click"} or retry.type not in {
            "click",
            "double_click",
        }:
            return False
        return (prior.x - retry.x) ** 2 + (prior.y - retry.y) ** 2 <= 50**2

    @staticmethod
    def _toggle_intent(intent: str) -> bool:
        normalized = " ".join(intent.casefold().split())
        return any(
            marker in normalized
            for marker in (
                "toggle",
                "enabl",
                "disabl",
                "turn on",
                "turn off",
                "switch on",
                "switch off",
            )
        )

    @staticmethod
    def _completion_rejection_reason(
        run: RunSnapshot,
        verdict: VerificationDecision,
        *,
        action: PendingAction | None = None,
    ) -> str | None:
        if verdict.verdict != "complete":
            return None
        expected = len(run.plan.success_criteria) if run.plan is not None else 0
        assessments = {
            item.criterion_index: item for item in verdict.criteria
        }
        if expected and set(assessments) != set(range(expected)):
            return (
                "complete verdict did not assess every success criterion "
                f"(expected indexes 0..{expected - 1})"
            )
        for index in range(expected):
            if not assessments[index].satisfied:
                return f"criterion {index} was explicitly reported unsatisfied"
            if not assessments[index].evidence.strip():
                return f"criterion {index} has no specific visible evidence"
        claim = " ".join([verdict.summary, *verdict.evidence]).casefold()
        contradiction = re.search(
            r"\b(?:not yet|has not|have not|not been|not complete|incomplete|"
            r"still needs?|remains? to be|overall task[^.]{0,80}\bnot\b)",
            claim,
        )
        if contradiction is not None:
            return (
                "complete verdict contradicts its own evidence near "
                f"{contradiction.group(0)!r}"
            )
        task = " ".join(
            str(run.computer_task or run.task).casefold().split()
        )
        if (
            re.search(r"\breopen(?:ed|ing)?\b", task)
            and not AgentHarness._has_verified_reopen_after_save(
                run,
                action=action,
                verdict=verdict,
            )
        ):
            return (
                "task requires a separately verified reopen action after save"
            )
        return None

    @staticmethod
    def _has_verified_reopen_after_save(
        run: RunSnapshot,
        *,
        action: PendingAction | None = None,
        verdict: VerificationDecision | None = None,
    ) -> bool:
        """Require a durable reopen transition after a durable save action."""

        verified = AgentHarness._recent_verified_actions(run, limit=None)
        if (
            action is not None
            and verdict is not None
            and verdict.verdict in {"verified", "complete"}
        ):
            # The current verifier decision is evaluated before its
            # model.completed event is recorded. Include that one executed
            # action as provisional evidence for this gate only; the caller
            # records it immediately after the gate accepts or downgrades it.
            verified.append(
                {
                    "action_index": action.index,
                    "intent": action.intent,
                    "verdict": verdict.verdict,
                    "summary": verdict.summary[:500],
                }
            )
        save_indexes: list[int] = []
        reopen_indexes: list[int] = []
        for item in verified:
            action_index = item.get("action_index")
            if not isinstance(action_index, int):
                continue
            intent = " ".join(
                str(item.get("intent") or "").casefold().split()
            )
            opens_open_dialog = bool(
                re.search(
                    r"\bopen(?:ed|ing)?\b.{0,80}"
                    r"\b(?:native\s+)?open dialog\b",
                    intent,
                )
                and not re.search(
                    r"\bopen(?:ed|ing)?\b.{0,80}"
                    r"\b(?:from|in|using)\b.{0,80}"
                    r"\b(?:native\s+)?open dialog\b",
                    intent,
                )
            )
            reopening = bool(
                not opens_open_dialog
                and (
                    re.search(r"\breopen(?:ed|ing)?\b", intent)
                    or re.search(
                        r"\bopen(?:ed|ing)?\b.{0,80}"
                        r"\b(?:saved|file|document|workbook)\b",
                        intent,
                    )
                    or re.search(
                        r"\bopen(?:ed|ing)?\b.{0,80}"
                        r"\b(?:from|in|using)\b.{0,80}"
                        r"\b(?:native\s+)?open dialog\b",
                        intent,
                    )
                )
            )
            if reopening:
                reopen_indexes.append(action_index)
            save_without_dialog = intent.replace("save as", "")
            confirmed_save_as_replacement = bool(
                "save as" in intent
                and re.search(
                    r"\bconfirm(?:ed|ing)?\b.{0,80}"
                    r"\breplac(?:e|ed|ement|ing)\b",
                    intent,
                )
            )
            committed_save_as = bool(
                "save as" in intent
                and re.search(
                    r"\bcommit(?:s|ted|ting)?\b.{0,80}\bsave as\b",
                    intent,
                )
            )
            if (
                not reopening
                and (
                    confirmed_save_as_replacement
                    or committed_save_as
                    or re.search(
                        r"\bsav(?:e|ed|ing)\b",
                        save_without_dialog,
                    )
                )
            ):
                save_indexes.append(action_index)
        return bool(
            save_indexes
            and reopen_indexes
            and max(reopen_indexes) > max(save_indexes)
        )

    @staticmethod
    def _verified_action_rejection_reason(
        action: PendingAction | None,
        verdict: VerificationDecision,
    ) -> str | None:
        if (
            action is None
            or verdict.verdict != "verified"
            or not action.expected_evidence
        ):
            return None
        expected = len(action.expected_evidence)
        assessments = {
            item.criterion_index: item
            for item in verdict.action_criteria
        }
        if set(assessments) != set(range(expected)):
            return (
                "verified verdict did not assess every expected-evidence item "
                f"(expected indexes 0..{expected - 1})"
            )
        for index in range(expected):
            if not assessments[index].satisfied:
                return f"expected evidence {index} was explicitly unsatisfied"
            if not assessments[index].evidence.strip():
                return f"expected evidence {index} has no specific visible evidence"
        return None

    async def _model_failed(
        self,
        run: RunSnapshot,
        role: str,
        exc: Exception,
    ) -> None:
        # No HID action is accepted at a model boundary. Treat provider
        # exhaustion as a resumable operational outage rather than turning a
        # transient OAuth/API/CLI failure into a terminal computer run.
        run.status = RunStatus.PAUSED
        run.error = str(exc)
        run.record("model.failed", role=role, error=str(exc))
        await self.store.save(run)

    async def _model_budget_exhausted(
        self,
        run: RunSnapshot,
        role: str,
        exc: ModelBudgetExceeded,
    ) -> None:
        run.status = RunStatus.PAUSED
        run.error = str(exc)
        run.record(
            "model.budget_exhausted",
            role=role,
            reason=str(exc),
            provider_attempts=run.model_budget.provider_attempts,
            provider_attempt_limit=self.budget_policy.max_provider_attempts,
            committed_cost_microusd=run.model_budget.committed_cost_microusd,
            outstanding_cost_microusd=(
                run.model_budget.outstanding_cost_microusd
            ),
            max_cost_microusd=self.budget_policy.max_cost_microusd,
        )
        await self.store.save(run)

    def _model_budget(self, run: RunSnapshot) -> DurableRunModelBudget:
        return DurableRunModelBudget(
            run=run,
            store=self.store,
            policy=self.budget_policy,
        )

    def _model_event_sink(self, run: RunSnapshot, role: str):
        async def record(kind: str, data: dict[str, object]) -> None:
            run.record(f"model.{kind}", role=role, **data)
            await self.store.save(run)

        return record

    @staticmethod
    def _visible_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return redact_secrets(actions)

    @staticmethod
    def _public_input_receipts(
        raw: dict[str, Any],
        actions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return public_input_receipts(raw, actions)

    @staticmethod
    def _recoverable_failure(
        observation: ComputerObservation | None,
    ) -> bool:
        if observation is None or observation.status != "failed":
            return False
        return str(observation.raw.get("reason") or "") in {
            "type_unverified",
            "focus_lost",
        }
