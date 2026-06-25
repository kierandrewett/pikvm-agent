"""FastAPI daemon — the long-running owner of sessions, watchers, and execution.

The MCP server is a thin facade over these HTTP endpoints. Routes mirror
``docs/PLAN.md`` → *FastAPI daemon*. The runtime is created once at startup and
shared across requests.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from pikvm_agent.config import AppConfig
from pikvm_agent.core.errors import PikvmAgentError, SessionNotFoundError
from pikvm_agent.runtime import Runtime


class StartSessionRequest(BaseModel):
    task: str
    policy: dict[str, Any] = {}
    operator: dict[str, Any] = {}


class AbortRequest(BaseModel):
    reason: str = ""


def create_app(config: AppConfig | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = await Runtime.from_config(config)
        try:
            yield
        finally:
            await app.state.runtime.aclose()

    app = FastAPI(title="PiKVM Agent Daemon", version="0.1.0", lifespan=lifespan)

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

    @app.post("/sessions")
    async def start_session(req: StartSessionRequest, request: Request) -> dict[str, Any]:
        return await rt(request).start_session(req.task, req.policy, req.operator)

    @app.post("/sessions/{session_id}/continue")
    async def continue_session(session_id: str, request: Request) -> dict[str, Any]:
        return await rt(request).continue_session(session_id)

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str, request: Request) -> dict[str, Any]:
        return await rt(request).get_session_summary(session_id)

    @app.post("/sessions/{session_id}/approvals/{approval_id}")
    async def approval(session_id: str, approval_id: str, decision: dict[str, Any],
                       request: Request) -> dict[str, Any]:
        return await rt(request).submit_approval(session_id, approval_id, decision)

    @app.post("/sessions/{session_id}/abort")
    async def abort(session_id: str, body: AbortRequest, request: Request) -> dict[str, Any]:
        return await rt(request).abort_session(session_id, body.reason)

    @app.get("/sessions/{session_id}/memory-update")
    async def memory_update(session_id: str, request: Request) -> dict[str, Any]:
        return await rt(request).export_memory_update(session_id)

    return app


app = create_app()
