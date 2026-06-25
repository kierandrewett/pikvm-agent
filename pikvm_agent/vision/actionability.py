"""Actionability gate — is this element safe to act on *right now*?

A resolved VisualElement is not enough: the world may have moved on, the element
may be off-screen or covered by a modal/toast, or the locator may have matched
more than one thing. The Actionability checker is the single place that answers
"may we click this?". It produces evidence only — policy still owns risk.

Checks run in order; the first failure wins:
    1. same_world  — element planned against the current (world_version, frame_id)
    2. unique      — exactly one candidate matched the locator
    3. visible     — bbox is (mostly) within the frame and has positive area
    4. unobscured  — no modal/toast/notification covers the element's center
    5. safe_target — placeholder; the policy engine owns real risk decisions
"""

from __future__ import annotations

from pydantic import BaseModel

from pikvm_agent.core.models import BBox, ElementMap, VisualElement

# An obscuring overlay of one of these kinds, sitting over the target's center,
# blocks the action.
OBSCURING_KINDS: frozenset[str] = frozenset({"modal", "toast", "notification"})

# An element is "visible" if at least this fraction of its area falls inside the
# frame. A partially clipped control is still clickable; a mostly-off-screen one
# is not.
VISIBLE_AREA_THRESHOLD = 0.8


class ActionabilityResult(BaseModel):
    ok: bool
    reason: str = ""


def center(bbox: BBox) -> tuple[int, int]:
    """The integer center point of a box, in frame-pixel space."""
    return (bbox.x + bbox.w // 2, bbox.y + bbox.h // 2)


def contains(outer: BBox, point: tuple[int, int]) -> bool:
    """True if `point` falls within `outer` (inclusive of edges)."""
    px, py = point
    return (
        outer.x <= px <= outer.x + outer.w
        and outer.y <= py <= outer.y + outer.h
    )


def _visible_fraction(bbox: BBox, frame_width: int, frame_height: int) -> float:
    """Fraction of the bbox's area that lies inside the frame rectangle."""
    area = bbox.area()
    if area <= 0:
        return 0.0
    ix = max(bbox.x, 0)
    iy = max(bbox.y, 0)
    ix2 = min(bbox.x + bbox.w, frame_width)
    iy2 = min(bbox.y + bbox.h, frame_height)
    iw = max(0, ix2 - ix)
    ih = max(0, iy2 - iy)
    return (iw * ih) / area


class Actionability:
    """Decide whether a resolved element may be acted on in the current world."""

    def check(
        self,
        element: VisualElement,
        element_map: ElementMap,
        *,
        current_frame_id: int,
        current_world_version: int,
        frame_width: int,
        frame_height: int,
        candidates: int = 1,
    ) -> ActionabilityResult:
        # 1. same_world: the element must belong to the world we are acting in.
        if element.world_version != current_world_version:
            return ActionabilityResult(ok=False, reason="stale_world")
        if element.frame_id != current_frame_id:
            return ActionabilityResult(ok=False, reason="stale_frame")

        # 2. unique: an ambiguous locator must not be acted on.
        if candidates != 1:
            return ActionabilityResult(ok=False, reason="not_unique")

        # 3. visible: bbox must have positive area and be (mostly) on-screen.
        if _visible_fraction(element.bbox, frame_width, frame_height) < VISIBLE_AREA_THRESHOLD:
            return ActionabilityResult(ok=False, reason="not_visible")

        # 4. unobscured: no modal/toast/notification covers the target's center.
        target_center = center(element.bbox)
        for other in element_map.elements:
            if other.id == element.id:
                continue
            if other.kind in OBSCURING_KINDS and contains(other.bbox, target_center):
                return ActionabilityResult(ok=False, reason=f"obscured_by_{other.kind}")

        # 5. safe_target: placeholder — policy owns risk.
        return ActionabilityResult(ok=True, reason="")
