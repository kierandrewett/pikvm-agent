"""GuardedTransactionExecutor — the only thing that turns a decision into HID.

Freshness + policy were already enforced by the graph's execute node before this
runs (it re-observed and world-checked). This executor dispatches each action:
clicks resolve through the visual locator + actionability gate (raw coordinate
clicks are not an action type — they are debug-only by construction); typed text
is verified by the watched typer (or a best-effort read-back); scrolls carry a
real direction+amount (never a no-op). It returns a TransactionResult whose
verification is produced by the verifier — never asserted by anything else.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pikvm_agent.pikvm import timing

from pikvm_agent.core.models import (
    ElementMap,
    GuardedTransaction,
    TransactionResult,
    VerificationResult,
)
from pikvm_agent.executor.verification import verify_text
from pikvm_agent.vision.actionability import Actionability
from pikvm_agent.vision.visual_locator import VisualLocator

# direction -> (dx, dy) per the PiKVM convention (dy>0 up, dx>0 right).
_SCROLL_VECTORS = {
    "up": (0, 1),
    "down": (0, -1),
    "right": (1, 0),
    "left": (-1, 0),
}


class GuardedTransactionExecutor:
    def __init__(self, backend: Any, ocr: Any, *, typer: Any = None,
                 locator: VisualLocator | None = None,
                 actionability: Actionability | None = None) -> None:
        self.backend = backend
        self.ocr = ocr
        self.typer = typer
        self.locator = locator or VisualLocator()
        self.actionability = actionability or Actionability()

    async def execute(self, tx: GuardedTransaction, state: dict[str, Any],
                      should_continue: Callable[[], bool] | None = None) -> TransactionResult:
        # The graph passes the live control gate through the state (keeps deps.execute a
        # 2-arg contract); an explicit arg still wins for direct callers/tests.
        if should_continue is None:
            should_continue = state.get("_should_continue")
        # Human reaction time: a brief "saw the screen, now act" pause before touching
        # HID. Skipped under the fake backend so tests stay fast.
        if tx.actions and not os.environ.get("PIKVM_AGENT_FAKE"):
            await asyncio.sleep(timing.reaction_s())
        element_map = ElementMap.model_validate(
            state.get("element_map") or {"frame_id": tx.based_on_frame_id,
                                         "world_version": tx.based_on_world_version}
        )
        executed: list[dict[str, Any]] = []
        verification: VerificationResult | None = None

        for action in tx.actions:
            # Per-ACTION control gate: an abort / panic / steer mid-transaction stops the
            # remaining actions (and type_text is itself interruptible mid-text below).
            if should_continue is not None and not should_continue():
                await self._release_all_quietly()
                return TransactionResult(
                    status="blocked_by_policy", executed_actions=executed,
                    verification=verification, error="control_changed")
            a = action if isinstance(action, dict) else action.model_dump()
            kind = a.get("type")
            if kind == "keypress":
                await self.backend.keypress(a["keys"])
            elif kind == "type_text":
                verification = await self._type_and_verify(a["text"], state, should_continue)
                if not verification.verified and verification.status not in (
                    "unverified_truncated", "unverified_ambiguous", "unverified_wrong_region"
                ):
                    executed.append(a)
                    return TransactionResult(
                        status="failed", executed_actions=executed, verification=verification,
                        error=f"typing verification: {verification.status}",
                    )
            elif kind == "click_element":
                blocked = await self._click_element(a, element_map, state)
                if blocked is not None:
                    executed.append(a)
                    return TransactionResult(status="failed", executed_actions=executed,
                                             error=blocked)
            elif kind == "scroll":
                ux, uy = _SCROLL_VECTORS.get(a["direction"], (0, -1))
                amount = max(1, int(a.get("amount", 3)))  # E10: never a (0,0) no-op
                await self.backend.scroll(ux * amount, uy * amount)
            elif kind == "wait":
                await asyncio.sleep(max(0, int(a.get("ms", 0))) / 1000.0)
            elif kind == "wait_for_mode":
                reached = await self._wait_for_mode(a["mode"], int(a.get("timeout_ms", 1000)))
                if not reached:
                    executed.append(a)
                    return TransactionResult(
                        status="failed", executed_actions=executed,
                        error=f"mode '{a['mode']}' not reached before typing/acting",
                    )
            executed.append(a)

        return TransactionResult(
            status="verified" if (verification and verification.verified) else "executed",
            executed_actions=executed,
            verification=verification,
            world_version_after=tx.based_on_world_version,
        )

    async def _wait_for_mode(self, target: str, timeout_ms: int) -> bool:
        """Observe + detect until the screen reaches ``target`` mode, or timeout.
        Prevents typing into the wrong target if a shortcut is delayed/swallowed."""
        from pikvm_agent.vision.mode_detector import detect_mode

        deadline = asyncio.get_event_loop().time() + min(timeout_ms, 10000) / 1000.0
        while True:
            frame = await self.backend.screenshot()
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
                fh.write(frame.data)
                tmp = Path(fh.name)
            try:
                text = (await self.ocr.ocr(tmp)).text
            finally:
                tmp.unlink(missing_ok=True)
            if detect_mode(text, None) == target:
                return True
            if asyncio.get_event_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.15)

    async def _release_all_quietly(self) -> None:
        rel = getattr(self.backend, "release_all", None)
        if callable(rel):
            with contextlib.suppress(Exception):
                await rel()

    async def _type_and_verify(self, text: str, state: dict[str, Any],
                               should_continue: Callable[[], bool] | None = None) -> VerificationResult:
        if self.typer is not None:
            result = await self.typer.type_text(text, should_continue=should_continue)
            return VerificationResult(
                status=getattr(result, "status", "unverified_ambiguous"),
                safe_to_continue=getattr(result, "ok", False),
                intended=text, observed=getattr(result, "field_text", None) or "",
                detail=getattr(result, "summary", ""),
            )
        # Fallback: type, then a best-effort full-frame read-back + verify.
        await self.backend.type_text(text)
        frame = await self.backend.screenshot()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
            fh.write(frame.data)
            tmp = Path(fh.name)
        try:
            observed = (await self.ocr.ocr(tmp)).text
        finally:
            tmp.unlink(missing_ok=True)
        return verify_text(text, observed, state.get("mode"))

    async def _click_element(self, action: dict[str, Any], element_map: ElementMap,
                             state: dict[str, Any]) -> str | None:
        spec = action.get("element_id") or action.get("locator")
        if not spec:
            return "click_element needs an element_id or locator"
        candidates = self.locator.resolve_all(spec, element_map)
        element = self.locator.resolve(spec, element_map)
        if element is None:
            return f"element not found or ambiguous ({len(candidates)} matches)"
        dims = self.backend.get_dimensions()
        check = self.actionability.check(
            element, element_map,
            current_frame_id=state.get("frame_id", element.frame_id),
            current_world_version=state.get("world_version", element.world_version),
            frame_width=dims["width"], frame_height=dims["height"],
            candidates=len(candidates),
        )
        if not check.ok:
            return f"not actionable: {check.reason}"
        cx, cy = element.bbox.center()
        await self.backend.click(cx, cy)
        return None
