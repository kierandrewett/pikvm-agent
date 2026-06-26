"""PiKVM HID burst engine — key mapping, dispatch, mid-burst interruption."""

from __future__ import annotations

from pikvm_agent.executor.burst import BurstError, normalize_keys, run_burst
from pikvm_agent.pikvm.fake import FakeBackend


def test_normalize_keys_friendly_and_passthrough() -> None:
    assert normalize_keys(["CTRL", "P"]) == ["ControlLeft", "KeyP"]
    assert normalize_keys(["ctrl", "shift", "k"]) == ["ControlLeft", "ShiftLeft", "KeyK"]
    assert normalize_keys(["META"]) == ["MetaLeft"]
    assert normalize_keys(["ENTER"]) == ["Enter"]
    assert normalize_keys(["F11"]) == ["F11"]
    assert normalize_keys(["5"]) == ["Digit5"]
    # already-valid PiKVM codes pass straight through
    assert normalize_keys(["ControlLeft", "KeyA"]) == ["ControlLeft", "KeyA"]


async def test_run_burst_executes_in_order() -> None:
    be = FakeBackend()
    actions = [
        {"type": "key", "keys": ["CTRL", "P"]},
        {"type": "wait", "ms": 1},
        {"type": "type_text", "text": "readme.md", "method": "print"},
        {"type": "key", "keys": ["ENTER"]},
        {"type": "click", "x": 100, "y": 200},
        {"type": "scroll", "direction": "down", "amount": 3},
    ]
    out = await run_burst(actions, backend=be)
    assert out.status == "completed"
    assert out.completed == out.total == 6
    methods = [m for m, _ in be.calls]
    assert "keypress" in methods and "print_text" in methods and "click" in methods and "scroll" in methods
    # Ctrl+P mapped to PiKVM codes
    kp = next(kw for m, kw in be.calls if m == "keypress")
    assert kw_keys(kp) == ["ControlLeft", "KeyP"]


def kw_keys(kw):
    return kw.get("keys")


async def test_burst_stops_mid_sequence_on_control_change() -> None:
    be = FakeBackend()
    n = {"i": 0}

    def gate() -> bool:
        n["i"] += 1
        return n["i"] <= 2  # allow the first two action-checks, then revoke

    actions = [
        {"type": "key", "keys": ["KeyA"]},
        {"type": "key", "keys": ["KeyB"]},
        {"type": "key", "keys": ["KeyC"]},  # should NOT run
    ]
    out = await run_burst(actions, backend=be, should_continue=gate)
    assert out.status == "interrupted" and out.reason == "control_changed"
    assert out.completed == 2 and out.remaining == 1
    pressed = [kw["keys"] for m, kw in be.calls if m == "keypress"]
    assert pressed == [["KeyA"], ["KeyB"]]  # the third never fired


async def test_burst_deadline_stops_before_next_action() -> None:
    be = FakeBackend()
    # deadline already in the past -> nothing runs.
    out = await run_burst([{"type": "key", "keys": ["KeyA"]}], backend=be, deadline_ms=1.0)
    assert out.status == "interrupted" and out.reason == "deadline" and out.completed == 0


async def test_burst_unknown_action_raises() -> None:
    be = FakeBackend()
    try:
        await run_burst([{"type": "frobnicate"}], backend=be)
        assert False, "expected BurstError"
    except BurstError:
        pass


async def test_burst_backend_failure_is_reported_not_raised() -> None:
    be = FakeBackend()

    async def boom(*_a, **_k):
        raise RuntimeError("hid offline")

    be.keypress = boom  # type: ignore[method-assign]
    out = await run_burst([{"type": "key", "keys": ["KeyA"]}], backend=be)
    assert out.status == "failed" and "hid offline" in out.error
