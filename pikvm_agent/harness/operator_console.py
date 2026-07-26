"""Lifecycle helpers for an operator console embedded in a benchmark run."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import uvicorn

from pikvm_agent.harness.agent_models import RunStatus
from pikvm_agent.harness.agent_store import RunStore


def operator_console_url(host: str, port: int) -> str:
    """Return a browser URL without losing IPv6 address boundaries."""
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{rendered_host}:{port}/app/"


def write_operator_console_descriptor(
    path: Path,
    *,
    url: str,
    access_token_env: str,
) -> None:
    """Persist discovery metadata without persisting credentials or targets."""
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "url": url,
                "access_token_env": access_token_env,
                "authentication": "bearer token entered in the browser tab",
                "approval_binding": "approval id intent header",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


async def wait_for_operator_approval(
    store: RunStore,
    run: Any,
    *,
    poll_interval_s: float = 0.2,
) -> Any:
    """Wait until the console resolves, rejects, or aborts this exact gate."""
    pending = run.pending_approval or {}
    approval_id = str(pending.get("approval_id") or "")
    if not approval_id:
        raise ValueError("approval-required run has no approval_id")
    while True:
        current = await store.get_state(run.run_id)
        current_pending = current.pending_approval or {}
        current_approval_id = str(current_pending.get("approval_id") or "")
        if (
            current.status is not RunStatus.NEEDS_APPROVAL
            or current_approval_id != approval_id
        ):
            return current
        await asyncio.sleep(poll_interval_s)


class _EmbeddedServer(uvicorn.Server):
    """Do not replace the benchmark runner's process signal handlers."""

    @contextlib.contextmanager
    def capture_signals(self) -> Any:
        yield


class OperatorConsoleServer:
    """Run Uvicorn on the benchmark's existing asyncio event loop."""

    def __init__(self, app: Any, *, host: str, port: int) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.url = operator_console_url(host, port)
        self.server = _EmbeddedServer(
            uvicorn.Config(
                app,
                host=host,
                port=port,
                log_level="warning",
                timeout_graceful_shutdown=3,
            )
        )
        self._task: asyncio.Task[None] | None = None

    async def start(self, *, timeout_s: float = 10.0) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self.server.serve(),
            name="pikvm-embedded-operator-console",
        )
        deadline = asyncio.get_running_loop().time() + timeout_s
        while not self.server.started:
            if self._task.done():
                await self._task
                raise RuntimeError("operator console exited during startup")
            if asyncio.get_running_loop().time() >= deadline:
                await self.close()
                raise RuntimeError("operator console did not start")
            await asyncio.sleep(0.02)

    async def close(self) -> None:
        shutdown = getattr(self.app.state, "shutdown_requested", None)
        if shutdown is not None:
            shutdown.set()
        self.server.should_exit = True
        if self._task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
        except TimeoutError:
            self.server.force_exit = True
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        finally:
            self._task = None
