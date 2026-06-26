"""Runtime composition + session lifecycle.

Libraries are instantiated inside our runtime; they never call each other or
PiKVM directly. The Runtime owns the backend, the shared services (screen
parser, operator, policy, the compiled LangGraph), the session store, and
per-session frame/trace/deps state. ``continue_session`` drives the graph until
the next approval interrupt or completion; ``submit_approval`` resumes it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.types import Command

from pikvm_agent.config import AppConfig, load_config
from pikvm_agent.core.errors import SessionNotFoundError
from pikvm_agent.debuglog import DEBUG
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
from pikvm_agent.vision.omniparser_manager import OmniParserManager
from pikvm_agent.vision.paddleocr_client import paddleocr_available
from pikvm_agent.vision.providers import build_ocr_provider, build_screen_parser
from pikvm_agent.vision.tesseract_ocr import tesseract_available

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
    # Bumped on abort / panic / steer; the executor refuses a transaction whose
    # decision was made under a stale epoch (see graph.nodes.execute_transaction).
    control_epoch: int = 0
    # Sticky terminal brake — latched by abort / panic. The epoch invalidates an
    # in-flight decision but a re-planned loop re-stamps the new epoch and would pass;
    # this latch makes the stop survive re-planning AND blocks resume of a paused session.
    stopped: bool = False


class Runtime:
    def __init__(self, config: AppConfig, store: SessionStore, backend: Any, *,
                 screen_parser: Any, operator: Any, policy: SafetyPolicyEngine,
                 graph: Any, checkpointer: Any, executor: Any, recovery: Any,
                 omniparser: OmniParserManager | None = None) -> None:
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
        self._omniparser = omniparser
        self._sessions: dict[str, SessionRuntime] = {}
        # /status is polled constantly by the readiness UI; cache it so each poll doesn't
        # re-run the (network) health probes and contend with real work.
        self._status_cache: tuple[float, dict[str, Any]] | None = None

    @classmethod
    async def from_config(cls, config: AppConfig | None = None) -> "Runtime":
        config = config or load_config()
        # Wire the ultimate debug log first so startup itself is captured.
        DEBUG.configure(config.daemon.debug_log_path, session_dir=config.daemon.session_dir,
                        enabled=config.daemon.debug_log, truncate=config.daemon.debug_log_truncate)
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

        omniparser: OmniParserManager | None = None
        if config.omniparser.enabled:
            omniparser = OmniParserManager(config.omniparser)
            up = await omniparser.ensure_running(wait_s=config.omniparser.startup_wait_s)
            base = config.omniparser.base_url
            if up:
                log.info("OmniParser ready at %s", base)
            elif omniparser.spawned_child:
                # We launched it; it just isn't healthy within the short boot window.
                log.warning(
                    "OmniParser launched at %s but not ready yet — it loads models on "
                    "boot (the first GPU run can take a few minutes); sessions needing "
                    "element grounding will be unavailable until it finishes loading", base)
            else:
                # Not managed here and nothing is listening — the user must start it.
                sev = log.error if config.omniparser.required else log.warning
                sev("OmniParser is not reachable at %s and is not managed by the daemon "
                    "(omniparser.mode=%s) — start it; sessions will fail until it is up",
                    base, config.omniparser.mode)

        graph_db = str(Path(config.daemon.sqlite_path).with_name("graph.sqlite3"))
        checkpointer = await build_checkpointer(graph_db)
        graph = build_graph(checkpointer)
        return cls(config, store, backend, screen_parser=screen_parser, operator=operator,
                   policy=policy, graph=graph, checkpointer=checkpointer,
                   executor=executor, recovery=recovery, omniparser=omniparser)

    async def aclose(self) -> None:
        try:
            await self.backend.aclose()
        finally:
            # Close pooled HTTP clients on the operator / element parser if present.
            for owner in (self._operator, getattr(self._screen_parser, "elements", None)):
                closer = getattr(owner, "aclose", None)
                if closer is not None:
                    try:
                        await closer()
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        pass
            if self._omniparser is not None:
                await self._omniparser.stop()
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
        sr = SessionRuntime(session_id=session_id, task=task, frames=frames, trace=trace, deps=deps)
        # The executor reads the session's LIVE epoch + stop latch through deps; bumping
        # the epoch (steer) invalidates an in-flight decision, and the latch (abort / panic)
        # refuses every subsequent action even after a re-plan.
        deps.control_epoch_getter = lambda: sr.control_epoch
        deps.stop_getter = lambda: sr.stopped
        self._sessions[session_id] = sr
        return {"session_id": session_id, "status": row["status"], "task": task,
                "created_at": row["created_at"]}

    async def get_session_summary(self, session_id: str, capture: bool = True) -> dict[str, Any]:
        """Report the session's status + frame metadata.

        ``capture=True`` (pikvm_observe) grabs a FRESH screenshot and records an
        ``observe`` step — an explicit "look now". ``capture=False`` is read-only: it
        returns the LAST captured frame without touching the backend or the trace, so a
        UI can poll it cheaply (polling must never drive captures or flood the trace)."""
        DEBUG.set_session(session_id)
        sr = self._get(session_id)
        if capture:
            try:
                await self.backend.connect()
            except Exception as exc:  # noqa: BLE001
                log.warning("backend.connect failed: %s", exc)
            frame = await sr.frames.capture()
            sr.trace.append("observe", frame_id=frame.frame_id, world_version=frame.world_version,
                            screenshot_path=frame.image_path)
        else:
            frame = sr.frames.latest()
        row = await self.store.get_session(session_id)
        status = row["status"] if row else sr.status
        base = {"session_id": session_id, "status": status, "task": sr.task,
                "events": sr.events[-20:], "error": sr.error}
        if frame is None:  # read-only poll before the first capture
            return {**base, "frame_id": None, "world_version": None, "screenshot_path": None,
                    "width": None, "height": None, "keyboard_state": None}
        return {
            **base,
            "frame_id": frame.frame_id, "world_version": frame.world_version,
            "screenshot_path": frame.image_path, "width": frame.width, "height": frame.height,
            "keyboard_state": frame.keyboard_state.model_dump(),
        }

    async def abort_session(self, session_id: str, reason: str = "") -> dict[str, Any]:
        sr = self._get(session_id)
        sr.control_epoch += 1  # invalidate any in-flight transaction
        sr.stopped = True       # latch: refuse re-planned actions + block resume
        sr.status = "failed"
        sr.error = reason or "aborted by human"
        sr.trace.append("abort", reason=reason)
        await self.store.update_session(session_id, status="failed", error=sr.error)
        return {"session_id": session_id, "status": "failed", "reason": reason}

    async def panic_stop(self) -> dict[str, Any]:
        """Emergency brake — independent of any agent/MCP. Bumps every session's
        control epoch (so any in-flight transaction is refused before it executes) and
        marks active sessions failed. The currently-executing micro-action may finish,
        but no further action runs without a fresh decision under the new epoch."""
        # Drop any held keys/mouse buttons first (a hotkey or drag in flight).
        try:
            await self.backend.release_all()
        except Exception as exc:  # noqa: BLE001
            log.warning("panic_stop release_all failed: %s", exc)
        stopped: list[str] = []
        for sid, sr in list(self._sessions.items()):
            sr.control_epoch += 1
            sr.stopped = True  # latch ALL sessions so none can be resumed after a panic
            # Any non-terminal session is halted — including a budget-`paused` one, which
            # would otherwise stay resumable and re-plan under the bumped epoch.
            if sr.status in ("running", "needs_approval", "paused"):
                sr.status = "failed"
                sr.error = "panic_stop"
                sr.trace.append("panic_stop")
                try:
                    await self.store.update_session(sid, status="failed", error="panic_stop")
                except Exception as exc:  # noqa: BLE001 - best-effort persistence
                    log.warning("panic_stop persist failed for %s: %s", sid, exc)
                stopped.append(sid)
        log.warning("PANIC STOP — halted %d session(s): %s", len(stopped), stopped)
        return {"ok": True, "stopped": stopped}

    # ---- operator loop (LangGraph) --------------------------------------- #

    async def continue_session(self, session_id: str, max_transactions: int | None = None,
                               max_runtime_ms: int | None = None) -> dict[str, Any]:
        """Run the graph until the next approval, completion, or the per-call budget is
        spent — then it PAUSES (resumable). None/None = unbounded (daemon-direct
        default); the MCP facade passes small bounds so interrupting the agent stops it
        within one transaction instead of letting one call run for minutes."""
        DEBUG.set_session(session_id)
        sr = self._get(session_id)
        if sr.stopped:
            # Aborted / panicked — never resume the loop (a paused session must stay dead).
            return {"session_id": session_id, "task": sr.task, "status": "failed",
                    "error": sr.error or "stopped"}
        try:
            await self.backend.connect()
        except Exception as exc:  # noqa: BLE001
            log.warning("backend.connect failed: %s", exc)
        config = self._graph_config(sr)
        budget = self._budget_fields(max_transactions, max_runtime_ms)
        if not sr.started:
            sr.started = True
            initial = {"session_id": session_id, "task": sr.task, "step": 0,
                       "max_steps": DEFAULT_MAX_STEPS, **budget}
            result = await self._graph.ainvoke(initial, config)
        elif sr.status == "paused":
            # Resume a budget pause: reset the per-call counter + apply the new budget.
            sr.status = "running"
            result = await self._graph.ainvoke(Command(resume=None, update=budget), config)
        else:
            # Already running/paused without a pending approval — let it proceed.
            result = await self._graph.ainvoke(None, config)
        return await self._after_run(sr, result)

    @staticmethod
    def _budget_fields(max_transactions: int | None, max_runtime_ms: int | None) -> dict[str, Any]:
        deadline = (time.monotonic() * 1000 + max_runtime_ms) if max_runtime_ms else 0
        return {"tx_this_call": 0,
                "max_transactions": max_transactions if max_transactions is not None else 0,
                "deadline_ms": deadline}

    async def submit_approval(self, session_id: str, approval_id: str,
                             decision: dict) -> dict[str, Any]:
        """Resume a paused graph with the human's approval decision."""
        DEBUG.set_session(session_id)
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
        # If a panic / abort landed WHILE this graph run was in flight, the run may return
        # a stale "paused"/"done"/"needs_approval" — the latch wins, force it terminal so
        # the emergency stop can't be overwritten by an already-running invocation.
        if sr.stopped:
            sr.status = "failed"
            sr.error = sr.error or "stopped"
            await self.store.update_session(sr.session_id, status="failed", error=sr.error)
            return {**base, "status": "failed", "error": sr.error}
        if "__interrupt__" in result:
            itr = result["__interrupt__"]
            val = getattr(itr[0], "value", None) if itr else None
            # A budget pause is a RESUMABLE checkpoint, not an approval — report it as
            # "paused" so the next continue resumes the loop (rather than awaiting input).
            if isinstance(val, dict) and val.get("reason") == "budget_paused":
                sr.status = "paused"
                await self.store.update_session(sr.session_id, status="paused")
                return {**base, "status": "paused"}
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

    # ---- console support -------------------------------------------------- #

    async def list_sessions(self) -> list[dict[str, Any]]:
        rows = await self.store.list_sessions()
        live = {sid: sr.status for sid, sr in self._sessions.items()}
        for row in rows:
            row["live_status"] = live.get(row["id"], row["status"])
        return rows

    async def status(self) -> dict[str, Any]:
        """Readiness snapshot for UIs. The daemon is up if this responds; we report
        each dependency the daemon needs to actually drive a session:

          * pikvm       — the target host (reachable?)
          * omniparser  — element grounding (enabled/required/reachable; lags the
                          daemon by minutes on the first GPU boot)
          * operator    — the planner LLM (provider + whether its API key is set)
          * ocr         — the read-back engine (provider + whether it's installed)
          * store       — the local session/checkpoint sqlite (connected at boot)

        ``ok`` is True only when every REQUIRED dependency is satisfied (the target
        reachable, the operator configured, and OmniParser reachable when required).
        """
        cfg = self.config

        # Serve a recent snapshot — the readiness pill polls this every few seconds and
        # the health probes are network calls; recomputing each time piled up slow /status
        # requests that contended with everything else (panel polls, the loop).
        cached = self._status_cache
        if cached is not None and (time.monotonic() - cached[0]) < 3.0:
            return cached[1]

        async def _probe(coro: Any) -> bool:
            # Hard-bound each probe: a busy OmniParser (mid GPU-parse) or a slow PiKVM
            # must not make /status take many seconds.
            try:
                return bool(await asyncio.wait_for(coro, timeout=2.0))
            except Exception:  # noqa: BLE001 - a probe failure/timeout is just "not ready"
                return False

        probes = [_probe(self.backend.health())]
        if self._omniparser is not None:
            probes.append(_probe(self._omniparser.healthy()))
        results = await asyncio.gather(*probes)
        pikvm_ok = results[0]
        omni_ok = results[1] if self._omniparser is not None else False

        op = cfg.operator
        operator = {
            "provider": op.provider,
            "configured": op.provider == "fake" or op.api_key is not None,
        }

        ocr_provider = cfg.ocr.provider
        if ocr_provider == "paddleocr":
            ocr_available = paddleocr_available()
        elif ocr_provider == "tesseract":
            ocr_available = tesseract_available()
        else:  # "pikvm" — uses the target's built-in OCR, so it tracks pikvm reachability
            ocr_available = True

        deps: dict[str, Any] = {
            "pikvm": {"base_url": cfg.pikvm.base_url, "reachable": pikvm_ok},
            "omniparser": {
                "enabled": cfg.omniparser.enabled,
                "required": cfg.omniparser.required,
                "reachable": omni_ok,
            },
            "operator": operator,
            "ocr": {"provider": ocr_provider, "available": ocr_available},
            "store": {"connected": True},
        }
        ready = (
            pikvm_ok
            and operator["configured"]
            and (omni_ok or not cfg.omniparser.required)
        )
        result = {"ok": ready, "dependencies": deps}
        self._status_cache = (time.monotonic(), result)
        return result

    def latest_frame_path(self, session_id: str) -> str | None:
        sr = self._sessions.get(session_id)
        if sr is None:
            return None
        frame = sr.frames.latest()
        return frame.image_path if frame else None

    async def pending_approvals(self, session_id: str) -> list[dict[str, Any]]:
        self._get(session_id)
        return await self.store.pending_approvals(session_id)

    def recent_trace(self, session_id: str, limit: int = 40) -> list[dict[str, Any]]:
        sr = self._get(session_id)
        return sr.trace.read()[-limit:]

    async def export_memory_update(self, session_id: str) -> dict[str, Any]:
        """Produce a safe Atlas memory-update proposal from the session trace.

        Returns a redacted markdown page + structured incident (no screenshots,
        secrets, credentials, or verbatim typed/message bodies) for Claude/Codex
        to write to Atlas via the atlas MCP tools."""
        from pikvm_agent.memory.atlas_export import build_memory_update

        sr = self._get(session_id)
        mu = build_memory_update(session_id, sr.task, sr.trace.read(), status=sr.status)
        return mu.model_dump()
