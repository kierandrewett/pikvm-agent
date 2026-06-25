"""Recovery flows — offline, deterministic, asserted against FakeBackend.calls.

Every recovery must be safe: it may quit a pager, dismiss an overlay, or refocus
a target, but it must NEVER press a submit/confirm key (Enter / NumpadEnter).
"""

from __future__ import annotations

import pytest

from pikvm_agent.core.models import BBox, ElementMap, VisualElement
from pikvm_agent.executor.recovery import RECOVERABLE_MODES, Recovery
from pikvm_agent.pikvm.fake import FakeBackend

FRAME_ID = 4242
WORLD_VERSION = 7


def _element(
    el_id: str,
    *,
    kind: str = "button",
    bbox: BBox | None = None,
) -> VisualElement:
    return VisualElement(
        id=el_id,
        frame_id=FRAME_ID,
        world_version=WORLD_VERSION,
        bbox=bbox or BBox(x=100, y=100, w=40, h=20),
        kind=kind,
    )


def _map(*elements: VisualElement) -> ElementMap:
    return ElementMap(
        frame_id=FRAME_ID,
        world_version=WORLD_VERSION,
        elements=list(elements),
    )


def _press_keys(backend: FakeBackend) -> list[str]:
    """All key codes the backend was asked to press (press_key + keypress)."""
    codes: list[str] = []
    for method, kw in backend.calls:
        if method == "press_key":
            codes.append(kw["code"])
        elif method == "keypress":
            codes.extend(kw["keys"])
    return codes


def _assert_no_submit(backend: FakeBackend) -> None:
    assert not ({"Enter", "NumpadEnter"} & set(_press_keys(backend)))


# --------------------------------------------------------------------------- #
# Individual flows
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_recover_pager_presses_q() -> None:
    backend = FakeBackend()
    result = await Recovery(backend).recover_pager()

    assert result == {"action": "pager_quit", "ok": True}
    assert ("press_key", {"code": "KeyQ"}) in backend.calls
    _assert_no_submit(backend)


@pytest.mark.asyncio
async def test_dismiss_modal_clicks_close_button_centre() -> None:
    backend = FakeBackend()
    close = _element("close-1", kind="close_button", bbox=BBox(x=200, y=50, w=20, h=20))
    result = await Recovery(backend).dismiss_modal(_map(_element("m", kind="modal"), close))

    assert result == {"action": "dismiss", "method": "click", "ok": True}
    cx, cy = close.bbox.center()
    assert ("click", {"x": cx, "y": cy, "button": "left"}) in backend.calls
    _assert_no_submit(backend)


@pytest.mark.asyncio
async def test_dismiss_modal_without_close_button_presses_escape() -> None:
    backend = FakeBackend()
    result = await Recovery(backend).dismiss_modal(_map(_element("m", kind="modal")))

    assert result == {"action": "dismiss", "method": "escape", "ok": True}
    assert ("press_key", {"code": "Escape"}) in backend.calls
    assert all(method != "click" for method, _ in backend.calls)
    _assert_no_submit(backend)


@pytest.mark.asyncio
async def test_dismiss_modal_no_element_map_presses_escape() -> None:
    backend = FakeBackend()
    result = await Recovery(backend).dismiss_modal(None)

    assert result == {"action": "dismiss", "method": "escape", "ok": True}
    assert ("press_key", {"code": "Escape"}) in backend.calls
    _assert_no_submit(backend)


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_recover_terminal_pager_dispatches_to_pager_quit() -> None:
    backend = FakeBackend()
    result = await Recovery(backend).recover("terminal.pager")

    assert result == {"action": "pager_quit", "ok": True}
    assert ("press_key", {"code": "KeyQ"}) in backend.calls
    _assert_no_submit(backend)


@pytest.mark.asyncio
async def test_recover_modal_element_map_dispatches_to_dismiss() -> None:
    backend = FakeBackend()
    element_map = _map(_element("toast-1", kind="toast"))
    result = await Recovery(backend).recover("unknown", element_map)

    assert result["action"] == "dismiss"
    assert result["ok"] is True
    # No grounded close_button -> Escape.
    assert ("press_key", {"code": "Escape"}) in backend.calls
    _assert_no_submit(backend)


@pytest.mark.asyncio
async def test_recover_modal_mode_dispatches_to_dismiss() -> None:
    backend = FakeBackend()
    result = await Recovery(backend).recover("windows.update_modal")

    assert result["action"] == "dismiss"
    assert result["ok"] is True
    _assert_no_submit(backend)


@pytest.mark.asyncio
async def test_recover_unrecoverable_mode_is_noop() -> None:
    backend = FakeBackend()
    result = await Recovery(backend).recover("vscode.editor")

    assert result["ok"] is False
    assert result["action"] == "none"
    assert "no recovery for mode" in result["reason"]
    # No keys, no clicks, no mouse moves were emitted.
    assert backend.calls == []
    _assert_no_submit(backend)


# --------------------------------------------------------------------------- #
# Refocus
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_refocus_clicks_element_centre() -> None:
    backend = FakeBackend()
    el = _element("editor", kind="editor", bbox=BBox(x=300, y=400, w=100, h=50))
    result = await Recovery(backend).refocus(el)

    assert result == {"action": "refocus", "ok": True}
    cx, cy = el.bbox.center()
    assert ("click", {"x": cx, "y": cy, "button": "left"}) in backend.calls
    _assert_no_submit(backend)


@pytest.mark.asyncio
async def test_refocus_without_target_is_noop() -> None:
    backend = FakeBackend()
    result = await Recovery(backend).refocus(None)

    assert result["ok"] is False
    assert result["reason"] == "no target"
    assert backend.calls == []


# --------------------------------------------------------------------------- #
# Invariants
# --------------------------------------------------------------------------- #


def test_recoverable_modes_constant_shape() -> None:
    assert RECOVERABLE_MODES["terminal.pager"] == "pager_quit"
    assert RECOVERABLE_MODES["windows.update_modal"] == "dismiss"
    assert RECOVERABLE_MODES["system.notification"] == "dismiss"


@pytest.mark.asyncio
async def test_no_recovery_ever_presses_a_submit_key() -> None:
    # Exercise every recovery path on one fresh backend each, then assert no
    # Enter/NumpadEnter was ever recorded.
    for coro_factory in (
        lambda r: r.recover_pager(),
        lambda r: r.dismiss_modal(_map(_element("c", kind="close_button"))),
        lambda r: r.dismiss_modal(None),
        lambda r: r.recover("terminal.pager"),
        lambda r: r.recover("windows.update_modal"),
        lambda r: r.recover("system.notification"),
        lambda r: r.recover("vscode.editor"),
        lambda r: r.refocus(_element("e", kind="editor")),
        lambda r: r.refocus(None),
    ):
        backend = FakeBackend()
        await coro_factory(Recovery(backend))
        _assert_no_submit(backend)
