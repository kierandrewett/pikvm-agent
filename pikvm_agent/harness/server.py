"""Standalone local operator-harness server construction."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from pikvm_agent.harness.agent import AgentHarness
from pikvm_agent.harness.agent_models import HarnessConfig
from pikvm_agent.harness.agent_store import SqliteRunStore
from pikvm_agent.harness.api import create_harness_app
from pikvm_agent.harness.config import (
    HarnessSettings,
    build_model_budget_policy,
    build_model_pool,
    ensure_provider_prerequisites,
    ensure_safe_bind,
)
from pikvm_agent.harness.direct_calls import DirectCallCoordinator
from pikvm_agent.harness.mcp_computer import (
    McpComputerDriver,
    PersistentMcpToolClient,
)
from pikvm_agent.harness.live_frames import DaemonLiveFrameSource


def build_harness_app(settings: HarnessSettings) -> FastAPI:
    """Wire adapters once; the :class:`AgentHarness` owns the workflow."""

    ensure_safe_bind(settings)
    ensure_provider_prerequisites(settings)
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    daemon_url = settings.daemon_url()
    tool_client = PersistentMcpToolClient(
        daemon_url=daemon_url,
        artifact_dir=settings.artifact_dir,
    )
    live_frames = DaemonLiveFrameSource(daemon_url)
    computer = McpComputerDriver(tool_client)
    models = build_model_pool(settings)
    store = SqliteRunStore(settings.state_path)
    harness = AgentHarness(
        computer=computer,
        models=models,
        store=store,
        config=HarnessConfig(
            max_actions_per_advance=settings.max_actions_per_advance,
            max_actions_per_burst=settings.max_actions_per_burst,
            max_total_actions=settings.max_total_actions,
            max_provider_attempts_per_run=(
                settings.model_budget.max_provider_attempts_per_run
            ),
        ),
        budget_policy=build_model_budget_policy(settings),
    )
    direct_calls = DirectCallCoordinator(store=store, computer=computer)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        await tool_client.start()
        try:
            yield
        finally:
            await tool_client.close()
            await live_frames.aclose()
            for provider in models.providers.values():
                close = getattr(provider, "aclose", None)
                if close is not None:
                    await close()

    app = create_harness_app(
        harness=harness,
        store=store,
        models=models,
        access_token=settings.access_token(),
        agent_token=settings.agent_token(),
        observer_token=settings.observer_token(),
        allowed_origins=settings.resolved_origins(),
        live_frames=live_frames,
        direct_calls=direct_calls,
        max_autonomous_resumes=settings.max_autonomous_resumes,
        lifespan=lifespan,
    )
    app.state.harness = harness
    app.state.harness_store = store
    app.state.model_pool = models
    app.state.direct_calls = direct_calls
    return app
