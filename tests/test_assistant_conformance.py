from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.harness.agent_models import ModelRequest, ModelResponse
from pikvm_agent.harness.assistant_conformance import (
    AssistantAcceptanceReport,
    run_assistant_acceptance,
    write_assistant_acceptance_report,
)
from pikvm_agent.harness.general_tools import ToolDescriptor, ToolResult
from pikvm_agent.harness.model_budget import ModelBudgetPolicy
from pikvm_agent.harness.model_pool import ModelPool, RoleRoute


class _ScriptedProvider:
    name = "live-test-provider"

    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = list(decisions)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(
            provider=self.name,
            model="test-model",
            data=self.decisions.pop(0),
            latency_ms=5,
        )


class _ResearchTools:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def catalog(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name="web.search_text",
                title="Search the web",
                description="Find public web sources.",
                read_only=True,
                open_world=True,
            )
        ]

    def health(self) -> dict[str, dict[str, object]]:
        return {"web": {"ready": True, "tools": 1}}

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        del arguments
        self.calls.append(name)
        return ToolResult(
            content=(
                '{"content":[{"type":"text","text":"'
                '[{\\"title\\":\\"Python 3.14.0\\",'
                '\\"href\\":\\"https://www.python.org/downloads/\\"}]"}],'
                '"structured_content":null}'
            )
        )


@pytest.mark.asyncio
async def test_live_assistant_acceptance_covers_every_route_without_target() -> None:
    provider = _ScriptedProvider(
        [
            {
                "outcome": "reply",
                "message": "Hello! How can I help?",
            },
            {
                "outcome": "reply",
                "message": "42, because six groups of seven contain 42.",
            },
            {
                "outcome": "tool",
                "tool_call": {
                    "name": "web.search_text",
                    "arguments_json": (
                        '{"query":"site:python.org latest Python"}'
                    ),
                },
            },
            {
                "outcome": "reply",
                "message": (
                    "The current release is listed on "
                    "https://www.python.org/downloads/."
                ),
            },
            {
                "outcome": "computer",
                "message": "I’ll inspect the connected computer.",
                "computer_task": "Describe the currently visible screen.",
            },
        ]
    )
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            "reasoner": RoleRoute([provider.name]),
            "controller": RoleRoute([provider.name]),
            "verifier": RoleRoute([provider.name]),
        },
    )

    report = await run_assistant_acceptance(
        models=pool,
        tools=_ResearchTools(),
        provider=provider.name,
        budget_policy=ModelBudgetPolicy(max_provider_attempts=20),
    )

    assert report.passed
    assert report.computer_target_contacted is False
    assert report.cases_passed == report.cases_requested == 4
    assert report.provider_calls == 5
    assert report.tool_calls == 1
    assert report.assistant_tools_available == 1
    assert report.tool_servers_ready == report.tool_servers_total == 1
    assert [result.case_id for result in report.results] == [
        "greeting",
        "general-question",
        "web-research",
        "computer-handoff",
    ]
    assert all(result.first_activity_ms is not None for result in report.results)
    assert report.results[2].citation_hosts == ["www.python.org"]
    assert report.results[3].final_mode == "computer"


@pytest.mark.asyncio
async def test_research_case_fails_closed_without_visible_tool_or_citation() -> None:
    provider = _ScriptedProvider(
        [
            {"outcome": "reply", "message": "Hello."},
            {"outcome": "reply", "message": "42."},
            {
                "outcome": "reply",
                "message": "Python has a recent stable release.",
            },
            {
                "outcome": "computer",
                "computer_task": "Describe the visible screen.",
            },
        ]
    )
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            "reasoner": RoleRoute([provider.name]),
            "controller": RoleRoute([provider.name]),
            "verifier": RoleRoute([provider.name]),
        },
    )

    report = await run_assistant_acceptance(
        models=pool,
        tools=_ResearchTools(),
        provider=provider.name,
        budget_policy=ModelBudgetPolicy(max_provider_attempts=20),
    )

    research = next(
        result for result in report.results if result.case_id == "web-research"
    )
    assert research.passed is False
    assert research.tool_calls == []
    assert research.citation_hosts == []
    assert report.cases_failed == 1


def test_report_is_private_and_never_overwritten(tmp_path: Path) -> None:
    destination = tmp_path / "assistant.json"
    report = AssistantAcceptanceReport(
        provider="provider",
        cases_requested=1,
        cases_passed=1,
        cases_failed=0,
        provider_calls=1,
        tool_calls=0,
        evaluation_wall_ms=1,
        assistant_tools_available=0,
        tool_servers_ready=0,
        tool_servers_total=0,
        results=[],
    )

    write_assistant_acceptance_report(destination, report)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_assistant_acceptance_report(destination, report)


def test_cli_requires_explicit_live_provider_consent(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "harness",
            "assistant-conformance",
            "--config",
            str(config),
            "--provider",
            "provider",
            "--out",
            str(tmp_path / "report.json"),
        ],
    )

    assert result.exit_code == 2
    assert "--allow-provider-calls" in result.output
