"""FastAPI daemon — the long-running owner of sessions, watchers, and execution.

The MCP server is a thin facade over these HTTP endpoints. Routes mirror
``docs/PLAN.md`` → *FastAPI daemon*. The runtime is created once at startup and
shared across requests.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from pikvm_agent.config import AppConfig
from pikvm_agent.core.errors import PikvmAgentError, SessionNotFoundError
from pikvm_agent.runtime import Runtime

_WEBUI = Path(__file__).resolve().parent / "webui"


class StartSessionRequest(BaseModel):
    task: str
    policy: dict[str, Any] = {}
    operator: dict[str, Any] = {}


class AbortRequest(BaseModel):
    reason: str = ""


class BurstRequest(BaseModel):
    # A controller-authored HID burst (the fast path). Actions are raw HID steps; the
    # daemon gates on freshness + control, runs them locally, returns one screenshot.
    actions: list[dict[str, Any]] = []
    based_on_world_version: int | None = None
    based_on_control_epoch: int | None = None
    max_runtime_ms: int = 4000
    return_screenshot: bool = True


class ContinueRequest(BaseModel):
    # Per-call budget. None = unbounded (the daemon-direct default). The MCP facade
    # passes small values so a single continue can't run on for minutes.
    max_transactions: int | None = None
    max_runtime_ms: int | None = None


def create_app(config: AppConfig | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = await Runtime.from_config(config)
        try:
            yield
        finally:
            await app.state.runtime.aclose()

    app = FastAPI(title="PiKVM Agent Daemon", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def _timing(request: Request, call_next):
        import time as _t

        from pikvm_agent.debuglog import DEBUG

        start = _t.monotonic()
        status = 0
        try:
            resp = await call_next(request)
            status = resp.status_code
            return resp
        finally:
            DEBUG.event("http", method=request.method, path=request.url.path,
                        status=status, dur_ms=round((_t.monotonic() - start) * 1000, 1))

    def rt(request: Request) -> Runtime:
        return request.app.state.runtime

    @app.exception_handler(SessionNotFoundError)
    async def _not_found(_request: Request, exc: SessionNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": "session_not_found", "detail": str(exc)})

    @app.exception_handler(PikvmAgentError)
    async def _runtime_error(_request: Request, exc: PikvmAgentError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": exc.__class__.__name__, "detail": str(exc)})

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        return await rt(request).status()

    # ---- human console (Phase 7) ----------------------------------------- #

    @app.get("/", response_class=HTMLResponse)
    async def console() -> str:
        return (_WEBUI / "console.html").read_text()

    @app.get("/ui/app.js", response_class=PlainTextResponse)
    async def console_js() -> PlainTextResponse:
        return PlainTextResponse((_WEBUI / "app.js").read_text(),
                                 media_type="application/javascript")

    @app.get("/sessions")
    async def list_sessions(request: Request) -> list[dict[str, Any]]:
        return await rt(request).list_sessions()

    @app.get("/sessions/{session_id}/frame")
    async def session_frame(session_id: str, request: Request):
        path = rt(request).latest_frame_path(session_id)
        if not path or not Path(path).exists():
            return JSONResponse(status_code=404, content={"error": "no_frame"})
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/sessions/{session_id}/approvals")
    async def session_approvals(session_id: str, request: Request) -> list[dict[str, Any]]:
        return await rt(request).pending_approvals(session_id)

    @app.get("/sessions/{session_id}/trace")
    async def session_trace(session_id: str, request: Request) -> list[dict[str, Any]]:
        return rt(request).recent_trace(session_id)

    @app.post("/sessions")
    async def start_session(req: StartSessionRequest, request: Request) -> dict[str, Any]:
        return await rt(request).start_session(req.task, req.policy, req.operator)

    @app.post("/sessions/{session_id}/burst")
    async def run_burst(session_id: str, body: BurstRequest, request: Request) -> dict[str, Any]:
        return await rt(request).run_burst(
            session_id, body.actions,
            based_on_world_version=body.based_on_world_version,
            based_on_control_epoch=body.based_on_control_epoch,
            max_runtime_ms=body.max_runtime_ms, return_screenshot=body.return_screenshot)

    @app.post("/sessions/{session_id}/parse")
    async def parse_screen(session_id: str, request: Request) -> dict[str, Any]:
        return await rt(request).parse_screen_now(session_id)

    @app.post("/sessions/{session_id}/ocr-region")
    async def ocr_region(session_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
        return await rt(request).ocr_region(session_id, int(body.get("x", 0)), int(body.get("y", 0)),
                                            int(body.get("w", 0)), int(body.get("h", 0)))

    @app.post("/sessions/{session_id}/find-text")
    async def find_text(session_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
        return await rt(request).find_text(session_id, str(body.get("text", "")))

    @app.post("/sessions/{session_id}/continue")
    async def continue_session(session_id: str, request: Request,
                               body: ContinueRequest | None = None) -> dict[str, Any]:
        b = body or ContinueRequest()
        return await rt(request).continue_session(session_id, b.max_transactions, b.max_runtime_ms)

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str, request: Request,
                          capture: bool = False) -> dict[str, Any]:
        # Read-only by default so UI polling can't drive screenshot captures or spam the
        # trace; pikvm_observe passes capture=true for an explicit fresh look.
        return await rt(request).get_session_summary(session_id, capture=capture)

    @app.post("/sessions/{session_id}/approvals/{approval_id}")
    async def approval(session_id: str, approval_id: str, decision: dict[str, Any],
                       request: Request) -> dict[str, Any]:
        return await rt(request).submit_approval(session_id, approval_id, decision)

    @app.post("/sessions/{session_id}/abort")
    async def abort(session_id: str, body: AbortRequest, request: Request) -> dict[str, Any]:
        return await rt(request).abort_session(session_id, body.reason)

    @app.post("/panic-stop")
    async def panic_stop(request: Request) -> dict[str, Any]:
        # Emergency brake — out-of-band, no agent involved. Halts every session.
        return await rt(request).panic_stop()

    @app.get("/sessions/{session_id}/memory-update")
    async def memory_update(session_id: str, request: Request) -> dict[str, Any]:
        return await rt(request).export_memory_update(session_id)

    return app


app = create_app()
