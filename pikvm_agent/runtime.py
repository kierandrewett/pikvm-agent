"""Runtime composition + session lifecycle.

Libraries are instantiated inside our runtime; they never call each other or
PiKVM directly. The Runtime owns the backend, the shared services (screen
parser, operator, policy, the compiled LangGraph), the session store, and
per-session frame/trace/deps state. ``continue_session`` drives the graph until
the next approval interrupt or completion; ``submit_approval`` resumes it.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.types import Command

from pikvm_agent.config import AppConfig, load_config
from pikvm_agent.core.errors import SessionNotFoundError
from pikvm_agent.executor.recovery import Recovery
from pikvm_agent.executor.transactions import GuardedTransactionExecutor
from pikvm_agent.graph.checkpoints import build_checkpointer, close_checkpointer
from pikvm_agent.graph.deps import GraphDeps
from pikvm_agent.graph.graph import build_graph
from pikvm_agent.operator.fake import FakeOperator
from pikvm_agent.pikvm.client import PiKVMBackend
from pikvm_agent.pikvm.fake import FakeBackend
from pikvm_agent.policy.safety import SafetyPolicyEngine
from pikvm_agent.store.frames import FrameStore
from pikvm_agent.store.sqlite import SessionStore
from pikvm_agent.store.trace import TraceLog
from pikvm_agent.vision.providers import build_ocr_provider, build_screen_parser

log = logging.getLogger("pikvm_agent.runtime")

DEFAULT_MAX_STEPS = 12


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


def build_operator(config: AppConfig, backend: Any) -> Any:
    """OpenRouter operator when configured + keyed, else the deterministic fake."""
    op = config.operator
    if op.provider == "openrouter" and op.api_key:
        from pikvm_agent.operator.openrouter import OpenRouterOperator

        log.info("Using OpenRouterOperator (lanes: %s)", ", ".join(op.lanes))
        return OpenRouterOperator(op)
    log.info("Using FakeOperator (operator.provider=%s)", op.provider)
    return FakeOperator()


@dataclass
class SessionRuntime:
    session_id: str
    task: str
    frames: FrameStore
    trace: TraceLog
    deps: GraphDeps
    started: bool = False
    status: str = "running"
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


class Runtime:
    def __init__(self, config: AppConfig, store: SessionStore, backend: Any, *,
                 screen_parser: Any, operator: Any, policy: SafetyPolicyEngine,
                 graph: Any, checkpointer: Any, executor: Any, recovery: Any) -> None:
        self.config = config
        self.store = store
        self.backend = backend
        self._screen_parser = screen_parser
        self._operator = operator
        self._policy = policy
        self._graph = graph
        self._checkpointer = checkpointer
        self._executor = executor
        self._recovery = recovery
        self._sessions: dict[str, SessionRuntime] = {}

    @classmethod
    async def from_config(cls, config: AppConfig | None = None) -> "Runtime":
        config = config or load_config()
        store = SessionStore(config.daemon.sqlite_path)
        await store.connect()
        backend = build_backend(config)
        screen_parser = build_screen_parser(config, backend)
        operator = build_operator(config, backend)
        policy = SafetyPolicyEngine(config.policy)
        ocr = build_ocr_provider(config, backend)
        from pikvm_agent.executor.typing import WatchedTyper

        typer = WatchedTyper(backend, ocr)
        executor = GuardedTransactionExecutor(backend, ocr, typer=typer)
        recovery = Recovery(backend)
        graph_db = str(Path(config.daemon.sqlite_path).with_name("graph.sqlite3"))
        checkpointer = await build_checkpointer(graph_db)
        graph = build_graph(checkpointer)
        return cls(config, store, backend, screen_parser=screen_parser, operator=operator,
                   policy=policy, graph=graph, checkpointer=checkpointer,
                   executor=executor, recovery=recovery)

    async def aclose(self) -> None:
        try:
            await self.backend.aclose()
        finally:
            await close_checkpointer(self._checkpointer)
            await self.store.close()

    def _get(self, session_id: str) -> SessionRuntime:
        sr = self._sessions.get(session_id)
        if sr is None:
            raise SessionNotFoundError(session_id)
        return sr

    def _graph_config(self, sr: SessionRuntime) -> dict[str, Any]:
        return {"configurable": {"deps": sr.deps, "thread_id": sr.session_id}}

    # ---- lifecycle -------------------------------------------------------- #

    async def start_session(self, task: str, policy: dict | None = None,
                            operator: dict | None = None) -> dict[str, Any]:
        session_id = "s_" + uuid.uuid4().hex[:12]
        row = await self.store.create_session(session_id, task, policy or {}, operator or {})
        frames = FrameStore(session_id, self.config.daemon.session_dir, self.backend,
                            fp_meaningful=self.config.watchers.fp_meaningful)
        trace = TraceLog(session_id, self.config.daemon.session_dir)
        trace.append("session_start", task=task, policy=policy or {}, operator=operator or {})
        deps = GraphDeps(
            backend=self.backend, frames=frames, trace=trace,
            screen_parser=self._screen_parser, operator=self._operator, policy=self._policy,
            execute=self._executor.execute, recovery=self._recovery,
            max_steps=DEFAULT_MAX_STEPS,
        )
        self._sessions[session_id] = SessionRuntime(
            session_id=session_id, task=task, frames=frames, trace=trace, deps=deps
        )
        return {"session_id": session_id, "status": row["status"], "task": task,
                "created_at": row["created_at"]}

    async def get_session_summary(self, session_id: str) -> dict[str, Any]:
        """pikvm_observe: capture the current screen and report frame metadata."""
        sr = self._get(session_id)
        try:
            await self.backend.connect()
        except Exception as exc:  # noqa: BLE001
            log.warning("backend.connect failed: %s", exc)
        frame = await sr.frames.capture()
        sr.trace.append("observe", frame_id=frame.frame_id, world_version=frame.world_version,
                        screenshot_path=frame.image_path)
        row = await self.store.get_session(session_id)
        status = row["status"] if row else sr.status
        return {
            "session_id": session_id, "status": status, "task": sr.task,
            "frame_id": frame.frame_id, "world_version": frame.world_version,
            "screenshot_path": frame.image_path, "width": frame.width, "height": frame.height,
            "keyboard_state": frame.keyboard_state.model_dump(),
            "events": sr.events[-20:], "error": sr.error,
        }

    async def abort_session(self, session_id: str, reason: str = "") -> dict[str, Any]:
        sr = self._get(session_id)
        sr.status = "failed"
        sr.error = reason or "aborted by human"
        sr.trace.append("abort", reason=reason)
        await self.store.update_session(session_id, status="failed", error=sr.error)
        return {"session_id": session_id, "status": "failed", "reason": reason}

    # ---- operator loop (LangGraph) --------------------------------------- #

    async def continue_session(self, session_id: str) -> dict[str, Any]:
        """Run the graph until the next approval interrupt or completion."""
        sr = self._get(session_id)
        try:
            await self.backend.connect()
        except Exception as exc:  # noqa: BLE001
            log.warning("backend.connect failed: %s", exc)
        config = self._graph_config(sr)
        if not sr.started:
            sr.started = True
            initial = {"session_id": session_id, "task": sr.task, "step": 0,
                       "max_steps": DEFAULT_MAX_STEPS}
            result = await self._graph.ainvoke(initial, config)
        else:
            # Already running/paused without a pending approval — let it proceed.
            result = await self._graph.ainvoke(None, config)
        return await self._after_run(sr, result)

    async def submit_approval(self, session_id: str, approval_id: str,
                             decision: dict) -> dict[str, Any]:
        """Resume a paused graph with the human's approval decision."""
        sr = self._get(session_id)
        # Validate the id matches THIS session's pending approval before resuming —
        # a stale/mistyped id must never approve the current pending action.
        appr = await self.store.get_approval(approval_id)
        if appr is None or appr.get("session_id") != session_id or appr.get("status") != "pending":
            return {"session_id": session_id, "approval_id": approval_id, "status": "error",
                    "error": "unknown or already-resolved approval_id for this session"}
        result = await self._graph.ainvoke(Command(resume=decision), self._graph_config(sr))
        status_word = "approved" if decision.get("type") == "approve" else decision.get("type", "resolved")
        try:
            await self.store.resolve_approval(approval_id, decision, status_word)
        except Exception as exc:  # noqa: BLE001
            log.warning("resolve_approval failed: %s", exc)
        return await self._after_run(sr, result)

    async def _after_run(self, sr: SessionRuntime, result: dict[str, Any]) -> dict[str, Any]:
        base = {
            "session_id": sr.session_id, "task": sr.task,
            "frame_id": result.get("frame_id"), "world_version": result.get("world_version"),
            "screenshot_path": result.get("frame_path"), "step": result.get("step", 0),
        }
        if "__interrupt__" in result:
            appr = result.get("approval_request") or {}
            sr.status = "needs_approval"
            if appr.get("approval_id"):
                await self.store.save_approval(appr["approval_id"], sr.session_id, appr)
            await self.store.update_session(sr.session_id, status="needs_approval")
            return {**base, "status": "needs_approval", "approval_request": appr}
        status = result.get("status", "done")
        sr.status = status
        sr.error = result.get("error", "")
        await self.store.update_session(sr.session_id, status=status, error=sr.error)
        return {**base, "status": status, "error": sr.error}

    async def export_memory_update(self, session_id: str) -> dict[str, Any]:
        self._get(session_id)
        return {"session_id": session_id, "note": "Atlas memory export arrives in Phase 8"}
