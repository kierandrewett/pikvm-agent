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


class _StubTyper:
    """Stand-in watched typer that returns a chosen verification status."""
    def __init__(self, status: str) -> None:
        self.status = status
        self.calls: list[str] = []

    async def type_text(self, text, *, code=False, secret=False, should_continue=None):
        self.calls.append(text)
        class _R:
            pass
        r = _R(); r.status = self.status; r.ok = not self.status.startswith("failed_"); r.summary = "stub"
        return r


async def test_burst_type_text_verifies_and_stops_on_mismatch() -> None:
    # Confirmed-wrong typing must stop the burst BEFORE the following Enter (the Ctrl+F risk).
    be = FakeBackend()
    typer = _StubTyper("failed_focus_lost")
    out = await run_burst(
        [{"type": "type_text", "text": "securityadmin"}, {"type": "key", "keys": ["ENTER"]}],
        backend=be, typer=typer)
    assert out.status == "failed" and out.reason == "type_unverified"
    assert typer.calls == ["securityadmin"]
    assert not any(m == "keypress" for m, _ in be.calls)  # ENTER never ran


async def test_burst_type_text_proceeds_when_verified() -> None:
    be = FakeBackend()
    typer = _StubTyper("verified_exact")
    out = await run_burst(
        [{"type": "type_text", "text": "hi"}, {"type": "key", "keys": ["ENTER"]}],
        backend=be, typer=typer)
    assert out.status == "completed"
    assert any(m == "keypress" for m, _ in be.calls)


async def test_burst_print_method_skips_verify() -> None:
    be = FakeBackend()
    typer = _StubTyper("failed_focus_lost")  # would fail IF consulted
    out = await run_burst([{"type": "type_text", "text": "long", "method": "print"}],
                          backend=be, typer=typer)
    assert out.status == "completed" and typer.calls == []  # fast path didn't use the typer
    assert any(m == "print_text" for m, _ in be.calls)
