"""Playbook expansion + run_playbook through the runtime."""

from __future__ import annotations

import pytest

from pikvm_agent.executor import playbooks
from pikvm_agent.runtime import Runtime


def test_expand_substitutes_args() -> None:
    actions = playbooks.expand("vscode.quick_open_file", {"path": "src/app.ts"})
    assert any(a.get("text") == "src/app.ts" for a in actions)
    # Ctrl+P leads, Enter follows somewhere.
    assert actions[0] == {"type": "key", "keys": ["CTRL", "P"]}
    assert {"type": "key", "keys": ["ENTER"]} in actions


def test_expand_unknown_playbook_raises() -> None:
    with pytest.raises(playbooks.UnknownPlaybook):
        playbooks.expand("nope.not_real", {})


def test_expand_missing_arg_raises() -> None:
    with pytest.raises(playbooks.MissingPlaybookArg):
        playbooks.expand("vscode.quick_open_file", {})  # no {{path}}


def test_names_lists_builtins() -> None:
    n = playbooks.names()
    assert "vscode.quick_open_file" in n and "terminal.type_command" in n


async def test_run_playbook_executes_burst(runtime: Runtime) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    shot = await runtime.get_session_summary(sid, capture=True)
    res = await runtime.run_playbook(
        sid, "vscode.quick_open_file", {"path": "readme.md"},
        based_on_world_version=shot["world_version"],
        based_on_control_epoch=shot["control_epoch"])
    assert res["status"] == "completed"
    # Ctrl+P + Enter both went through as keypresses.
    pressed = [kw["keys"] for m, kw in runtime.backend.calls if m == "keypress"]
    assert ["ControlLeft", "KeyP"] in pressed and ["Enter"] in pressed


async def test_run_playbook_unknown_returns_available(runtime: Runtime) -> None:
    sid = (await runtime.start_session("direct"))["session_id"]
    res = await runtime.run_playbook(sid, "bogus.thing", {})
    assert res["status"] == "failed" and "available" in res
    assert "vscode.quick_open_file" in res["available"]


def test_windows_run_expands_and_waits_before_typing() -> None:
    # The reliability contract: open Run, then a settle/wait BEFORE type_text, then submit.
    actions = playbooks.expand("windows.run", {"command": "calc"})
    kinds = [a["type"] for a in actions]
    assert kinds[0] == "key" and actions[0]["keys"] == ["META", "r"]
    ti = kinds.index("type_text")
    assert "wait_for_stable_screen" in kinds[:ti]  # we settle before typing
    assert actions[ti]["text"] == "calc"
    assert {"type": "key", "keys": ["ENTER"]} in actions  # and submit


def test_windows_run_dialog_does_not_submit() -> None:
    actions = playbooks.expand("windows.run_dialog", {"command": "notepad"})
    assert not any(a.get("type") == "key" and a.get("keys") == ["ENTER"] for a in actions)
