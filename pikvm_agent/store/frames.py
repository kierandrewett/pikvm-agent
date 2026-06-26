"""Per-session frame store: numbering, world-versioning, freshness, disk images.

This is where ``frame_id`` and ``world_version`` live. ``world_version`` is the
plan-invalidation counter — bumped whenever a freshly captured full frame
differs meaningfully from the previous one, or the keyboard state changes. A
decision is only valid against the exact ``(frame_id, world_version)`` it cited.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from pikvm_agent.core.models import FrameRecord, KeyboardState, Region
from pikvm_agent.pikvm.client import PiKVMBackend
from pikvm_agent.vision.frame_diff import FP_MEANINGFUL, fingerprint, screen_hash


@dataclass
class _Look:
    fp: np.ndarray
    at_ms: float


class FrameStore:
    def __init__(self, session_id: str, session_dir: str | Path, backend: PiKVMBackend,
                 fp_meaningful: float = FP_MEANINGFUL) -> None:
        self.session_id = session_id
        self.backend = backend
        self.fp_meaningful = fp_meaningful
        self._dir = Path(session_dir) / session_id / "frames"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._frame_seq = 0
        self._world_version = 1
        self._last_fp: np.ndarray | None = None
        self._last_keyboard: tuple | None = None
        self._latest: FrameRecord | None = None
        self._last_look: _Look | None = None

    @property
    def world_version(self) -> int:
        return self._world_version

    def bump_world(self, _reason: str = "") -> int:
        """Explicitly invalidate the current plan (used by watchers/events)."""
        self._world_version += 1
        return self._world_version

    def _keyboard_state(self) -> KeyboardState:
        return KeyboardState(
            layout=self.backend.get_layout(),
            caps_lock=self.backend.get_caps_lock(),
            online=self.backend.is_hid_online(),
        )

    async def capture(self, region: Region | None = None, *, mark_look: bool = True) -> FrameRecord:
        cf = await self.backend.screenshot(region)
        kb = self._keyboard_state()

        if region is None:
            # Fingerprint decodes + resizes the JPEG — offload so it doesn't block the loop.
            fp = await asyncio.to_thread(fingerprint, cf.data)
            kb_sig = (kb.layout, kb.caps_lock)
            if self._last_fp is not None and float(
                np.abs(self._last_fp.astype(np.int32) - fp.astype(np.int32)).sum()
            ) / len(fp) / 255.0 > self.fp_meaningful:
                self._world_version += 1
            elif self._last_keyboard is not None and kb_sig != self._last_keyboard:
                self._world_version += 1
            self._last_fp = fp
            self._last_keyboard = kb_sig
            self._frame_seq += 1
            frame_id = self._frame_seq
            shash = screen_hash(fp)
            if mark_look:
                self._last_look = _Look(fp=fp, at_ms=time.monotonic() * 1000)
        else:
            frame_id = self._frame_seq or 1
            shash = ""

        name = f"frame_{frame_id:06d}.jpg" if region is None else f"crop_{frame_id:06d}_{int(time.monotonic()*1000)}.jpg"
        path = self._dir / name
        await asyncio.to_thread(path.write_bytes, cf.data)

        record = FrameRecord(
            frame_id=frame_id,
            world_version=self._world_version,
            captured_at=cf.captured_at or datetime.now(timezone.utc).isoformat(),
            monotonic_ms=cf.monotonic_ms,
            image_path=str(path),
            image_sha256=cf.sha256,
            screen_hash=shash,
            width=cf.width,
            height=cf.height,
            keyboard_state=kb,
        )
        if region is None:
            self._latest = record
        return record

    def latest(self) -> FrameRecord | None:
        return self._latest

    def mark_look(self) -> None:
        if self._last_fp is not None:
            self._last_look = _Look(fp=self._last_fp, at_ms=time.monotonic() * 1000)

    def look_freshness(self, current_fp: np.ndarray | None) -> dict:
        """Has the screen materially changed since the last full-frame look?
        ``changed`` is True when it changed OR there is no baseline; False when we
        can't tell (no current fingerprint) so we never wrongly block."""
        now = time.monotonic() * 1000
        if self._last_look is None:
            return {"has_baseline": False, "age_ms": float("inf"), "delta": None, "changed": True}
        age = now - self._last_look.at_ms
        if current_fp is None:
            return {"has_baseline": True, "age_ms": age, "delta": None, "changed": False}
        base = self._last_look.fp
        n = min(len(base), len(current_fp))
        delta = float(np.abs(base[:n].astype(np.int32) - current_fp[:n].astype(np.int32)).sum()) / n / 255.0
        return {"has_baseline": True, "age_ms": age, "delta": delta, "changed": delta > self.fp_meaningful}
