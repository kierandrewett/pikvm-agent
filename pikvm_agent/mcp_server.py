"""MCP facade — a thin stdio server over the daemon's HTTP API.

This process owns no state. Every tool forwards to the FastAPI daemon, which
owns sessions, watchers, the operator loop, approvals, and execution. Raw HID
tools are intentionally NOT exposed as normal tools (see AGENTS.md); they can be
enabled for harness debugging only via PIKVM_AGENT_ENABLE_DEBUG_HID=1.

Run with:  pikvm-agent mcp   (or  python -m pikvm_agent.mcp_server)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

DAEMON_URL = os.environ.get("PIKVM_AGENT_DAEMON", "http://127.0.0.1:47615")

mcp = FastMCP("pikvm", json_response=True)


def _daemon_client(timeout: float) -> httpx.AsyncClient:
    """Factory for the daemon HTTP client. Patched in tests to talk to an
    in-process ASGI app instead of a live port."""
    return httpx.AsyncClient(base_url=DAEMON_URL, timeout=timeout)


async def _get(path: str, timeout: float = 60.0) -> dict[str, Any]:
    async with _daemon_client(timeout) as client:
        resp = await client.get(path)
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, json: dict[str, Any] | None = None, timeout: float = 60.0) -> dict[str, Any]:
    async with _daemon_client(timeout) as client:
        resp = await client.post(path, json=json or {})
        resp.raise_for_status()
        return resp.json()


# Strong refs to in-flight abort tasks so the GC doesn't drop a detached one.
_pending_aborts: set[asyncio.Task[Any]] = set()


async def _abort_quietly(session_id: str) -> None:
    """Best-effort abort used when a blocking call is cancelled. Never raises."""
    with contextlib.suppress(Exception):
        await _post(f"/sessions/{session_id}/abort", {"reason": "cancelled by caller"}, timeout=15.0)


def _fire_abort(session_id: str) -> asyncio.Task[None]:
    task = asyncio.ensure_future(_abort_quietly(session_id))
    _pending_aborts.add(task)
    task.add_done_callback(_pending_aborts.discard)
    return task


async def _run_or_abort(session_id: str, coro: Any) -> dict[str, Any]:
    """Await a loop-running daemon call; if the CALLER cancels it (e.g. you press
    Esc in Claude), abort the session too — so interrupting the agent actually
    halts the machine instead of leaving the daemon driving on its own."""
    try:
        return await coro
    except asyncio.CancelledError:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(_fire_abort(session_id))
        raise


@mcp.tool()
async def pikvm_start_task(task: str, policy: dict | None = None,
                           operator: dict | None = None) -> dict:
    """Start a guarded PiKVM computer-use session for a high-level task."""
    return await _post("/sessions", {"task": task, "policy": policy or {}, "operator": operator or {}})


@mcp.tool()
async def pikvm_continue(session_id: str) -> dict:
    """Continue a session until the next checkpoint, approval, or completion.

    If the caller cancels this call (e.g. you press Esc in Claude), the daemon run
    is aborted too, so interrupting the agent actually stops the machine."""
    return await _run_or_abort(session_id, _post(f"/sessions/{session_id}/continue", timeout=900.0))


@mcp.tool()
async def pikvm_observe(session_id: str) -> dict:
    """Return the current screen summary: frame id, world version, events, and
    the screenshot path."""
    return await _get(f"/sessions/{session_id}")


@mcp.tool()
async def pikvm_approve(session_id: str, approval_id: str, decision: dict) -> dict:
    """Approve / edit / reject / respond to a pending approval request.

    Resuming runs more actions, so cancelling this call aborts the session too."""
    return await _run_or_abort(
        session_id, _post(f"/sessions/{session_id}/approvals/{approval_id}", decision)
    )


@mcp.tool()
async def pikvm_abort(session_id: str, reason: str = "") -> dict:
    """Abort a PiKVM session."""
    return await _post(f"/sessions/{session_id}/abort", {"reason": reason})


@mcp.tool()
async def pikvm_export_memory_update(session_id: str) -> dict:
    """Export a safe Atlas memory-update proposal from the session trace."""
    return await _get(f"/sessions/{session_id}/memory-update")


if os.environ.get("PIKVM_AGENT_ENABLE_DEBUG_HID") == "1":  # pragma: no cover
    @mcp.tool()
    async def debug_pikvm_raw(session_id: str, action: dict) -> dict:
        """DEBUG-ONLY raw HID passthrough (disabled unless explicitly enabled)."""
        return await _post(f"/sessions/{session_id}/debug/hid", action)


def main() -> None:  # pragma: no cover - stdio entrypoint
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
