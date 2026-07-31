"""PiKVM HID burst engine — the fast path for a model-in-the-loop controller.

A *burst* is a short, controller-authored sequence of raw HID actions (keys, typed
text, clicks, scrolls, waits) that the daemon executes LOCALLY in one shot — so one
model decision covers "Ctrl+P → type path → Enter → wait for the screen to settle"
instead of five round-trips. No OmniParser, no full-frame OCR, no operator LLM in
this path: the controller (Claude/Codex) is the brain and already knows what to do.

The engine only DISPATCHES HID — freshness / control-epoch / panic gating and the
screenshot bookkeeping live in the runtime. Between every action it polls a
``should_continue`` gate (abort/panic/steer/lease) and the per-call deadline, so a
burst stops mid-sequence the instant control changes. It reuses the same humanized
backend (WindMouse, humanized typing) as everything else.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pikvm_agent.core.models import VERIFIED_STATUSES
from pikvm_agent.core.spreadsheet_grid import (
    SpreadsheetGrid,
    SpreadsheetGridError,
    validate_spreadsheet_grid,
)
from pikvm_agent.executor.typing import chunk_text
from pikvm_agent.executor.verification import (
    is_editor_prose,
    is_exact_text,
    levenshtein,
    norm,
)
from pikvm_agent.pikvm.text import flatten_line_breaks
from pikvm_agent.vision.frame_diff import FP_MEANINGFUL, grid

# --- key-name normalisation ------------------------------------------------ #
# Accept friendly tokens ("CTRL", "P", "ENTER") AND already-valid PiKVM codes
# ("ControlLeft", "KeyP", "Enter"), so a controller can use either.

_MODS = {
    "CTRL": "ControlLeft", "CONTROL": "ControlLeft", "LCTRL": "ControlLeft", "RCTRL": "ControlRight",
    "SHIFT": "ShiftLeft", "LSHIFT": "ShiftLeft", "RSHIFT": "ShiftRight",
    "ALT": "AltLeft", "OPTION": "AltLeft", "LALT": "AltLeft", "RALT": "AltRight", "ALTGR": "AltRight",
    "META": "MetaLeft", "WIN": "MetaLeft", "WINDOWS": "MetaLeft", "CMD": "MetaLeft", "SUPER": "MetaLeft",
}
_NAMED = {
    "ENTER": "Enter", "RETURN": "Enter", "TAB": "Tab", "ESC": "Escape", "ESCAPE": "Escape",
    "SPACE": "Space", "SPACEBAR": "Space", "BACKSPACE": "Backspace", "BKSP": "Backspace",
    "DELETE": "Delete", "DEL": "Delete", "HOME": "Home", "END": "End",
    "PAGEUP": "PageUp", "PGUP": "PageUp", "PAGEDOWN": "PageDown", "PGDN": "PageDown",
    "UP": "ArrowUp", "DOWN": "ArrowDown", "LEFT": "ArrowLeft", "RIGHT": "ArrowRight",
    "INSERT": "Insert", "INS": "Insert", "CAPSLOCK": "CapsLock", "PRINTSCREEN": "PrintScreen",
    "MINUS": "Minus", "EQUAL": "Equal", "PLUS": "Equal", "PERIOD": "Period", "DOT": "Period",
    "COMMA": "Comma", "SLASH": "Slash", "BACKSLASH": "Backslash", "SEMICOLON": "Semicolon",
}
_VALID_KEY_CODE = re.compile(
    r"(?:"
    r"(?:Control|Shift|Alt|Meta)(?:Left|Right)|"
    r"Key[A-Z]|Digit[0-9]|"
    r"F(?:[1-9]|1[0-9]|2[0-4])|"
    r"Numpad(?:[0-9]|Add|Subtract|Multiply|Divide|Decimal|Enter)|"
    r"Enter|Tab|Escape|Space|Backspace|Delete|Home|End|"
    r"PageUp|PageDown|ArrowUp|ArrowDown|ArrowLeft|ArrowRight|"
    r"Insert|CapsLock|NumLock|ScrollLock|Pause|PrintScreen|"
    r"Backquote|Minus|Equal|BracketLeft|BracketRight|Backslash|"
    r"IntlBackslash|Semicolon|Quote|Comma|Period|Slash"
    r")"
)


def normalize_key(token: str) -> str | None:
    t = (token or "").strip()
    if not t:
        return None
    up = t.upper()
    if up in _MODS:
        return _MODS[up]
    if up in _NAMED:
        return _NAMED[up]
    if len(t) == 1 and t.isalpha():
        return "Key" + t.upper()
    if len(t) == 1 and t.isdigit():
        return "Digit" + t
    if up[0] == "F" and up[1:].isdigit() and 1 <= int(up[1:]) <= 24:
        return up  # F1..F24
    if _VALID_KEY_CODE.fullmatch(t):
        return t
    raise BurstError(
        f"unsupported key token {t!r}; use separate friendly names or "
        "PiKVM KeyboardEvent.code values"
    )


def normalize_keys(keys: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in keys:
        token = str(raw or "").strip()
        if not token:
            continue
        # Models commonly spell a chord as ["ctrl+End"]. Canonicalise that
        # familiar notation before transport; a literal plus key remains
        # available as "PLUS" or "Equal" with Shift.
        parts = token.split("+") if "+" in token else [token]
        if any(not part.strip() for part in parts):
            raise BurstError(
                f"malformed composite key token {token!r}; "
                "list each key separately"
            )
        for part in parts:
            key = normalize_key(part)
            if key:
                normalized.append(key)
    return normalized


# --- outcome --------------------------------------------------------------- #

@dataclass
class BurstOutcome:
    status: str                 # "completed" | "interrupted" | "failed"
    completed: int              # actions fully executed
    total: int
    reason: str = ""            # why it stopped early (control_changed / deadline / error / …)
    error: str = ""
    executed: list[str] = field(default_factory=list)  # action types that ran
    partial_action: dict[str, Any] | None = None
    action_receipts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return self.total - self.completed


class BurstError(Exception):
    """A malformed/unsupported burst action — surfaced to the controller, never executed."""


class TypingNotVerified(Exception):
    """Typed text was not verified or the field was not focused.

    The burst stops before any following active action (for example Enter), so
    ambiguous OCR cannot turn an uncertain draft into an irreversible submit.
    """

    def __init__(
        self,
        message: str,
        *,
        ambiguous: bool = False,
        action_receipt: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous
        self.action_receipt = action_receipt


class BurstInterrupted(Exception):
    """The active micro-action observed a stop/deadline gate and halted part-way."""

    def __init__(
        self,
        partial_action: dict[str, Any] | None = None,
        *,
        action_receipt: dict[str, Any] | None = None,
    ) -> None:
        super().__init__("active micro-action interrupted")
        self.partial_action = partial_action
        self.action_receipt = action_receipt


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


MAX_TYPE_TEXT_CHARS = _positive_int_env("PIKVM_AGENT_MAX_TYPE_TEXT_CHARS", 240)
MAX_BURST_TYPE_TEXT_CHARS = _positive_int_env("PIKVM_AGENT_MAX_BURST_TYPE_TEXT_CHARS", 480)
MAX_BURST_ACTIONS = _positive_int_env("PIKVM_AGENT_MAX_BURST_ACTIONS", 20)
AUTO_RUNTIME_FLOOR_MS = 4_000
AUTO_RUNTIME_CEILING_MS = 110_000
DEFAULT_STABLE_TIMEOUT_MS = 1_500
DEFAULT_CHANGE_TIMEOUT_MS = 8_000

_DENSE_BASE64_TOKEN = re.compile(
    r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{64,}={0,2}(?![A-Za-z0-9+/=])"
)
_ENCODED_FILE_TRANSFER_COMMAND = re.compile(
    r"(?:\b(?:printf|echo)\b.*(?:>>?|out-file|set-content)|"
    r"(?:>>?|out-file|set-content).*\b(?:printf|echo)\b)",
    re.IGNORECASE,
)
_ENCODED_SHELL_COMMAND = re.compile(
    r"\b(?:powershell|pwsh)\b.*\s-(?:enc|encodedcommand)\b",
    re.IGNORECASE,
)
_HEREDOC_SHELL_PAYLOAD = re.compile(
    r"(?:^|[;&|]\s*)(?:python(?:3)?|bash|sh|zsh|cat|tee|ruby|node)\b"
    r"[^\r\n]{0,80}<<-?\s*['\"]?[A-Za-z_][A-Za-z0-9_]*['\"]?",
    re.IGNORECASE,
)
_NESTED_SHELL_LAUNCHER = re.compile(
    r"^\s*(?:(?:bash|sh|zsh)\s+-[A-Za-z]*c\b|"
    r"cmd(?:\.exe)?\s+/[ck]\b|"
    r"(?:powershell|pwsh)(?:\.exe)?\b.*\s-(?:command|c)\b)",
    re.IGNORECASE,
)
MAX_INSPECTABLE_NESTED_SHELL_CHARS = 120


def _unsafe_payload_reason(text: str) -> str | None:
    if _ENCODED_SHELL_COMMAND.search(text):
        return "encoded shell command"
    if _HEREDOC_SHELL_PAYLOAD.search(text):
        return "heredoc shell payload"
    if (
        len(text) > MAX_INSPECTABLE_NESTED_SHELL_CHARS
        and _NESTED_SHELL_LAUNCHER.search(text)
    ):
        return "complex nested shell payload"
    if (
        _DENSE_BASE64_TOKEN.search(text)
        and _ENCODED_FILE_TRANSFER_COMMAND.search(text)
    ):
        return "encoded file-transfer payload"
    return None


def recommended_runtime_ms(actions: list[dict[str, Any]]) -> int:
    """Return a bounded default deadline that covers the work in ``actions``.

    A fixed four-second default cannot accommodate even a short verified phrase at
    the backend's deliberately human cadence.  The estimate leaves 400ms per typed
    character plus OCR/read-back overhead and declared waits.  It remains a deadline,
    not a delay: control-change and panic gates are still polled between typing chunks.
    """
    typed_characters = 0
    exact_readbacks = 0
    navigation_actions = 0
    declared_wait_ms = 0
    for raw in actions:
        action = raw if isinstance(raw, dict) else dict(raw)
        kind = action.get("type")
        if kind == "type_text":
            typed_characters += len(str(action.get("text", "")))
            if (
                action.get("verification") == "exact"
                or action.get("code") is True
            ):
                exact_readbacks += 1
        elif kind == "spreadsheet_grid":
            try:
                grid_contract = validate_spreadsheet_grid(
                    action.get("rows"),
                    max_characters=MAX_TYPE_TEXT_CHARS,
                )
            except SpreadsheetGridError:
                continue
            typed_characters += grid_contract.character_count
            navigation_actions += grid_contract.navigation_count
        elif kind == "wait":
            declared_wait_ms += max(0, int(action.get("ms", 0)))
        elif kind == "wait_for_stable_screen":
            declared_wait_ms += max(
                0,
                int(action.get("timeout_ms", DEFAULT_STABLE_TIMEOUT_MS)),
            )
        elif kind == "wait_for_change":
            declared_wait_ms += max(
                0,
                int(action.get("timeout_ms", DEFAULT_CHANGE_TIMEOUT_MS)),
            )

    typing_ms = (
        6_000 + (typed_characters * 400)
        if typed_characters
        else 0
    )
    estimate = (
        AUTO_RUNTIME_FLOOR_MS
        + declared_wait_ms
        + typing_ms
        + (exact_readbacks * 15_000)
        + (navigation_actions * 250)
    )
    return min(AUTO_RUNTIME_CEILING_MS, max(AUTO_RUNTIME_FLOOR_MS, estimate))


def needs_post_action_settle(actions: list[dict[str, Any]]) -> bool:
    """Whether the runtime should add a bounded settle before its evidence frame."""

    if not any(
        action.get("type")
        in {
            "key",
            "type_text",
            "spreadsheet_grid",
            "click",
            "double_click",
            "scroll",
        }
        for action in actions
    ):
        return False
    final = actions[-1]
    if final.get("type") in {"wait_for_stable_screen", "wait_for_change"}:
        return False
    if final.get("type") == "wait" and int(final.get("ms", 0)) >= 300:
        return False
    return True


def validate_actions(
    actions: list[dict[str, Any]],
    *,
    max_type_text_chars: int = MAX_TYPE_TEXT_CHARS,
    max_burst_type_text_chars: int = MAX_BURST_TYPE_TEXT_CHARS,
    max_actions: int = MAX_BURST_ACTIONS,
) -> None:
    """Reject burst shapes that are too large to be safe for raw HID execution."""
    if len(actions) > max_actions:
        raise BurstError(
            f"burst has {len(actions)} actions; max is {max_actions}. "
            "Split it into smaller look-plan-act chunks."
        )

    def reject_unsafe_payload(text: str, action_label: str) -> None:
        unsafe_payload = _unsafe_payload_reason(text)
        if unsafe_payload is not None:
            raise BurstError(
                f"{action_label} contains an {unsafe_payload}; "
                "stage larger content through an explicit transfer channel and "
                "verify its bytes instead of HID-typing an encoding"
            )

    total_type_text_chars = 0
    contiguous_text_parts: list[str] = []
    contiguous_text_start = 0
    spreadsheet_grid_count = 0
    other_active_action_count = 0
    for index, raw in enumerate(actions):
        try:
            action = raw if isinstance(raw, dict) else dict(raw)
        except Exception as exc:  # noqa: BLE001
            raise BurstError(f"action {index} is not a mapping") from exc

        if "no_verify" in action:
            raise BurstError(
                f"action {index} uses forbidden no_verify; verification policy "
                "cannot be disabled by a caller"
            )

        action_type = action.get("type")
        if action_type == "spreadsheet_grid":
            spreadsheet_grid_count += 1
        elif action_type not in {
            "wait",
            "wait_for_stable_screen",
            "wait_for_change",
        }:
            other_active_action_count += 1

        if action_type == "key":
            keys = action.get("keys") or (
                [action["key"]] if action.get("key") else []
            )
            if not isinstance(keys, list):
                raise BurstError(f"key action {index} needs a list of keys")
            if not normalize_keys(keys):
                raise BurstError(
                    f"key action {index} needs 'keys' (or 'key')"
                )

        if action_type == "spreadsheet_grid":
            rows = action.get("rows")
            try:
                grid_contract = validate_spreadsheet_grid(
                    rows,
                    max_characters=max_type_text_chars,
                )
            except SpreadsheetGridError as exc:
                raise BurstError(
                    f"spreadsheet_grid action {index} {exc}"
                ) from exc
            reject_unsafe_payload(
                grid_contract.payload(),
                f"spreadsheet_grid action {index}",
            )

        if action.get("type") != "type_text":
            if len(contiguous_text_parts) > 1:
                reject_unsafe_payload(
                    "".join(contiguous_text_parts),
                    (
                        "contiguous type_text actions "
                        f"{contiguous_text_start}-{index - 1}"
                    ),
                )
            contiguous_text_parts = []
            continue

        verification = str(action.get("verification") or "auto").lower()
        if verification not in {"auto", "exact"}:
            raise BurstError(
                f"type_text action {index} has unsupported verification "
                f"{verification!r}; use 'auto' or 'exact'"
            )
        text = str(action.get("text", ""))
        editor_prose = (
            str(action.get("context", "")).lower() == "editor"
            and action.get("code") is not True
            and not is_exact_text(text)
        )
        if editor_prose and text and text[-1].isspace():
            raise BurstError(
                f"type_text action {index} is editor prose and must not end in "
                "whitespace; put one explicit separator at the start of the "
                "next continuation"
            )
        if editor_prose and "  " in text:
            raise BurstError(
                f"type_text action {index} is editor prose and contains repeated "
                "spaces; correct the exact payload before HID delivery"
            )
        if not contiguous_text_parts:
            contiguous_text_start = index
        contiguous_text_parts.append(text)
        reject_unsafe_payload(text, f"type_text action {index}")
        char_count = len(text)
        if char_count > max_type_text_chars:
            raise BurstError(
                f"type_text action {index} is {char_count} chars; max is "
                f"{max_type_text_chars}. Create a temporary file in the target editor, "
                "diff it, and apply a small surgical change instead of HID-typing a "
                "large blob."
            )

        total_type_text_chars += char_count
        if total_type_text_chars > max_burst_type_text_chars:
            raise BurstError(
                f"burst contains {total_type_text_chars} typed chars; max is "
                f"{max_burst_type_text_chars}. Split the work into smaller bursts, or "
                "use the file-and-diff workflow for larger edits."
            )
    if len(contiguous_text_parts) > 1:
        reject_unsafe_payload(
            "".join(contiguous_text_parts),
            (
                "contiguous type_text actions "
                f"{contiguous_text_start}-{len(actions) - 1}"
            ),
        )
    if spreadsheet_grid_count and (
        spreadsheet_grid_count != 1 or other_active_action_count
    ):
        raise BurstError(
            "spreadsheet_grid requires a separate verified focus action"
        )


# --- the engine ------------------------------------------------------------ #

ShouldContinue = Callable[[], bool]


async def run_burst(
    actions: list[dict[str, Any]],
    *,
    backend: Any,
    should_continue: ShouldContinue | None = None,
    deadline_ms: float | None = None,
    typer: Any = None,
) -> BurstOutcome:
    """Execute ``actions`` as one local HID burst. Polls ``should_continue`` (control /
    panic / lease) and ``deadline_ms`` between every action and stops mid-burst if either
    trips — returning how far it got so the controller can re-plan from a fresh screen."""
    validate_actions(actions)
    total = len(actions)
    executed: list[str] = []
    action_receipts: list[dict[str, Any]] = []

    def _stop() -> tuple[str, str] | None:
        if should_continue is not None and not should_continue():
            return ("interrupted", "control_changed")
        if deadline_ms is not None and time.monotonic() * 1000 >= deadline_ms:
            return ("interrupted", "deadline")
        return None

    unverified_error = ""
    passive_evidence_actions = {
        "wait",
        "wait_for_change",
        "wait_for_stable_screen",
    }
    pending_change_baseline: Any = None
    for i, raw in enumerate(actions):
        stop = _stop()
        if stop is not None:
            await _release_all_quietly(backend)
            return BurstOutcome(
                stop[0],
                i,
                total,
                reason=stop[1],
                executed=executed,
                action_receipts=action_receipts,
            )
        a = raw if isinstance(raw, dict) else dict(raw)
        kind = a.get("type")
        try:
            if (
                i + 1 < total
                and kind not in passive_evidence_actions
                and (
                    actions[i + 1]
                    if isinstance(actions[i + 1], dict)
                    else dict(actions[i + 1])
                ).get("type")
                == "wait_for_change"
            ):
                # Capture before the input. If the guest paints quickly, taking
                # the baseline only after the key/click races the very
                # transition the caller asked us to observe.
                pending_change_baseline = await _capture_screen_grid(backend)
            action_receipt = await _dispatch(
                a,
                kind,
                backend=backend,
                typer=typer,
                should_continue=lambda: _stop() is None,
                change_baseline=(
                    pending_change_baseline
                    if kind == "wait_for_change"
                    else None
                ),
            )
            if kind == "wait_for_change":
                pending_change_baseline = None
            if action_receipt is not None:
                action_receipts.append({"index": i, **action_receipt})
        except BurstError:
            raise
        except BurstInterrupted as exc:
            if exc.action_receipt is not None:
                action_receipts.append({"index": i, **exc.action_receipt})
            await _release_all_quietly(backend)
            return BurstOutcome(
                "interrupted",
                i,
                total,
                reason=(_stop() or ("interrupted", "control_changed"))[1],
                executed=executed,
                partial_action=exc.partial_action,
                action_receipts=action_receipts,
            )
        except TypingNotVerified as exc:
            if exc.action_receipt is not None:
                action_receipts.append({"index": i, **exc.action_receipt})
            remaining_are_passive = all(
                (item if isinstance(item, dict) else dict(item)).get("type")
                in passive_evidence_actions
                for item in actions[i + 1 :]
            )
            if exc.ambiguous and remaining_are_passive:
                # The text physically landed but local OCR could not prove it.
                # Complete only passive settling/evidence actions, then expose an
                # explicit unverified state for diagnosis. It cannot authorize
                # task completion or any key/click/second type action.
                unverified_error = str(exc)
                executed.append(str(kind))
                continue
            # Confirmed wrong text, lost focus, or any following active action:
            # stop BEFORE the next action (especially Enter).
            return BurstOutcome(
                "failed",
                i,
                total,
                reason="type_unverified",
                error=str(exc),
                executed=executed,
                action_receipts=action_receipts,
            )
        except Exception as exc:  # noqa: BLE001 - a backend failure ends the burst, not the daemon
            return BurstOutcome(
                "failed",
                i,
                total,
                reason="action_error",
                error=f"{kind}: {exc}",
                executed=executed,
                action_receipts=action_receipts,
            )
        executed.append(str(kind))

    if unverified_error:
        return BurstOutcome(
            "unverified",
            total,
            total,
            reason="type_unverified",
            error=unverified_error,
            executed=executed,
            action_receipts=action_receipts,
        )
    return BurstOutcome(
        "completed",
        total,
        total,
        executed=executed,
        action_receipts=action_receipts,
    )


async def _release_all_quietly(backend: Any) -> None:
    release_all = getattr(backend, "release_all", None)
    if callable(release_all):
        try:
            await release_all()
        except Exception:
            pass


def _typing_receipt(
    action: dict[str, Any],
    result: Any,
    *,
    precise: bool,
) -> dict[str, Any]:
    requested = str(action.get("text", ""))
    delivery = flatten_line_breaks(requested)
    observed = str(getattr(result, "field_text", "") or "")
    status = str(getattr(result, "status", "") or "unverified")
    verdict = str(getattr(result, "verdict", "") or "unverified")
    receipt: dict[str, Any] = {
        "type": "type_text",
        "status": status,
        "verdict": verdict,
        "observed_text": observed,
        "observed_text_redacted": False,
        # This is how much input the sender issued. RFB/HID completion is not an
        # acknowledgement from the guest application.
        "issued_characters": int(
            getattr(result, "typed_characters", len(delivery)) or 0
        ),
        "requested_characters": len(requested),
        "delivery_characters": len(delivery),
        "delivery_transformed": delivery != requested,
        "observed_characters": len(observed),
        "correction_count": int(
            getattr(result, "correction_count", 0) or 0
        ),
        "delivery_retries": int(
            getattr(result, "delivery_retries", 0) or 0
        ),
        "used_fast_path": bool(getattr(result, "used_fast_path", False)),
    }
    summary = str(getattr(result, "summary", "") or "")
    if summary:
        receipt["summary"] = summary
    intended_norm = norm(delivery, precise)
    observed_norm = norm(observed, precise)
    receipt["edit_distance"] = levenshtein(
        intended_norm,
        observed_norm,
        max(len(intended_norm), len(observed_norm)),
    )
    issued_characters = min(
        len(delivery),
        max(0, int(receipt["issued_characters"])),
    )
    requested_sha256 = hashlib.sha256(requested.encode("utf-8")).hexdigest()
    delivery_sha256 = hashlib.sha256(delivery.encode("utf-8")).hexdigest()
    issued_prefix_sha256 = hashlib.sha256(
        delivery[:issued_characters].encode("utf-8")
    ).hexdigest()
    readback_sha256 = hashlib.sha256(observed.encode("utf-8")).hexdigest()
    receipt.update(
        {
            "requested_sha256": requested_sha256,
            "delivery_sha256": delivery_sha256,
            "issued_prefix_sha256": issued_prefix_sha256,
            "readback_sha256": readback_sha256,
            "exact_readback_sha256_match": (
                issued_characters == len(delivery)
                and delivery_sha256 == readback_sha256
            ),
        }
    )
    emitted_characters = getattr(result, "emitted_characters", None)
    emitted_sha256 = str(getattr(result, "emitted_sha256", "") or "")
    emitted_exactly_once = getattr(result, "emitted_exactly_once", None)
    readback_frame_sha256 = str(
        getattr(result, "readback_frame_sha256", "") or ""
    )
    if isinstance(emitted_characters, int) and not isinstance(
        emitted_characters, bool
    ):
        receipt["emitted_characters"] = max(0, emitted_characters)
    if re.fullmatch(r"[0-9a-f]{64}", emitted_sha256):
        receipt["emitted_sha256"] = emitted_sha256
    if isinstance(emitted_exactly_once, bool):
        receipt["emitted_exactly_once"] = emitted_exactly_once
    if re.fullmatch(r"[0-9a-f]{64}", readback_frame_sha256):
        receipt["readback_frame_sha256"] = readback_frame_sha256
    receipt["proof_state"] = _typing_proof_state(
        status=status,
        verdict=verdict,
        intended=delivery,
        observed=observed,
        issued_characters=issued_characters,
        exact_readback_sha256_match=bool(
            receipt["exact_readback_sha256_match"]
        ),
        readback_frame_sha256=readback_frame_sha256,
    )
    if status == "failed_focus_lost":
        receipt["focus_evidence"] = "focus_lost"
    elif status.startswith("verified_") or verdict in {"match", "contains"}:
        receipt["focus_evidence"] = "read_back_verified"
    elif status.startswith("unverified_"):
        receipt["focus_evidence"] = "read_back_unverified"
    else:
        receipt["focus_evidence"] = "read_back_mismatch"
    return receipt


def _typing_proof_state(
    *,
    status: str,
    verdict: str,
    intended: str,
    observed: str,
    issued_characters: int,
    exact_readback_sha256_match: bool,
    readback_frame_sha256: str = "",
) -> str:
    """Describe target evidence without treating sender completion as an ACK."""

    if (
        status == "verified_exact"
        and verdict == "match"
        and issued_characters == len(intended)
        and exact_readback_sha256_match
    ):
        if re.fullmatch(r"[0-9a-f]{64}", readback_frame_sha256):
            return "exact_visual_readback"
        return "exact_ocr_readback"
    if (
        status == "verified_safe_normalized"
        and verdict in {"match", "contains"}
    ):
        return "normalized_readback"
    if observed:
        if len(observed) < len(intended) and intended.startswith(observed):
            return "partial_readback"
        if verdict == "mismatch" or status.startswith("failed_"):
            return "mismatched_readback"
        return "ambiguous_readback"
    return "issued_only"


def _unwatched_typing_receipt(
    text: str,
    *,
    secret: bool,
    typed_characters: int | None = None,
) -> dict[str, Any]:
    delivery = flatten_line_breaks(text)
    requested_characters = len(text)
    delivery_characters = len(delivery)
    issued_characters = (
        delivery_characters
        if typed_characters is None
        else min(delivery_characters, max(0, typed_characters))
    )
    receipt: dict[str, Any] = {
        "type": "type_text",
        "status": "delivered_unverified",
        "verdict": "unverified",
        "observed_text_redacted": secret,
        "issued_characters": issued_characters,
        "requested_characters": requested_characters,
        "delivery_characters": delivery_characters,
        "delivery_transformed": delivery != text,
        "correction_count": 0,
        "delivery_retries": 0,
        "used_fast_path": False,
        "focus_evidence": (
            "read_back_not_retained" if secret else "read_back_unavailable"
        ),
        "proof_state": "not_retained" if secret else "issued_only",
    }
    if not secret:
        receipt.update(
            {
                "requested_sha256": hashlib.sha256(
                    text.encode("utf-8")
                ).hexdigest(),
                "delivery_sha256": hashlib.sha256(
                    delivery.encode("utf-8")
                ).hexdigest(),
                "issued_prefix_sha256": hashlib.sha256(
                    delivery[:issued_characters].encode("utf-8")
                ).hexdigest(),
                "exact_readback_sha256_match": False,
            }
        )
    return receipt


def _spreadsheet_grid_receipt(
    grid_contract: SpreadsheetGrid,
    *,
    issued_cells: int,
) -> dict[str, Any]:
    requested_payload = grid_contract.payload()
    issued_payload = grid_contract.payload(cell_limit=issued_cells)
    issued_characters = grid_contract.issued_character_count(issued_cells)
    return {
        "type": "spreadsheet_grid",
        "status": "delivered_unverified",
        "verdict": "unverified",
        "proof_state": "issued_only",
        "focus_evidence": "read_back_unavailable",
        "requested_cells": grid_contract.cell_count,
        "issued_cells": issued_cells,
        "requested_characters": grid_contract.character_count,
        "issued_characters": issued_characters,
        "emitted_characters": issued_characters,
        "emitted_exactly_once": True,
        "requested_sha256": hashlib.sha256(
            requested_payload.encode("utf-8")
        ).hexdigest(),
        "issued_prefix_sha256": hashlib.sha256(
            issued_payload.encode("utf-8")
        ).hexdigest(),
        "emitted_sha256": hashlib.sha256(
            issued_payload.encode("utf-8")
        ).hexdigest(),
    }


async def _dispatch(
    a: dict[str, Any],
    kind: str | None,
    *,
    backend: Any,
    typer: Any,
    should_continue: ShouldContinue | None,
    change_baseline: Any = None,
) -> dict[str, Any] | None:
    if kind == "key":
        keys = normalize_keys(a.get("keys") or ([a["key"]] if a.get("key") else []))
        if not keys:
            raise BurstError("key action needs 'keys' (or 'key')")
        await backend.keypress(keys)
    elif kind == "type_text":
        text = a.get("text", "")
        method = str(a.get("method", "")).lower()
        code, secret = bool(a.get("code")), bool(a.get("secret"))
        context = str(a.get("context", "")).lower()
        editor_prose = (
            not code
            and context == "editor"
            and is_editor_prose(str(text))
        )
        requested_verification = str(a.get("verification") or "").lower()
        exact_verification = (
            requested_verification == "exact"
            or not requested_verification
        )
        precise = (
            exact_verification
            or code
            or context in {"field", "terminal"}
            or (is_exact_text(str(text)) and not editor_prose)
        )
        fast = method in ("print", "hid_print", "pikvm_hid_print")
        if typer is not None and not secret:
            transport_code = code or (
                is_exact_text(str(text)) and not editor_prose
            )
            # A caller may request the printer transport, but it cannot disable
            # watched delivery and read-back when the runtime has a typer. The
            # typer itself selects guarded printer chunks for eligible prose.
            res = await typer.type_text(
                text,
                code=transport_code,
                prose=editor_prose,
                exact=precise,
                secret=secret,
                context=context,
                should_continue=should_continue,
            )
            action_receipt = _typing_receipt(a, res, precise=precise)
            status = str(getattr(res, "status", "") or "")
            if status == "blocked_by_policy":
                raise BurstInterrupted(
                    {
                        "type": "type_text",
                        "issued_characters": int(
                            getattr(res, "typed_characters", 0) or 0
                        ),
                        "requested_characters": int(
                            getattr(res, "intended_characters", len(text))
                            or len(text)
                        ),
                    },
                    action_receipt=action_receipt,
                )
            unverified = status.startswith("unverified_")
            if status not in VERIFIED_STATUSES:
                raise TypingNotVerified(
                    f"typed {text!r} but read-back disagrees ({status}): "
                    f"{getattr(res, 'summary', '')}",
                    ambiguous=unverified,
                    action_receipt=action_receipt,
                )
            return action_receipt
        elif fast and hasattr(backend, "print_text"):
            # Bootstrap/fake runtimes without a watched typer retain a bounded raw
            # printer transport. Production runtimes inject a typer above.
            delivery_text = flatten_line_breaks(str(text))
            typed_characters = 0
            for chunk in chunk_text(delivery_text):
                if should_continue is not None and not should_continue():
                    receipt = _unwatched_typing_receipt(
                        str(text),
                        secret=secret,
                        typed_characters=typed_characters,
                    )
                    raise BurstInterrupted(
                        {
                            "type": "type_text",
                            "issued_characters": typed_characters,
                            "requested_characters": len(str(text)),
                        },
                        action_receipt=receipt,
                    )
                await backend.print_text(chunk)
                typed_characters += len(chunk)
            return _unwatched_typing_receipt(str(text), secret=secret)
        else:
            await backend.type_text(text, code=code, secret=secret)
            return _unwatched_typing_receipt(str(text), secret=secret)
    elif kind == "spreadsheet_grid":
        grid_contract = validate_spreadsheet_grid(
            a.get("rows"),
            max_characters=MAX_TYPE_TEXT_CHARS,
        )
        rows = grid_contract.rows
        issued_cells = 0
        requested_cells = grid_contract.cell_count

        def require_grid_control() -> None:
            if should_continue is not None and not should_continue():
                raise BurstInterrupted(
                    {
                        "type": "spreadsheet_grid",
                        "issued_cells": issued_cells,
                        "requested_cells": requested_cells,
                    },
                    action_receipt=_spreadsheet_grid_receipt(
                        grid_contract,
                        issued_cells=issued_cells,
                    ),
                )

        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                require_grid_control()
                await backend.type_text(str(value), code=True, secret=False)
                issued_cells += 1
                if column_index < len(row) - 1:
                    require_grid_control()
                    await backend.keypress(normalize_keys(["TAB"]))
            require_grid_control()
            await backend.keypress(normalize_keys(["ENTER"]))
            if row_index < len(rows) - 1:
                require_grid_control()
                await backend.keypress(normalize_keys(["HOME"]))
        return _spreadsheet_grid_receipt(
            grid_contract,
            issued_cells=issued_cells,
        )
    elif kind in ("click", "double_click"):
        x, y = int(a["x"]), int(a["y"])
        button = a.get("button", "left")
        if kind == "double_click" and hasattr(backend, "double_click"):
            await backend.double_click(x, y, button)
        else:
            await backend.click(x, y, button)
    elif kind == "move":
        await backend.move_mouse(int(a["x"]), int(a["y"]))
    elif kind == "scroll":
        ux, uy = _SCROLL.get(a.get("direction", "down"), (0, -1))
        amount = max(1, int(a.get("amount", 3)))
        await backend.scroll(ux * amount, uy * amount)
    elif kind == "wait":
        await asyncio.sleep(max(0, int(a.get("ms", 0))) / 1000.0)
    elif kind == "wait_for_stable_screen":
        await wait_for_stable_screen(backend, stable_ms=int(a.get("stable_ms", 300)),
                                     timeout_ms=int(a.get(
                                         "timeout_ms", DEFAULT_STABLE_TIMEOUT_MS
                                     )),
                                     should_continue=should_continue)
    elif kind == "wait_for_change":
        await wait_for_screen_change(backend, timeout_ms=int(a.get(
                                         "timeout_ms", DEFAULT_CHANGE_TIMEOUT_MS
                                     )),
                                     should_continue=should_continue,
                                     base_grid=change_baseline)
    else:
        raise BurstError(f"unsupported burst action: {kind!r}")


_SCROLL = {"up": (0, 1), "down": (0, -1), "right": (1, 0), "left": (-1, 0)}


async def wait_for_screen_change(
    backend: Any,
    *,
    timeout_ms: int = DEFAULT_CHANGE_TIMEOUT_MS,
    poll_ms: int = 150,
    should_continue: ShouldContinue | None = None,
    base_grid: Any = None,
) -> bool:
    """Block until the screen CHANGES from how it looks right now (an app launching, a remote
    desktop connecting, a page loading), or ``timeout_ms`` elapses — so a burst can say 'wait
    for it to appear' instead of guessing a blind 20s wait. Returns True if it changed."""
    import numpy as np

    deadline = time.monotonic() * 1000 + max(0, timeout_ms)
    base = base_grid
    while True:
        if should_continue is not None and not should_continue():
            return False
        try:
            frame = await backend.screenshot()
            g = await asyncio.to_thread(grid, frame.data) if frame and frame.data else None
        except Exception:  # noqa: BLE001
            g = None
        if g is not None:
            if base is None:
                base = g
            else:
                delta = float(np.abs(g.astype(np.int32) - base.astype(np.int32)).sum()) / max(1, g.size) / 255.0
                if delta > FP_MEANINGFUL:
                    return True
        if time.monotonic() * 1000 >= deadline:
            return False
        await asyncio.sleep(poll_ms / 1000.0)


