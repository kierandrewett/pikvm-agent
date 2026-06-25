"""Runtime composition + session lifecycle.

Libraries are instantiated inside our runtime; they never call each other or
PiKVM directly. The Runtime owns the backend, the session store, and per-session
frame/trace state. The LangGraph operator loop is wired in Phase 3; Phase 1
exposes session creation + observation (the "own the shell" milestone).
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from pikvm_agent.config import AppConfig, load_config
from pikvm_agent.core.errors import SessionNotFoundError
from pikvm_agent.pikvm.client import PiKVMBackend
from pikvm_agent.pikvm.fake import FakeBackend
from pikvm_agent.store.frames import FrameStore
from pikvm_agent.store.sqlite import SessionStore
from pikvm_agent.store.trace import TraceLog

log = logging.getLogger("pikvm_agent.runtime")


def build_backend(config: AppConfig) -> Any:
    """Pick a backend: real PiKVM when credentials are present, else the fake."""
    if os.environ.get("PIKVM_AGENT_FAKE") == "1":
        log.info("PIKVM_AGENT_FAKE=1 — using FakeBackend")
        return FakeBackend()
    pk = config.pikvm
    if pk.username or pk.token:
        log.info("Using PiKVMBackend at %s", pk.base_url)
        return PiKVMBackend(pk)
    log.warning("No PiKVM credentials (%s/%s/%s unset) — using FakeBackend",
                pk.username_env, pk.password_env, pk.token_env)
    return FakeBackend()


@dataclass
class SessionRuntime:
    session_id: str
    task: str
    frames: FrameStore
    trace: TraceLog
    status: str = "running"
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


class Runtime:
    def __init__(self, config: AppConfig, store: SessionStore, backend: Any) -> None:
        self.config = config
        self.store = store
        self.backend = backend
        self._sessions: dict[str, SessionRuntime] = {}

    @classmethod
    async def from_config(cls, config: AppConfig | None = None) -> "Runtime":
        config = config or load_config()
        store = SessionStore(config.daemon.sqlite_path)
        await store.connect()
        backend = build_backend(config)
        return cls(config, store, backend)

    async def aclose(self) -> None:
        try:
            await self.backend.aclose()
        finally:
            await self.store.close()

    def _get(self, session_id: str) -> SessionRuntime:
        sr = self._sessions.get(session_id)
        if sr is None:
            raise SessionNotFoundError(session_id)
        return sr

    # ---- lifecycle -------------------------------------------------------- #

    async def start_session(self, task: str, policy: dict | None = None,
                            operator: dict | None = None) -> dict[str, Any]:
        session_id = "s_" + uuid.uuid4().hex[:12]
        policy = policy or {}
        operator = operator or {}
        row = await self.store.create_session(session_id, task, policy, operator)
        frames = FrameStore(session_id, self.config.daemon.session_dir, self.backend,
                            fp_meaningful=self.config.watchers.fp_meaningful)
        trace = TraceLog(session_id, self.config.daemon.session_dir)
        trace.append("session_start", task=task, policy=policy, operator=operator)
        self._sessions[session_id] = SessionRuntime(
            session_id=session_id, task=task, frames=frames, trace=trace
        )
        return {
            "session_id": session_id,
            "status": row["status"],
            "task": task,
            "created_at": row["created_at"],
        }

    async def get_session_summary(self, session_id: str) -> dict[str, Any]:
        """pikvm_observe: capture the current screen and report frame metadata."""
        sr = self._get(session_id)
        try:
            await self.backend.connect()
        except Exception as exc:  # noqa: BLE001 - degrade to whatever capture can do
            log.warning("backend.connect failed: %s", exc)
        frame = await sr.frames.capture()
        sr.trace.append(
            "observe",
            frame_id=frame.frame_id,
            world_version=frame.world_version,
            screenshot_path=frame.image_path,
        )
        row = await self.store.get_session(session_id)
        status = row["status"] if row else sr.status
        return {
            "session_id": session_id,
            "status": status,
            "task": sr.task,
            "frame_id": frame.frame_id,
            "world_version": frame.world_version,
            "screenshot_path": frame.image_path,
            "width": frame.width,
            "height": frame.height,
            "keyboard_state": frame.keyboard_state.model_dump(),
            "events": sr.events[-20:],
            "error": sr.error,
        }

    async def abort_session(self, session_id: str, reason: str = "") -> dict[str, Any]:
        sr = self._get(session_id)
        sr.status = "failed"
        sr.error = reason or "aborted by human"
        sr.trace.append("abort", reason=reason)
        await self.store.update_session(session_id, status="failed", error=sr.error)
        return {"session_id": session_id, "status": "failed", "reason": reason}

    # ---- placeholders wired in later phases ------------------------------- #

    async def continue_session(self, session_id: str) -> dict[str, Any]:
        """Advance the operator loop. The LangGraph runner lands in Phase 3; until
        then this performs a single observation so the session stays inspectable."""
        summary = await self.get_session_summary(session_id)
        summary["note"] = "operator loop arrives in Phase 3 (LangGraph)"
        return summary

    async def submit_approval(self, session_id: str, approval_id: str,
                             decision: dict) -> dict[str, Any]:
        self._get(session_id)
        return {"session_id": session_id, "approval_id": approval_id,
                "note": "approval resolution wired with the graph in Phase 3"}

    async def export_memory_update(self, session_id: str) -> dict[str, Any]:
        self._get(session_id)
        return {"session_id": session_id,
                "note": "Atlas memory export arrives in Phase 8"}
