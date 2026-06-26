"""GuardedTransactionExecutor: action dispatch, locator+actionability, verify."""

from __future__ import annotations

from pikvm_agent.core.models import BBox, ElementMap, GuardedTransaction, RiskAssessment, VisualElement
from pikvm_agent.executor.transactions import GuardedTransactionExecutor
from pikvm_agent.pikvm.fake import FakeBackend
from pikvm_agent.vision.pikvm_ocr import PiKVMOcrProvider


def _tx(actions: list[dict]) -> GuardedTransaction:
    return GuardedTransaction(
        id="t1", session_id="s1", based_on_frame_id=1, based_on_world_version=1,
        intent="x", actions=actions,
        risk=RiskAssessment(level="low", category="navigation", requires_human=False),
    )


def _button_map(extra: list[VisualElement] | None = None) -> ElementMap:
    el = VisualElement(id="e0", frame_id=1, world_version=1,
                       bbox=BBox(x=100, y=100, w=80, h=30), kind="button", text="OK")
    return ElementMap(frame_id=1, world_version=1, elements=[el] + (extra or []))


_STATE = {"frame_id": 1, "world_version": 1, "mode": "unknown"}


async def test_control_change_stops_remaining_actions() -> None:
    # Layer 4: a mid-transaction abort/panic/steer (should_continue flips False) must
    # stop the REST of a multi-action transaction and drop any held keys.
    be = FakeBackend()
    ex = GuardedTransactionExecutor(be, PiKVMOcrProvider(be))
    n = {"calls": 0}

    def gate() -> bool:
        n["calls"] += 1
        return n["calls"] <= 1  # allow the first action's check, refuse the second

    res = await ex.execute(_tx([
        {"type": "keypress", "keys": ["KeyA"]},
        {"type": "keypress", "keys": ["KeyB"]},
    ]), dict(_STATE), should_continue=gate)

    assert res.status == "blocked_by_policy"
    assert res.error == "control_changed"
    assert len(res.executed_actions) == 1  # only the first action ran
    assert any(m == "release_all" for m, _ in be.calls)  # held keys dropped


async def test_keypress_and_scroll_and_wait() -> None:
    be = FakeBackend()
    ex = GuardedTransactionExecutor(be, PiKVMOcrProvider(be))
    res = await ex.execute(_tx([
        {"type": "keypress", "keys": ["ControlLeft", "KeyP"]},
        {"type": "scroll", "direction": "up", "amount": 5},
        {"type": "wait", "ms": 50},
    ]), dict(_STATE))
    assert res.status in ("executed", "verified")
    calls = dict((m, kw) for m, kw in be.calls)
    assert ("keypress" in [m for m, _ in be.calls])
    # E10: scroll carries a real delta (dy=+5 for "up"), never (0,0)
    scroll = next(kw for m, kw in be.calls if m == "scroll")
    assert scroll == {"dx": 0, "dy": 5}


async def test_click_element_resolves_and_clicks() -> None:
    be = FakeBackend()
    ex = GuardedTransactionExecutor(be, PiKVMOcrProvider(be))
    state = {**_STATE, "element_map": _button_map().model_dump()}
    res = await ex.execute(_tx([{"type": "click_element", "element_id": "e0"}]), state)
    assert res.status in ("executed", "verified")
    click = next(kw for m, kw in be.calls if m == "click")
    assert (click["x"], click["y"]) == (140, 115)  # bbox centre


async def test_click_element_obscured_is_blocked() -> None:
    be = FakeBackend()
    ex = GuardedTransactionExecutor(be, PiKVMOcrProvider(be))
    modal = VisualElement(id="e1", frame_id=1, world_version=1,
                          bbox=BBox(x=50, y=50, w=300, h=300), kind="modal")
    state = {**_STATE, "element_map": _button_map([modal]).model_dump()}
    res = await ex.execute(_tx([{"type": "click_element", "element_id": "e0"}]), state)
    assert res.status == "failed" and "actionable" in res.error
    assert not any(m == "click" for m, _ in be.calls)  # the obscured click never fired


async def test_click_element_missing_is_blocked() -> None:
    be = FakeBackend()
    ex = GuardedTransactionExecutor(be, PiKVMOcrProvider(be))
    state = {**_STATE, "element_map": _button_map().model_dump()}
    res = await ex.execute(_tx([{"type": "click_element", "element_id": "nope"}]), state)
    assert res.status == "failed" and "not found" in res.error


async def test_type_text_verified_via_readback() -> None:
    be = FakeBackend()
    be.ocr_text = "compose a friendly note to the team"
    ex = GuardedTransactionExecutor(be, PiKVMOcrProvider(be))
    res = await ex.execute(_tx([{"type": "type_text",
                                 "text": "compose a friendly note to the team"}]), dict(_STATE))
    assert res.verification is not None and res.verification.verified
    assert any(m == "type_text" for m, _ in be.calls)


async def test_type_text_confident_mismatch_fails() -> None:
    # A confident layout slip (alnum identical, symbols differ) is a hard fail,
    # unlike a wrong-region read which is merely unverified.
    be = FakeBackend()
    be.ocr_text = "ls ~ sort"  # intended pipe came out as ~
    ex = GuardedTransactionExecutor(be, PiKVMOcrProvider(be))
    res = await ex.execute(_tx([{"type": "type_text", "text": "ls | sort"}]), dict(_STATE))
    assert res.status == "failed" and res.verification is not None
    assert res.verification.status == "failed_keyboard_layout"