async def _capture_screen_grid(backend: Any) -> Any:
    """Best-effort frame fingerprint immediately before an input action."""

    try:
        frame = await backend.screenshot()
        if frame and frame.data:
            return await asyncio.to_thread(grid, frame.data)
    except Exception:  # noqa: BLE001
        pass
    return None


async def wait_for_stable_screen(
    backend: Any,
    *,
    stable_ms: int = 300,
    timeout_ms: int = DEFAULT_STABLE_TIMEOUT_MS,
                                 poll_ms: int = 120, should_continue: ShouldContinue | None = None) -> bool:
    """Block until the screen stops changing for ``stable_ms`` (cheap grid frame-diff), or
    ``timeout_ms`` elapses. Lets a burst say 'wait for the editor to finish loading'
    without a model round-trip. Returns True if it settled, False on timeout."""
    deadline = time.monotonic() * 1000 + max(0, timeout_ms)
    last = None
    stable_since: float | None = None
    while True:
        if should_continue is not None and not should_continue():
            return False
        now = time.monotonic() * 1000
        try:
            frame = await backend.screenshot()
            g = await asyncio.to_thread(grid, frame.data) if frame and frame.data else None
        except Exception:  # noqa: BLE001
            g = None
        if g is not None and last is not None:
            import numpy as np

            delta = float(np.abs(g.astype(np.int32) - last.astype(np.int32)).sum()) / max(1, g.size) / 255.0
            if delta <= FP_MEANINGFUL:
                if stable_since is None:
                    stable_since = now
                elif now - stable_since >= stable_ms:
                    return True
            else:
                stable_since = None
        if g is not None:
            last = g
        if now >= deadline:
            return False
        await asyncio.sleep(poll_ms / 1000.0)
