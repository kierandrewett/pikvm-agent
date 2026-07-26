"""High-level MCP facade for the visible PiKVM operator harness.

This server intentionally exposes no click/type/burst or approval tools.
Claude Code, Codex, and other MCP clients submit a task and inspect or control
non-approval checkpoints. The harness UI and its provider-neutral engine own
the live screen/action loop and human approval boundary.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from pikvm_agent.harness.client_setup import normalize_caller_label

_INSTRUCTIONS = """\
This is the high-level PiKVM operator harness. Submit a goal with
computer_start_task; the dedicated harness plans, drives the raw PiKVM MCP
tools, crosses its own bounded action slices and safe replans, verifies
evidence, and exposes every checkpoint in its operator UI. The coding client
does not clock the action loop. Use computer_status to inspect progress and
computer_continue only to resume a meaningful paused checkpoint.
When status=needs_approval, direct the user to the operator UI; this MCP server
cannot approve its own proposed action. computer_pause interrupts model progress
without discarding a checkpointed action; computer_abort is the emergency stop.
Raw click/type/HID tools are intentionally not exposed here.
"""

mcp = FastMCP("pikvm-harness", json_response=True, instructions=_INSTRUCTIONS)


def _connection() -> tuple[str, str]:
    url = os.environ.get("PIKVM_HARNESS_URL", "").strip().rstrip("/")
    token = os.environ.get("PIKVM_HARNESS_AGENT_TOKEN", "")
    if not url:
        raise RuntimeError("PIKVM_HARNESS_URL is not set")
    if len(token) < 32:
        raise RuntimeError(
            "PIKVM_HARNESS_AGENT_TOKEN is missing or too short"
        )
    return url, token


async def _request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    approval_id: str | None = None,
) -> Any:
    url, token = _connection()
    headers = {"Authorization": f"Bearer {token}"}
    if approval_id:
        headers["X-PiKVM-Approval-Intent"] = approval_id
    try:
        async with httpx.AsyncClient(base_url=url, timeout=300.0) as client:
            response = await client.request(
                method, path, headers=headers, json=body
            )
    except (httpx.ConnectError, httpx.TimeoutException):
        raise RuntimeError(
            "managed harness unavailable; keep this task in managed mode and "
            "retry after the operator harness restarts"
        ) from None
    except httpx.RequestError:
        raise RuntimeError(
            "managed harness transport failed; keep this task in managed mode "
            "and ask the operator to inspect the harness"
        ) from None
    if response.status_code in {401, 403}:
        raise RuntimeError(
            "managed harness authorization failed; the operator must repair "
            "the scoped agent credential"
        )
    if response.status_code == 404:
        raise RuntimeError("managed harness run was not found")
    if response.status_code >= 500:
        raise RuntimeError(
            "managed harness service failed; retry after the operator service "
            "recovers"
        )
    if response.is_error:
        raise RuntimeError(
            f"managed harness refused the request (HTTP {response.status_code})"
        )
    try:
        return response.json()
    except ValueError:
        raise RuntimeError(
            "managed harness returned an invalid response"
        ) from None


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
async def computer_start_task(task: str) -> dict[str, Any]:
    """Create and begin a visible guarded computer task.

    Returns the run ID, initial screen state, and current checkpoint. Open the
    operator UI from PIKVM_HARNESS_URL for the live frame and event timeline.
    """

    result = await _request(
        "POST",
        "/api/runs",
        body={
            "task": task,
            "auto_start": True,
            "source_client": normalize_caller_label(
                os.environ.get("PIKVM_MCP_CALLER_LABEL", "mcp-client")
            ),
        },
    )
    url, _ = _connection()
    result["operator_ui"] = f"{url}/app/"
    return result


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def computer_status(run_id: str) -> dict[str, Any]:
    """Read the run's plan, status, current frame metadata, and event history."""

    return await _request("GET", f"/api/runs/{run_id}")


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
async def computer_continue(run_id: str) -> dict[str, Any]:
    """Resume a meaningful paused run. Does not bypass a pending approval."""

    return await _request("POST", f"/api/runs/{run_id}/continue", body={})


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def computer_pause(
    run_id: str, reason: str = "paused by user"
) -> dict[str, Any]:
    """Interrupt the control loop and retain its resumable checkpoint."""

    return await _request(
        "POST", f"/api/runs/{run_id}/pause", body={"reason": reason}
    )


async def computer_resolve_approval(
    run_id: str, approval_id: str, decision: str, reason: str = ""
) -> dict[str, Any]:
    """Compatibility helper for trusted host code; not exposed as an MCP tool.

    Browser-origin enforcement at the API means ordinary MCP callers cannot use
    this helper to approve their own proposed actions.
    """

    if decision not in {"approve", "reject", "take_over"}:
        raise ValueError("decision must be approve, reject, or take_over")
    return await _request(
        "POST",
        f"/api/runs/{run_id}/approvals/{approval_id}",
        body={"type": decision, "reason": reason},
        approval_id=approval_id,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def computer_abort(
    run_id: str, reason: str = "stopped by user"
) -> dict[str, Any]:
    """Emergency-stop a run and release held input."""

    return await _request(
        "POST", f"/api/runs/{run_id}/abort", body={"reason": reason}
    )


def main() -> None:  # pragma: no cover
    from pikvm_agent.harness.stdio_transport import run_fastmcp_stdio

    run_fastmcp_stdio(mcp)


if __name__ == "__main__":  # pragma: no cover
    main()
