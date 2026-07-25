"""Persistent MCP adapter used by the agent harness.

This is intentionally a client of the public raw PiKVM MCP server rather than a
shortcut into daemon internals.  Production orchestration and replay therefore
exercise the same contract that Claude/Codex previously drove by hand.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pikvm_agent.harness.agent_models import ComputerObservation
from pikvm_agent.harness.mcp_driver import unpack_tool_result


def harness_child_environment(
    daemon_url: str, *, inherited: dict[str, str] | None = None
) -> dict[str, str]:
    """Build the private raw-MCP child environment.

    The child can relay an approval already made in the operator API. It must
    never inherit the external direct-call observer, otherwise managed harness
    calls would be duplicated as model-driven calls.
    """

    env = dict(os.environ if inherited is None else inherited)
    env["PIKVM_AGENT_DAEMON"] = daemon_url
    env["PIKVM_AGENT_TRUSTED_APPROVAL_CLIENT"] = "1"
    for key in (
        "PIKVM_HARNESS_OBSERVER_URL",
        "PIKVM_HARNESS_OBSERVER_TOKEN",
        "PIKVM_HARNESS_OBSERVER_MODE",
        "PIKVM_MCP_CALLER_LABEL",
        "PIKVM_MCP_PROVIDER",
        "PIKVM_MCP_MODEL",
    ):
        env.pop(key, None)
    return env


class ToolClient(Protocol):
    async def call(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]: ...


class PersistentMcpToolClient:
    """One stdio child/session for an entire harness process."""

    def __init__(
        self,
        *,
        daemon_url: str,
        artifact_dir: Path,
        executable: str | None = None,
    ) -> None:
        self.daemon_url = daemon_url
        self.artifact_dir = artifact_dir
        self.executable = executable or sys.executable
        self._stdio_context: Any = None
        self._session_context: Any = None
        self._session: ClientSession | None = None
        self._start_lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._session is not None:
            return
        async with self._start_lock:
            if self._session is not None:
                return
            env = harness_child_environment(self.daemon_url)
            params = StdioServerParameters(
                command=self.executable,
                args=["-m", "pikvm_agent.cli", "mcp"],
                env=env,
                cwd=Path(__file__).resolve().parents[2],
            )
            self._stdio_context = stdio_client(params)
            reader, writer = await self._stdio_context.__aenter__()
            try:
                self._session_context = ClientSession(reader, writer)
                self._session = await self._session_context.__aenter__()
                await self._session.initialize()
            except Exception:
                self._session = None
                await self._stdio_context.__aexit__(*sys.exc_info())
                self._stdio_context = None
                self._session_context = None
                raise

    async def close(self) -> None:
        async with self._start_lock:
            if self._session_context is not None:
                await self._session_context.__aexit__(None, None, None)
            if self._stdio_context is not None:
                await self._stdio_context.__aexit__(None, None, None)
            self._session = None
            self._session_context = None
            self._stdio_context = None

    async def __aenter__(self) -> "PersistentMcpToolClient":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def call(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        await self.start()
        if self._session is None:  # pragma: no cover - guarded by start
            raise RuntimeError("MCP session did not start")
        # FastMCP sessions are request/response streams: serialize calls to keep
        # action results and their screenshot artifact directories unambiguous.
        async with self._call_lock:
            call_dir = self.artifact_dir / str(uuid.uuid4())
            result = await self._session.call_tool(name, arguments=arguments)
            return unpack_tool_result(result, call_dir)


class McpComputerDriver:
    def __init__(self, client: ToolClient) -> None:
        self.client = client

    async def open(self, label: str) -> ComputerObservation:
        return self._observation(
            await self.client.call("pikvm_open", {"label": label})
        )

    async def refresh(self, *, session_id: str) -> ComputerObservation:
        return self._observation(
            await self.client.call(
                "pikvm_screenshot",
                {"session_id": session_id},
            )
        )

    async def burst(
        self,
        *,
        session_id: str,
        actions: list[dict[str, Any]],
        based_on_world_version: int | None,
        based_on_control_epoch: int | None,
        idempotency_key: str,
    ) -> ComputerObservation:
        return self._observation(
            await self.client.call(
                "pikvm_run_burst",
                {
                    "session_id": session_id,
                    "actions": actions,
                    "based_on_world_version": based_on_world_version,
                    "based_on_control_epoch": based_on_control_epoch,
                    "idempotency_key": idempotency_key,
                },
            )
        )

    async def resolve_approval(
        self, *, session_id: str, approval_id: str, decision: dict[str, Any]
    ) -> ComputerObservation:
        return self._observation(
            await self.client.call(
                "pikvm_resolve_approval",
                {
                    "session_id": session_id,
                    "approval_id": approval_id,
                    "decision": decision,
                },
            )
        )

    async def abort(
        self, *, session_id: str, reason: str
    ) -> ComputerObservation:
        return self._observation(
            await self.client.call(
                "pikvm_abort", {"session_id": session_id, "reason": reason}
            )
        )

    @staticmethod
    def _observation(result: dict[str, Any]) -> ComputerObservation:
        state = result.get("state")
        if result.get("is_error") or not isinstance(state, dict):
            text = "\n".join(result.get("texts") or [])
            raise RuntimeError(text or "MCP tool returned no state")
        images = result.get("images") or []
        return ComputerObservation(
            session_id=str(state.get("session_id") or ""),
            status=str(state.get("status") or "unknown"),
            machine=(
                state.get("machine")
                if isinstance(state.get("machine"), dict)
                else {}
            ),
            frame_id=state.get("frame_id"),
            world_version=state.get("world_version"),
            control_epoch=state.get("control_epoch"),
            width=state.get("width"),
            height=state.get("height"),
            image_path=str(images[0]) if images else None,
            approval_request=state.get("approval_request"),
            error=state.get("error"),
            raw=state,
        )
