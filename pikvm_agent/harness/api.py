"""Authenticated FastAPI surface for the visible operator harness."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pikvm_agent.harness.agent import AgentHarness
from pikvm_agent.harness.assistant import AssistantHarness
from pikvm_agent.harness.agent_models import (
    TERMINAL_RUN_STATUSES,
    ArtifactAcceptance,
    ArtifactAcceptanceState,
    MediaTransactionState,
    ProviderAlias,
    RunModelRoute,
    RunSnapshot,
    RunStatus,
)
from pikvm_agent.harness.agent_store import RunNotFoundError, RunStore
from pikvm_agent.harness.config import HARNESS_ACCESS_TOKEN_MIN_LENGTH
from pikvm_agent.harness.direct_calls import (
    DirectCallBegin,
    DirectCallCoordinator,
    DirectCallFinish,
)
from pikvm_agent.harness.performance import summarize_run_performance
from pikvm_agent.harness.provider_connections import (
    ProviderConnectionConflict,
    ProviderConnectionPolicyConflict,
    ProviderConnectionRequest,
    ProviderConnectionResult,
)
from pikvm_agent.harness.provider_support import public_provider_catalog
from pikvm_agent.harness.redaction import redact_secrets
from pikvm_agent.harness.showcase import ShowcaseRepository

RUN_EVENT_TAIL_LIMIT = 500
RUN_EVENT_PAGE_LIMIT = 500
RUN_EVENT_PAGE_MAX = 1_000
RUN_TIMELINE_EVENT_LIMIT = 5_000
RUN_TIMELINE_EVENT_KINDS = frozenset(
    {
        "action.attempted",
        "action.checkpointed",
        "action.completed",
        "action.completed_unverified",
        "action.failed",
        "action.pre_action_evidence_captured",
        "action.recoverable_failure",
        "action.refused_by_operator",
        "action.refused_by_policy",
        "action.refused_stale",
        "action.stale_world_refreshed",
        "action.transport_uncertain",
        "action.ungrounded_budget_exhausted",
        "action.ungrounded_refresh_failed",
        "action.ungrounded_refreshed",
        "action.ungrounded_repeated",
        "approval.required",
        "assistant.computer_handoff",
        "assistant.computer_handoff_completed",
        "assistant.computer_handoff_failed",
        "assistant.computer_handoff_started",
        "model.completed",
        "run.aborted",
        "run.blocked",
        "run.completed",
        "run.failed",
        "run.rejected",
        "target.identity_changed",
        "tool.approval_required",
        "tool.completed",
        "tool.failed",
        "tool.refused",
        "tool.started",
        "verification.completed",
        "verification.evidence_captured",
    }
)
STREAM_HEARTBEAT_SECONDS = 5.0
UNCACHED_UI_ENTRYPOINTS = frozenset(
    {
        "/app",
        "/app/",
        "/app/index.html",
        "/app/app.js",
        "/app/styles.css",
        "/app/version.json",
    }
)
_AUTONOMOUS_PAUSE_REASONS = {
    "per-call action budget reached",
    "verifier requires more work",
}
_AUTONOMOUS_PAUSE_EVENTS = {
    "action.ungrounded_refreshed",
    "controller.requested_replan",
    "run.autonomous_resume",
    "verification.failed",
}


def _autonomous_resume_reason(run: RunSnapshot) -> str | None:
    """Return the internal yield reason that must stay hidden from callers."""

    if run.status is not RunStatus.PAUSED or not run.events:
        return None
    event = run.events[-1]
    if (
        event.kind == "action.stale_world_refreshed"
        and event.data.get("status") == "stale_world"
    ):
        return event.kind
    if event.kind in _AUTONOMOUS_PAUSE_EVENTS:
        return event.kind
    if event.kind != "run.paused":
        return None
    reason = str(event.data.get("reason") or "")
    return reason if reason in _AUTONOMOUS_PAUSE_REASONS else None


def _continue_after_approval(run: RunSnapshot) -> bool:
    return (
        run.mode == "assistant" and run.status is RunStatus.RUNNING
    ) or _autonomous_resume_reason(run) is not None


class HealthSource(Protocol):
    def health(self) -> dict[str, dict[str, object]]: ...


class ProviderConnectionSource(Protocol):
    async def connect(
        self,
        request: ProviderConnectionRequest,
    ) -> ProviderConnectionResult: ...


class LiveFrameSource(Protocol):
    async def get(self, session_id: str) -> Any: ...


class MediaTransactionSource(Protocol):
    async def resolve_approval(
        self,
        run_id: str,
        approval_id: str,
        decision: dict[str, Any],
    ) -> RunSnapshot: ...

    async def release(
        self,
        run_id: str,
        reason: str = "virtual media no longer needed",
    ) -> RunSnapshot: ...


class ModelPreferences(BaseModel):
    """Per-role primary choices expanded server-side into fallback routes."""

    model_config = ConfigDict(extra="forbid")

    reasoner: ProviderAlias | None = None
    controller: ProviderAlias | None = None
    verifier: ProviderAlias | None = None

    @model_validator(mode="after")
    def at_least_one_preference(self) -> "ModelPreferences":
        if not any((self.reasoner, self.controller, self.verifier)):
            raise ValueError("model preferences must select at least one role")
        return self


class CreateRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=20_000)
    mode: Literal["assistant", "computer"] = "computer"
    auto_start: bool = True
    model_provider: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.:-]{1,128}$",
    )
    model_preferences: ModelPreferences | None = None
    source_client: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{1,64}$",
    )
    client_request_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.:-]{1,128}$",
    )

    @model_validator(mode="after")
    def model_selection_is_unambiguous(self) -> "CreateRunBody":
        if self.model_provider is not None and self.model_preferences is not None:
            raise ValueError(
                "model_provider and model_preferences cannot both be selected"
            )
        return self


def _expanded_model_route(
    preferences: ModelPreferences,
    provider_health: dict[str, dict[str, object]],
) -> RunModelRoute:
    """Resolve primary choices into durable per-role fallback candidates."""

    selected_by_role = preferences.model_dump(exclude_none=True)
    expanded: dict[str, list[str]] = {}
    for role, selected_value in selected_by_role.items():
        selected = str(selected_value)
        if selected not in provider_health:
            raise KeyError(selected)
        if not provider_health[selected].get("ready", True):
            raise RuntimeError(selected)
        default_candidates: list[tuple[int, str]] = []
        for name, health in provider_health.items():
            if not health.get("ready", True):
                continue
            routes = health.get("routes")
            if not isinstance(routes, list):
                continue
            for route in routes:
                if not isinstance(route, dict) or route.get("role") != role:
                    continue
                position = route.get("position")
                default_candidates.append(
                    (
                        (
                            int(position)
                            if isinstance(position, int)
                            and not isinstance(position, bool)
                            else 10_000
                        ),
                        name,
                    )
                )
        ordered = [selected]
        for _, candidate in sorted(default_candidates):
            if candidate not in ordered:
                ordered.append(candidate)
        expanded[role] = ordered
    return RunModelRoute.model_validate(expanded)


class ApprovalBody(BaseModel):
    type: str
    reason: str = ""


class PauseBody(BaseModel):
    reason: str = "paused by operator"


class SteerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=2_000)
    auto_resume: bool = True


class AbortBody(BaseModel):
    reason: str = "stopped by operator"


class ArtifactAcceptanceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["office_artifact"]
    label: str = Field(min_length=1, max_length=200)
    state: ArtifactAcceptanceState
    artifact_format: Literal["docx", "xlsx"] | None = None
    checks_passed: int = Field(default=0, ge=0)
    checks_total: int = Field(default=0, ge=0)
    byte_count: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    error_class: str | None = Field(default=None, max_length=100)


class HarnessAuthMiddleware:
    """Pure ASGI authentication that does not buffer streaming responses."""

    def __init__(
        self,
        app: Any,
        *,
        access_token: str,
        agent_token: str | None,
        observer_token: str | None,
        allowed_origins: set[str],
    ) -> None:
        self.app = app
        self.expected_authorization = (
            f"Bearer {access_token}".encode("latin-1")
        )
        self.expected_observer_authorization = (
            f"Bearer {observer_token}".encode("latin-1")
            if observer_token is not None
            else None
        )
        self.expected_agent_authorization = (
            f"Bearer {agent_token}".encode("latin-1")
            if agent_token is not None
            else None
        )
        self.allowed_origins = {
            origin.encode("latin-1") for origin in allowed_origins
        }

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if not path.startswith("/api/") or path == "/api/health":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        supplied = headers.get(b"authorization", b"")
        artifact_acceptance_path = (
            path.startswith("/api/runs/")
            and path.endswith("/artifact-acceptance")
        )
        operator_only_path = (
            "/approvals/" in path or path.endswith("/steer")
        )
        operator_authorized = (
            not artifact_acceptance_path
            and secrets.compare_digest(
                supplied, self.expected_authorization
            )
        )
        observer_authorized = (
            (
                path.startswith("/api/direct/")
                or artifact_acceptance_path
            )
            and self.expected_observer_authorization is not None
            and secrets.compare_digest(
                supplied, self.expected_observer_authorization
            )
        )
        agent_authorized = (
            (
                path.startswith("/api/runs")
                or path.startswith("/api/agent/")
                or path.startswith("/api/showcases")
            )
            and not operator_only_path
            and not artifact_acceptance_path
            and self.expected_agent_authorization is not None
            and secrets.compare_digest(
                supplied, self.expected_agent_authorization
            )
        )
        if (
            not operator_authorized
            and not observer_authorized
            and not agent_authorized
        ):
            await JSONResponse(
                status_code=401,
                content={"detail": "valid harness bearer token required"},
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return
        origin = headers.get(b"origin")
        if origin and origin not in self.allowed_origins:
            await JSONResponse(
                status_code=403,
                content={"detail": "origin is not allowed"},
            )(scope, receive, send)
            return
        method = str(scope.get("method") or "")
        if method == "POST" and "/approvals/" in path:
            if origin is None:
                await JSONResponse(
                    status_code=409,
                    content={
                        "detail": (
                            "approval decisions require the authenticated "
                            "operator UI origin"
                        )
                    },
                )(scope, receive, send)
                return
            approval_id = path.rsplit("/", 1)[-1].encode("latin-1")
            intent = headers.get(b"x-pikvm-approval-intent", b"")
            if not secrets.compare_digest(intent, approval_id):
                await JSONResponse(
                    status_code=409,
                    content={
                        "detail": (
                            "approval requires an exact "
                            "X-PiKVM-Approval-Intent header"
                        )
                    },
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


class ShutdownSafeStreamingResponse(StreamingResponse):
    """Treat server cancellation of a long-lived SSE request as disconnect."""

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        try:
            await super().__call__(scope, receive, send)
        except asyncio.CancelledError:
            return


def create_harness_app(
    *,
    harness: AgentHarness,
    assistant: AssistantHarness | None = None,
    store: RunStore,
    models: HealthSource,
    access_token: str,
    agent_token: str | None = None,
    observer_token: str | None = None,
    allowed_origins: set[str],
    live_frames: LiveFrameSource | None = None,
    direct_calls: DirectCallCoordinator | None = None,
    media_transactions: MediaTransactionSource | None = None,
    provider_connections: ProviderConnectionSource | None = None,
    run_locks: dict[str, asyncio.Lock] | None = None,
    max_autonomous_resumes: int = 64,
    external_driver: bool = False,
    computer_control_enabled: bool = True,
    managed_mcp_name: str = "Managed PiKVM MCP",
    computer_name: str = "Managed computer",
    showcase_dir: Path | None = None,
    ui_dir: Path | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """Create the local operator API.

    ``access_token`` is the browser/operator credential and is mandatory even
    on loopback. Optional model-side agent and observer credentials receive
    non-approval-run and direct-ingest scopes respectively. The process
    launcher also refuses non-loopback binds unless an operator explicitly
    enables a secured deployment. Approval requests add a second, ID-bound
    intent header so a generic authenticated fetch cannot accidentally approve
    a pending action.
    """

    if len(access_token) < HARNESS_ACCESS_TOKEN_MIN_LENGTH:
        raise ValueError(
            "harness access token must be at least "
            f"{HARNESS_ACCESS_TOKEN_MIN_LENGTH} characters"
        )
    if not allowed_origins:
        raise ValueError("at least one allowed UI origin is required")
    if max_autonomous_resumes < 1:
        raise ValueError("max_autonomous_resumes must be positive")
    if agent_token is not None:
        if len(agent_token) < HARNESS_ACCESS_TOKEN_MIN_LENGTH:
            raise ValueError(
                "agent token must contain at least "
                f"{HARNESS_ACCESS_TOKEN_MIN_LENGTH} characters"
            )
        if secrets.compare_digest(access_token, agent_token):
            raise ValueError("agent token must differ from operator token")
    if direct_calls is not None:
        if (
            observer_token is None
            or len(observer_token) < HARNESS_ACCESS_TOKEN_MIN_LENGTH
        ):
            raise ValueError(
                "direct-call visibility requires a separate observer token "
                f"of at least {HARNESS_ACCESS_TOKEN_MIN_LENGTH} characters"
            )
        if secrets.compare_digest(access_token, observer_token):
            raise ValueError(
                "direct-call observer token must differ from operator token"
            )
        if agent_token is not None and secrets.compare_digest(
            agent_token, observer_token
        ):
            raise ValueError(
                "direct-call observer token must differ from agent token"
            )

    supplied_lifespan = lifespan

    @asynccontextmanager
    async def managed_lifespan(app: FastAPI) -> AsyncIterator[None]:
        @asynccontextmanager
        async def active() -> AsyncIterator[None]:
            await resume_automatic_runs()
            try:
                yield
            finally:
                shutdown_requested.set()
                tasks = [task for task in active_tasks if not task.done()]
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

        if supplied_lifespan is None:
            async with active():
                yield
            return
        async with supplied_lifespan(app):
            async with active():
                yield

    app = FastAPI(
        title="PiKVM Agent Workspace",
        version="0.1.0",
        lifespan=managed_lifespan,
    )
    app.add_middleware(
        HarnessAuthMiddleware,
        access_token=access_token,
        agent_token=agent_token,
        observer_token=observer_token,
        allowed_origins=allowed_origins,
    )

    @app.middleware("http")
    async def prevent_stale_ui_entrypoints(
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> Response:
        response = await call_next(request)
        if request.url.path in UNCACHED_UI_ENTRYPOINTS:
            response.headers["Cache-Control"] = "no-store"
        return response

    shutdown_requested = asyncio.Event()
    app.state.shutdown_requested = shutdown_requested
    active_tasks: set[asyncio.Task[Any]] = set()
    continuation_tasks: dict[str, set[asyncio.Task[Any]]] = {}
    background_approvals: set[tuple[str, str]] = set()
    resolved_run_locks = run_locks if run_locks is not None else {}
    app.state.run_locks = resolved_run_locks

    def lock_for(run_id: str) -> asyncio.Lock:
        return resolved_run_locks.setdefault(run_id, asyncio.Lock())

    def managed_driver(run: RunSnapshot) -> Any:
        assistant_thread = (
            assistant is not None
            and run.origin == "managed"
            and bool(run.conversation)
            and (
                run.mode == "assistant"
                or run.status in TERMINAL_RUN_STATUSES
            )
        )
        return assistant if assistant_thread else harness

    async def continue_managed_run(run_id: str) -> RunSnapshot:
        run = await store.get_state(run_id)
        if run.mode == "assistant" and assistant is None:
            raise RuntimeError(
                "assistant mode is not configured for this harness"
            )
        return await managed_driver(run).continue_run(run_id)

    async def guarded_continue(run_id: str) -> Any:
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - asyncio always has one here
            raise RuntimeError("continuation has no asyncio task")
        if any(
            candidate is not task and not candidate.done()
            for candidate in continuation_tasks.get(run_id, set())
        ):
            # A queued continuation must not cross a meaningful pause reached
            # by the task that currently owns model/tool progression.
            return await store.get_control(run_id)
        continuation_tasks.setdefault(run_id, set()).add(task)
        try:
            durable_run = await store.get(run_id)
            autonomous_resumes = sum(
                event.kind == "run.autonomous_resume"
                for event in durable_run.events
            )
            try:
                while True:
                    async with lock_for(run_id):
                        run = await continue_managed_run(run_id)
                        resume_reason = _autonomous_resume_reason(run)
                        if resume_reason is None:
                            return run
                        if autonomous_resumes >= max_autonomous_resumes:
                            run.error = (
                                "automatic continuation slice limit reached"
                            )
                            run.record(
                                "run.autonomy_stopped",
                                reason=resume_reason,
                                limit=max_autonomous_resumes,
                            )
                            await store.save(run)
                            return run
                        autonomous_resumes += 1
                        run.record(
                            "run.autonomous_resume",
                            reason=resume_reason,
                            source="harness_supervisor",
                        )
                        await store.save(run)
                    # Release the per-run lock between bounded slices so an
                    # operator pause or stop can acquire authority immediately.
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                # Pause/abort deliberately cancel an in-flight provider/tool
                # wait before taking the run lock. The HTTP caller still needs
                # a truthful response; letting cancellation escape causes
                # Starlette's BaseHTTPMiddleware to emit a misleading 500.
                return await store.get_control(run_id)
        finally:
            tasks = continuation_tasks.get(run_id)
            if tasks is not None:
                tasks.discard(task)
                if not tasks:
                    continuation_tasks.pop(run_id, None)

    async def cancel_continuations(run_id: str) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in continuation_tasks.get(run_id, set())
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def guarded_managed_approval(
        run_id: str,
        approval_id: str,
        decision: dict[str, Any],
    ) -> tuple[RunSnapshot, bool]:
        """Resolve and verify an approval while remaining operator-cancellable."""

        task = asyncio.current_task()
        if task is None:  # pragma: no cover - asyncio always has one here
            raise RuntimeError("approval resolution has no asyncio task")
        continuation_tasks.setdefault(run_id, set()).add(task)
        try:
            try:
                async with lock_for(run_id):
                    existing = await store.get_state(run_id)
                    driver = managed_driver(existing)
                    return (
                        await driver.resolve_approval(
                            run_id,
                            approval_id,
                            decision,
                        ),
                        False,
                    )
            except asyncio.CancelledError:
                return await store.get_control(run_id), True
        finally:
            tasks = continuation_tasks.get(run_id)
            if tasks is not None:
                tasks.discard(task)
                if not tasks:
                    continuation_tasks.pop(run_id, None)

    async def resolve_managed_approval_in_background(
        run_id: str,
        approval_id: str,
        decision: dict[str, Any],
    ) -> None:
        key = (run_id, approval_id)
        try:
            run, cancelled = await guarded_managed_approval(
                run_id,
                approval_id,
                decision,
            )
            if (
                not cancelled
                and _continue_after_approval(run)
            ):
                schedule(guarded_continue(run_id))
        except Exception as exc:  # noqa: BLE001 - publish async failure durably
            log.exception("background approval resolution failed")
            run = await store.get_control(run_id)
            if run.status is RunStatus.NEEDS_APPROVAL:
                run.status = RunStatus.PAUSED
                run.error = (
                    "approval resolution failed; no completion was claimed"
                )
                run.record(
                    "approval.failed",
                    approval_id=approval_id,
                    error_class=type(exc).__name__,
                )
                await store.save(run)
        finally:
            background_approvals.discard(key)

    def schedule(coro: Any) -> None:
        task = asyncio.create_task(coro)
        active_tasks.add(task)
        task.add_done_callback(active_tasks.discard)

    async def resume_automatic_runs() -> None:
        for summary in await store.list_summaries(limit=10_000):
            if summary.status is not RunStatus.PAUSED:
                continue
            run = await store.get_control(summary.run_id)
            if _autonomous_resume_reason(run) is not None:
                schedule(guarded_continue(run.run_id))

    @app.exception_handler(RunNotFoundError)
    async def not_found(_: Request, exc: RunNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"detail": f"unknown run: {exc.args[0]}"},
        )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        # No provider names, sessions, or machine state before authentication.
        return {
            "status": "ok",
            "control_mode": (
                "external_benchmark" if external_driver else "interactive"
            ),
            "direct_call_visibility": (
                "enabled" if direct_calls is not None else "disabled"
            ),
            "computer_control": (
                "enabled" if computer_control_enabled else "disabled"
            ),
        }

    @app.get("/api/providers")
    async def providers() -> dict[str, dict[str, object]]:
        return models.health()

    @app.get("/api/computer-connection")
    async def computer_connection() -> dict[str, str | bool]:
        return {
            "enabled": computer_control_enabled,
            "mcp_server_name": managed_mcp_name,
            "machine_name": computer_name,
        }

    showcase_repository = (
        ShowcaseRepository(showcase_dir)
        if showcase_dir is not None
        else None
    )

    @app.get("/api/showcases")
    async def list_showcases() -> list[dict[str, Any]]:
        if showcase_repository is None:
            return []
        return [
            campaign.model_dump(mode="json")
            for campaign in showcase_repository.list()
        ]

    @app.get("/api/showcases/current")
    async def current_showcase() -> dict[str, Any]:
        campaign = (
            showcase_repository.current()
            if showcase_repository is not None
            else None
        )
        if campaign is None:
            raise HTTPException(404, "no recorded campaign is available")
        return campaign.model_dump(mode="json")

    @app.get("/api/showcases/{campaign_id}")
    async def get_showcase(campaign_id: str) -> dict[str, Any]:
        campaign = (
            showcase_repository.get(campaign_id)
            if showcase_repository is not None
            else None
        )
        if campaign is None:
            raise HTTPException(404, "recorded campaign is unavailable")
        return campaign.model_dump(mode="json")

    @app.get(
        "/api/showcases/{campaign_id}/tasks/{task_id}/{kind}",
        response_class=FileResponse,
    )
    async def get_showcase_media(
        campaign_id: str,
        task_id: str,
        kind: Literal["recording", "poster"],
    ) -> FileResponse:
        path = (
            showcase_repository.media(campaign_id, task_id, kind)
            if showcase_repository is not None
            else None
        )
        if path is None:
            raise HTTPException(404, "recorded task media is unavailable")
        return FileResponse(
            path,
            media_type=(
                "video/mp4" if kind == "recording" else _image_mime(path)
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/tools")
    async def assistant_tool_catalog() -> list[dict[str, Any]]:
        if assistant is None:
            return []
        return [
            tool.model_dump(mode="json")
            for tool in await assistant.catalog()
        ]

    @app.get("/api/tool-servers")
    async def assistant_tool_server_health() -> dict[str, dict[str, object]]:
        return assistant.tool_health() if assistant is not None else {}

    @app.post(
        "/api/providers",
        status_code=201,
        response_model=ProviderConnectionResult,
    )
    async def connect_provider(
        body: ProviderConnectionRequest,
    ) -> ProviderConnectionResult:
        if provider_connections is None:
            raise HTTPException(
                503,
                "provider connections are not enabled for this harness",
            )
        try:
            return await provider_connections.connect(body)
        except (
            ProviderConnectionConflict,
            ProviderConnectionPolicyConflict,
        ) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/provider-catalog")
    async def provider_catalog() -> list[dict[str, object]]:
        return public_provider_catalog()

    @app.get("/api/direct/health")
    async def direct_health() -> dict[str, str]:
        if direct_calls is None:
            raise HTTPException(503, "direct-call visibility is not configured")
        return {"status": "ok", "scope": "direct-call-ingest"}

    @app.get("/api/agent/health")
    async def agent_health() -> dict[str, str]:
        return {"status": "ok", "scope": "managed-harness-control"}

    @app.post("/api/direct/calls/begin")
    async def begin_direct_call(body: DirectCallBegin) -> dict[str, Any]:
        if direct_calls is None:
            raise HTTPException(503, "direct-call visibility is not configured")
        return (await direct_calls.begin(body)).model_dump(mode="json")

    @app.post("/api/direct/calls/finish")
    async def finish_direct_call(body: DirectCallFinish) -> dict[str, Any]:
        if direct_calls is None:
            raise HTTPException(503, "direct-call visibility is not configured")
        try:
            run = await direct_calls.finish(body)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {
            "ok": True,
            "run_id": run.run_id,
            "status": run.status.value,
            "cursor": run.events[-1].sequence if run.events else 0,
        }

    async def create_run_once(body: CreateRunBody) -> dict[str, Any]:
        if external_driver:
            raise HTTPException(
                409,
                "this console observes an externally driven benchmark",
            )
        provider_health = (
            models.health()
            if body.model_provider is not None
            or body.model_preferences is not None
            else {}
        )
        if body.model_provider is not None:
            if body.model_provider not in provider_health:
                raise HTTPException(
                    422,
                    f"unknown model provider: {body.model_provider}",
                )
            if not provider_health[body.model_provider].get("ready", True):
                raise HTTPException(
                    409,
                    f"model provider is not ready: {body.model_provider}",
                )
        model_route: RunModelRoute | None = None
        if body.model_preferences is not None:
            try:
                model_route = _expanded_model_route(
                    body.model_preferences,
                    provider_health,
                )
            except KeyError as exc:
                raise HTTPException(
                    422,
                    f"unknown model provider: {exc.args[0]}",
                ) from exc
            except RuntimeError as exc:
                raise HTTPException(
                    409,
                    f"model provider is not ready: {exc.args[0]}",
                ) from exc
        create_options: dict[str, Any] = {}
        if body.source_client or body.client_request_id:
            caller: dict[str, str] = {}
            if body.source_client:
                caller.update(
                    {
                        "interface": (
                            "chat_workspace"
                            if body.source_client == "chat-workspace"
                            else "managed_mcp"
                        ),
                        "label": body.source_client,
                    }
                )
            if body.client_request_id:
                caller["request_id"] = body.client_request_id
            create_options["caller"] = caller
        if body.model_provider:
            create_options["model_provider"] = body.model_provider
        if model_route is not None:
            create_options["model_route"] = model_route
        if body.mode == "assistant":
            if assistant is None:
                raise HTTPException(
                    503,
                    "normal assistant mode is not configured",
                )
            run = await assistant.create(body.task, **create_options)
        else:
            run = await harness.create(body.task, **create_options)
        if body.auto_start and run.status.value not in {
            "failed",
            "aborted",
            "rejected",
            "completed",
        }:
            schedule(guarded_continue(run.run_id))
        return _visible_run(run)

    @app.post("/api/runs")
    async def create_run(body: CreateRunBody) -> dict[str, Any]:
        request_id = body.client_request_id
        if request_id is None:
            return await create_run_once(body)
        async with lock_for(f"client-create:{request_id}"):
            for summary in await store.list_summaries(limit=10_000):
                if summary.caller.get("request_id") != request_id:
                    continue
                if summary.task != body.task or summary.mode != body.mode:
                    raise HTTPException(
                        409,
                        "client_request_id was already used for another task",
                    )
                return _visible_run(await store.get_state(summary.run_id))
            return await create_run_once(body)

    @app.get("/api/runs")
    async def list_runs(
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return [
            _visible_run_summary(run)
            for run in await store.list_summaries(limit=limit)
        ]

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        summary = await store.get_summary(run_id)
        state = await store.get_state(run_id)
        event_page, timeline_page = await asyncio.gather(
            store.events_after(
                run_id,
                max(0, summary.event_cursor - RUN_EVENT_TAIL_LIMIT),
                RUN_EVENT_TAIL_LIMIT,
            ),
            store.events_matching(
                run_id,
                RUN_TIMELINE_EVENT_KINDS,
                RUN_TIMELINE_EVENT_LIMIT,
            ),
        )
        state.events = event_page.events
        return _visible_run(
            state,
            event_count=summary.event_count,
            event_cursor=summary.event_cursor,
            timeline_events=timeline_page.events,
            timeline_events_truncated=timeline_page.has_more,
        )

    @app.get("/api/runs/{run_id}/performance")
    async def get_run_performance(run_id: str) -> dict[str, Any]:
        report = summarize_run_performance(await store.get(run_id))
        return report.model_dump(mode="json")

    @app.get("/api/runs/{run_id}/events")
    async def get_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(
            default=RUN_EVENT_PAGE_LIMIT,
            ge=1,
            le=RUN_EVENT_PAGE_MAX,
        ),
    ) -> dict[str, Any]:
        summary, page = await store.updates_after(run_id, after, limit)
        cursor = page.events[-1].sequence if page.events else after
        return {
            "run_id": run_id,
            "status": summary.status.value,
            "cursor": cursor,
            "latest_cursor": page.latest_cursor,
            "has_more": page.has_more,
            "events": [
                redact_secrets(event.model_dump(mode="json"))
                for event in page.events
            ],
        }

    @app.get("/api/runs/{run_id}/stream")
    async def stream_events(
        request: Request, run_id: str, after: int = Query(default=0, ge=0)
    ) -> StreamingResponse:
        await store.get_summary(run_id)

        async def stream() -> Any:
            cursor = after
            last_status: str | None = None
            last_activity: dict[str, Any] | None = None
            loop = asyncio.get_running_loop()
            last_heartbeat = loop.time()
            try:
                initial = await store.get_summary(run_id)
                yield _sse_event(
                    "stream.ready",
                    {
                        "run_id": run_id,
                        "cursor": cursor,
                        "status": initial.status.value,
                    },
                    retry_ms=1_000,
                )
                while (
                    not shutdown_requested.is_set()
                    and not await request.is_disconnected()
                ):
                    summary, page = await store.updates_after(
                        run_id,
                        cursor,
                        RUN_EVENT_PAGE_MAX,
                    )
                    while True:
                        for event in page.events:
                            cursor = event.sequence
                            yield _sse_event(
                                "run.event",
                                redact_secrets(
                                    event.model_dump(mode="json")
                                ),
                                event_id=cursor,
                            )
                        if not page.has_more:
                            break
                        _, page = await store.updates_after(
                            run_id,
                            cursor,
                            RUN_EVENT_PAGE_MAX,
                        )
                    active_activity = (
                        summary.active_activity.model_dump(mode="json")
                        if summary.active_activity is not None
                        else None
                    )
                    if (
                        summary.status.value != last_status
                        or active_activity != last_activity
                    ):
                        last_status = summary.status.value
                        last_activity = active_activity
                        yield _sse_event(
                            "run.state",
                            {
                                "run_id": run_id,
                                "status": last_status,
                                "frame_id": summary.frame_id,
                                "active_activity": active_activity,
                            },
                        )
                    now = loop.time()
                    if now - last_heartbeat >= STREAM_HEARTBEAT_SECONDS:
                        yield _sse_event(
                            "stream.heartbeat",
                            {
                                "run_id": run_id,
                                "cursor": cursor,
                                "status": summary.status.value,
                            },
                        )
                        last_heartbeat = now
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                # Uvicorn cancels long-lived streams after its bounded graceful
                # shutdown window. This is a normal disconnect, not an
                # application failure to log as a traceback.
                return

        return ShutdownSafeStreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/runs/{run_id}/frame")
    async def get_frame(run_id: str) -> Response:
        run = await store.get_state(run_id)
        if live_frames is not None and run.session_id:
            try:
                frame = await live_frames.get(run.session_id)
                headers = {
                    "Cache-Control": "no-store",
                    "X-PiKVM-Frame-Mode": "live",
                }
                if frame.captured_at:
                    headers["X-PiKVM-Captured-At"] = str(frame.captured_at)
                if frame.width is not None:
                    headers["X-PiKVM-Width"] = str(frame.width)
                if frame.height is not None:
                    headers["X-PiKVM-Height"] = str(frame.height)
                return Response(
                    content=frame.data,
                    media_type=frame.media_type,
                    headers=headers,
                )
            except Exception:
                # Visibility degrades to the durable action checkpoint.  A
                # preview outage must never fail or alter the active run.
                pass
        image_path = run.observation.image_path if run.observation else None
        if not image_path:
            raise HTTPException(404, "run has no frame")
        path = Path(image_path)
        if not path.is_file():
            raise HTTPException(404, "frame artifact is unavailable")
        return Response(
            content=path.read_bytes(),
            media_type=_image_mime(path),
            headers={
                "Cache-Control": "no-store",
                "X-PiKVM-Frame-Mode": "checkpoint",
                "X-PiKVM-Live-Capable": (
                    "true" if live_frames is not None else "false"
                ),
            },
        )

    @app.get("/api/runs/{run_id}/verification-image")
    async def get_verification_image(run_id: str) -> Response:
        run = await store.get_state(run_id)
        image_path = run.latest_verification_image_path
        if not image_path:
            raise HTTPException(404, "run has no verification image")
        return _verification_image_response(image_path)

    @app.get("/api/runs/{run_id}/verification-images/{revision}")
    async def get_verification_image_revision(
        run_id: str,
        revision: int,
    ) -> Response:
        run = await store.get_state(run_id)
        evidence = next(
            (
                item
                for item in run.verification_images
                if item.revision == revision
            ),
            None,
        )
        if evidence is None:
            raise HTTPException(404, "verification image revision is unavailable")
        return _verification_image_response(
            evidence.path,
            kind=evidence.kind,
            revision=evidence.revision,
            action_index=evidence.action_index,
            before_frame_id=evidence.before_frame_id,
            after_frame_id=evidence.after_frame_id,
        )

    @app.get(
        "/api/runs/{run_id}/verification-images/{revision}/click-target"
    )
    async def get_verification_click_target(
        run_id: str,
        revision: int,
        x: int = Query(ge=0, le=65_535),
        y: int = Query(ge=0, le=65_535),
        screen_width: int = Query(ge=1, le=65_535),
        screen_height: int = Query(ge=1, le=65_535),
    ) -> Response:
        run = await store.get_state(run_id)
        evidence = next(
            (
                item
                for item in run.verification_images
                if item.revision == revision
            ),
            None,
        )
        if evidence is None:
            raise HTTPException(404, "verification image revision is unavailable")
        return _verification_click_target_response(
            evidence.path,
            kind=evidence.kind,
            x=x,
            y=y,
            screen_width=screen_width,
            screen_height=screen_height,
            revision=evidence.revision,
            action_index=evidence.action_index,
        )

    @app.post("/api/runs/{run_id}/artifact-acceptance")
    async def update_artifact_acceptance(
        run_id: str,
        body: ArtifactAcceptanceBody,
    ) -> dict[str, Any]:
        """Attach host-owned file evidence; model credentials cannot call this."""

        async with lock_for(run_id):
            run = await store.get_control(run_id)
            if run.origin != "managed":
                raise HTTPException(
                    409,
                    "artifact acceptance is only available for managed runs",
                )
            acceptance = ArtifactAcceptance.model_validate(body.model_dump())
            current = run.artifact_acceptance
            if current is not None:
                current_payload = current.model_dump(exclude={"updated_at"})
                incoming_payload = acceptance.model_dump(
                    exclude={"updated_at"}
                )
                if current_payload == incoming_payload:
                    return _visible_run(run)
                if current.state in {
                    ArtifactAcceptanceState.PASSED,
                    ArtifactAcceptanceState.FAILED,
                }:
                    raise HTTPException(
                        409,
                        "terminal artifact acceptance is immutable",
                    )
                allowed = {
                    ArtifactAcceptanceState.PENDING: {
                        ArtifactAcceptanceState.CAPTURING,
                        ArtifactAcceptanceState.FAILED,
                    },
                    ArtifactAcceptanceState.CAPTURING: {
                        ArtifactAcceptanceState.PASSED,
                        ArtifactAcceptanceState.FAILED,
                    },
                }
                if (
                    current.kind != acceptance.kind
                    or current.label != acceptance.label
                    or acceptance.state not in allowed[current.state]
                ):
                    raise HTTPException(
                        409,
                        "invalid artifact acceptance transition",
                    )
            elif acceptance.state is not ArtifactAcceptanceState.PENDING:
                raise HTTPException(
                    409,
                    "artifact acceptance must start as pending",
                )
            if acceptance.state in {
                ArtifactAcceptanceState.PASSED,
                ArtifactAcceptanceState.FAILED,
            } and run.status not in TERMINAL_RUN_STATUSES:
                raise HTTPException(
                    409,
                    "terminal artifact evidence requires a terminal run",
                )
            if (
                acceptance.state is ArtifactAcceptanceState.PASSED
                and run.status is not RunStatus.COMPLETED
            ):
                raise HTTPException(
                    409,
                    "artifact acceptance cannot pass an incomplete run",
                )
            run.artifact_acceptance = acceptance
            run.record(
                f"artifact.{acceptance.state.value}",
                acceptance_kind=acceptance.kind,
                label=acceptance.label,
                artifact_format=acceptance.artifact_format,
                checks_passed=acceptance.checks_passed,
                checks_total=acceptance.checks_total,
                byte_count=acceptance.byte_count,
                sha256=acceptance.sha256,
                error_class=acceptance.error_class,
            )
            await store.save(run)
        return _visible_run(run)

    @app.post("/api/runs/{run_id}/continue")
    async def continue_run(
        run_id: str,
        background: bool = False,
    ) -> dict[str, Any]:
        if external_driver:
            raise HTTPException(
                409,
                "the benchmark runner owns model progression",
            )
        run = await store.get_state(run_id)
        if run.origin == "direct_mcp":
            if direct_calls is None:
                raise HTTPException(
                    503, "direct-call visibility is not configured"
                )
            return _visible_run(await direct_calls.resume(run_id))
        if background:
            if run.origin != "managed":
                raise HTTPException(
                    409,
                    "only managed runs support background continuation",
                )
            if run.status is not RunStatus.PAUSED or run.pending_approval:
                raise HTTPException(
                    409,
                    "run is not at a continuable checkpoint",
                )
            schedule(guarded_continue(run_id))
            return _visible_run(run)
        return _visible_run(await guarded_continue(run_id))

    @app.post("/api/runs/{run_id}/start")
    async def start_run(run_id: str) -> dict[str, Any]:
        """Start a visible managed run without holding the HTTP request open."""

        if external_driver:
            raise HTTPException(
                409,
                "the benchmark runner owns model progression",
            )
        run = await store.get_state(run_id)
        if run.origin != "managed":
            raise HTTPException(
                409,
                "only managed runs can be started by the harness",
            )
        startable = run.status is RunStatus.RUNNING or (
            run.mode == "assistant"
            and run.status is RunStatus.PLANNING
        )
        if not startable or run.pending_approval:
            raise HTTPException(
                409,
                "run is not at a startable checkpoint",
            )
        schedule(guarded_continue(run_id))
        return _visible_run(run)

    @app.post("/api/runs/{run_id}/pause")
    async def pause_run(run_id: str, body: PauseBody) -> dict[str, Any]:
        if external_driver:
            raise HTTPException(
                409,
                "pause is unavailable while the benchmark runner owns progression",
            )
        # Interrupt model/tool waiting first. A pending action was committed
        # before its MCP call, so an ambiguous cancellation remains resumable
        # with the same idempotency key.
        await cancel_continuations(run_id)
        async with lock_for(run_id):
            existing = await store.get_state(run_id)
            if existing.origin == "direct_mcp":
                if direct_calls is None:
                    raise HTTPException(
                        503, "direct-call visibility is not configured"
                    )
                run = await direct_calls.pause(run_id, body.reason)
            else:
                driver = managed_driver(existing)
                run = await driver.pause(run_id, body.reason)
        return _visible_run(run)

    @app.post("/api/runs/{run_id}/steer")
    async def steer_run(
        run_id: str,
        body: SteerBody,
    ) -> dict[str, Any]:
        if external_driver:
            raise HTTPException(
                409,
                "steering is unavailable while the benchmark runner owns progression",
            )
        await cancel_continuations(run_id)
        async with lock_for(run_id):
            existing = await store.get_state(run_id)
            if existing.origin == "direct_mcp":
                raise HTTPException(
                    409,
                    "direct MCP runs remain controlled by their external client",
                )
            try:
                driver = managed_driver(existing)
                run = await driver.steer(run_id, body.instruction)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        if body.auto_resume:
            schedule(guarded_continue(run_id))
        return _visible_run(run)

    @app.post("/api/runs/{run_id}/approvals/{approval_id}")
    async def resolve_approval(
        run_id: str,
        approval_id: str,
        body: ApprovalBody,
        background: bool = False,
    ) -> dict[str, Any]:
        existing = await store.get_state(run_id)
        decision = {"type": body.type, "reason": body.reason}
        cancelled = False
        managed_approval = existing.origin == "managed" and (
            existing.pending_approval or {}
        ).get("kind") != "virtual_media_attach"
        if managed_approval and background:
            pending = existing.pending_approval or {}
            if (
                existing.status is not RunStatus.NEEDS_APPROVAL
                or pending.get("approval_id") != approval_id
            ):
                raise HTTPException(
                    409,
                    "run is not waiting for this exact approval",
                )
            key = (run_id, approval_id)
            if key in background_approvals:
                raise HTTPException(409, "approval is already resolving")
            background_approvals.add(key)
            schedule(
                resolve_managed_approval_in_background(
                    run_id,
                    approval_id,
                    decision,
                )
            )
            return _visible_run(existing)
        if managed_approval:
            run, cancelled = await guarded_managed_approval(
                run_id,
                approval_id,
                decision,
            )
        else:
            async with lock_for(run_id):
                existing = await store.get_state(run_id)
                pending = existing.pending_approval or {}
                if pending.get("kind") == "virtual_media_attach":
                    if media_transactions is None:
                        raise HTTPException(
                            503, "virtual-media transactions are not configured"
                        )
                    try:
                        run = await media_transactions.resolve_approval(
                            run_id,
                            approval_id,
                            decision,
                        )
                    except ValueError as exc:
                        raise HTTPException(409, str(exc)) from exc
                elif existing.origin == "direct_mcp":
                    if direct_calls is None:
                        raise HTTPException(
                            503, "direct-call visibility is not configured"
                        )
                    try:
                        run = await direct_calls.resolve_approval(
                            run_id, approval_id, decision
                        )
                    except ValueError as exc:
                        raise HTTPException(409, str(exc)) from exc
                else:  # pragma: no cover - managed handled above
                    run = await harness.resolve_approval(
                        run_id,
                        approval_id,
                        decision,
                    )
        if (
            not cancelled
            and _continue_after_approval(run)
        ):
            schedule(guarded_continue(run_id))
        return _visible_run(run)

    @app.post("/api/runs/{run_id}/abort")
    async def abort_run(run_id: str, body: AbortBody) -> dict[str, Any]:
        await cancel_continuations(run_id)
        async with lock_for(run_id):
            existing = await store.get_state(run_id)
            if existing.origin == "direct_mcp":
                if direct_calls is None:
                    raise HTTPException(
                        503, "direct-call visibility is not configured"
                    )
                try:
                    run = await direct_calls.abort(run_id, body.reason)
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from exc
            else:
                driver = managed_driver(existing)
                run = await driver.abort(run_id, body.reason)
            transaction = run.media_transaction
            if (
                media_transactions is not None
                and transaction is not None
                and transaction.state
                in {
                    MediaTransactionState.ATTACHED,
                    MediaTransactionState.VERIFIED,
                }
            ):
                run = await media_transactions.release(
                    run_id,
                    f"emergency stop: {body.reason}",
                )
        return _visible_run(run)

    resolved_ui = ui_dir or Path(__file__).resolve().parent.parent / "harness_ui"
    if resolved_ui.is_dir():
        app.mount(
            "/app",
            StaticFiles(directory=resolved_ui, html=True),
            name="harness-ui",
        )

        @app.get("/", include_in_schema=False)
        async def index() -> RedirectResponse:
            return RedirectResponse("/app/")

    return app


def _image_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "image/jpeg"


def _verification_image_response(
    image_path: str,
    *,
    kind: str = "before_after",
    revision: int | None = None,
    action_index: int | None = None,
    before_frame_id: int | None = None,
    after_frame_id: int | None = None,
) -> Response:
    path = Path(image_path)
    if not path.is_file():
        raise HTTPException(
            404, "verification image artifact is unavailable"
        )
    headers = {
        "Cache-Control": "no-store",
        "X-PiKVM-Evidence-Mode": kind.replace("_", "-"),
    }
    for name, value in {
        "X-PiKVM-Evidence-Revision": revision,
        "X-PiKVM-Action-Index": action_index,
        "X-PiKVM-Before-Frame": before_frame_id,
        "X-PiKVM-After-Frame": after_frame_id,
    }.items():
        if value is not None:
            headers[name] = str(value)
    return Response(
        content=path.read_bytes(),
        media_type=_image_mime(path),
        headers=headers,
    )


def _verification_click_target_response(
    image_path: str,
    *,
    kind: str = "before_after",
    x: int,
    y: int,
    screen_width: int,
    screen_height: int,
    revision: int,
    action_index: int,
) -> Response:
    """Return a small marked crop from the pre-action evidence."""

    path = Path(image_path)
    if not path.is_file():
        raise HTTPException(
            404, "verification image artifact is unavailable"
        )
    try:
        with Image.open(path) as source:
            source.load()
            if kind == "pre_action":
                panel_width = source.width
                label_height = 0
                panel_height = source.height
            else:
                panel_width = source.width // 2
                label_height = min(32, max(0, source.height - 1))
                panel_height = source.height - label_height
            if panel_width < 2 or panel_height < 2:
                raise HTTPException(422, "visual evidence image is too small")

            target_x = round(
                min(screen_width, x) / screen_width * (panel_width - 1)
            )
            target_y = round(
                min(screen_height, y) / screen_height * (panel_height - 1)
            )
            crop_width = min(
                panel_width,
                max(160, round(panel_width * 0.25)),
            )
            crop_height = min(
                panel_height,
                max(90, round(crop_width * 9 / 16)),
            )
            if crop_height == panel_height:
                crop_width = min(
                    crop_width,
                    max(2, round(crop_height * 16 / 9)),
                )
            left = min(
                panel_width - crop_width,
                max(0, target_x - crop_width // 2),
            )
            top = min(
                panel_height - crop_height,
                max(0, target_y - crop_height // 2),
            )
            cropped = source.convert("RGB").crop(
                (
                    left,
                    label_height + top,
                    left + crop_width,
                    label_height + top + crop_height,
                )
            )
            preview = cropped.resize((240, 135), Image.Resampling.LANCZOS)
            marker_x = round((target_x - left) / crop_width * preview.width)
            marker_y = round((target_y - top) / crop_height * preview.height)
            draw = ImageDraw.Draw(preview)
            for radius, color, width in (
                (10, "#050608", 5),
                (8, "#8ff0b5", 3),
                (2, "#f5fff8", 2),
            ):
                draw.ellipse(
                    (
                        marker_x - radius,
                        marker_y - radius,
                        marker_x + radius,
                        marker_y + radius,
                    ),
                    outline=color,
                    width=width,
                )
            payload = BytesIO()
            preview.save(payload, format="PNG", optimize=True)
    except (OSError, UnidentifiedImageError) as exc:
        raise HTTPException(
            404, "verification image artifact is unreadable"
        ) from exc

    return Response(
        content=payload.getvalue(),
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "X-PiKVM-Evidence-Mode": "click-target",
            "X-PiKVM-Evidence-Revision": str(revision),
            "X-PiKVM-Action-Index": str(action_index),
        },
    )


def _sse_event(
    event: str,
    data: dict[str, Any],
    *,
    event_id: int | None = None,
    retry_ms: int | None = None,
) -> str:
    lines: list[str] = []
    if retry_ms is not None:
        lines.append(f"retry: {retry_ms}")
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    lines.append(f"data: {payload}")
    return "\n".join(lines) + "\n\n"


def _visible_run(
    run: Any,
    *,
    event_count: int | None = None,
    event_cursor: int | None = None,
    timeline_events: list[Any] | None = None,
    timeline_events_truncated: bool = False,
) -> dict[str, Any]:
    """Serialize a run without exposing secret-marked input to UI/API clients."""

    visible_event_count = len(run.events)
    snapshot_event_cursor = max(
        int(getattr(run, "event_cursor", 0) or 0),
        run.events[-1].sequence if run.events else 0,
    )
    durable_event_count = (
        snapshot_event_cursor if event_count is None else event_count
    )
    durable_event_cursor = (
        snapshot_event_cursor if event_cursor is None else (event_cursor or 0)
    )
    payload = run.model_dump(
        mode="json",
        exclude={
            "events",
            "latest_verification_image_path",
            "verification_images",
        },
    )
    observation = payload.get("observation")
    if isinstance(observation, dict):
        observation.pop("image_path", None)
        observation.pop("raw", None)
    payload["verification_image_available"] = bool(
        run.latest_verification_image_path
    )
    payload["verification_image_revision"] = (
        run.latest_verification_image_revision
    )
    payload["verification_images"] = [
        evidence.model_dump(mode="json", exclude={"path"})
        for evidence in run.verification_images
    ]
    payload["events"] = [
        event.model_dump(mode="json")
        for event in run.events[-RUN_EVENT_TAIL_LIMIT:]
    ]
    visible_timeline = (
        timeline_events
        if timeline_events is not None
        else [
            event
            for event in run.events
            if event.kind in RUN_TIMELINE_EVENT_KINDS
        ]
    )
    payload["timeline_events"] = [
        event.model_dump(mode="json")
        for event in visible_timeline[-RUN_TIMELINE_EVENT_LIMIT:]
    ]
    payload["timeline_events_truncated"] = timeline_events_truncated or (
        len(visible_timeline) > RUN_TIMELINE_EVENT_LIMIT
    )
    payload["event_count"] = durable_event_count
    payload["event_cursor"] = durable_event_cursor
    payload["events_truncated"] = (
        durable_event_count > len(payload["events"])
    )
    return redact_secrets(payload)


def _visible_run_summary(run: Any) -> dict[str, Any]:
    """Return the light run-rail contract without durable event history."""

    payload = {
        "run_id": run.run_id,
        "task": run.task,
        "status": run.status.value,
        "mode": run.mode,
        "origin": run.origin,
        "model_provider": run.model_provider,
        "model_route": (
            run.model_route.model_dump(mode="json", exclude_none=True)
            if run.model_route is not None
            else None
        ),
        "caller": run.caller,
        "session_id": run.session_id,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "error": run.error,
        "event_count": run.event_count,
        "event_cursor": run.event_cursor,
        "artifact_acceptance_state": (
            run.artifact_acceptance_state.value
            if run.artifact_acceptance_state is not None
            else None
        ),
    }
    return redact_secrets(payload)
