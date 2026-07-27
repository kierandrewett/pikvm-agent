"""Standalone local operator-harness server construction."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from pikvm_agent.harness.agent import AgentHarness
from pikvm_agent.harness.agent_models import HarnessConfig
from pikvm_agent.harness.agent_store import SqliteRunStore
from pikvm_agent.harness.assistant import AssistantHarness
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
    UnavailableComputerDriver,
)
from pikvm_agent.harness.live_frames import DaemonLiveFrameSource
from pikvm_agent.harness.general_tools import (
    McpServerConnection,
    McpToolBroker,
)
from pikvm_agent.harness.provider_connections import ProviderConnectionManager


def build_harness_app(
    settings: HarnessSettings,
    *,
    settings_path: Path | None = None,
) -> FastAPI:
    """Wire adapters once; the :class:`AgentHarness` owns the workflow."""

    ensure_safe_bind(settings)
    ensure_provider_prerequisites(settings)
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    models = build_model_pool(settings)
    store = SqliteRunStore(settings.state_path)
    daemon_url = settings.optional_daemon_url()
    if daemon_url is None:
        tool_client = None
        live_frames = None
        computer = UnavailableComputerDriver()
        direct_calls = None
    else:
        tool_client = PersistentMcpToolClient(
            daemon_url=daemon_url,
            artifact_dir=settings.artifact_dir,
        )
        live_frames = DaemonLiveFrameSource(daemon_url)
        computer = McpComputerDriver(tool_client)
        direct_calls = DirectCallCoordinator(store=store, computer=computer)
    harness = AgentHarness(
        computer=computer,
        models=models,
        store=store,
        config=HarnessConfig(
            max_actions_per_advance=settings.max_actions_per_advance,
            max_actions_per_burst=settings.max_actions_per_burst,
            max_total_actions=settings.max_total_actions,
            max_ungrounded_navigation_replans=(
                settings.max_ungrounded_navigation_replans
            ),
            max_provider_attempts_per_run=(
                settings.model_budget.max_provider_attempts_per_run
            ),
        ),
        budget_policy=build_model_budget_policy(settings),
    )
    tool_broker = McpToolBroker(
        [
            McpServerConnection(
                name=name,
                transport=spec.transport,
                command=spec.command,
                args=tuple(spec.args),
                cwd=spec.cwd,
                inherited_env=tuple(spec.inherited_env),
                url=spec.url,
                header_env=dict(spec.header_env),
                allowed_tools=frozenset(spec.allowed_tools),
                read_only_tools=frozenset(spec.read_only_tools),
                timeout_s=spec.timeout_s,
            )
            for name, spec in settings.assistant_tools.items()
        ]
    )
    assistant = AssistantHarness(
        models=models,
        store=store,
        computer=harness,
        tools=tool_broker,
        budget_policy=build_model_budget_policy(settings),
    )
    provider_connections = (
        ProviderConnectionManager(
            settings=settings,
            settings_path=settings_path,
            models=models,
        )
        if settings_path is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        if tool_client is not None:
            await tool_client.start()
        await tool_broker.start()
        try:
            yield
        finally:
            await tool_broker.close()
            if tool_client is not None:
                await tool_client.close()
            if live_frames is not None:
                await live_frames.aclose()
            for provider in models.providers.values():
                close = getattr(provider, "aclose", None)
                if close is not None:
                    await close()

    app = create_harness_app(
        harness=harness,
        assistant=assistant,
        store=store,
        models=models,
        access_token=settings.access_token(),
        agent_token=(
            settings.agent_token()
            if daemon_url is not None
            or settings.agent_token_env in os.environ
            else None
        ),
        observer_token=(
            settings.observer_token() if daemon_url is not None else None
        ),
        allowed_origins=settings.resolved_origins(),
        live_frames=live_frames,
        direct_calls=direct_calls,
        provider_connections=provider_connections,
        max_autonomous_resumes=settings.max_autonomous_resumes,
        computer_control_enabled=daemon_url is not None,
        managed_mcp_name=settings.managed_mcp_name,
        computer_name=settings.computer_name,
        lifespan=lifespan,
    )
    app.state.harness = harness
    app.state.assistant_harness = assistant
    app.state.assistant_tools = tool_broker
    app.state.harness_store = store
    app.state.model_pool = models
    app.state.direct_calls = direct_calls
    app.state.provider_connections = provider_connections
    app.state.computer_control_enabled = daemon_url is not None
    return app
