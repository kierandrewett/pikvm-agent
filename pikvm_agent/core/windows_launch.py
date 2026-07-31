"""Strict grammar for routine local launches through the Windows Run dialog."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

SAFE_WINDOWS_RUN_EXECUTABLES = frozenset(
    {
        "calc",
        "excel",
        "explorer",
        "mspaint",
        "notepad",
        "taskmgr",
        "winver",
        "winword",
        "write",
    }
)
_SAFE_WINDOWS_SETTINGS_URI = re.compile(
    r"ms-settings:[a-z0-9][a-z0-9-]{0,80}"
)
_WINDOWS_RUN_MODIFIERS = frozenset(
    {"cmd", "meta", "super", "win", "windows"}
)
_PASSIVE_ACTION_TYPES = frozenset(
    {"wait", "wait_for_change", "wait_for_stable_screen"}
)


def is_safe_windows_run_text(text: str) -> bool:
    """Return whether text is one allowlisted executable or Settings URI."""

    executable = text.casefold()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    return (
        executable in SAFE_WINDOWS_RUN_EXECUTABLES
        or _SAFE_WINDOWS_SETTINGS_URI.fullmatch(text) is not None
    )


def is_windows_run_key_action(action: Mapping[str, Any]) -> bool:
    """Return whether one action is the Windows Run shortcut."""

    run_keys = action.get("keys")
    return (
        action.get("type") == "key"
        and isinstance(run_keys, list)
        and len(run_keys) == 2
        and str(run_keys[0]).casefold() in _WINDOWS_RUN_MODIFIERS
        and str(run_keys[1]).casefold() in {"keyr", "r"}
    )


def is_windows_run_focus_preflight(action: Mapping[str, Any]) -> bool:
    """Return whether one action harmlessly clears stale desktop focus."""

    keys = action.get("keys")
    return (
        action.get("type") == "key"
        and isinstance(keys, list)
        and len(keys) == 1
        and str(keys[0]).casefold() in {"escape", "esc"}
    )


def is_verified_windows_run_launch(
    actions: Sequence[Mapping[str, Any]],
) -> bool:
    """Recognise one atomic Win+R, exact text, Enter launch.

    Requiring the focus gesture in the same burst prevents an exact but
    misplaced string from being committed in chat, email, or a previously
    focused field.
    """

    active_actions = [
        (index, action)
        for index, action in enumerate(actions)
        if action.get("type") not in _PASSIVE_ACTION_TYPES
    ]
    has_focus_preflight = (
        len(active_actions) == 4
        and is_windows_run_focus_preflight(active_actions[0][1])
    )
    if has_focus_preflight:
        active_actions = active_actions[1:]
    if len(active_actions) != 3:
        return False
    (run_index, run_key), (_, typed), (_, submit_key) = active_actions
    if not (
        (run_index == 0 or has_focus_preflight)
        and is_windows_run_key_action(run_key)
    ):
        return False
    text = typed.get("text")
    if not (
        typed.get("type") == "type_text"
        and isinstance(text, str)
        and typed.get("context") == "field"
        and typed.get("verification") == "exact"
        and not typed.get("code", False)
        and not typed.get("secret", False)
        and is_safe_windows_run_text(text)
    ):
        return False
    submit_keys = submit_key.get("keys")
    return (
        submit_key.get("type") == "key"
        and isinstance(submit_keys, list)
        and len(submit_keys) == 1
        and str(submit_keys[0]).casefold() in {"enter", "return"}
    )
