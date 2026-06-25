"""Playwright-style visual locator.

Resolves a declarative locator (id / text / kind / region / app_hint) against a
grounded ElementMap. It only *produces a candidate*; it never decides whether
that candidate is safe to act on — `actionability.Actionability` owns the gate,
including the uniqueness check. A multi-match resolve returns None on purpose so
that callers must consult `resolve_all` to see (and report) the ambiguity.
"""

from __future__ import annotations

from pydantic import BaseModel

from pikvm_agent.core.models import ElementMap, VisualElement


class LocatorSpec(BaseModel):
    """A declarative description of the element to find. All fields optional."""

    element_id: str | None = None
    text: str | None = None
    kind: str | None = None
    region: str | None = None
    app_hint: str | None = None


class VisualLocator:
    """Resolve a LocatorSpec against an ElementMap into a single VisualElement."""

    @staticmethod
    def _coerce(spec: str | dict | LocatorSpec) -> LocatorSpec:
        if isinstance(spec, LocatorSpec):
            return spec
        if isinstance(spec, str):
            return LocatorSpec(element_id=spec)
        return LocatorSpec(**spec)

    def resolve_all(
        self, spec: str | dict | LocatorSpec, element_map: ElementMap
    ) -> list[VisualElement]:
        """Return every element matching the spec (so callers can see ambiguity)."""
        locator = self._coerce(spec)

        # Exact id match short-circuits everything else.
        if locator.element_id is not None:
            hit = element_map.by_id(locator.element_id)
            return [hit] if hit is not None else []

        candidates: list[VisualElement] = list(element_map.elements)

        if locator.text is not None:
            needle = locator.text.casefold()
            candidates = [el for el in candidates if _text_matches(el, needle)]

        if locator.kind is not None:
            candidates = [el for el in candidates if el.kind == locator.kind]

        if locator.app_hint is not None:
            candidates = [el for el in candidates if el.app_hint == locator.app_hint]

        return candidates

    def resolve(
        self, spec: str | dict | LocatorSpec, element_map: ElementMap
    ) -> VisualElement | None:
        """Return the single matching element, or None if zero or ambiguous (many)."""
        candidates = self.resolve_all(spec, element_map)
        if len(candidates) == 1:
            return candidates[0]
        return None


def _text_matches(element: VisualElement, needle_casefold: str) -> bool:
    """Case-insensitive substring match against an element's text or caption."""
    for haystack in (element.text, element.caption):
        if haystack is not None and needle_casefold in haystack.casefold():
            return True
    return False
