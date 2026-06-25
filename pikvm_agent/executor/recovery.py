"""Deterministic recovery flows for stuck / blocking screen states.

These are *escape hatches*, not decisions: when the runtime detects a known
blocking state (a terminal pager, a modal/credential/notification overlay, lost
focus) it runs a small, fixed recovery and then re-observes — the graph decides
what to do next from the fresh frame.

Recovery NEVER submits: it never presses ``Enter`` / ``NumpadEnter`` or any other
confirm/send key. The most it does is quit a pager, dismiss an overlay, or
re-focus a target. Only the injected ``backend`` performs I/O; this module is
pure dispatch logic with no network.
"""

from __future__ import annotations

from typing import Any

from pikvm_agent.core.models import ElementMap, Mode, VisualElement

# --------------------------------------------------------------------------- #
# Recoverable modes
# --------------------------------------------------------------------------- #

# Modes the runtime knows how to unstick, mapped to the recovery action name.
# Anything not listed here has no deterministic recovery (the operator must
# decide). Kept as a module constant so callers can gate on it cheaply.
RECOVERABLE_MODES: dict[Mode, str] = {
    "terminal.pager": "pager_quit",
    "windows.update_modal": "dismiss",
    "system.notification": "dismiss",
}

# Element kinds whose mere presence implies a dismissable overlay.
_OVERLAY_KINDS: frozenset[str] = frozenset(
    {"modal", "toast", "notification", "close_button"}
)

# Modes that present a dismissable overlay (modal / notification surfaces).
_DISMISS_MODES: frozenset[str] = frozenset(
    {"windows.update_modal", "system.notification"}
)

# Keys recovery must never emit — these submit/confirm and could fire an action.
_FORBIDDEN_KEYS: frozenset[str] = frozenset({"Enter", "NumpadEnter"})


class Recovery:
    """Run fixed recovery flows against an injected computer backend."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    # ---- individual flows ------------------------------------------------- #

    async def recover_pager(self) -> dict[str, Any]:
        """Quit a terminal pager (less/more/man) by pressing ``q``.

        ``q`` quits every common pager without committing input. The graph
        re-observes afterwards to confirm we returned to a readline prompt.
        """
        await self.backend.press_key("KeyQ")
        return {"action": "pager_quit", "ok": True}

    async def dismiss_modal(
        self, element_map: ElementMap | None = None
    ) -> dict[str, Any]:
        """Dismiss a blocking overlay.

        Prefer clicking a grounded ``close_button`` element; otherwise fall back
        to ``Escape``. Neither path submits anything.
        """
        close = _find_close_button(element_map)
        if close is not None:
            cx, cy = close.bbox.center()
            await self.backend.click(cx, cy)
            return {"action": "dismiss", "method": "click", "ok": True}
        await self.backend.press_key("Escape")
        return {"action": "dismiss", "method": "escape", "ok": True}

    async def refocus(
        self, element: VisualElement | None = None
    ) -> dict[str, Any]:
        """Put input focus on a target by clicking its centre.

        With no target there is nothing safe to click, so this is a no-op.
        """
        if element is None:
            return {"action": "refocus", "ok": False, "reason": "no target"}
        cx, cy = element.bbox.center()
        await self.backend.click(cx, cy)
        return {"action": "refocus", "ok": True}

    # ---- dispatch --------------------------------------------------------- #

    async def recover(
        self, mode: str, element_map: ElementMap | None = None
    ) -> dict[str, Any]:
        """Dispatch to the right recovery for a detected ``mode``.

        - ``terminal.pager`` → quit the pager.
        - a modal / credential / notification screen → dismiss the overlay.
        - anything else → no recovery (the operator decides).
        """
        if mode == "terminal.pager":
            return await self.recover_pager()
        if mode in _DISMISS_MODES or _has_overlay_element(element_map):
            return await self.dismiss_modal(element_map)
        return {
            "action": "none",
            "ok": False,
            "reason": f"no recovery for mode {mode}",
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _find_close_button(element_map: ElementMap | None) -> VisualElement | None:
    """Return the first ``close_button`` element, if any."""
    if element_map is None:
        return None
    for el in element_map.elements:
        if el.kind == "close_button":
            return el
    return None


def _has_overlay_element(element_map: ElementMap | None) -> bool:
    """True if the map contains any dismissable-overlay element kind."""
    if element_map is None:
        return False
    return any(el.kind in _OVERLAY_KINDS for el in element_map.elements)


__all__ = ["Recovery", "RECOVERABLE_MODES"]
