"""Built-in PiKVM HID playbooks — named, parameterised burst macros.

A playbook is just a pre-canned burst (list of HID actions) with ``{{arg}}`` holes the
controller fills in. It lets a controller say "open this file in VS Code" in one call
instead of re-spelling Ctrl+P → type → Enter → wait every time. Pure HID; the same
freshness/control/humanisation as any burst applies — playbooks expand to a burst and run
through :meth:`Runtime.run_burst`.

These are conveniences, not magic: they encode the *human* key sequence for common apps.
Add more here as patterns recur. Everything is target-agnostic raw input.
"""

from __future__ import annotations

import re
from typing import Any

PLAYBOOKS: dict[str, list[dict[str, Any]]] = {
    # --- VS Code -------------------------------------------------------------
    "vscode.quick_open_file": [
        {"type": "key", "keys": ["CTRL", "P"]},
        {"type": "wait", "ms": 200},
        {"type": "type_text", "text": "{{path}}", "method": "print"},
        {"type": "key", "keys": ["ENTER"]},
        {"type": "wait_for_stable_screen", "stable_ms": 300, "timeout_ms": 1500},
    ],
    "vscode.command_palette": [
        {"type": "key", "keys": ["CTRL", "SHIFT", "P"]},
        {"type": "wait", "ms": 200},
        {"type": "type_text", "text": "{{command}}"},
        {"type": "wait", "ms": 150},
    ],
    "vscode.find_replace": [
        {"type": "key", "keys": ["CTRL", "H"]},
        {"type": "wait", "ms": 250},
        {"type": "type_text", "text": "{{find}}"},
        {"type": "key", "keys": ["TAB"]},
        {"type": "type_text", "text": "{{replace}}"},
    ],
    "vscode.save": [
        {"type": "key", "keys": ["CTRL", "S"]},
        {"type": "wait_for_stable_screen", "stable_ms": 250, "timeout_ms": 1000},
    ],
    "vscode.focus_terminal": [
        {"type": "key", "keys": ["CTRL", "BACKSLASH"]},
        {"type": "wait", "ms": 200},
    ],
    # --- terminal (type vs submit kept separate ON PURPOSE) ------------------
    "terminal.type_command": [  # types but never submits — review before Enter
        {"type": "type_text", "text": "{{command}}", "method": "print"},
    ],
    "terminal.submit": [
        {"type": "key", "keys": ["ENTER"]},
    ],
    # --- Windows / browser ---------------------------------------------------
    "windows.start_search": [
        {"type": "key", "keys": ["META"]},
        {"type": "wait", "ms": 300},
        {"type": "type_text", "text": "{{query}}"},
        {"type": "wait", "ms": 300},
    ],
    "browser.goto_url": [
        {"type": "key", "keys": ["CTRL", "L"]},
        {"type": "wait", "ms": 150},
        {"type": "type_text", "text": "{{url}}"},
        {"type": "key", "keys": ["ENTER"]},
        {"type": "wait_for_stable_screen", "stable_ms": 400, "timeout_ms": 3000},
    ],
}

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def names() -> list[str]:
    return sorted(PLAYBOOKS)


class UnknownPlaybook(KeyError):
    pass


class MissingPlaybookArg(KeyError):
    pass


def expand(name: str, args: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Resolve a playbook to a concrete list of burst actions. Raises UnknownPlaybook for
    a bad name and MissingPlaybookArg if a ``{{hole}}`` has no value."""
    tpl = PLAYBOOKS.get(name)
    if tpl is None:
        raise UnknownPlaybook(name)
    args = args or {}
    return [_sub(action, args) for action in tpl]


def _sub(obj: Any, args: dict[str, Any]) -> Any:
    if isinstance(obj, str):
        def repl(m: re.Match[str]) -> str:
            key = m.group(1)
            if key not in args:
                raise MissingPlaybookArg(key)
            return str(args[key])
        return _PLACEHOLDER.sub(repl, obj)
    if isinstance(obj, dict):
        return {k: _sub(v, args) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sub(v, args) for v in obj]
    return obj
