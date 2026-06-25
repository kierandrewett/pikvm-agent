"""VisualLocator resolution + Actionability gate."""

from __future__ import annotations

from pikvm_agent.core.models import BBox, ElementMap, VisualElement
from pikvm_agent.vision.actionability import Actionability, center, contains
from pikvm_agent.vision.visual_locator import LocatorSpec, VisualLocator

FRAME_ID = 18429
WORLD_VERSION = 702
FRAME_W = 1920
FRAME_H = 1080


def _element(
    el_id: str,
    *,
    bbox: BBox,
    kind: str = "button",
    text: str | None = None,
    caption: str | None = None,
    app_hint: str | None = None,
    frame_id: int = FRAME_ID,
    world_version: int = WORLD_VERSION,
) -> VisualElement:
    return VisualElement(
        id=el_id,
        frame_id=frame_id,
        world_version=world_version,
        bbox=bbox,
        kind=kind,
        text=text,
        caption=caption,
        app_hint=app_hint,
    )


def _map(*elements: VisualElement) -> ElementMap:
    return ElementMap(
        frame_id=FRAME_ID, world_version=WORLD_VERSION, elements=list(elements)
    )


# --------------------------------------------------------------------------- #
# VisualLocator
# --------------------------------------------------------------------------- #


def test_resolve_by_id_returns_element():
    el = _element("e1", bbox=BBox(x=10, y=10, w=40, h=20))
    em = _map(el, _element("e2", bbox=BBox(x=200, y=10, w=40, h=20)))
    assert VisualLocator().resolve("e1", em) is el
    assert VisualLocator().resolve({"element_id": "e1"}, em) is el
    assert VisualLocator().resolve(LocatorSpec(element_id="e1"), em) is el


def test_resolve_by_unique_text_returns_element():
    el = _element("save", bbox=BBox(x=10, y=10, w=40, h=20), text="Save")
    em = _map(el, _element("cancel", bbox=BBox(x=200, y=10, w=40, h=20), text="Cancel"))
    # Case-insensitive substring.
    assert VisualLocator().resolve({"text": "sav"}, em) is el


def test_resolve_ambiguous_text_returns_none_but_resolve_all_lists_both():
    a = _element("ok1", bbox=BBox(x=10, y=10, w=40, h=20), text="OK")
    b = _element("ok2", bbox=BBox(x=200, y=10, w=40, h=20), caption="ok, got it")
    em = _map(a, b)
    locator = VisualLocator()
    assert locator.resolve({"text": "ok"}, em) is None
    assert locator.resolve_all({"text": "ok"}, em) == [a, b]


def test_resolve_no_match_returns_none():
    em = _map(_element("e1", bbox=BBox(x=10, y=10, w=40, h=20), text="Save"))
    assert VisualLocator().resolve({"text": "nope"}, em) is None
    assert VisualLocator().resolve_all({"text": "nope"}, em) == []


def test_resolve_filters_by_kind_and_app_hint():
    btn = _element("b", bbox=BBox(x=10, y=10, w=40, h=20), text="Go", kind="button", app_hint="vscode")
    inp = _element("i", bbox=BBox(x=10, y=60, w=40, h=20), text="Go", kind="input", app_hint="browser")
    em = _map(btn, inp)
    locator = VisualLocator()
    assert locator.resolve({"text": "go", "kind": "button"}, em) is btn
    assert locator.resolve({"text": "go", "app_hint": "browser"}, em) is inp


# --------------------------------------------------------------------------- #
# Actionability
# --------------------------------------------------------------------------- #


def _check(element: VisualElement, em: ElementMap, **overrides):
    kwargs = dict(
        current_frame_id=FRAME_ID,
        current_world_version=WORLD_VERSION,
        frame_width=FRAME_W,
        frame_height=FRAME_H,
        candidates=1,
    )
    kwargs.update(overrides)
    return Actionability().check(element, em, **kwargs)


def test_actionability_happy_path_ok():
    el = _element("ok", bbox=BBox(x=100, y=100, w=80, h=30))
    res = _check(el, _map(el))
    assert res.ok is True
    assert res.reason == ""


def test_actionability_stale_world():
    el = _element("ok", bbox=BBox(x=100, y=100, w=80, h=30), world_version=WORLD_VERSION - 1)
    res = _check(el, _map(el))
    assert res.ok is False
    assert "stale" in res.reason


def test_actionability_stale_frame():
    el = _element("ok", bbox=BBox(x=100, y=100, w=80, h=30), frame_id=FRAME_ID - 1)
    res = _check(el, _map(el))
    assert res.ok is False
    assert "stale" in res.reason


def test_actionability_obscured_by_modal():
    target = _element("target", bbox=BBox(x=100, y=100, w=80, h=30))
    # A modal covering the whole frame sits over the target's center.
    modal = _element("modal", bbox=BBox(x=0, y=0, w=FRAME_W, h=FRAME_H), kind="modal")
    res = _check(target, _map(target, modal))
    assert res.ok is False
    assert "obscured" in res.reason


def test_actionability_not_obscured_when_overlay_elsewhere():
    target = _element("target", bbox=BBox(x=100, y=100, w=80, h=30))
    toast = _element("toast", bbox=BBox(x=1700, y=900, w=200, h=80), kind="toast")
    res = _check(target, _map(target, toast))
    assert res.ok is True


def test_actionability_off_screen_not_visible():
    el = _element("off", bbox=BBox(x=FRAME_W + 50, y=100, w=80, h=30))
    res = _check(el, _map(el))
    assert res.ok is False
    assert res.reason == "not_visible"


def test_actionability_zero_area_not_visible():
    el = _element("zero", bbox=BBox(x=100, y=100, w=0, h=0))
    res = _check(el, _map(el))
    assert res.ok is False
    assert res.reason == "not_visible"


def test_actionability_not_unique():
    el = _element("ok", bbox=BBox(x=100, y=100, w=80, h=30))
    res = _check(el, _map(el), candidates=2)
    assert res.ok is False
    assert res.reason == "not_unique"


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #


def test_center_and_contains():
    box = BBox(x=10, y=20, w=40, h=60)
    assert center(box) == (30, 50)
    assert contains(box, (30, 50)) is True
    assert contains(box, (10, 20)) is True  # edge inclusive
    assert contains(box, (5, 50)) is False
