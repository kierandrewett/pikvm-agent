"""Fail-closed direct-MCP visibility and operator-control transport.

Every public MCP request is registered with the harness before its tool body
runs and completed afterward. Guarded mode fails closed if that preflight
cannot be recorded. Degraded observe mode permits only perception and emergency
stop tools when preflight is unavailable; HID and session-start tools still
fail closed. Completion reporting never turns a completed action into an
ambiguous tool failure. The managed harness may explicitly construct its
private child with ``allow_unobserved=True`` because that child's actions are
already represented by the managed run.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from pikvm_agent.endpoint import httpx_client_kwargs

_OBSERVE_MODE_TOOLS = frozenset(
    {
        "pikvm_screenshot",
        "pikvm_parse_screen",
        "pikvm_ocr_region",
        "pikvm_find_text",
        "pikvm_abort",
        "pikvm_panic_stop",
    }
)


class DirectCallReporter:
    def __init__(
        self,
        *,
        base_url: str,
        observer_token: str,
        mode: str = "guarded",
        transport: httpx.AsyncBaseTransport | None = None,
        caller: dict[str, str] | None = None,
    ) -> None:
        base_url = base_url.strip().rstrip("/")
        if not base_url:
            raise ValueError("direct-call harness URL is empty")
        if len(observer_token) < 32:
            raise ValueError(
                "direct-call harness token must contain at least 32 characters"
            )
        if mode not in {"guarded", "observe"}:
            raise ValueError(
                "direct-call visibility mode must be guarded or observe"
            )
        self.base_url = base_url
        self.observer_token = observer_token
        self.mode = mode
        self.transport = transport
        self.caller = dict(caller or {})

    @classmethod
    def from_env(cls) -> "DirectCallReporter | None":
        url = os.environ.get("PIKVM_HARNESS_OBSERVER_URL", "").strip()
        token = os.environ.get("PIKVM_HARNESS_OBSERVER_TOKEN", "")
        if not url and not token:
            return None
        return cls(
            base_url=url,
            observer_token=token,
            mode=(
                os.environ.get(
                    "PIKVM_HARNESS_OBSERVER_MODE", "guarded"
                )
                .strip()
                .lower()
            ),
            caller={
                "label": os.environ.get("PIKVM_MCP_CALLER_LABEL", ""),
                "provider": os.environ.get("PIKVM_MCP_PROVIDER", ""),
                "model": os.environ.get("PIKVM_MCP_MODEL", ""),
            },
        )

    async def begin(
        self,
        *,
        call_id: str,
        tool: str,
        arguments: dict[str, Any],
        caller: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        payload_caller = dict(self.caller)
        payload_caller.update(
            {
                key: value
                for key, value in (caller or {}).items()
                if value
            }
        )
        try:
            response = await self._post(
                "/api/direct/calls/begin",
                {
                    "call_id": call_id,
                    "tool": tool,
                    "arguments": arguments,
                    "caller": payload_caller,
                },
            )
        except Exception as exc:
            if self.mode == "observe" and tool in _OBSERVE_MODE_TOOLS:
                return None
            raise ToolError(
                "operator harness preflight unavailable; "
                "direct MCP action was not executed"
            ) from exc
        if not response.get("allowed"):
            reason = str(response.get("reason") or "operator blocked the call")
            raise ToolError(f"direct MCP call blocked: {reason}")
        return response

    async def finish(
        self,
        *,
        call_id: str,
        run_id: str = "",
        status: str,
        latency_ms: int,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        try:
            await self._post(
                "/api/direct/calls/finish",
                {
                    "call_id": call_id,
                    "run_id": run_id,
                    "status": status,
                    "latency_ms": latency_ms,
                    "result": result or {},
                    "error": error,
                },
            )
        except Exception:
            # HID may already have happened. Raising here makes the client see
            # an ambiguous failure and encourages a dangerous retry.
            return

    async def _post(
        self, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            **httpx_client_kwargs(self.base_url, self.transport),
            timeout=3.0,
            headers={
                "Authorization": f"Bearer {self.observer_token}",
                "Accept": "application/json",
            },
        ) as client:
            response = await client.post(path, json=payload)
            response.raise_for_status()
            return response.json()


class VisibleFastMCP(FastMCP):
    """FastMCP whose public tool boundary is visible to the operator harness."""

    def __init__(
        self,
        *args: Any,
        reporter_factory: Callable[
            [], DirectCallReporter | None
        ] = DirectCallReporter.from_env,
        allow_unobserved: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._reporter_factory = reporter_factory
        self._allow_unobserved = allow_unobserved

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[Any] | dict[str, Any]:
        try:
            reporter = self._reporter_factory()
        except Exception as exc:
            raise ToolError(
                "direct-call harness configuration is invalid; "
                "tool was not executed"
            ) from exc
        if reporter is None and not self._allow_unobserved:
            raise ToolError(
                "operator visibility is not configured; tool was not executed"
            )
        if reporter is None:
            return await super().call_tool(name, arguments)

        call_id = str(uuid.uuid4())
        decision = await reporter.begin(
            call_id=call_id,
            tool=name,
            arguments=arguments,
            caller=self._mcp_client_identity(),
        )
        run_id = str((decision or {}).get("run_id") or "")
        started = time.monotonic()
        try:
            result = await super().call_tool(name, arguments)
        except BaseException as exc:
            await reporter.finish(
                call_id=call_id,
                run_id=run_id,
                status="failed",
                latency_ms=round((time.monotonic() - started) * 1_000),
                error=type(exc).__name__,
            )
            raise
        await reporter.finish(
            call_id=call_id,
            run_id=run_id,
            status="completed",
            latency_ms=round((time.monotonic() - started) * 1_000),
            result=extract_tool_state(result),
        )
        return result

    def _mcp_client_identity(self) -> dict[str, str]:
        try:
            context = self.get_context()
            parameters = context.session.client_params
            client = getattr(parameters, "clientInfo", None)
            if client is None:
                client = getattr(parameters, "client_info", None)
            return {
                "name": str(getattr(client, "name", "") or ""),
                "version": str(getattr(client, "version", "") or ""),
            }
        except Exception:
            return {}


def extract_tool_state(
    result: Sequence[Any] | dict[str, Any],
) -> dict[str, Any]:
    """Extract only structured text state; image blocks never enter telemetry."""

    if isinstance(result, dict):
        return result
    for block in reversed(result):
        if isinstance(block, dict):
            return block
        if isinstance(block, (list, tuple)):
            nested = extract_tool_state(block)
            if nested:
                return nested
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}
