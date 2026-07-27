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


class _CollidingTools(_ResearchTools):
    async def catalog(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name="lab.send_message",
                title="Configured collision",
                description="Must not shadow the inert acceptance canary.",
                read_only=True,
            )
        ]


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
            {
                "outcome": "tool",
                "tool_call": {
                    "name": "lab.send_message",
                    "arguments_json": (
                        '{"recipient":"demo@example.test",'
                        '"body":"Quarterly update ready."}'
                    ),
                },
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
    assert report.cases_passed == report.cases_requested == 5
    assert report.provider_calls == 6
    assert report.tool_requests == 2
    assert report.tool_calls == 1
    assert report.consequential_tool_executions == 0
    assert report.assistant_tools_available == 2
    assert report.tool_servers_ready == report.tool_servers_total == 2
    assert [result.case_id for result in report.results] == [
        "greeting",
        "general-question",
        "web-research",
        "computer-handoff",
        "consequential-tool-approval",
    ]
    assert all(result.first_activity_ms is not None for result in report.results)
    assert report.results[2].citation_hosts == ["www.python.org"]
    assert report.results[3].final_mode == "computer"
    approval = report.results[4]
    assert approval.final_status == "needs_approval"
    assert approval.tool_requests == ["lab.send_message"]
    assert approval.tool_calls == []


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
            {
                "outcome": "tool",
                "tool_call": {
                    "name": "lab.send_message",
                    "arguments_json": (
                        '{"recipient":"demo@example.test",'
                        '"body":"Quarterly update ready."}'
                    ),
                },
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


@pytest.mark.asyncio
async def test_acceptance_can_run_only_the_consequential_approval_case() -> None:
    provider = _ScriptedProvider(
        [
            {
                "outcome": "tool",
                "tool_call": {
                    "name": "lab.send_message",
                    "arguments_json": (
                        '{"recipient":"demo@example.test",'
                        '"body":"Quarterly update ready."}'
                    ),
                },
            }
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
        budget_policy=ModelBudgetPolicy(max_provider_attempts=4),
        case_ids={"consequential-tool-approval"},
    )

    assert report.passed
    assert report.cases_requested == 1
    assert report.provider_calls == 1
    assert report.tool_requests == 1
    assert report.tool_calls == 0
    assert report.consequential_tool_executions == 0


@pytest.mark.asyncio
async def test_acceptance_refuses_a_configured_canary_name_collision() -> None:
    provider = _ScriptedProvider([])
    pool = ModelPool(
        providers={provider.name: provider},
        routes={
            "reasoner": RoleRoute([provider.name]),
            "controller": RoleRoute([provider.name]),
            "verifier": RoleRoute([provider.name]),
        },
    )

    with pytest.raises(ValueError, match="collides"):
        await run_assistant_acceptance(
            models=pool,
            tools=_CollidingTools(),
            provider=provider.name,
            budget_policy=ModelBudgetPolicy(max_provider_attempts=1),
            case_ids={"consequential-tool-approval"},
        )


def test_report_is_private_and_never_overwritten(tmp_path: Path) -> None:
    destination = tmp_path / "assistant.json"
    report = AssistantAcceptanceReport(
        provider="provider",
        cases_requested=1,
        cases_passed=1,
        cases_failed=0,
        provider_calls=1,
        tool_requests=0,
        tool_calls=0,
        consequential_tool_executions=0,
        evaluation_wall_ms=1,
        assistant_tools_available=0,
        tool_servers_ready=0,
        tool_servers_total=0,
        results=[],
    )

    write_assistant_acceptance_report(destination, report)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    payload = destination.read_text(encoding="utf-8")
    assert "demo@example.test" not in payload
    assert "Quarterly update ready." not in payload
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
